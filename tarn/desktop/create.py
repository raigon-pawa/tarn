"""Create a VM. Mirrors views.new in app.js and builds the same spec for Hypervisor.create_vm."""

from __future__ import annotations

import os
import re

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .widgets import button, fmt_bytes, fmt_gib, label, styled  # noqa: E402

OS_CHOICES = (
    ("linux", "Linux", "VirtIO devices, UEFI"),
    ("windows", "Windows", "TPM, Secure Boot, Hyper-V tuning"),
    ("other", "Other", "BSD or older systems, BIOS"),
)


def clean_file_name(name: str) -> str:
    """Same rules as cleanFileName() in app.js, so volume names stay valid (63 characters, extension kept)."""
    n = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    n = re.sub(r"^[^A-Za-z0-9]+", "", n)
    ext = (re.search(r"\.[A-Za-z0-9]{1,4}$", n) or [""])[0]
    if len(n) > 63:
        n = n[:63 - len(ext)] + ext
    return n or "upload.iso"


def vbox(*children, spacing: int = 12) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    for c in children:
        if c is not None:
            box.pack_start(c, False, False, 0)
    return box


def hbox(*children, spacing: int = 16, homogeneous: bool = False) -> Gtk.Box:
    box = Gtk.Box(spacing=spacing, homogeneous=homogeneous)
    for c in children:
        if c is not None:
            box.pack_start(c, homogeneous, True, 0)  # fill: a child set to hexpand takes the spare width
    return box


def field(title: str, widget: Gtk.Widget, hint: str | None = None) -> Gtk.Box:
    return vbox(label(title, "flabel"), widget,
                label(hint, "hint", wrap=True, max_width_chars=70) if hint else None, spacing=5)


def check(title: str, hint: str | None = None) -> Gtk.CheckButton:
    btn = Gtk.CheckButton()
    btn.add(vbox(label(title, "ctitle"), label(hint, "hint", wrap=True, max_width_chars=70) if hint else None,
                 spacing=1))
    return btn


class CreateView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.meta: dict | None = None
        self.upload_cancel = None
        styled(self, "section")
        self.pack_start(label("Create a virtual machine", "h1"), False, False, 0)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pack_start(self.body, False, False, 0)
        self.body.pack_start(label("Loading…", "muted"), False, False, 20)

    # ------------------------------------------------------------ loading

    def open(self):
        """Called each time the page is shown: reload ISOs, images and pools."""
        if self.meta is not None:
            self.error.set_text("")
        self.app.backend.call(self.app.backend.hv.create_meta, done=self._on_meta)

    def _on_meta(self, meta, err):
        if err:
            self.app.show_toast(f"Couldn't load the create form: {err}", error=True)
            return False
        first = self.meta is None
        self.meta = meta
        if first:
            self._build()
            self._apply_os_defaults("linux")
            self.name.grab_focus()
        else:
            self._fill_isos()
            self._fill_images()
        return False

    # ------------------------------------------------------------ form

    def _build(self):
        meta, caps = self.meta, self.meta["caps"]
        for child in self.body.get_children():
            self.body.remove(child)
        if caps["virt_type"] != "kvm":
            self.body.pack_start(label("KVM isn't available, so this VM will run in slow software emulation. "
                                       "Check the Host page in the web UI.", "note", wrap=True), False, False, 12)
        self.grid = styled(Gtk.Grid(column_spacing=40), "formgrid")
        self.grid_rows = 0

        # Operating system tiles
        self.os_buttons = {}
        tiles = Gtk.Box(spacing=10, homogeneous=True, halign=Gtk.Align.START)
        group = None
        for value, title, note in OS_CHOICES:
            rb = Gtk.RadioButton.new_from_widget(group)
            group = group or rb
            rb.set_mode(False)  # draw as a tile, not a dot
            styled(rb, "ostile")
            rb.add(vbox(label(title, "ttitle"), label(note, "tnote"), spacing=2))
            rb.connect("toggled", self._on_os_toggled, value)
            self.os_buttons[value] = rb
            tiles.pack_start(rb, True, True, 0)
        self._section("Operating system", "Sets sensible hardware defaults. You can change everything below.", tiles)

        # Name
        self.name = Gtk.Entry(placeholder_text="win11", width_chars=32, halign=Gtk.Align.START)
        self.name.connect("activate", lambda *_: self._submit())
        self._section("Name", None, field("VM name", self.name, "Letters, digits, dots, dashes and underscores."))

        # Install media
        self.iso = Gtk.ComboBoxText(halign=Gtk.Align.START)
        self.driver_iso = Gtk.ComboBoxText(halign=Gtk.Align.START)
        self.upload_btn = button("Add an ISO from this computer…", self._choose_iso)
        self.upload_btn.set_halign(Gtk.Align.START)
        self.upload_bar = Gtk.ProgressBar(show_text=True, hexpand=True, valign=Gtk.Align.CENTER)
        self.upload_row = hbox(self.upload_bar, button("Cancel upload", self._cancel_upload, "danger"), spacing=10)
        self.upload_row.set_no_show_all(True)
        self.virtio = check("Use VirtIO disk and network",
                            "Faster, but Windows setup won't see the disk until you load the driver from the "
                            "VirtIO ISO. For an imported Windows disk, tick this only if the VirtIO drivers "
                            "were installed before imaging.")
        self.windows_bits = vbox(
            field("VirtIO driver ISO", self.driver_iso,
                  "Attached as a second drive so Windows setup can load storage and network drivers. "
                  "Get virtio-win.iso from the Fedora project and add it with the button above."),
            self.virtio)
        self._section("Install media", None, vbox(
            field("Installer ISO", self.iso, "ISOs in the storage pools. Adding one from this computer copies "
                                             "it into the ISO pool through libvirt."),
            self.upload_btn, self.upload_row, self.windows_bits))
        self._fill_isos()

        # Disk
        self.disk_modes = {}
        group = None
        seg = Gtk.Box(halign=Gtk.Align.START)
        for value, text in (("new", "New disk"), ("existing", "Use an existing image"), ("none", "No disk")):
            rb = Gtk.RadioButton.new_with_label_from_widget(group, text)
            group = group or rb
            rb.set_mode(False)
            styled(rb, "seg")
            rb.connect("toggled", self._on_disk_mode)
            self.disk_modes[value] = rb
            seg.pack_start(rb, False, False, 0)
        self.size = Gtk.SpinButton.new_with_range(1, 64 * 1024, 1)
        self.pool = Gtk.ComboBoxText()
        for p in meta["pools"]:
            if p["active"]:
                free = f", {fmt_gib(p['available'])} free" if p.get("available") else ""
                self.pool.append(p["name"], f"{p['name']}{free}")
        if not self.pool.set_active_id(meta["default_pool"]):
            self.pool.set_active(0)
        self.new_disk = hbox(field("Size (GiB)", self.size, "Thin provisioned: it only uses space as the guest writes."),
                             field("Storage pool", self.pool), homogeneous=True)
        self.image = Gtk.ComboBoxText(halign=Gtk.Align.START)
        self.existing_disk = field("Disk image", self.image,
                                   "Images in any storage pool that no VM uses yet. Copy a converted image "
                                   "into /var/lib/libvirt/images and it shows up here.")
        self._fill_images()
        self._section("Disk", None, vbox(seg, self.new_disk, self.existing_disk))

        # CPU and memory
        threads = meta["host_threads"] or 256
        self.vcpus = Gtk.SpinButton.new_with_range(1, threads, 1)
        self.mem = Gtk.SpinButton.new_with_range(0.25, max(1, meta["host_memory"] // 1024 ** 3), 0.25)
        self.mem.set_digits(2)
        self._section("CPU and memory", None, hbox(
            field("vCPUs", self.vcpus, f"The host has {threads} threads."),
            field("Memory (GiB)", self.mem, f"The host has {fmt_gib(meta['host_memory'])}."), homogeneous=True))

        # Firmware
        self.fw = Gtk.ComboBoxText(halign=Gtk.Align.START)
        self.fw.append("efi", "UEFI")
        self.fw.append("bios", "Legacy BIOS")
        self.fw.set_active_id("efi" if caps["efi"] else "bios")
        self.fw.set_sensitive(caps["efi"])
        self.fw.connect("changed", lambda *_: self._sync_secure_boot())
        self.sb = check("Secure Boot", "Windows 11 wants this." if caps["secure_boot"]
                        else "This host has no secure-boot UEFI build.")
        self.tpm = check("TPM 2.0", "Emulated with swtpm. Windows 11 requires it." if caps["tpm"]
                         else "Install swtpm on the host to enable this.")
        self.tpm.set_sensitive(caps["tpm"])
        self._section("Firmware", None, vbox(field("Boot firmware", self.fw), self.sb, self.tpm, spacing=10))

        # Gaming: the same tweaks the VM page offers, applied before the first start
        self.pin = check("Pin to one CPU cluster", "Whole cores of one CPU cluster, sharing one L3 cache. "
                         "Smoother games and audio. Caps the vCPUs at that cluster's core count.")
        self.card = Gtk.ComboBoxText(halign=Gtk.Align.START, margin_start=28)
        for c in meta.get("cards", []):
            shared = f" (also on {', '.join(c['assigned_to'])})" if c["assigned_to"] else ""
            self.card.append(c["slot"], c["name"] + shared)
        self.card.set_active(0)
        self.use_card = check("Pass through a graphics card",
                              "Simplest after Windows is installed and updated: the VM's page can add it later. "
                              "NVIDIA laptop cards also get a fake battery, so the driver doesn't stop with Code 43.")
        self.use_card.connect("toggled", lambda b: self.card.set_sensitive(b.get_active()))
        self.lg = check("Looking Glass", "Shows the card's picture on this screen through /dev/kvmfr0. Windows "
                        "needs the IVSHMEM driver and the Looking Glass host app.")
        gaming = [self.pin]
        if meta.get("cards"):
            gaming += [self.use_card, self.card]
        if meta.get("looking_glass"):
            gaming.append(self.lg)
        self.card.set_sensitive(False)
        self._section("Gaming", "Optional. Each one can be switched later on the VM's page.",
                      vbox(*gaming, spacing=10))

        # Network
        self.net = Gtk.ComboBoxText(halign=Gtk.Align.START)
        for n in meta["networks"]:
            mode = "NAT" if n["mode"] == "nat" else n["mode"]
            subnet = f", {n['subnet']}" if n["subnet"] else ""
            self.net.append(f"network:{n['name']}", f"{n['name']} ({mode}{subnet})")
        for b in meta["bridges"]:
            self.net.append(f"bridge:{b}", f"Bridge {b} (VM appears on your LAN)")
        self.net.append("none:", "No network")
        if not self.net.set_active_id(f"network:{meta['default_network']}"):
            self.net.set_active(0)
        self.start = check("Start the VM after creating it")
        self.start.set_active(True)
        self._section("Network", "NAT works over Wi-Fi. A bridge needs wired Ethernet.",
                      vbox(field("Connect to", self.net), self.start, spacing=10))

        # Footer
        self.submit = button("Create VM", lambda *_: self._submit(), "primary", "big")
        self.error = label("", "error-text", wrap=True, max_width_chars=80)
        foot = styled(hbox(self.submit, button("Cancel", lambda *_: self.app.show_overview(), "big"),
                           self.error, spacing=10), "formfoot")

        self.body.pack_start(self.grid, False, False, 0)
        self.body.pack_start(foot, False, False, 0)
        self.body.show_all()
        self.windows_bits.hide()
        self.existing_disk.hide()

    def _section(self, title: str, note: str | None, content: Gtk.Widget):
        left = styled(vbox(label(title, "h3"),
                           label(note, "muted", "small", wrap=True, max_width_chars=28) if note else None,
                           spacing=4), "label-col")
        left.set_size_request(230, -1)
        # A full-width cell carries the divider, whatever the content's own alignment.
        cell = styled(vbox(content), "input-col")
        cell.set_hexpand(True)
        self.grid.attach(left, 0, self.grid_rows, 1, 1)
        self.grid.attach(cell, 1, self.grid_rows, 1, 1)
        self.grid_rows += 1

    def _fill_isos(self):
        isos = self.meta["isos"]
        for combo, blank in ((self.iso, "No installer, boot from disk"), (self.driver_iso, "None")):
            keep = combo.get_active_id()
            combo.remove_all()
            combo.append("", blank)
            for i in isos:
                combo.append(i["path"], i["name"])
            if not (keep and combo.set_active_id(keep)):
                combo.set_active(0)
        if not self.driver_iso.get_active_id():
            virtio_win = next((i for i in isos if "virtio-win" in i["name"].lower()), None)
            if virtio_win:
                self.driver_iso.set_active_id(virtio_win["path"])

    def _fill_images(self):
        images = self.meta["images"]
        self.image.remove_all()
        for i in images:
            self.image.append(i["path"], f"{i['name']} ({i['format'] or '?'}, {fmt_bytes(i['capacity'])})")
        if not images:
            self.image.append("", "No unused images found")
        self.image.set_active(0)
        self.image.set_sensitive(bool(images))

    # ------------------------------------------------------------ behaviour

    def _selected_os(self) -> str:
        return next(v for v, rb in self.os_buttons.items() if rb.get_active())

    def _on_os_toggled(self, rb, value):
        if rb.get_active():
            self._apply_os_defaults(value)

    def _apply_os_defaults(self, os_type: str):
        caps, win = self.meta["caps"], os_type == "windows"
        self.os_buttons[os_type].set_active(True)
        self.vcpus.set_value(min(4, self.meta["host_threads"] or 4) if win else 2)
        self.mem.set_value(8 if win else 1 if os_type == "other" else 2)
        self.size.set_value(80 if win else 16 if os_type == "other" else 32)
        if caps["efi"]:
            self.fw.set_active_id("bios" if os_type == "other" else "efi")
        self.sb.set_active(win and caps["secure_boot"])
        self.tpm.set_active(win and caps["tpm"])
        self.virtio.set_active(False)
        self.windows_bits.set_visible(win)
        for box in (self.pin, self.use_card, self.lg):
            box.set_active(False)
        self.name.set_placeholder_text({"windows": "win11", "linux": "alpine-dev"}.get(os_type, "freebsd"))
        self._sync_secure_boot()

    def _sync_secure_boot(self):
        ok = self.meta["caps"]["secure_boot"] and self.fw.get_active_id() == "efi"
        self.sb.set_sensitive(ok)
        if not ok:
            self.sb.set_active(False)

    def _on_disk_mode(self, rb):
        if not rb.get_active():
            return
        mode = next(v for v, b in self.disk_modes.items() if b is rb)
        self.new_disk.set_visible(mode == "new")
        self.existing_disk.set_visible(mode == "existing")

    # ------------------------------------------------------------ ISO upload

    def _choose_iso(self, _btn):
        dlg = Gtk.FileChooserDialog(title="Add an ISO", transient_for=self.app, action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add ISO", Gtk.ResponseType.OK)
        filt = Gtk.FileFilter()
        filt.set_name("Disc images (*.iso)")
        filt.add_pattern("*.iso")
        filt.add_pattern("*.ISO")
        dlg.add_filter(filt)
        dlg.set_current_folder(os.path.expanduser("~"))
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.OK else None
        dlg.destroy()
        if not path:
            return

        name = clean_file_name(os.path.basename(path))
        if not name.lower().endswith(".iso"):
            name = f"{name[:59]}.iso"
        self.upload_btn.set_sensitive(False)
        self.submit.set_sensitive(False)  # a half-copied ISO would boot nothing
        self.upload_bar.set_fraction(0)
        self.upload_bar.set_text(f"Copying {name}… 0%")
        self.upload_row.set_no_show_all(False)
        self.upload_row.show_all()

        def progress(frac):
            self.upload_bar.set_fraction(frac)
            self.upload_bar.set_text(f"Copying {name}… {round(frac * 100)}%")
            return False

        def done(vol_path, err):
            self.upload_cancel = None
            self.upload_row.hide()
            self.upload_btn.set_sensitive(True)
            self.submit.set_sensitive(True)
            if err:
                self.app.show_toast(f"{name}: {err}", error=True)
                return False
            self.app.show_toast(f"{name} added")
            target = self.driver_iso if "virtio-win" in name.lower() else self.iso

            def select(meta, err2):
                self._on_meta(meta, err2)
                if not err2:
                    target.set_active_id(vol_path)
                return False

            self.app.backend.call(self.app.backend.hv.create_meta, done=select)
            return False

        self.upload_cancel = self.app.backend.upload(self.meta["iso_pool"], name, path, progress, done)

    def _cancel_upload(self, _btn):
        if self.upload_cancel:
            self.upload_cancel.set()

    # ------------------------------------------------------------ submit

    def _spec(self) -> dict:
        os_type = self._selected_os()
        mode = next(v for v, b in self.disk_modes.items() if b.get_active())
        ntype, nname = self.net.get_active_id().split(":", 1)
        windows = os_type == "windows"
        if mode == "new":
            disk = {"mode": mode, "size_gib": self.size.get_value(), "pool": self.pool.get_active_id()}
        elif mode == "existing":
            disk = {"mode": mode, "path": self.image.get_active_id() or ""}
        else:
            disk = {"mode": mode}
        return {
            "name": self.name.get_text().strip(),
            "os_type": os_type,
            "vcpus": self.vcpus.get_value_as_int(),
            "memory_mib": round(self.mem.get_value() * 1024),
            "firmware": self.fw.get_active_id(),
            "secure_boot": self.sb.get_active(),
            "tpm": self.tpm.get_active(),
            "virtio": self.virtio.get_active() if windows else os_type == "linux",
            "iso": self.iso.get_active_id() or None,
            "driver_iso": (self.driver_iso.get_active_id() or None) if windows else None,
            "disk": disk,
            "network": {"type": ntype, "name": nname or None},
            "start": self.start.get_active(),
            "cpu_pinning": self.pin.get_active(),
            "card": self.card.get_active_id() if self.use_card.get_active() else None,
            "looking_glass": self.lg.get_active(),
        }

    def _submit(self):
        if not self.submit.get_sensitive():
            return
        self.vcpus.update()  # commit text typed into the spin buttons
        self.mem.update()
        self.size.update()
        spec = self._spec()
        self.error.set_text("")
        self.submit.set_sensitive(False)
        self.submit.set_label("Creating…")

        def done(res, err):
            self.submit.set_sensitive(True)
            self.submit.set_label("Create VM")
            if err:
                self.error.set_text(err)
                return False
            if res.get("warning"):
                self.app.show_toast(f"{spec['name']} created, but it didn't start: {res['warning']}", error=True)
            else:
                self.app.show_toast(" ".join(filter(None, [
                    f"{spec['name']} created{' and started' if res['started'] else ''}.", res.get("note")])))
            self.name.set_text("")
            self.app.show_overview()
            return False

        self.app.backend.call(self.app.backend.hv.create_vm, spec, done=done)
