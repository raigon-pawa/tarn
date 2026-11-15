"""One VM's page: status and actions, hardware, drives, GPU passthrough, snapshots. Mirrors views.vm in app.js."""

from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .create import field, hbox, vbox  # noqa: E402
from .overview import OS_LABEL, STATE_LABEL  # noqa: E402
from .widgets import button, clear, fmt_bytes, fmt_gib, label, styled  # noqa: E402

GiB = 1024 ** 3


class VmPage(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.uuid: str | None = None
        self.vm: dict | None = None
        self.state: str | None = None
        self.loading = False
        styled(self, "section")
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pack_start(self.body, False, False, 0)

    # ------------------------------------------------------------ loading

    def open(self, uuid: str):
        if uuid != self.uuid:
            self.uuid, self.vm, self.state = uuid, None, None
            clear(self.body)
            self.body.pack_start(label("Loading…", "muted"), False, False, 20)
            self.body.show_all()
        self.reload()

    def close(self):
        self.uuid = None

    def reload(self):
        if self.uuid is None or self.loading:
            return
        self.loading = True
        hv = self.app.backend.hv
        uuid = self.uuid

        def collect():
            meta = hv.create_meta()
            return {"vm": hv.vm_detail(uuid), "cards": hv.pci_candidates(), "isos": meta["isos"],
                    "threads": meta["host_threads"], "memory": meta["host_memory"],
                    "snapshots": hv.list_snapshots(uuid)}

        self.app.backend.call(collect, done=self._on_loaded)

    def _on_loaded(self, data, err):
        self.loading = False
        if self.uuid is None:  # left the page meanwhile
            return False
        if err:
            if "No VM with UUID" in err:
                self.app.show_toast("That VM no longer exists.", error=True)
                self.app.show_overview()
            else:
                self.app.show_toast(err, error=True)
            return False
        self.vm, self.state = data["vm"], data["vm"]["state"]
        self._render(data)
        return False

    def on_poll(self, vms: list[dict]):
        """Called with every overview sample: reload when this VM's state changes."""
        me = next((v for v in vms if v["uuid"] == self.uuid), None)
        if me is None and self.vm is not None:
            self.app.show_toast(f"{self.vm['name']} was deleted.")
            self.app.show_overview()
        elif me and self.vm and (me["state"], me["name"]) != (self.state, self.vm["name"]) and not self.loading:
            self.reload()  # changed elsewhere: the web UI, virsh, or the guest shutting itself down

    # ------------------------------------------------------------ helpers

    def _do(self, fn, *args, ok: str | None = "Saved", btn: Gtk.Button | None = None, then=None):
        """Run a Hypervisor call off the GTK thread, toast the outcome, reload the page."""
        if btn:
            btn.set_sensitive(False)
        self.app.backend.refresh_cards()  # a GPU may have changed hands

        def done(result, err):
            if btn:
                btn.set_sensitive(True)
            if err:
                self.app.show_toast(err, error=True)
            else:
                msg = ok(result) if callable(ok) else ok
                if msg:
                    self.app.show_toast(msg)
                if then:
                    then(result)
            self.reload()
            self.app.refresh()
            return False

        self.app.backend.call(fn, *args, done=done)

    def _section(self, grid, title: str, note: str | None, content: Gtk.Widget):
        row = len(grid.get_children()) // 2
        left = styled(vbox(label(title, "h3"),
                           label(note, "muted", "small", wrap=True, max_width_chars=28) if note else None,
                           spacing=4), "label-col")
        left.set_size_request(230, -1)
        cell = styled(vbox(content), "input-col")
        cell.set_hexpand(True)
        grid.attach(left, 0, row, 1, 1)
        grid.attach(cell, 1, row, 1, 1)

    @staticmethod
    def _facts(pairs) -> Gtk.Grid:
        g = Gtk.Grid(column_spacing=24, row_spacing=6)
        for i, (k, v) in enumerate(pairs):
            g.attach(label(k, "muted"), 0, i, 1, 1)
            g.attach(label(v, selectable=True, wrap=True, max_width_chars=70), 1, i, 1, 1)
        return g

    # ------------------------------------------------------------ page

    def _render(self, data: dict):
        vm, hv = data["vm"], self.app.backend.hv
        stopped = vm["state"] in ("stopped", "crashed")
        clear(self.body)

        # Header: name, state, actions
        dot = styled(Gtk.Box(valign=Gtk.Align.CENTER), "dot", vm["state"])
        title = hbox(dot, label(vm["name"], "h1"), spacing=12)
        state = label(f"{OS_LABEL.get(vm['os_type'], vm['os_type'])}, "
                      f"{STATE_LABEL.get(vm['state'], vm['state']).lower()}", "state", vm["state"])
        acts = Gtk.Box(spacing=8)
        for b in self._actions(vm):
            acts.pack_start(b, False, False, 0)
        back = button("← Overview", lambda *_: self.app.show_overview())
        state.set_valign(Gtk.Align.CENTER)
        head = styled(Gtk.Box(spacing=16), "pagehead")
        for w in (back, title, state):
            head.pack_start(w, False, False, 0)
        head.pack_end(acts, False, False, 0)  # actions sit at the right edge
        self.body.pack_start(head, False, False, 0)

        grid = styled(Gtk.Grid(column_spacing=40), "formgrid")
        grid.set_margin_top(18)

        # Status
        ips = ", ".join(i["addr"] for i in vm["ips"]) or ("none yet" if not stopped else "–")
        fw = "UEFI" if vm["firmware"] == "efi" else "BIOS"
        if vm["secure_boot"]:
            fw += ", Secure Boot"
        if vm["tpm"]:
            fw += ", TPM 2.0"
        autostart = Gtk.Switch(active=vm["autostart"], halign=Gtk.Align.START)
        autostart.connect("state-set", lambda _s, on: (self._do(
            hv.set_autostart, self.uuid, on, ok="Starts with the host" if on else "Won't start with the host"), False)[1])
        facts = [("IP address", ips), ("Firmware", fw), ("Machine", vm["machine"] or "–"),
                 ("Snapshots", str(vm["snapshot_count"]))]
        g = vm.get("guest")
        if g:  # from the guest agent, while the VM runs
            guest = [("Guest OS", g["os"] + (f", named {g['hostname']}" if g["hostname"] else ""))] if g["os"] else []
            guest.append(("Signed in", ", ".join(g["users"]) or "nobody"))
            for f in g["filesystems"]:
                free = f["total"] - (f["used"] or 0)
                guest.append((f"Drive {f['mount'].rstrip(chr(92))}",
                              f"{fmt_bytes(free)} free of {fmt_bytes(f['total'])}"))
            facts = guest + facts
        self._section(grid, "Status", None, vbox(
            self._facts(facts),
            hbox(autostart, label("Start this VM when the host boots", "ctitle"), spacing=10)))

        # CPU and memory
        self._section(grid, "CPU and memory",
                      "Changes to a running VM apply the next time it starts.", self._cpu_mem(vm, data))

        # Drives
        self._section(grid, "Drives", None, self._drives(vm, data["isos"]))

        # Network and display
        nets = [(f"{n['model'] or '?'} NIC", f"{n['source'] or n['type']}, MAC {n['mac']}") for n in vm["nics"]]
        displays = f"Video {vm['video'] or '–'}"
        self._section(grid, "Network and display", None, vbox(
            self._facts(nets + [("Display", displays)]), self._looking_glass(vm, stopped), spacing=14))

        # PCI passthrough
        self._section(grid, "GPU passthrough",
                      "Hands a whole graphics card to this VM. The VM must be shut down to add or remove it.",
                      self._cards(vm, data["cards"], stopped))

        # Snapshots
        self._section(grid, "Snapshots", None, self._snapshots(vm, data["snapshots"]))

        # Name and deletion
        self._section(grid, "Manage", None, self._manage(vm, stopped))

        self.body.pack_start(grid, False, False, 0)
        self.body.show_all()

    def _actions(self, vm: dict) -> list[Gtk.Button]:
        hv, s = self.app.backend.hv, vm["state"]

        def act(action, text, *classes, confirm=None):
            def clicked(btn):
                if confirm and not self.app.confirm(*confirm):
                    return
                btn.set_sensitive(False)
                self.app.run_action(vm, action, text, lambda: (btn.set_sensitive(True), self.reload()))
            return button(text, clicked, *classes)

        out = []
        if s == "running":
            if vm["looking_glass"]:
                out.append(button("Looking Glass", lambda *_: self.app.looking_glass(vm), "primary"))
            out.append(button("Console", lambda *_: self.app.show_console(vm), *(() if vm["looking_glass"] else ("primary",))))
            out.append(act("shutdown", "Shut down"))
            out.append(act("reboot", "Reboot"))
        if s in ("running", "stopping", "paused"):
            out.append(act("poweroff", "Force off", "danger", confirm=(
                f"Force off {vm['name']}?",
                "This cuts the power like pulling the plug. Unsaved work in the VM is lost.", "Force off")))
        if s == "paused":
            out.insert(0, act("resume", "Resume", "primary"))
        if s in ("stopped", "crashed"):
            if vm["looking_glass"]:
                def play(btn):
                    btn.set_sensitive(False)
                    self.app.start_with_looking_glass(vm, lambda: (btn.set_sensitive(True), self.reload()))
                out.append(button("Start + Looking Glass", play, "primary"))
                out.append(act("start", "Start"))
            else:
                out.append(act("start", "Start", "primary"))
        del hv
        return out

    # ------------------------------------------------------------ sections

    def _cpu_mem(self, vm: dict, data: dict) -> Gtk.Widget:
        hv = self.app.backend.hv
        vcpus = Gtk.SpinButton.new_with_range(1, data["threads"] or 256, 1)
        vcpus.set_value(vm["vcpus"])
        mem = Gtk.SpinButton.new_with_range(0.25, max(1, data["memory"] // GiB), 0.25)
        mem.set_digits(2)
        mem.set_value(vm["memory"] / GiB)

        def save(btn):
            vcpus.update()
            mem.update()
            self._do(hv.set_resources, self.uuid, vcpus.get_value_as_int(), round(mem.get_value() * 1024), btn=btn,
                     ok=lambda r: "Saved. It applies the next time the VM starts." if r["applies_after_restart"] else "Saved")

        pin = vm["pinning"]
        pin_switch = Gtk.Switch(active=bool(pin), halign=Gtk.Align.START, valign=Gtk.Align.START)
        if pin:
            cores = ", ".join(sorted(set(pin["vcpus"].values()), key=lambda c: int(c.split("-")[0].split(",")[0])))
            pin_text = (f"On: each vCPU has its own core ({cores}); QEMU's housekeeping runs on "
                        f"CPUs {pin['emulator'] or 'any'}.")
        else:
            pin_text = ("Off. Turn on for games and audio: the VM gets whole cores of one CPU cluster "
                        "to itself, sharing one L3 cache.")

        def toggle(_s, on):
            self._do(hv.set_cpu_pinning, self.uuid, on,
                     ok=lambda r: " ".join(filter(None, [
                         "CPU pinning on." if on else "CPU pinning off.", r.get("note"),
                         "It applies the next time the VM starts." if r["applies_after_restart"] else None])))
            return False

        pin_switch.connect("state-set", toggle)
        return vbox(
            hbox(field("vCPUs", vcpus), field("Memory (GiB)", mem),
                 vbox(label(" ", "flabel"), button("Save", save, "primary"), spacing=5), spacing=16),
            hbox(pin_switch, vbox(label("Pin to one CPU cluster", "ctitle"),
                                  label(pin_text, "hint", wrap=True, max_width_chars=70), spacing=2), spacing=10),
            spacing=16)

    def _looking_glass(self, vm: dict, stopped: bool) -> Gtk.Widget:
        hv = self.app.backend.hv
        on = vm["looking_glass"]
        switch = Gtk.Switch(active=on, halign=Gtk.Align.START, valign=Gtk.Align.START, sensitive=stopped)
        text = ("On: the passed-through GPU's picture shows here with the Looking Glass button or Super+L. "
                "Windows needs the IVSHMEM driver and the Looking Glass host app." if on else
                "Off. Turn on to see a passed-through GPU's picture on this screen, through /dev/kvmfr0.")
        if not stopped:
            text += " Shut the VM down to change this."

        def toggle(_s, want):
            self._do(hv.set_looking_glass, self.uuid, want,
                     ok=lambda r: " ".join(filter(None, [
                         "Looking Glass shared memory added." if want else "Looking Glass shared memory removed.",
                         r["note"]])))
            return False

        switch.connect("state-set", toggle)
        return hbox(switch, vbox(label("Looking Glass", "ctitle"),
                                 label(text, "hint", wrap=True, max_width_chars=70), spacing=2), spacing=10)

    def _drives(self, vm: dict, isos: list[dict]) -> Gtk.Widget:
        hv = self.app.backend.hv
        rows = []
        for d in sorted(vm["disks"], key=lambda d: d["device"] != "disk"):  # hard disks first
            boot = f", boot {d['boot_order']}" if d.get("boot_order") else ""
            if d["device"] == "disk":
                size = (f"{fmt_gib(d['capacity'])}, {fmt_bytes(d['allocation'])} used"
                        if d.get("capacity") else "size unknown")
                rows.append(vbox(label(f"{d['bus']} disk {d['target']}{boot}", "ctitle"),
                                 label(f"{size}. {d['path']}", "hint", wrap=True, selectable=True), spacing=1))
            elif d["device"] == "cdrom":
                combo = Gtk.ComboBoxText(halign=Gtk.Align.START)
                combo.append("", "Empty")
                for i in isos:
                    combo.append(i["path"], i["name"])
                if d["path"] and not combo.set_active_id(d["path"]):
                    combo.append(d["path"], d["path"].rsplit("/", 1)[-1])
                    combo.set_active_id(d["path"])
                elif not d["path"]:
                    combo.set_active_id("")
                target = d["target"]

                def changed(c, target=target):
                    self._do(hv.change_media, self.uuid, target, c.get_active_id() or None,
                             ok="Ejected" if not c.get_active_id() else "Disc inserted")

                combo.connect("changed", changed)
                rows.append(field(f"CD/DVD drive {target}{boot}", combo))
        rows.append(button("Boot from the hard disk only", lambda b: self._do(
            hv.set_boot_disk_first, self.uuid, btn=b, ok="Boot order: hard disk first, CD drives off"), ))
        rows[-1].set_halign(Gtk.Align.START)
        return vbox(*rows, spacing=12)

    def _cards(self, vm: dict, cards: list[dict], stopped: bool) -> Gtk.Widget:
        hv = self.app.backend.hv
        mine = set(vm["hostdevs"])
        rows = []
        for c in cards:
            attached = any(f["address"] in mine for f in c["functions"])
            funcs = ", ".join(f"{f['address']} ({f['driver'] or 'no driver'})" for f in c["functions"])
            others = [n for n in c["assigned_to"] if n != vm["name"]]
            shared = ""
            if others:
                shared = f" {'Also assigned' if attached else 'Assigned'} to {', '.join(others)}"
                shared += (f"; {c['running_on']} is running now." if c["running_on"] in others else
                           "; only one of them can run at a time.")
            if attached:
                status, btn = "Attached to this VM." + shared, button("Remove", lambda b, s=c["slot"]: self._do(
                    hv.detach_card, self.uuid, s, btn=b, ok="Card removed from the VM"), "danger")
            elif not c["ready"]:
                status, btn = ("The host is using this card (it's not on vfio-pci), so it can't be passed through."
                               if any(f["driver"] not in (None, "vfio-pci") for f in c["functions"])
                               else "Not bound to vfio-pci yet."), None
            else:
                note = "" if c["isolated"] else " Its IOMMU group holds other devices, so it may not work."
                status, btn = "Ready to attach." + note + shared, button("Attach", lambda b, s=c["slot"]: self._do(
                    hv.attach_card, self.uuid, s, btn=b,
                    ok=lambda r: " ".join(filter(None, ["Card attached. Start the VM to use it.", r["note"]]))),
                    "primary")
            if btn is not None:
                btn.set_sensitive(stopped)
                btn.set_valign(Gtk.Align.CENTER)
            text = vbox(label(c["name"], "ctitle", wrap=True), label(f"{status} {funcs}", "hint", wrap=True,
                                                                         max_width_chars=80), spacing=2)
            rows.append(hbox(text, btn) if btn else text)
        if not cards:
            rows.append(label("No graphics cards found on the host.", "muted"))
        return vbox(*rows, spacing=12)

    def _snapshots(self, vm: dict, snaps: list[dict]) -> Gtk.Widget:
        hv = self.app.backend.hv
        name = Gtk.Entry(placeholder_text="before-update", width_chars=24)
        take = button("Take snapshot", lambda b: self._do(
            hv.create_snapshot, self.uuid, name.get_text().strip(), "", btn=b,
            ok=f"Snapshot {name.get_text().strip()} taken"), "primary")
        name.connect("activate", lambda *_: take.clicked())
        rows = [hbox(name, take, spacing=10)]
        if vm["firmware"] == "efi" and vm["state"] == "running":
            rows.append(label("Some libvirt versions can't snapshot a running UEFI VM. If it fails, "
                              "shut the VM down and try again.", "note", wrap=True))
        for s in reversed(snaps):
            when = datetime.fromtimestamp(s["created"]).strftime("%d %b %Y, %H:%M") if s["created"] else ""
            text = vbox(label(s["name"] + ("  (current)" if s["current"] else ""), "ctitle"),
                        label(f"{when}, VM was {s['state'] or '?'}", "hint"), spacing=1)

            def revert(b, n=s["name"]):
                if self.app.confirm(f"Revert to {n}?", "The VM's disks and state go back to this snapshot. "
                                    "Changes made since then are lost.", "Revert"):
                    self._do(hv.revert_snapshot, self.uuid, n, btn=b, ok=f"Reverted to {n}")

            def delete(b, n=s["name"]):
                if self.app.confirm(f"Delete snapshot {n}?", "The VM stays as it is now; only the snapshot "
                                    "is removed.", "Delete snapshot"):
                    self._do(hv.delete_snapshot, self.uuid, n, btn=b, ok="Snapshot deleted")

            text.set_hexpand(True)
            rows.append(hbox(text, button("Revert", revert), button("Delete", delete, "danger"), spacing=8))
        if not snaps:
            rows.append(label("No snapshots yet.", "muted"))
        return vbox(*rows, spacing=10)

    def _manage(self, vm: dict, stopped: bool) -> Gtk.Widget:
        hv = self.app.backend.hv
        name = Gtk.Entry(text=vm["name"], width_chars=24, sensitive=stopped)
        rename = button("Rename", lambda b: self._do(hv.rename, self.uuid, name.get_text().strip(), btn=b,
                                                     ok="Renamed"))
        rename.set_sensitive(stopped)
        wipe = Gtk.CheckButton(label="Also delete its disk images")

        def delete(b):
            disks = " and its disk images" if wipe.get_active() else " (its disk images are kept)"
            if not self.app.confirm(f"Delete {vm['name']}?",
                                    f"This removes the VM{disks}. It can't be undone.", "Delete VM"):
                return
            self._do(hv.delete_vm, self.uuid, wipe.get_active(), btn=b,
                     ok=lambda removed: f"{vm['name']} deleted" + (f", {len(removed)} disk(s) removed" if removed else ""),
                     then=lambda _r: self.app.show_overview())

        return vbox(
            hbox(field("Name", name), vbox(label(" ", "flabel"), rename, spacing=5), spacing=10),
            None if stopped else label("Shut the VM down to rename it.", "hint"),
            hbox(button("Delete VM", delete, "danger"), wipe, spacing=12), spacing=10)
