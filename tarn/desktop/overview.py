"""Overview: the host horizon on top, the VM list below. Mirrors views.overview in app.js."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .widgets import GiB, Ridge, button, clear, fmt_bytes, fmt_gib, fmt_pct, label, styled  # noqa: E402

OS_LABEL = {"linux": "Linux", "windows": "Windows", "other": "Other OS"}
STATE_LABEL = {
    "running": "Running", "stopped": "Stopped", "paused": "Paused", "stopping": "Shutting down",
    "crashed": "Crashed", "suspended": "Suspended", "unknown": "Unknown",
}


class Overview(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        pal = app.palette

        # -- horizon
        self.title = label("…", "hostname")
        self.sub = label("", "sub", wrap=True)
        self.readouts = Gtk.Box(spacing=36)
        wrap = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "wrap")
        top = Gtk.Box(spacing=16)
        top.pack_start(self.title, False, False, 0)
        host_btn = button("Host details", app.show_host)
        host_btn.set_tooltip_text("Readiness checklist, graphics cards, temperatures, storage")
        top.pack_end(host_btn, False, False, 0)
        wrap.pack_start(top, False, False, 0)
        wrap.pack_start(self.sub, False, False, 0)
        wrap.pack_start(self.readouts, False, False, 18)
        self.range = Ridge(pal, 108, grid=True)
        horizon = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "horizon")
        horizon.pack_start(wrap, False, False, 0)
        horizon.pack_start(self.range, False, False, 0)

        # -- VM list
        self.count = label("", "muted")
        head = Gtk.Box(spacing=16)
        head.pack_start(label("Virtual machines", "h2"), False, False, 0)
        head.pack_start(self.count, False, False, 0)
        head.pack_end(button("Create VM", app.show_create, "primary"), False, False, 0)
        self.list = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "vmlist")
        section = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14), "section")
        section.pack_start(head, False, False, 0)
        section.pack_start(self.list, False, False, 0)

        self.pack_start(horizon, False, False, 0)
        self.pack_start(section, False, False, 0)

        # The other columns have fixed widths; one shared width for the name
        # column keeps every row's ridgeline starting at the same x, like the web grid.
        self.name_cols = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.rows: dict[str, dict] = {}
        self.host_shown = False
        self.empty_shown = False

    # ------------------------------------------------------------ updates

    def update(self, snap: dict):
        info = snap["host"]
        if not self.host_shown:
            self.host_shown = True
            self.title.set_text(info["hostname"])
            self.sub.set_text(f"{info['os']}, kernel {info['kernel']}. {info['cpu_model']}, "
                              f"{info['threads']} threads, {fmt_gib(info['memory_total'])} RAM.")
            if info.get("virt_type") != "kvm":
                self.sub.set_text(self.sub.get_text() + " No KVM acceleration: see the Host page.")
                styled(self.sub, "warn")

        vms = snap["vms"]
        running = sum(1 for v in vms if v["state"] == "running")
        self.count.set_text(f"{running} of {len(vms)} running" if vms else "")
        self._readouts(snap, running, len(vms))
        self.range.set_values(snap["history"])
        self._list(vms)

    def _readouts(self, snap, running: int, total: int):
        cur = snap["current"] or {}
        items = [
            ("Host CPU", "–" if cur.get("cpu") is None else str(round(cur["cpu"])), "%", False),
            ("Memory in use", f"{cur['mem_used'] / GiB:.1f}" if cur.get("mem_total") else "–",
             f" of {fmt_gib(cur['mem_total'])}" if cur.get("mem_total") else "", False),
            ("VMs running", str(running), f" of {total}", False),
        ]
        p = snap["power"] or {}
        if p.get("batteries"):
            b = p["batteries"][0]
            items.append(("Battery, on charger" if p.get("ac_online") else "Battery, unplugged",
                          "–" if b["capacity"] is None else str(b["capacity"]), "%", not p.get("ac_online")))
        tctl = next((t for t in p.get("temps") or [] if t["sensor"] in ("k10temp", "coretemp", "zenpower")), None)
        if tctl:
            items.append(("CPU temperature", str(round(tctl["celsius"])), "°C", tctl["celsius"] >= 90))
        if p.get("platform_profile"):
            items.append(("Power profile", p["platform_profile"].replace("-", " "), "", False))
        for g in snap.get("gpus") or []:
            text = {"ready": "Ready", "in use": "In use", "stuck": "Stuck"}[g["status"]]
            unit = {"in use": f" by {g['detail']}", "stuck": f", {g['detail']}"}.get(g["status"], "")
            items.append((f"{g['name']}, passthrough", text, unit, g["status"] == "stuck"))

        clear(self.readouts)
        for key, value, unit, warn in items:
            v = Gtk.Box()
            v.pack_start(label(value, "v"), False, False, 0)
            if unit:
                u = label(unit, "unit")
                u.set_valign(Gtk.Align.END)
                u.set_margin_bottom(4)
                v.pack_start(u, False, False, 0)
            box = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL), "readout", *(("warn",) if warn else ()))
            box.pack_start(v, False, False, 0)
            box.pack_start(label(key, "k"), False, False, 0)
            self.readouts.pack_start(box, False, False, 0)
        self.readouts.show_all()

    def _list(self, vms: list[dict]):
        if not vms:
            for row in self.rows.values():
                self.name_cols.remove_widget(row["name_col"])
            self.rows.clear()
            if not self.empty_shown:
                self.empty_shown = True
                clear(self.list)
                empty = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6), "empty")
                empty.pack_start(label("No virtual machines yet", "h3"), False, False, 0)
                empty.pack_start(label("Create one from an installer ISO, or import a disk image you already "
                                       "have. You can also manage this host from another device at "
                                       f"{self.app.web_url}.", "muted", wrap=True), False, False, 0)
                start = button("Create VM", self.app.show_create, "primary")
                start.set_halign(Gtk.Align.START)
                start.set_margin_top(8)
                empty.pack_start(start, False, False, 0)
                self.list.pack_start(empty, False, False, 0)
                self.list.show_all()
            return
        self.empty_shown = False

        seen = {v["uuid"] for v in vms}
        for uuid in [u for u in self.rows if u not in seen]:
            self.name_cols.remove_widget(self.rows.pop(uuid)["name_col"])
        els = [self._row(vm) for vm in vms]
        # Only reorder when something changed, so hover survives polling.
        if self.list.get_children() != els:
            for child in self.list.get_children():
                self.list.remove(child)
            for el in els:
                self.list.pack_start(el, False, False, 0)
        self.list.show_all()

    def _row(self, vm: dict) -> Gtk.Widget:
        row = self.rows.get(vm["uuid"])
        if row is None:
            el = styled(Gtk.Box(spacing=16), "vmrow")
            row = self.rows[vm["uuid"]] = {"el": el, "sig": None,
                                           "ridge": Ridge(self.app.palette, 30)}
        sig = (vm["name"], vm["state"], vm["vcpus"], vm["memory"], vm["os_type"])
        if sig != row["sig"]:
            row["sig"] = sig
            self._build_row(row, vm)
        st = vm["stats"]
        row["cpu"].set_text(fmt_pct(st["cpu"]) if st else "")
        row["mem"].set_text(f"{fmt_bytes(st['mem_used'])} used" if st and st["mem_max"] else "")
        row["ridge"].set_values(vm["cpu_history"])
        return row["el"]

    def _build_row(self, row: dict, vm: dict):
        el = row["el"]
        if row.get("name_col"):
            self.name_cols.remove_widget(row["name_col"])
        for child in el.get_children():
            el.remove(child)
        state = vm["state"]

        dot = styled(Gtk.Box(), "dot", state)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_tooltip_text(STATE_LABEL.get(state, state))
        name = label(vm["name"], "vmname", ellipsize=Pango.EllipsizeMode.END)
        os_line = Gtk.Box()
        os_line.pack_start(label(f"{OS_LABEL.get(vm['os_type'], vm['os_type'])}, ", "os"), False, False, 0)
        os_line.pack_start(label(STATE_LABEL.get(state, state).lower(), "os", "state", state), False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.pack_start(name, False, False, 0)
        text.pack_start(os_line, False, False, 0)
        inner = Gtk.Box(spacing=10)
        inner.pack_start(dot, False, False, 0)
        inner.pack_start(text, True, True, 0)
        # The name opens the VM's page; a flat button keeps it keyboard-reachable.
        name_col = styled(Gtk.Button(relief=Gtk.ReliefStyle.NONE), "namebtn")
        name_col.add(inner)
        name_col.set_tooltip_text("Open this VM's page")
        name_col.connect("clicked", lambda *_: self.app.show_vm(vm["uuid"]))
        name_col.set_size_request(200, -1)

        res = label(f"{vm['vcpus']} vCPU, {fmt_gib(vm['memory'])}", "res")
        res.set_size_request(130, -1)
        row["cpu"] = label("", "cpu", xalign=1)
        row["cpu"].set_size_request(56, -1)
        row["mem"] = label("", "mem")
        row["mem"].set_size_request(110, -1)
        acts = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        acts.set_size_request(380, -1)
        for btn in self._actions(vm):
            acts.pack_start(btn, False, False, 0)

        ridge = row["ridge"]
        ridge.set_valign(Gtk.Align.CENTER)
        ridge.set_size_request(120, 30)

        el.pack_start(name_col, True, True, 0)
        el.pack_start(res, False, False, 0)
        el.pack_start(ridge, True, True, 0)
        el.pack_start(row["cpu"], False, False, 0)
        el.pack_start(row["mem"], False, False, 0)
        el.pack_start(acts, False, False, 0)
        self.name_cols.add_widget(name_col)
        row["name_col"] = name_col

    def _actions(self, vm: dict) -> list[Gtk.Button]:
        state = vm["state"]

        def act(action, text, *classes, confirm=None):
            def clicked(btn):
                if confirm and not self.app.confirm(*confirm):
                    return
                btn.set_sensitive(False)
                self.app.run_action(vm, action, text, lambda: btn.set_sensitive(True))
            return button(text, clicked, *classes)

        if state == "running":
            lg = [button("Looking Glass", lambda *_: self.app.looking_glass(vm), "primary")] if vm.get("looking_glass") else []
            return lg + [
                button("Console", lambda *_: self.app.show_console(vm), *(() if lg else ("primary",))),
                act("shutdown", "Shut down"),
                act("poweroff", "Force off", "danger",
                    confirm=(f"Force off {vm['name']}?",
                             "This cuts the power like pulling the plug. Unsaved work in the VM is lost.",
                             "Force off")),
            ]
        if state == "stopping":
            return [act("poweroff", "Force off", "danger",
                        confirm=(f"Force off {vm['name']}?",
                                 "It's still shutting down. Forcing it off may lose unsaved work.",
                                 "Force off"))]
        if state == "paused":
            return [act("resume", "Resume")]
        if state in ("stopped", "crashed"):
            if vm.get("looking_glass"):
                def play(btn):
                    btn.set_sensitive(False)
                    self.app.start_with_looking_glass(vm, lambda: btn.set_sensitive(True))
                return [button("Start + Looking Glass", play, "primary"), act("start", "Start")]
            return [act("start", "Start", "primary")]
        return []
