"""The desktop window: polling, toasts, confirmations and the libvirt-unreachable screen."""

from __future__ import annotations

import socket

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import theme  # noqa: E402
from .backend import Backend, access_hint  # noqa: E402
from .console import ConsoleView  # noqa: E402
from .create import CreateView  # noqa: E402
from .hostpage import HostPage  # noqa: E402
from . import lookingglass  # noqa: E402
from .overview import Overview  # noqa: E402
from .panel import Panel  # noqa: E402
from .vmpage import VmPage  # noqa: E402
from .widgets import label, styled  # noqa: E402


def _lan_address() -> str:
    """The address other devices would use, without sending any packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1; connect() on UDP only picks a route
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


class TarnWindow(Gtk.Window):
    def __init__(self, cfg, theme_name: str, fullscreen: bool):
        super().__init__(title="Tarn")
        self.palette = theme.PALETTES[theme_name]
        self.web_url = f"https://{_lan_address()}:{cfg['server']['port']}"
        self.backend = Backend(cfg)

        styled(self, "tarn")
        self.set_default_size(1280, 800)
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", theme_name == "dark")
        provider = Gtk.CssProvider()
        provider.load_from_data(theme.css(theme_name))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.overview = Overview(self)
        self.create = CreateView(self)
        self.console = ConsoleView(self)
        self.vmpage = VmPage(self)
        self.hostpage = HostPage(self)
        self.page = "overview"  # what to show once libvirt answers
        self.problem = styled(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10), "problem")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        # Each page scrolls on its own, except the console, which must fill the window.
        self.scrollers = {}
        for name, page in (("overview", self.overview), ("create", self.create), ("vm", self.vmpage),
                           ("host", self.hostpage),
                           ("problem", self.problem),
                           ("loading", label("Connecting to libvirt…", "muted", xalign=0.5))):
            sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sc.add(page)
            self.scrollers[name] = sc
            self.stack.add_named(sc, name)
        self.stack.add_named(self.console, "console")

        self.toast_label = label("", wrap=True, max_width_chars=60)
        self.toast_box = styled(Gtk.Box(), "toast")
        self.toast_box.add(self.toast_label)
        self.toast = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
                                  halign=Gtk.Align.START, valign=Gtk.Align.END)
        self.toast.add(self.toast_box)
        self._toast_timer = 0

        overlay = Gtk.Overlay()
        overlay.add(self.stack)
        overlay.add_overlay(self.toast)
        # The top panel (Wi-Fi, Bluetooth, sound...) sits above every page but the console.
        self.panel = Panel(self)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self.panel, False, False, 0)
        outer.pack_start(overlay, True, True, 0)
        self.add(outer)

        self.connect("key-press-event", self._on_key)
        self.connect("destroy", Gtk.main_quit)
        self.is_fullscreen = fullscreen
        if fullscreen:
            self.fullscreen()

        self.show_all()
        self.stack.set_visible_child_name("loading")
        self.refresh()
        GLib.timeout_add(int(self.backend.interval * 1000), self._tick)

    # ------------------------------------------------------------ polling

    def _tick(self):
        self.refresh()
        return True  # keep the timer

    def refresh(self):
        self.backend.poll(self._on_snapshot)

    def _on_snapshot(self, snap):
        if snap["error"]:
            self._show_problem(snap)
        else:
            self.overview.update(snap)
            if self.page == "vm":
                self.vmpage.on_poll(snap["vms"])
            elif self.page == "host":
                self.hostpage.on_poll(snap)
            if self.stack.get_visible_child_name() in ("loading", "problem"):
                self.stack.set_visible_child_name(self.page)
        return False  # one-shot idle callback

    # ------------------------------------------------------------ pages

    def _show(self, page: str):
        if self.page == "console" and page != "console":
            self.console.close()
        if self.page == "vm" and page != "vm":
            self.vmpage.close()
        if self.page == "host" and page != "host":
            self.hostpage.close()
        self.page = page
        self.panel.set_visible(page != "console")
        if self.stack.get_visible_child_name() != "problem":
            self.stack.set_visible_child_name(page)
        if page in self.scrollers:
            self.scrollers[page].get_vadjustment().set_value(0)

    def show_create(self, *_):
        self._show("create")
        self.create.open()

    def show_overview(self, *_):
        self._show("overview")
        self.refresh()

    def show_vm(self, uuid: str):
        self._show("vm")
        self.vmpage.open(uuid)

    def show_host(self, *_):
        self._show("host")
        self.hostpage.open()

    def show_console(self, vm: dict):
        self._show("console")
        self.console.open(vm)

    def toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
        (self.fullscreen if self.is_fullscreen else self.unfullscreen)()

    def _show_problem(self, snap):
        for child in self.problem.get_children():
            self.problem.remove(child)
        self.problem.pack_start(label("Can't reach libvirt", "h2"), False, False, 0)
        self.problem.pack_start(label(f"Connecting to {snap['uri']} failed: {snap['error']}",
                                      "muted", wrap=True, selectable=True), False, False, 0)
        hint = access_hint(snap["uri"])
        if hint:
            self.problem.pack_start(label("To fix it, run this in a terminal:", "muted"), False, False, 6)
            self.problem.pack_start(label(hint, "code", selectable=True), False, False, 0)
        self.problem.pack_start(label("Tarn keeps retrying every few seconds.", "muted"), False, False, 6)
        self.problem.show_all()
        self.stack.set_visible_child_name("problem")

    # ------------------------------------------------------------ actions

    def run_action(self, vm: dict, action: str, text: str, done):
        def finished(err):
            done()
            if err:
                self.show_toast(f"{vm['name']}: {err}", error=True)
            else:
                self.show_toast(f"{vm['name']}: {text.lower()} sent")
            self.refresh()
            return False

        self.backend.action(vm["uuid"], action, finished)

    def looking_glass(self, vm: dict):
        """Open Looking Glass for a running VM, on that VM's own SPICE port."""
        def got(target, err):
            if err:
                self.show_toast(f"{vm['name']}: {err}", error=True)
                return False
            port = target["port"] if target["type"] == "spice" else None
            started, message = lookingglass.launch(port)
            self.show_toast(message, error=not started and "already open" not in message)
            return False

        self.backend.call(self.backend.hv.console_target, vm["uuid"], done=got)

    def start_with_looking_glass(self, vm: dict, done):
        """Start the VM and open Looking Glass straight away; the client waits for Windows to boot."""
        def finished(err):
            done()
            if err:
                self.show_toast(f"{vm['name']}: {err}", error=True)
            else:
                self.looking_glass(vm)
            self.refresh()
            return False

        self.backend.action(vm["uuid"], "start", finished)

    def confirm(self, title: str, body: str, ok: str) -> bool:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.format_secondary_text(body)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        styled(dlg.add_button(ok, Gtk.ResponseType.OK), "destructive-action")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        answer = dlg.run()
        dlg.destroy()
        return answer == Gtk.ResponseType.OK

    def show_toast(self, text: str, error: bool = False):
        self.toast_label.set_text(text)
        ctx = self.toast_box.get_style_context()
        (ctx.add_class if error else ctx.remove_class)("error")
        self.toast.set_reveal_child(True)
        if self._toast_timer:
            GLib.source_remove(self._toast_timer)
        self._toast_timer = GLib.timeout_add(6000 if error else 3500, self._hide_toast)

    def _hide_toast(self):
        self.toast.set_reveal_child(False)
        self._toast_timer = 0
        return False

    # ------------------------------------------------------------ keys

    def _on_key(self, _w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        shift = ev.state & Gdk.ModifierType.SHIFT_MASK
        if self.page == "console":
            # Every other key belongs to the guest. Shift+F11/F12 follow virt-viewer.
            if shift and ev.keyval == Gdk.KEY_F11:
                self.toggle_fullscreen()
                return True
            if shift and ev.keyval == Gdk.KEY_F12:
                self.show_overview()
                return True
            return False
        if ev.keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        if ctrl and ev.keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.destroy()
            return True
        if ev.keyval == Gdk.KEY_Escape and self.page in ("create", "vm", "host"):
            self.show_overview()
            return True
        if ev.keyval == Gdk.KEY_F5 or (ctrl and ev.keyval in (Gdk.KEY_r, Gdk.KEY_R)):
            self.refresh()
            if self.page == "host":
                self.hostpage.reload()
            return True
        return False


def run(cfg, theme_name: str = "light", fullscreen: bool = False) -> int:
    GLib.set_prgname("tarn")  # becomes the Wayland app_id, which compositor window rules match on
    TarnWindow(cfg, theme_name, fullscreen)
    Gtk.main()
    return 0
