"""The top panel: Wi-Fi, Bluetooth, sound, brightness, battery and a clock, like a desktop's system tray.

Icons refresh every few seconds from system.summary(); each one opens a
popover that loads its details when opened. All system calls go through
Backend.call, so a slow wpa_cli or D-Bus reply never freezes the window.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from . import system  # noqa: E402
from .create import hbox, vbox  # noqa: E402
from .widgets import TrayIcon, button, clear, label, styled  # noqa: E402


class Tray:
    """One panel button: an icon, optional text, and a popover filled on open."""

    def __init__(self, panel: "Panel", kind: str, fill, text: bool = False):
        self.panel, self.fill = panel, fill
        self.icon = TrayIcon(panel.app.palette, kind)
        self.text = label("", "traytext") if text else None
        inner = hbox(self.icon, self.text, spacing=6)
        self.button = styled(Gtk.MenuButton(relief=Gtk.ReliefStyle.NONE), "traybtn")
        self.button.add(inner)
        self.popover = Gtk.Popover(relative_to=self.button, position=Gtk.PositionType.BOTTOM)
        styled(self.popover, "traypop")
        self.body = vbox(spacing=10)
        self.body.set_size_request(340, -1)
        self.popover.add(self.body)
        self.button.set_popover(self.popover)
        self.button.connect("toggled", self._toggled)

    def _toggled(self, btn):
        if btn.get_active():
            self.refill()

    def refill(self):
        clear(self.body)
        self.fill(self.body)
        self.body.show_all()

    @property
    def open(self) -> bool:
        return self.button.get_active()


class Panel(Gtk.Box):
    def __init__(self, app):
        super().__init__(spacing=2)
        self.app = app
        styled(self, "topbar")
        self.pack_start(label("Tarn", "brand"), False, False, 6)

        self.wifi = Tray(self, "wifi", self._fill_wifi, text=True)
        self.bt = Tray(self, "bluetooth", self._fill_bt)
        self.sound = Tray(self, "speaker", self._fill_sound)
        self.screen = Tray(self, "sun", self._fill_screen)
        self.battery = Tray(self, "battery", self._fill_battery, text=True)
        self.clock = label("", "clock")
        self.pack_end(self.clock, False, False, 8)
        for t in (self.battery, self.screen, self.sound, self.bt, self.wifi):
            self.pack_end(t.button, False, False, 0)

        self.state: dict = {}
        self.networks: list[dict] | None = None
        self.picked: str | None = None   # the Wi-Fi network whose row is expanded
        self.scanning = self.discovering = False
        self._debounce: dict[str, int] = {}
        self._tick_clock()
        GLib.timeout_add_seconds(20, self._tick_clock)
        self.refresh()
        GLib.timeout_add_seconds(5, self._tick)

    # ------------------------------------------------------------ polling

    def _tick(self):
        self.refresh()
        return True

    def _tick_clock(self):
        self.clock.set_text(time.strftime("%a %d %b  %H:%M"))
        return True

    def refresh(self):
        self.app.backend.call(system.summary, done=self._on_summary)

    def _on_summary(self, s, err):
        if err or not s:
            return False
        self.state = s
        w, eth = s["wifi"], [e for e in s["ethernet"] if e["up"]]
        if w.get("available") and w.get("connected"):
            self.wifi.icon.kind = "wifi"
            self.wifi.icon.set_state(w["signal"] or 0)
            self.wifi.text.set_text(w["ssid"] or "")
        elif eth:
            self.wifi.icon.kind = "ethernet"
            self.wifi.icon.set_state(100)
            self.wifi.text.set_text("Wired")
        else:
            self.wifi.icon.kind = "wifi"
            self.wifi.icon.set_state(None, alert=w.get("available", False) and w.get("on", False))
            self.wifi.text.set_text("Off" if w.get("available") and not w.get("on") else "")
        self.wifi.icon.queue_draw()
        self.wifi.button.set_tooltip_text(self._wifi_line(w, s["ethernet"]))

        b = s["bluetooth"]
        self.bt.icon.set_state(100 if b.get("available") and b.get("on") else None)
        self.bt.button.set_tooltip_text(
            b.get("why") or ("Bluetooth off" if not b["on"] else
                             f"Connected: {', '.join(b['connected'])}" if b["connected"] else "Bluetooth on"))

        so = s["sound"]
        self.sound.icon.set_state(None if not so.get("available") or so["muted"] else so["volume"])
        self.sound.button.set_tooltip_text(so.get("why") or f"Volume {so['volume']}%" + (", muted" if so["muted"] else ""))

        br = s["brightness"]
        self.screen.icon.set_state(br.get("percent", 50) if br.get("available") else None)
        self.screen.button.set_tooltip_text(br.get("why") or f"Brightness {br['percent']}%")

        bat = s["battery"]
        self.battery.button.set_visible(bat["present"])
        if bat["present"]:
            self.battery.icon.set_state(bat["percent"], charging=bool(bat["ac"]),
                                        alert=not bat["ac"] and (bat["percent"] or 0) <= 20)
            self.battery.text.set_text(f"{bat['percent']}%")
            self.battery.button.set_tooltip_text(self._battery_line(bat))

        if self.battery.open:
            self.battery.refill()  # cheap and live; the others reload after their own actions
        return False

    # ------------------------------------------------------------ helpers

    def _do(self, fn, *args, ok: str | None = None, then=None):
        def done(result, err):
            if err:
                self.app.show_toast(err, error=True)
            elif ok:
                self.app.show_toast(ok)
            if then:
                then(result, err)
            self.refresh()
            return False
        self.app.backend.call(fn, *args, done=done)

    def _debounced(self, key: str, fn, *args):
        """Apply slider changes once the slider rests, not on every pixel of a drag."""
        if self._debounce.get(key):
            GLib.source_remove(self._debounce[key])

        def fire():
            self._debounce[key] = 0
            self.app.backend.call(fn, *args, done=lambda *_: False)
            return False
        self._debounce[key] = GLib.timeout_add(120, fire)

    @staticmethod
    def _header(title: str, switch: Gtk.Switch | None = None) -> Gtk.Box:
        h = hbox(label(title, "h3"), spacing=8)
        if switch is not None:
            switch.set_valign(Gtk.Align.CENTER)
            h.pack_end(switch, False, False, 0)
        return h

    @staticmethod
    def _hint(text: str) -> Gtk.Label:
        return label(text, "hint", wrap=True, max_width_chars=44, selectable=True)

    @staticmethod
    def _wifi_line(w: dict, eth: list[dict]) -> str:
        parts = []
        if not w.get("available"):
            parts.append(w.get("why", "No Wi-Fi"))
        elif not w["on"]:
            parts.append("Wi-Fi off")
        elif w["connected"]:
            parts.append(f"Wi-Fi: {w['ssid']}, {w['signal']}% signal" + (f", {w['ip']}" if w.get("ip") else ""))
        else:
            parts.append(f"Wi-Fi not connected ({w.get('state') or 'idle'})")
        for e in eth:
            parts.append(f"Ethernet {e['iface']}: {'connected' if e['up'] else 'no cable'}")
        return "\n".join(parts)

    @staticmethod
    def _battery_line(b: dict) -> str:
        text = f"Battery {b['percent']}%, {b['status'] or 'unknown'}"
        return text + (", on the charger" if b["ac"] else ", on battery")

    # ------------------------------------------------------------ Wi-Fi

    def _fill_wifi(self, box: Gtk.Box):
        w = self.state.get("wifi", {})
        sw = Gtk.Switch(active=bool(w.get("on")), sensitive=bool(w.get("available")) and not w.get("hard_blocked"))

        def toggle(_s, on):
            if not on and w.get("connected") and not self.app.confirm(
                    "Turn Wi-Fi off?", "SSH sessions and the web UI reached over Wi-Fi drop until it's back on. "
                    "VMs keep running.", "Turn off"):
                return True  # keep it on
            self._do(system.set_radio, "wlan", on, ok="Wi-Fi on" if on else "Wi-Fi off",
                     then=lambda *_: (setattr(self, "networks", None), self.wifi.refill() if self.wifi.open else None))
            return False

        sw.connect("state-set", toggle)
        box.pack_start(self._header("Wi-Fi", sw), False, False, 0)
        box.pack_start(self._hint(self._wifi_line(w, self.state.get("ethernet", []))), False, False, 0)
        if not w.get("available") or not w.get("on"):
            return

        nets = vbox(spacing=2)
        box.pack_start(nets, False, False, 0)
        rescan = button("Scan again", lambda *_: self._scan())
        rescan.set_halign(Gtk.Align.START)
        box.pack_start(rescan, False, False, 0)
        if self.networks is None:
            nets.pack_start(label("Looking for networks…", "muted"), False, False, 4)
            self._scan()
            return
        for n in self.networks[:12]:
            nets.pack_start(self._network_row(n), False, False, 0)
        if not self.networks:
            nets.pack_start(label("No networks found.", "muted"), False, False, 4)

    def _scan(self):
        if self.scanning:
            return
        self.scanning = True

        def done(result, err):
            self.scanning = False
            if err:
                self.app.show_toast(err, error=True)
            self.networks = result or []
            if self.wifi.open:
                self.wifi.refill()
            return False
        self.app.backend.call(system.wifi_scan, done=done)

    def _network_row(self, n: dict) -> Gtk.Widget:
        icon = TrayIcon(self.app.palette, "wifi")
        icon.set_state(n["signal"])
        note = ("Connected" if n["current"] else "Saved" if n["known"] else
                "Needs a company login" if n["enterprise"] else "Secured" if n["secure"] else "Open")
        name = label(n["ssid"], "ctitle", ellipsize=Pango.EllipsizeMode.END, max_width_chars=26)
        row = hbox(icon, name, spacing=10)
        row.pack_end(label(note, "hint"), False, False, 0)
        btn = styled(Gtk.Button(relief=Gtk.ReliefStyle.NONE), "netrow")
        btn.add(row)

        def pick(*_):
            self.picked = None if self.picked == n["ssid"] else n["ssid"]
            self.wifi.refill()
        btn.connect("clicked", pick)
        if self.picked != n["ssid"]:
            return btn

        actions = hbox(spacing=8)
        after = lambda *_: (setattr(self, "networks", None), setattr(self, "picked", None),  # noqa: E731
                            self.wifi.refill() if self.wifi.open else None)
        if n["current"]:
            actions.pack_start(button("Disconnect", lambda *_: self._do(
                system.wifi_disconnect, ok="Disconnected", then=after)), False, False, 0)
        elif n["known"] or not n["secure"]:
            actions.pack_start(button("Connect", lambda *_: self._do(
                system.wifi_connect, n["ssid"], ok=f"Connecting to {n['ssid']}…", then=after), "primary"),
                False, False, 0)
        if n["known"]:
            def forget(*_):
                if self.app.confirm(f"Forget {n['ssid']}?", "Its saved password is deleted.", "Forget"):
                    self._do(system.wifi_forget, n["ssid"], ok=f"Forgot {n['ssid']}", then=after)
            actions.pack_start(button("Forget", forget, "danger"), False, False, 0)
        extra = vbox(actions, spacing=8)
        if n["secure"] and not n["known"] and not n["enterprise"]:
            pw = Gtk.Entry(visibility=False, placeholder_text="Password", activates_default=False)
            pw.set_input_purpose(Gtk.InputPurpose.PASSWORD)
            go = button("Connect", lambda *_: self._do(
                system.wifi_connect, n["ssid"], pw.get_text(), ok=f"Connecting to {n['ssid']}…", then=after),
                "primary")
            pw.connect("activate", lambda *_: go.clicked())
            pw.set_hexpand(True)
            extra.pack_start(hbox(pw, go, spacing=8), False, False, 0)
            GLib.idle_add(pw.grab_focus)
        elif n["enterprise"]:
            extra.pack_start(self._hint("Enterprise (802.1X) networks need wpa_supplicant set up by hand."),
                             False, False, 0)
        if not actions.get_children():
            actions.destroy()
        extra.set_margin_start(28)
        extra.set_margin_bottom(6)
        return vbox(btn, extra, spacing=4)

    # ------------------------------------------------------------ Bluetooth

    def _fill_bt(self, box: Gtk.Box):
        err = system.AGENT.register()  # quick D-Bus calls; on the GTK thread because the agent lives here
        b = self.state.get("bluetooth", {})
        sw = Gtk.Switch(active=bool(b.get("on")), sensitive=bool(b.get("available")) or "blocked" in b.get("why", ""))

        def toggle(_s, on):
            self._do(system.set_bluetooth, on, ok="Bluetooth on" if on else "Bluetooth off",
                     then=lambda *_: self.bt.refill() if self.bt.open else None)
            return False
        sw.connect("state-set", toggle)
        box.pack_start(self._header("Bluetooth", sw), False, False, 0)
        if not b.get("available"):
            box.pack_start(self._hint(b.get("why", "Bluetooth isn't available.")), False, False, 0)
            return
        if not b.get("on"):
            box.pack_start(self._hint("Off."), False, False, 0)
            return
        if err and "already" not in err.lower():
            box.pack_start(self._hint(f"Pairing helper: {err}"), False, False, 0)
        devices = vbox(label("Loading…", "muted"), spacing=6)
        box.pack_start(devices, False, False, 0)
        find = button("Looking for devices…" if self.discovering else "Find new devices", lambda *_: self._discover())
        find.set_sensitive(not self.discovering)
        find.set_halign(Gtk.Align.START)
        box.pack_start(find, False, False, 0)
        box.pack_start(self._hint("Put headphones or a mouse in pairing mode first."), False, False, 0)

        def got(bt, e):
            clear(devices)
            if e or not bt or not bt.get("available"):
                devices.pack_start(self._hint(e or (bt or {}).get("why", "")), False, False, 0)
            else:
                shown = [d for d in bt["devices"] if d["paired"] or d["named"]]
                for d in shown[:12]:
                    devices.pack_start(self._device_row(d), False, False, 0)
                if not shown:
                    devices.pack_start(label("No devices yet.", "muted"), False, False, 0)
            devices.show_all()
            return False
        self.app.backend.call(system.bluetooth, done=got)

    def _device_row(self, d: dict) -> Gtk.Widget:
        state = "Connected" if d["connected"] else "Paired" if d["paired"] else "Not paired"
        text = vbox(label(d["name"], "ctitle", ellipsize=Pango.EllipsizeMode.END, max_width_chars=24),
                    label(state + (f", {d['icon'].replace('-', ' ')}" if d["icon"] else ""), "hint"), spacing=0)
        text.set_hexpand(True)
        again = lambda *_: self.bt.refill() if self.bt.open else None  # noqa: E731
        btns = []
        if d["paired"]:
            btns.append(button("Disconnect" if d["connected"] else "Connect", lambda *_: self._do(
                system.bluetooth_connect, d["path"], not d["connected"],
                ok=f"{d['name']} {'disconnected' if d['connected'] else 'connected'}", then=again)))

            def forget(*_):
                if self.app.confirm(f"Forget {d['name']}?", "You'll need to pair it again to use it.", "Forget"):
                    self._do(system.bluetooth_forget, d["path"], ok=f"Forgot {d['name']}", then=again)
            btns.append(button("Forget", forget, "danger"))
        else:
            btns.append(button("Pair", lambda b: (b.set_sensitive(False), b.set_label("Pairing…"), self._do(
                system.bluetooth_pair, d["path"], ok=f"{d['name']} paired and connected", then=again)), "primary"))
        return hbox(text, *btns, spacing=6)

    def _discover(self):
        self.discovering = True
        if self.bt.open:
            self.bt.refill()

        def done(_r, err):
            self.discovering = False
            if err:
                self.app.show_toast(err, error=True)
            if self.bt.open:
                self.bt.refill()
            return False
        self.app.backend.call(system.bluetooth_discover, done=done)

    # ------------------------------------------------------------ sound

    def _fill_sound(self, box: Gtk.Box):
        so = self.state.get("sound", {})
        box.pack_start(self._header("Sound"), False, False, 0)
        if not so.get("available"):
            box.pack_start(self._hint(so.get("why", "")), False, False, 0)
            return
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_value(so["volume"])
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_hexpand(True)
        scale.connect("value-changed", lambda s: self._debounced("volume", system.set_volume, int(s.get_value())))
        mute = Gtk.ToggleButton(label="Muted" if so["muted"] else "Mute", active=so["muted"])
        styled(mute, "btn")

        def muted(b):
            b.set_label("Muted" if b.get_active() else "Mute")
            self._do(system.set_mute, b.get_active())
        mute.connect("toggled", muted)
        box.pack_start(hbox(scale, mute, spacing=8), False, False, 0)
        if len(so["outputs"]) > 1:
            box.pack_start(label("Play sound through", "flabel"), False, False, 0)
            group = None
            for o in so["outputs"]:
                rb = Gtk.RadioButton.new_with_label_from_widget(group, o["name"])
                group = group or rb
                rb.set_active(o["default"])
                rb.connect("toggled", lambda r, i=o["id"]: r.get_active() and self._do(system.set_output, i))
                box.pack_start(rb, False, False, 0)

    # ------------------------------------------------------------ screen

    def _fill_screen(self, box: Gtk.Box):
        br = self.state.get("brightness", {})
        box.pack_start(self._header("Screen"), False, False, 0)
        if br.get("available"):
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
            scale.set_value(br["percent"])
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.connect("value-changed",
                          lambda s: self._debounced("brightness", system.set_brightness, int(s.get_value())))
            box.pack_start(scale, False, False, 0)
        else:
            box.pack_start(self._hint(br.get("why", "")), False, False, 0)

        def off(*_):
            self.screen.popover.popdown()
            GLib.timeout_add(400, lambda: self._do(system.screen_off_now) and False)
        b = button("Turn the screen off now", off)
        b.set_halign(Gtk.Align.START)
        box.pack_start(b, False, False, 0)
        box.pack_start(self._hint("Any key or touch brings it back. It also turns off with the lid closed "
                                  "and after 10 idle minutes, which keeps the OLED from burning in."),
                       False, False, 0)

    # ------------------------------------------------------------ battery

    def _fill_battery(self, box: Gtk.Box):
        bat = self.state.get("battery", {})
        box.pack_start(self._header("Battery"), False, False, 0)
        if not bat.get("present"):
            box.pack_start(self._hint("No battery."), False, False, 0)
            return
        box.pack_start(label(self._battery_line(bat), "ctitle", wrap=True), False, False, 0)
        if bat["conservation"] is None:
            return
        sw = Gtk.Switch(active=bat["conservation"], sensitive=bat["conservation_writable"], valign=Gtk.Align.START)
        sw.connect("state-set", lambda _s, on: (self._do(
            system.set_conservation, on, ok="Conservation mode on" if on else "Conservation mode off"), False)[1])
        text = "Stops charging around 80%, which keeps a battery that lives on the charger healthy for longer."
        if not bat["conservation_writable"]:
            text += f" Switching it needs setup: {system.SETUP_HINT}"
        box.pack_start(hbox(sw, vbox(label("Conservation mode", "ctitle"), self._hint(text), spacing=2),
                            spacing=10), False, False, 0)
