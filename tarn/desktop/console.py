"""The VM's screen inside the app: spice-gtk when the VM has SPICE, gtk-vnc otherwise.

Both displays listen on 127.0.0.1 only (see domxml), so connecting by TCP from
this machine needs no password. SPICE brings sound, clipboard sharing and
resolution sync (the last two need spice-vdagent in the guest).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GObject, Gtk  # noqa: E402

from .widgets import button, label, styled  # noqa: E402

CAD = [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_Delete]


def _spice():
    try:
        gi.require_version("SpiceClientGLib", "2.0")
        gi.require_version("SpiceClientGtk", "3.0")
        from gi.repository import SpiceClientGLib, SpiceClientGtk
        return SpiceClientGLib, SpiceClientGtk
    except (ImportError, ValueError):
        return None


def _vnc():
    try:
        gi.require_version("GtkVnc", "2.0")
        from gi.repository import GtkVnc
        return GtkVnc
    except (ImportError, ValueError):
        return None


class SpiceViewer:
    """One SPICE session. Its methods named connect/disconnect shadow GObject's,
    so both are called through the class to stay unambiguous."""

    def __init__(self, host: str, port: int, on_display, on_closed):
        self.glib, self.gtk = _spice()
        self.on_display, self.on_closed = on_display, on_closed
        self.display = None
        self.session = self.glib.Session()
        self.session.set_property("host", host)
        self.session.set_property("port", str(port))
        GObject.Object.connect(self.session, "channel-new", self._channel_new)
        self.audio = self.glib.Audio.get(self.session, None)  # plays the guest's sound here
        self.glib.Session.connect(self.session)

    def _channel_new(self, _session, channel):
        if isinstance(channel, self.glib.MainChannel):
            GObject.Object.connect(channel, "channel-event", self._main_event)
        elif isinstance(channel, self.glib.DisplayChannel) and self.display is None:
            d = self.gtk.Display.new(self.session, channel.get_property("channel-id"))
            d.set_property("scaling", True)
            d.set_property("resize-guest", True)  # follows the window size once vdagent runs
            d.set_property("grab-keyboard", True)
            self.display = d
            self.on_display(d)

    def _main_event(self, _channel, event):
        ev = self.glib.ChannelEvent
        if event in (ev.CLOSED, ev.ERROR_CONNECT, ev.ERROR_IO, ev.ERROR_LINK, ev.ERROR_AUTH, ev.ERROR_TLS):
            self.on_closed("The connection to the VM's display closed."
                           if event == ev.CLOSED else f"SPICE error: {event.value_nick}")

    def send_cad(self):
        if self.display:
            self.display.send_keys(CAD, self.gtk.DisplayKeyEvent.CLICK)

    def close(self):
        self.glib.Session.disconnect(self.session)


class VncViewer:
    def __init__(self, host: str, port: int, on_display, on_closed):
        GtkVnc = _vnc()
        self.display = GtkVnc.Display()
        self.display.set_scaling(True)
        self.display.set_keep_aspect_ratio(True)
        self.display.connect("vnc-disconnected", lambda *_: on_closed("The connection to the VM's display closed."))
        self.display.connect("vnc-error", lambda _d, msg: on_closed(f"VNC error: {msg}"))
        on_display(self.display)
        self.display.open_host(host, str(port))

    def send_cad(self):
        self.display.send_keys(CAD)

    def close(self):
        self.display.close()


class ConsoleView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.vm: dict | None = None
        self.viewer = None

        self.title = label("", "ctitle-bar")
        self.kind = label("", "muted", "small")
        self.cad = button("Send Ctrl+Alt+Del", lambda *_: self.viewer and self.viewer.send_cad())
        self.full = button("Full screen", lambda *_: self.app.toggle_fullscreen())
        self.shutdown = button("Shut down", lambda *_: self._act("shutdown", "Shut down"))
        self.force = button("Force off", lambda *_: self._act("poweroff", "Force off"), "danger")
        bar = styled(Gtk.Box(spacing=8), "consolebar")
        bar.pack_start(button("← Overview", lambda *_: self.app.show_overview()), False, False, 0)
        bar.pack_start(self.title, False, False, 8)
        bar.pack_start(self.kind, False, False, 0)
        for b in (self.force, self.shutdown, self.full, self.cad):
            bar.pack_end(b, False, False, 0)

        self.screen = styled(Gtk.Box(), "screen")
        self.message = label("", "muted", wrap=True, xalign=0.5)
        self.screen_stack = Gtk.Stack()
        self.screen_stack.add_named(self.screen, "screen")
        self.screen_stack.add_named(self.message, "message")
        self.pack_start(bar, False, False, 0)
        self.pack_start(self.screen_stack, True, True, 0)

    def open(self, vm: dict):
        self.close()
        self.vm = vm
        self.title.set_text(vm["name"])
        self.kind.set_text("")
        self.cad.set_sensitive(False)
        self._say("Connecting…")
        self.app.backend.call(self.app.backend.hv.console_target, vm["uuid"], done=self._on_target)

    def _on_target(self, target, err):
        if self.vm is None:  # left the page meanwhile
            return False
        if err:
            self._say(err)
            return False
        kind = target["type"]
        if kind == "spice" and not _spice():
            kind = "vnc" if _vnc() else None
        if kind == "vnc" and not _vnc():
            kind = None
        if kind is None:
            self._say("No console widget is installed. Run: doas apk add spice-gtk gtk-vnc")
            return False
        if kind != target["type"]:  # SPICE VM but only gtk-vnc installed: use its VNC display
            self.app.backend.call(self.app.backend.hv.vnc_address, self.vm["uuid"],
                                  done=lambda addr, e: self._start("vnc", *addr) if not e else self._say(e))
            return False
        self._start(kind, target["host"], target["port"])
        return False

    def _start(self, kind: str, host: str, port: int):
        self.kind.set_text("SPICE, with sound" if kind == "spice" else "VNC, no sound")
        cls = SpiceViewer if kind == "spice" else VncViewer
        try:
            self.viewer = cls(host, port, self._attach, self._closed)
        except Exception as exc:
            self._say(f"Couldn't open the console: {exc}")
        return False

    def _attach(self, widget):
        for child in self.screen.get_children():
            self.screen.remove(child)
        self.screen.pack_start(widget, True, True, 0)
        widget.show()
        self.screen_stack.set_visible_child_name("screen")
        self.cad.set_sensitive(True)
        widget.grab_focus()

    def _closed(self, why: str):
        self._say(f"{why} The VM may have shut down.")
        self.cad.set_sensitive(False)

    def _say(self, text: str):
        self.message.set_text(text)
        self.screen_stack.set_visible_child_name("message")

    def _act(self, action: str, text: str):
        if not self.vm:
            return
        if action == "poweroff" and not self.app.confirm(
                f"Force off {self.vm['name']}?",
                "This cuts the power like pulling the plug. Unsaved work in the VM is lost.", "Force off"):
            return
        self.app.run_action(self.vm, action, text, lambda: None)

    def close(self):
        if self.viewer:
            try:
                self.viewer.close()
            except Exception:
                pass
        self.viewer = None
        self.vm = None
        for child in self.screen.get_children():
            self.screen.remove(child)
