"""Small GTK3 building blocks shared by the desktop views."""

from __future__ import annotations

import cairo  # noqa: F401  (pycairo: without it GTK draw callbacks fail on every frame)
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .theme import rgb  # noqa: E402

POINTS = 150  # same history length as the web UI's ridgelines


class Ridge(Gtk.DrawingArea):
    """CPU history as a filled ridgeline, the Cairo twin of setRidge() in app.js."""

    def __init__(self, palette: dict, height: int, grid: bool = False, maximum: float = 100):
        super().__init__()
        self.palette = palette
        self.grid = grid
        self.maximum = maximum
        self.values: list[float] = []
        self.set_size_request(-1, height)
        self.connect("draw", self._draw)

    def set_values(self, values: list[float | None]):
        self.values = [v or 0 for v in values][-POINTS:]
        self.queue_draw()

    def _draw(self, _widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        if self.grid:
            cr.set_source_rgb(*rgb(self.palette["line"]))
            cr.set_line_width(1)
            cr.set_dash([2, 4])
            for frac in (0.25, 0.5, 0.75):
                y = round(h * frac) + 0.5
                cr.move_to(0, y)
                cr.line_to(w, y)
            cr.stroke()
            cr.set_dash([])
        vals = self.values
        if len(vals) < 2:
            return False
        offset = POINTS - len(vals)
        sx = w / (POINTS - 1)

        def y(v):
            return h - min(1.0, max(0.0, v / self.maximum)) * 0.94 * h

        cr.move_to(offset * sx, h)
        for i, v in enumerate(vals):
            cr.line_to((offset + i) * sx, y(v))
        cr.line_to(w, h)
        cr.close_path()
        cr.set_source_rgb(*rgb(self.palette["accent_soft"]))
        cr.fill()

        for i, v in enumerate(vals):
            (cr.line_to if i else cr.move_to)((offset + i) * sx, y(v))
        cr.set_source_rgb(*rgb(self.palette["ridge"]))
        cr.set_line_width(1.5)
        cr.stroke()
        return False


def label(text: str = "", *classes: str, xalign: float = 0, **kw) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in classes:
        lbl.get_style_context().add_class(c)
    return lbl


def styled(widget, *classes: str):
    ctx = widget.get_style_context()
    for c in classes:
        ctx.add_class(c)
    return widget


def button(text: str, on_click, *classes: str) -> Gtk.Button:
    btn = styled(Gtk.Button(label=text), "btn", *classes)
    btn.set_valign(Gtk.Align.CENTER)
    btn.connect("clicked", on_click)
    return btn


def clear(box: Gtk.Container):
    for child in box.get_children():
        box.remove(child)


# ------------------------------------------------------------------ formatting

GiB = 1024 ** 3


def fmt_bytes(n, digits: int = 1) -> str:
    if n is None:
        return "–"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    while abs(n) >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.{0 if i == 0 or n >= 10 else digits}f} {units[i]}"


def fmt_gib(n) -> str:
    g = n / GiB
    return f"{round(g)} GiB" if g >= 10 or g == int(g) else f"{g:.1f} GiB"


def fmt_pct(n) -> str:
    if n is None:
        return "–"
    return f"{n:.1f}%" if n < 10 else f"{round(n)}%"


class TrayIcon(Gtk.DrawingArea):
    """Small line icons for the top panel, drawn with Cairo (no icon theme on a bare Alpine install).

    kind: wifi, ethernet, bluetooth, speaker, sun, battery. level is 0-100, None for
    "off". charging adds a plug mark to the battery.
    """

    SIZE = 18

    def __init__(self, palette: dict, kind: str):
        super().__init__()
        self.palette, self.kind = palette, kind
        self.level: int | None = None
        self.charging = False
        self.alert = False
        self.set_size_request(self.SIZE, self.SIZE)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def set_state(self, level: int | None, charging: bool = False, alert: bool = False):
        if (level, charging, alert) != (self.level, self.charging, self.alert):
            self.level, self.charging, self.alert = level, charging, alert
            self.queue_draw()

    def _draw(self, _w, cr):
        import math
        s = self.SIZE
        ink, dim = rgb(self.palette["ink"]), rgb(self.palette["muted"])
        warn = rgb(self.palette["warn"])
        off = self.level is None
        cr.set_line_width(1.6)
        cr.set_line_cap(1)  # round
        cr.set_line_join(1)
        cr.set_source_rgb(*(warn if self.alert else dim if off else ink))
        k, lv = self.kind, self.level or 0
        if k == "wifi":
            cx, cy = s / 2, s - 3
            for i, r in enumerate((4.5, 9, 13.5)):
                lit = not off and lv > i * 33
                cr.set_source_rgb(*(ink if lit else dim))
                cr.arc(cx, cy, r, math.radians(225), math.radians(315))
                cr.stroke()
            cr.set_source_rgb(*(dim if off else ink))
            cr.arc(cx, cy, 1.6, 0, 2 * math.pi)
            cr.fill()
        elif k == "ethernet":
            cr.rectangle(4, 3, 10, 8)
            cr.stroke()
            for x in (7, 9, 11):
                cr.move_to(x, 11)
                cr.line_to(x, 9)
            cr.move_to(9, 11)
            cr.line_to(9, 16)
            cr.stroke()
        elif k == "bluetooth":
            pts = [(5, 5.5), (13, 12), (9, 16), (9, 2), (13, 6), (5, 12.5)]
            cr.move_to(*pts[0])
            for p in pts[1:]:
                cr.line_to(*p)
            cr.stroke()
        elif k == "speaker":
            cr.move_to(2.5, 7)
            for p in ((5.5, 7), (9.5, 3.5), (9.5, 14.5), (5.5, 11), (2.5, 11)):
                cr.line_to(*p)
            cr.close_path()
            cr.fill()
            if off:  # muted
                cr.move_to(12, 6.5)
                cr.line_to(16.5, 11.5)
                cr.move_to(16.5, 6.5)
                cr.line_to(12, 11.5)
                cr.stroke()
            else:
                for i, r in enumerate((3, 6)):
                    if lv > i * 50:
                        cr.arc(9.5, 9, r + 1.5, math.radians(-50), math.radians(50))
                        cr.stroke()
        elif k == "sun":
            cr.arc(9, 9, 3.2, 0, 2 * math.pi)
            cr.stroke()
            for i in range(8):
                a = i * math.pi / 4
                cr.move_to(9 + 5.4 * math.cos(a), 9 + 5.4 * math.sin(a))
                cr.line_to(9 + 7.2 * math.cos(a), 9 + 7.2 * math.sin(a))
            cr.stroke()
        elif k == "battery":
            cr.rectangle(1.5, 5, 13, 8)
            cr.stroke()
            cr.rectangle(15, 7.5, 1.8, 3)
            cr.fill()
            if not off:
                cr.rectangle(3, 6.5, 10 * min(100, lv) / 100, 5)
                cr.fill()
            if self.charging:
                cr.set_source_rgb(*rgb(self.palette["accent"]))
                cr.move_to(9, 3)
                cr.line_to(6.5, 9.5)
                cr.line_to(9.5, 9.5)
                cr.line_to(7, 16)
                cr.stroke()
        return False
