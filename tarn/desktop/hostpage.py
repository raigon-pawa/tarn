"""The Host page: readiness checklist, graphics cards, power, storage and IOMMU groups. Mirrors views.host in app.js."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .. import host  # noqa: E402
from .create import hbox, vbox  # noqa: E402
from .widgets import button, clear, fmt_bytes, label, styled  # noqa: E402

# Readiness marks reuse the VM state dots: teal for fine, amber for a warning, red for a problem.
MARK = {"ok": "running", "warn": "paused", "error": "crashed"}


def _uptime(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {rest // 60} min"


class HostPage(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.loading = False
        self.shown = False
        styled(self, "section")
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pack_start(self.body, False, False, 0)
        self.power_box: Gtk.Box | None = None  # refilled from every poll, so temperatures stay live

    # ------------------------------------------------------------ loading

    def open(self):
        self.shown = True
        if not self.body.get_children():
            self.body.pack_start(label("Loading…", "muted"), False, False, 20)
            self.body.show_all()
        self.reload()

    def close(self):
        self.shown = False

    def reload(self):
        if self.loading:
            return
        self.loading = True
        hv = self.app.backend.hv

        def collect():
            cards = hv.pci_candidates()
            for c in cards:
                if c["ready"]:
                    c["health"] = host.passthrough_gpu_status(c["functions"][0]["address"], c["running_on"])
            return {"report": hv.host_report(), "cards": cards, "storage": hv.storage(),
                    "groups": host.iommu_groups()}

        self.app.backend.call(collect, done=self._on_loaded)

    def _on_loaded(self, data, err):
        self.loading = False
        if not self.shown:
            return False
        if err:
            self.app.show_toast(err, error=True)
            return False
        self._render(data)
        return False

    def on_poll(self, snap: dict):
        if self.shown and self.power_box is not None and snap.get("power") is not None:
            self._fill_power(snap["power"])

    # ------------------------------------------------------------ page

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

    def _render(self, data: dict):
        r = data["report"]
        clear(self.body)
        back = button("← Overview", lambda *_: self.app.show_overview())
        refresh = button("Refresh", lambda *_: self.reload())
        head = styled(hbox(back, label(r["hostname"], "h1"), spacing=16), "pagehead")
        head.pack_end(refresh, False, False, 0)
        self.body.pack_start(head, False, False, 0)
        versions = f"libvirt {r['libvirt_version']}" + (f", QEMU {r['qemu_version']}" if r.get("qemu_version") else "")
        self.body.pack_start(label(
            f"{r['os']}, kernel {r['kernel']}. {r['cpu_model']}, {r['threads']} threads. {versions}. "
            f"Up {_uptime(r['uptime'])}.", "muted", wrap=True, selectable=True), False, False, 0)

        grid = styled(Gtk.Grid(column_spacing=40), "formgrid")
        grid.set_margin_top(18)
        self._section(grid, "Readiness", "What VMs on this host need, and how to fix anything missing.",
                      self._readiness(r["readiness"]))
        self._section(grid, "Graphics cards", "Cards on vfio-pci can be given to a VM from its page.",
                      self._gpus(r["gpus"], data["cards"]))
        self.power_box = vbox(spacing=10)
        self._section(grid, "Power and temperature", None, self.power_box)
        self._fill_power(r["power"])
        self._section(grid, "Storage", "Files no VM uses can be deleted here.", self._storage(data["storage"]))
        self._section(grid, "IOMMU groups", "A device can be passed through only together with its whole group.",
                      self._groups(data["groups"]))
        self.body.pack_start(grid, False, False, 0)
        self.body.show_all()

    # ------------------------------------------------------------ sections

    def _readiness(self, checks: list[dict]) -> Gtk.Widget:
        rows = []
        for c in checks:
            mark = styled(Gtk.Box(valign=Gtk.Align.START, halign=Gtk.Align.CENTER), "dot", MARK[c["level"]])
            mark.set_margin_top(5)
            text = vbox(label(c["label"], "ctitle", wrap=True), label(c["detail"], "hint", wrap=True), spacing=1)
            if c["fix"]:
                text.pack_start(label(c["fix"], "code", wrap=True, selectable=True, max_width_chars=80,
                                      halign=Gtk.Align.START),
                                False, False, 4)
            rows.append(hbox(mark, text, spacing=10))
        bad = sum(1 for c in checks if c["level"] == "error")
        warn = sum(1 for c in checks if c["level"] == "warn")
        if bad:
            summary = label(f"{bad} problem{'s' if bad > 1 else ''} to fix before VMs run well.", "error-text")
        elif warn:
            summary = label(f"Ready for VMs, with {warn} suggestion{'s' if warn > 1 else ''} below.", "ctitle")
        else:
            summary = label("Everything VMs need is in place.", "ctitle")
        return vbox(summary, *rows, spacing=10)

    def _gpus(self, gpus: list[dict], cards: list[dict]) -> Gtk.Widget:
        by_slot = {c["slot"]: c for c in cards}
        rows = []
        for g in gpus:
            card = by_slot.get(g["address"].rsplit(".", 1)[0])
            if card and card["ready"]:
                h = card.get("health") or {}
                status = {"ready": "Ready for passthrough", "in use": f"In use by {h.get('detail')}",
                          "stuck": "Stuck: it fell off the bus after a VM stopped. Reboot the host to recover it."
                          }.get(h.get("status"), "On vfio-pci")
                if h.get("status") == "ready" and h.get("detail") == "asleep":
                    status += ", asleep to save power"
                assigned = (f" Assigned to {', '.join(card['assigned_to'])}."
                            if card["assigned_to"] and h.get("status") != "in use" else "")
                detail = f"{status}.{assigned} IOMMU group {card['iommu_group']}" + (
                    "" if card["isolated"] else ", shared with other devices")
                cls = "error-text" if h.get("status") == "stuck" else "hint"
            else:
                detail = f"Used by the host (driver {g['driver']})" if g["driver"] else "No driver loaded"
                cls = "hint"
            funcs = ", ".join(f["address"] for f in card["functions"]) if card else g["address"]
            rows.append(vbox(label(g["name"], "ctitle", wrap=True),
                             label(f"{detail}. {funcs}", cls, wrap=True, max_width_chars=90, selectable=True),
                             spacing=2))
        if not gpus:
            rows.append(label("No graphics cards found.", "muted"))
        return vbox(*rows, spacing=12)

    def _fill_power(self, p: dict):
        clear(self.power_box)
        facts = []
        if p.get("batteries"):
            for b in p["batteries"]:
                cap = "–" if b["capacity"] is None else f"{b['capacity']}%"
                facts.append(("Battery", f"{cap}, {(b['status'] or 'unknown').lower()}"))
        if p.get("ac_online") is not None:
            facts.append(("Charger", "connected" if p["ac_online"] else "unplugged"))
        if p.get("battery_conservation") is not None:
            facts.append(("Battery conservation", "on: charging stops near 80%" if p["battery_conservation"]
                          else "off: charges to 100%"))
        if p.get("platform_profile"):
            choices = ", ".join(p.get("platform_profile_choices") or [])
            facts.append(("Power profile", p["platform_profile"] + (f" (choices: {choices})" if choices else "")))
        for t in p.get("temps") or []:
            name = t["sensor"] if t["label"] == t["sensor"] else f"{t['sensor']} {t['label']}"
            facts.append((name, f"{t['celsius']:.0f} °C"))
        g = Gtk.Grid(column_spacing=24, row_spacing=6)
        hint = None
        if p.get("battery_conservation") is False and p.get("ac_online"):
            # Root-only sysfs file, so Tarn suggests the command instead of a switch.
            hint = vbox(label("On the charger all day? Conservation mode keeps the battery near 80%, "
                              "which it lasts much longer at. Turn it on with:", "hint", wrap=True,
                              max_width_chars=80),
                        label("echo 1 | doas tee /sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode",
                              "code", selectable=True, wrap=True, halign=Gtk.Align.START), spacing=6)
        for i, (k, v) in enumerate(facts):
            g.attach(label(k, "muted"), 0, i, 1, 1)
            warn = v.endswith("°C") and float(v.split()[0]) >= 90
            g.attach(label(v, *(("note",) if warn else ())), 1, i, 1, 1)
        self.power_box.pack_start(g if facts else label("No battery or sensors found.", "muted"), False, False, 0)
        if hint:
            self.power_box.pack_start(hint, False, False, 0)
        self.power_box.show_all()

    def _storage(self, pools: list[dict]) -> Gtk.Widget:
        hv = self.app.backend.hv
        rows = []
        for p in pools:
            if not p["active"]:
                rows.append(vbox(label(f"Pool {p['name']}", "ctitle"), label("Not active.", "note"), spacing=2))
                continue
            used = p["capacity"] - p["available"] if p.get("capacity") else 0
            bar = styled(Gtk.ProgressBar(fraction=used / max(1, p["capacity"]), valign=Gtk.Align.CENTER), "usage")
            bar.set_size_request(260, -1)
            head = hbox(label(f"Pool {p['name']}", "ctitle"), bar,
                        label(f"{fmt_bytes(p['available'])} free of {fmt_bytes(p['capacity'])}", "hint"), spacing=12)
            vols = []
            for v in p["volumes"]:
                text = vbox(label(v["name"], wrap=True, selectable=True, max_width_chars=60),
                            label(f"{fmt_bytes(v['capacity'])}"
                                  + (f", {fmt_bytes(v['allocation'])} on disk" if v["format"] == "qcow2" else "")
                                  + (f", used by {v['used_by']}" if v["used_by"] else ", not used by any VM"),
                                  "hint"), spacing=1)
                text.set_hexpand(True)

                def delete(b, pool=p["name"], name=v["name"], size=v["allocation"]):
                    if self.app.confirm(f"Delete {name}?", f"This frees {fmt_bytes(size)} in pool {pool}. "
                                        "It can't be undone.", "Delete file"):
                        self._do(hv.delete_volume, pool, name, btn=b, ok=f"{name} deleted")

                btn = None if v["used_by"] else button("Delete", delete, "danger")
                vols.append(hbox(text, btn, spacing=8) if btn else text)
            if not vols:
                vols.append(label("Empty.", "muted"))
            rows.append(vbox(head, label(p["path"] or "", "hint", selectable=True),
                             styled(vbox(*vols, spacing=8), "vols"), spacing=6))
        return vbox(*rows, spacing=22)

    def _groups(self, groups: list[dict]) -> Gtk.Widget:
        g = Gtk.Grid(column_spacing=16, row_spacing=4)
        i = 0
        for grp in groups:
            for j, d in enumerate(grp["devices"]):
                g.attach(label(str(grp["id"]) if j == 0 else "", "muted"), 0, i, 1, 1)
                g.attach(label(d["address"], "hint", selectable=True), 1, i, 1, 1)
                g.attach(label(d["name"], wrap=True, max_width_chars=70, selectable=True), 2, i, 1, 1)
                g.attach(label(d["driver"] or "–", *(("state", "running") if d["driver"] == "vfio-pci" else ("hint",))),
                         3, i, 1, 1)
                i += 1
        exp = Gtk.Expander(label=f"Show all {len(groups)} groups")
        exp.add(g)
        g.set_margin_top(10)
        return exp

    # ------------------------------------------------------------ helpers

    def _do(self, fn, *args, ok: str, btn: Gtk.Button | None = None):
        if btn:
            btn.set_sensitive(False)

        def done(_result, err):
            if btn:
                btn.set_sensitive(True)
            self.app.show_toast(err or ok, error=bool(err))
            self.reload()
            return False

        self.app.backend.call(fn, *args, done=done)
