"""Starting the Looking Glass client: the VM's GPU picture, at native speed, in its own window.

Used by the desktop app's buttons and by `tarn looking-glass` (the Super+L
shortcut), so both connect to the right VM and never open a second window.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess


def is_open() -> bool:
    """Whether this user already has a looking-glass-client running."""
    for comm in glob.glob("/proc/[0-9]*/comm"):
        try:
            if os.stat(comm).st_uid != os.getuid():
                continue
            with open(comm) as fh:
                if fh.read().strip() == "looking-glass-c":  # the kernel cuts names to 15 characters
                    return True
        except OSError:
            continue  # the process ended meanwhile
    return False


def launch(spice_port: int | None = None) -> tuple[bool, str]:
    """Start looking-glass-client detached (other settings come from ~/.config/looking-glass/client.ini).

    The client sends keyboard, mouse, clipboard and sound over the VM's SPICE
    display and quits at once if it can't reach it, so pass that VM's port.
    Returns (started, message). The client then waits on its own until the
    Looking Glass host in the guest is running.
    """
    exe = shutil.which("looking-glass-client")
    if not exe:
        return False, "looking-glass-client isn't installed (doas apk add looking-glass)."
    if is_open():
        return False, "Looking Glass is already open. Switch to it with Alt+Tab."
    args = [exe] + ([f"spice:host=127.0.0.1", f"spice:port={spice_port}"] if spice_port else [])
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True, "Starting Looking Glass… tap your capture key (Right Alt) to grab the mouse."


def running_vm(hv) -> tuple[str, int | None] | None:
    """The running VM with Looking Glass memory, and its SPICE port. Tarn lets only one run at a time."""
    for vm in hv.list_vms():
        if vm["state"] == "running" and vm.get("looking_glass"):
            target = hv.console_target(vm["uuid"])
            return vm["name"], target["port"] if target["type"] == "spice" else None
    return None
