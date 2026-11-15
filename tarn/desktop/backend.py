"""The desktop app's bridge to libvirt.

libvirt-python blocks, and a blocked GTK main loop freezes the screen, so
every call runs on a worker thread and hands its result back to GTK with
GLib.idle_add. This is the desktop twin of run() / asyncio.to_thread in web.py.
"""

from __future__ import annotations

import logging
import os
import threading

from gi.repository import GLib

from .. import host
from ..stats import Sampler
from ..virt import Hypervisor

log = logging.getLogger("tarn.desktop")


class Backend:
    def __init__(self, cfg):
        self.hv = Hypervisor(cfg["libvirt"]["uri"], cfg["storage"], cfg["network"])
        st = cfg["stats"]
        self.sampler = Sampler(self.hv, st["interval_seconds"], st["history_points"])
        self.interval = st["interval_seconds"]
        self._polling = threading.Lock()
        self._host_info: dict | None = None
        self._cards: list[dict] = []     # passthrough GPUs, refreshed now and then (it walks sysfs and every VM's XML)
        self._cards_age = 0

    def _host(self) -> dict:
        if self._host_info is None:
            info = host.host_info({})
            info["virt_type"] = self.hv.caps().virt_type
            self._host_info = info
        return self._host_info

    def poll(self, done):
        """Take one stats sample and list the VMs; done(snapshot) runs on the GTK thread.

        Skips the round if the previous one is still running, so a slow
        libvirtd can't pile up threads.
        """
        if not self._polling.acquire(blocking=False):
            return

        def work():
            try:
                self.sampler._tick()
                s = self.sampler
                vms = self.hv.list_vms()
                for vm in vms:
                    vm["stats"] = s.vm_current(vm["uuid"])
                    vm["cpu_history"] = [p["cpu"] for p in s.vm_history(vm["uuid"])]
                snap = {
                    "error": None,
                    "host": self._host(),
                    "vms": vms,
                    "current": s.host_current,
                    "history": [p["cpu"] for p in s.host_history],
                    "power": host.power_status(),
                    "gpus": self._gpu_health(vms),
                }
            except Exception as exc:  # shown on screen; keep polling so it recovers
                log.warning("poll failed: %s", exc)
                snap = {"error": str(exc), "uri": self.hv.uri}
            finally:
                self._polling.release()
            GLib.idle_add(done, snap)

        threading.Thread(target=work, name="tarn-poll", daemon=True).start()

    def _gpu_health(self, vms: list[dict]) -> list[dict]:
        """Status of each GPU parked on vfio-pci: ready, in use (by which VM), or stuck."""
        if self._cards_age <= 0:
            self._cards = [c for c in self.hv.pci_candidates() if c["ready"]]
            self._cards_age = 15  # about every 30 s at the default 2 s interval
        self._cards_age -= 1
        running = {v["name"] for v in vms if v["state"] in ("running", "paused")}
        out = []
        for c in self._cards:
            user = next((n for n in c["assigned_to"] if n in running), None)
            out.append({"name": host.short_gpu_name(c["name"]),
                        **host.passthrough_gpu_status(c["functions"][0]["address"], user)})
        return out

    def refresh_cards(self):
        """Re-read which VM each GPU belongs to on the next poll (after attaching or removing one)."""
        self._cards_age = 0

    def call(self, fn, *args, done):
        """Run any blocking Hypervisor call; done(result, error) runs on the GTK thread."""

        def work():
            result = err = None
            try:
                result = fn(*args)
            except Exception as exc:
                err = str(exc)
            GLib.idle_add(done, result, err)

        threading.Thread(target=work, name="tarn-call", daemon=True).start()

    def upload(self, pool: str, name: str, path: str, progress, done) -> threading.Event:
        """Stream a local file into a new volume, like volume_upload in web.py.

        Going through a libvirt stream means the desktop user never needs
        write access to the pool directory. progress(fraction) and
        done(volume_path, error) run on the GTK thread. Set the returned
        event to cancel.
        """
        cancel = threading.Event()

        def work():
            stream = vol = None
            try:
                size = os.path.getsize(path)
                stream, vol = self.hv.begin_upload(pool, name, size)
                sent, last = 0, -1.0
                with open(path, "rb") as fh:
                    while chunk := fh.read(4 * 1024 * 1024):
                        if cancel.is_set():
                            raise RuntimeError("Upload cancelled")
                        stream.send(chunk)
                        sent += len(chunk)
                        frac = sent / size
                        if frac - last >= 0.005:  # don't flood the main loop
                            last = frac
                            GLib.idle_add(progress, frac)
                if sent != size:
                    raise RuntimeError(f"Upload incomplete: sent {sent} of {size} bytes")
                stream.finish()
                GLib.idle_add(done, vol.path(), None)
            except Exception as exc:
                if stream is not None:
                    try:
                        stream.abort()
                    except Exception:
                        pass
                if vol is not None:
                    try:
                        vol.delete(0)
                    except Exception:
                        pass
                GLib.idle_add(done, None, str(exc))

        threading.Thread(target=work, name="tarn-upload", daemon=True).start()
        return cancel

    def action(self, uuid: str, action: str, done):
        """Run a lifecycle action (start, shutdown, poweroff, resume); done(error or None)."""

        def work():
            err = None
            try:
                self.hv.action(uuid, action)
            except Exception as exc:
                err = str(exc)
            GLib.idle_add(done, err)

        threading.Thread(target=work, name=f"tarn-{action}", daemon=True).start()


def access_hint(uri: str) -> str | None:
    """A fix to suggest when the desktop user can't reach libvirt, if we can tell."""
    if uri != "qemu:///system" or os.geteuid() == 0:
        return None
    import grp
    import pwd
    try:
        group = grp.getgrnam("libvirt")
    except KeyError:
        return "doas sh install.sh   # sets up the libvirt group"
    if group.gr_gid in os.getgroups():
        return None
    user = pwd.getpwuid(os.geteuid()).pw_name
    if user in group.gr_mem:
        return "Log out and back in so your new libvirt group membership applies."
    return f"doas addgroup {user} libvirt   # then log out and back in"
