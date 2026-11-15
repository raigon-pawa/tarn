"""Colours and GTK3 CSS for the desktop app.

The palettes mirror the CSS variables in static/app.css so the native app
and the web UI look like the same product. Python keeps the colours too,
because the ridgelines are drawn with Cairo rather than styled by CSS.
"""

from __future__ import annotations

PALETTES = {
    "light": {
        "bg": "#f2f5f4",          # snowfield
        "surface": "#fbfcfc",
        "ink": "#172a2f",         # wet granite
        "muted": "#5b6d71",
        "line": "#d3dedd",
        "accent": "#0e6e78",      # glacial water
        "accent_ink": "#ffffff",
        "accent_soft": "#cfe4e4", # ice
        "ridge": "#0e6e78",
        "warn": "#a96c12",        # larch in autumn
        "danger": "#ad3c27",      # iron-rich rock
        "danger_soft": "#f6e1db",
    },
    "dark": {
        "bg": "#0f2126",
        "surface": "#13292f",
        "ink": "#e2ecea",
        "muted": "#8da3a6",
        "line": "#23404a",
        "accent": "#5bc2c8",
        "accent_ink": "#0b1d22",
        "accent_soft": "#1b4750",
        "ridge": "#5bc2c8",
        "warn": "#e2a64c",
        "danger": "#e57d65",
        "danger_soft": "#3a2621",
    },
}


def rgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# GTK3 CSS has no var(); colours come in as @define-color names instead.
_CSS = """
/* GTK 3 only takes font weights in steps of 100, so the web's 650/750/850 round down. */
* { font-family: "Overpass", "Cantarell", sans-serif; }

window.tarn, window.tarn scrolledwindow, window.tarn viewport {
  background-color: @bg; color: @ink;
}

/* the horizon: host banner with the CPU ridgeline */
.horizon { border-bottom: 1px solid @line; }
.horizon .wrap { padding: 34px 28px 18px; }
.hostname { font-size: 52px; font-weight: 800; letter-spacing: -2px; }
.sub { color: @muted; font-size: 14px; margin-top: 4px; }
.sub .warn, label.warn { color: @warn; }
.readout .v { font-size: 26px; font-weight: 700; }
.readout .unit { font-size: 14px; font-weight: 600; color: @muted; }
.readout .k { font-size: 13px; color: @muted; }
.readout.warn .v { color: @warn; }

/* sections */
.section { padding: 28px; }
.h2 { font-size: 21px; font-weight: 700; }
.muted { color: @muted; }

/* VM list: rows, not cards */
.vmlist { border-top: 1px solid @line; }
.vmrow { padding: 12px 4px; border-bottom: 1px solid @line; }
.vmrow:hover { background-color: alpha(@accent_soft, 0.35); }
.vmname { font-weight: 700; font-size: 16px; }
.os, .res, .mem { font-size: 14px; color: @muted; }
.os { font-size: 13px; }
.cpu { font-weight: 700; }

.dot {
  min-width: 7px; min-height: 7px; border-radius: 50%;
  border: 2px solid @muted; background-color: transparent;
}
.dot.running { background-color: @accent; border-color: @accent; }
.dot.paused, .dot.suspended, .dot.stopping { background-color: @warn; border-color: @warn; }
.dot.crashed { background-color: @danger; border-color: @danger; }
.state { font-weight: 600; }
.state.running { color: @accent; }
.state.paused, .state.suspended, .state.stopping { color: @warn; }
.state.crashed { color: @danger; }

.empty { padding: 48px 4px; border-bottom: 1px solid @line; }
.empty .h3 { font-size: 18px; font-weight: 700; }

/* buttons: flat, outlined, like .btn on the web */
button.btn {
  background-image: none; background-color: @surface; color: @ink;
  border: 1px solid @line; border-radius: 6px; box-shadow: none; text-shadow: none;
  padding: 3px 11px; min-height: 26px; font-size: 13px; font-weight: 600;
}
button.btn:hover { border-color: @muted; }
button.btn:disabled { opacity: 0.45; }
button.btn.primary { background-color: @accent; border-color: @accent; color: @accent_ink; }
button.btn.primary:hover { border-color: @ink; }
button.btn.danger { color: @danger; }
button.btn.danger:hover { border-color: @danger; }
button.btn label { color: inherit; }

/* create form: label column on the left, inputs on the right */
.h1 { font-size: 30px; font-weight: 800; letter-spacing: -1px; margin-bottom: 18px; }
.formgrid .label-col, .formgrid .input-col { padding: 22px 0; border-top: 1px solid @line; }
.formgrid .h3 { font-size: 16px; font-weight: 700; }
.small { font-size: 13px; }
.flabel, .ctitle { font-weight: 600; font-size: 14px; }
.hint { font-size: 13px; color: @muted; }
.note { color: @warn; }
.error-text { color: @danger; font-weight: 600; }
.formfoot { padding: 22px 0 40px; border-top: 1px solid @line; }

entry, spinbutton, combobox button.combo {
  background-image: none; background-color: @surface; color: @ink;
  border: 1px solid @line; border-radius: 6px; box-shadow: none; min-height: 34px;
}
spinbutton entry { border: none; background-color: transparent; min-height: 0; }
spinbutton button { border: none; border-left: 1px solid @line; background-image: none;
  background-color: transparent; color: @ink; box-shadow: none; }
entry:focus, spinbutton:focus { border-color: @accent; box-shadow: 0 0 0 2px @accent_soft; }
entry selection, spinbutton selection { background-color: @accent; color: @accent_ink; }

button.ostile {
  background-image: none; background-color: @surface; color: @ink;
  border: 1px solid @line; border-radius: 6px; box-shadow: none; padding: 12px 14px;
}
button.ostile:hover { border-color: @muted; }
button.ostile:checked { border-color: @accent; box-shadow: inset 0 0 0 1px @accent;
  background-color: alpha(@accent_soft, 0.5); }
.ttitle { font-weight: 700; font-size: 15px; }
.tnote { font-size: 13px; color: @muted; }

button.seg {
  background-image: none; background-color: @surface; color: @ink; box-shadow: none;
  border: 1px solid @line; border-radius: 0; padding: 5px 14px; font-size: 14px;
}
button.seg:first-child { border-radius: 6px 0 0 6px; }
button.seg:last-child { border-radius: 0 6px 6px 0; }
button.seg:checked { background-color: @accent; border-color: @accent; color: @accent_ink; }

checkbutton check:checked, checkbutton check { border-radius: 4px; }
checkbutton check:checked { background-image: none; background-color: @accent; border-color: @accent;
  color: @accent_ink; }
checkbutton:disabled label { color: alpha(@muted, 0.7); }

progressbar progress { background-image: none; background-color: @accent; border-color: @accent; }
progressbar trough { background-color: @line; border: none; }
progressbar text { color: @muted; font-size: 13px; }

button.btn.big { padding: 6px 16px; min-height: 30px; font-size: 14px; }

/* the VM name in the overview is a flat button that opens the VM's page */
button.namebtn { padding: 0; border: none; background-image: none; background-color: transparent;
  box-shadow: none; }
button.namebtn:hover .vmname { text-decoration-line: underline; }

/* switches in the accent colour rather than GTK's blue */
switch:checked { background-color: @accent; border-color: @accent; background-image: none; }
switch slider { background-image: none; background-color: @surface; border: 1px solid @line; }

/* console */
.consolebar { padding: 6px 12px; border-bottom: 1px solid @line; background-color: @surface; }
.ctitle-bar { font-weight: 700; font-size: 15px; }
.screen { background-color: #000000; }

/* page headers: title, state and actions on one centred line */
.pagehead .h1 { margin-bottom: 0; }
.pagehead { margin-bottom: 14px; }

/* host page */
.vols { padding-left: 14px; border-left: 2px solid @line; }
progressbar.usage trough, progressbar.usage progress { min-height: 8px; border-radius: 4px; }
expander title label { font-weight: 600; font-size: 14px; }

/* top panel: a slim system tray */
.topbar { background-color: @surface; border-bottom: 1px solid @line; padding: 1px 8px; min-height: 30px; }
.topbar .brand { font-weight: 800; font-size: 14px; letter-spacing: -0.3px; }
.topbar .clock, .topbar .traytext { font-size: 13px; font-weight: 600; }
button.traybtn { padding: 2px 8px; border-radius: 6px; border: none; background-image: none;
  background-color: transparent; box-shadow: none; min-height: 24px; }
button.traybtn:hover, button.traybtn:checked { background-color: alpha(@accent_soft, 0.6); }
popover.traypop { background-color: @surface; border: 1px solid @line; border-radius: 8px; padding: 14px; }
popover.traypop .h3 { font-size: 16px; font-weight: 700; }
button.netrow { padding: 6px 6px; border: none; background-image: none; background-color: transparent;
  box-shadow: none; border-radius: 6px; }
button.netrow:hover { background-color: alpha(@accent_soft, 0.5); }
scale trough { background-color: @line; border: none; min-height: 4px; border-radius: 2px; }
scale highlight { background-color: @accent; border: none; border-radius: 2px; }
scale slider { background-image: none; background-color: @surface; border: 2px solid @accent;
  min-width: 14px; min-height: 14px; border-radius: 50%; }

/* toasts */
.toast {
  background-color: @ink; color: @bg; border-radius: 6px;
  padding: 10px 14px; margin: 16px; font-size: 14px;
}
.toast.error { background-color: @danger; color: #ffffff; }

/* full-window message when libvirt is unreachable */
.problem { padding: 48px 28px; }
.problem .h2 { margin-bottom: 8px; }
.code {
  font-family: "Overpass Mono", monospace; font-size: 13px;
  background-color: alpha(@line, 0.6); padding: 8px 10px; border-radius: 6px;
}
"""


def css(theme: str) -> bytes:
    pal = PALETTES[theme]
    defs = "".join(f"@define-color {k} {v};\n" for k, v in pal.items())
    return (defs + _CSS).encode()
