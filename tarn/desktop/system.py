"""The laptop's own settings for the top panel: Wi-Fi, Bluetooth, sound, brightness, battery.

Everything here blocks (subprocesses, D-Bus calls), so the panel runs it
through Backend.call on a worker thread. Each area reports {"available": False,
"why": ...} instead of raising when its tool or permission is missing, so the
panel can show how to set it up. packaging/desktop/system-access.sh grants the
permissions (netdev group for wpa_supplicant and /dev/rfkill, BlueZ, PipeWire).
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import subprocess
import time

from .. import host

SETUP_HINT = "doas sh ~/src/tarn/packaging/desktop/system-access.sh $USER"


def _run(*args: str, timeout: float = 5) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


# ------------------------------------------------------------------ radios (rfkill)

def radios() -> dict[str, dict]:
    """The real radios by type ('wlan', 'bluetooth'): the phy/hci entries, not the laptop's ACPI ones."""
    out = {}
    for d in sorted(glob.glob("/sys/class/rfkill/rfkill*")):
        kind, name = _read(f"{d}/type"), _read(f"{d}/name")
        if kind not in ("wlan", "bluetooth") or not name.startswith(("phy", "hci")):
            continue
        out.setdefault(kind, {"id": d.rsplit("rfkill", 1)[1], "blocked": _read(f"{d}/soft") == "1",
                              "hard_blocked": _read(f"{d}/hard") == "1"})
    return out


def set_radio(kind: str, on: bool) -> None:
    r = radios().get(kind)
    if r is None:
        raise RuntimeError(f"No {kind} radio found.")
    if not os.access("/dev/rfkill", os.W_OK):
        raise RuntimeError(f"Switching radios needs setup: {SETUP_HINT}")
    code, msg = _run("rfkill", "unblock" if on else "block", r["id"])
    if code:
        raise RuntimeError(msg or "rfkill failed")


# ------------------------------------------------------------------ Wi-Fi (wpa_supplicant)

def wifi_iface() -> str | None:
    for d in sorted(glob.glob("/sys/class/net/*/wireless")):
        return d.split("/")[4]
    return None


def _wpa(iface: str, *args: str) -> str:
    code, out = _run("wpa_cli", "-i", iface, *args)
    if code or out.startswith("Failed to connect") or out == "FAIL":
        raise RuntimeError(out or "wpa_cli failed")
    return out


def _unescape(ssid: str) -> str:
    """wpa_cli prints non-ASCII SSID bytes as \\xNN."""
    try:
        return ssid.encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8")
    except (UnicodeError, ValueError):
        return ssid


def signal_percent(dbm: int) -> int:
    return max(0, min(100, 2 * (dbm + 100)))


def wifi_status() -> dict:
    iface = wifi_iface()
    if iface is None:
        return {"available": False, "why": "No Wi-Fi adapter."}
    radio = radios().get("wlan", {})
    base = {"iface": iface, "on": not radio.get("blocked", False), "hard_blocked": radio.get("hard_blocked", False)}
    try:
        status = dict(line.split("=", 1) for line in _wpa(iface, "status").splitlines() if "=" in line)
    except RuntimeError:
        return {**base, "available": False, "why": f"Wi-Fi controls need setup: {SETUP_HINT}"}
    connected = status.get("wpa_state") == "COMPLETED"
    signal = None
    if connected:
        try:
            poll = dict(l.split("=", 1) for l in _wpa(iface, "signal_poll").splitlines() if "=" in l)
            signal = signal_percent(int(poll.get("RSSI", "-100")))
        except (RuntimeError, ValueError):
            pass
    return {**base, "available": True, "state": status.get("wpa_state", "").lower(), "connected": connected,
            "ssid": _unescape(status.get("ssid", "")) if connected else None,
            "ip": status.get("ip_address"), "signal": signal}


def wifi_scan(wait: float = 3.0) -> list[dict]:
    """Nearby networks, strongest first, one entry per name, marked known/current."""
    iface = wifi_iface()
    _wpa(iface, "scan")
    time.sleep(wait)
    nets: dict[str, dict] = {}
    for line in _wpa(iface, "scan_results").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 5 or not parts[4]:
            continue  # hidden networks have no name
        ssid = _unescape(parts[4])
        dbm = int(parts[2]) if parts[2].lstrip("-").isdigit() else -100
        flags = parts[3]
        if ssid not in nets or dbm > nets[ssid]["dbm"]:
            nets[ssid] = {"ssid": ssid, "dbm": dbm, "signal": signal_percent(dbm),
                          "secure": "WPA" in flags or "RSN" in flags or "SAE" in flags,
                          "enterprise": "EAP" in flags}
    known = _known(iface)
    for n in nets.values():
        n["known"] = n["ssid"] in known
        n["current"] = known.get(n["ssid"], {}).get("current", False)
    return sorted(nets.values(), key=lambda n: (not n["current"], -n["dbm"]))


def _known(iface: str) -> dict[str, dict]:
    known = {}
    for line in _wpa(iface, "list_networks").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 2:
            known.setdefault(_unescape(parts[1]), {"id": parts[0], "current": "[CURRENT]" in line})
    return known


def wifi_connect(ssid: str, password: str | None = None) -> None:
    """Join a network: a saved one by name, or a new one (WPA passphrase or open), saved for next time."""
    iface = wifi_iface()
    known = _known(iface)
    if ssid in known and not password:
        net = known[ssid]["id"]
    else:
        if ssid in known:  # new password for a saved network: replace it
            _wpa(iface, "remove_network", known[ssid]["id"])
        net = _wpa(iface, "add_network").splitlines()[-1].strip()
        try:
            # Hex values sidestep wpa_cli's quoting for any character in the name or password.
            _wpa(iface, "set_network", net, "ssid", ssid.encode().hex())
            if password:
                if not 8 <= len(password) <= 63:
                    raise RuntimeError("Wi-Fi passwords are 8 to 63 characters.")
                psk = hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()
                _wpa(iface, "set_network", net, "psk", psk)
            else:
                _wpa(iface, "set_network", net, "key_mgmt", "NONE")
        except RuntimeError:
            _wpa(iface, "remove_network", net)
            raise
    _wpa(iface, "select_network", net)
    # select_network disables every other saved network; turn them back on (it stays on this one)
    # so saving doesn't make that permanent.
    _wpa(iface, "enable_network", "all")
    try:
        _wpa(iface, "save_config")
    except RuntimeError:
        pass  # update_config isn't on; it still connects, just not saved for the next boot


def wifi_disconnect() -> None:
    _wpa(wifi_iface(), "disconnect")


def wifi_reconnect() -> None:
    iface = wifi_iface()
    _wpa(iface, "enable_network", "all")
    _wpa(iface, "reconnect")


def wifi_forget(ssid: str) -> None:
    iface = wifi_iface()
    net = _known(iface).get(ssid)
    if net:
        _wpa(iface, "remove_network", net["id"])
        try:
            _wpa(iface, "save_config")
        except RuntimeError:
            pass


def ethernet() -> list[dict]:
    """Wired links: name and whether a cable is plugged in."""
    out = []
    for d in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(d)
        if name == "lo" or os.path.exists(f"{d}/wireless") or not os.path.exists(f"{d}/device"):
            continue
        out.append({"iface": name, "up": _read(f"{d}/carrier") == "1"})
    return out


# ------------------------------------------------------------------ sound (PipeWire)

def sound() -> dict:
    if not shutil.which("wpctl"):
        return {"available": False, "why": f"Sound controls need PipeWire: {SETUP_HINT}"}
    code, out = _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
    m = re.search(r"Volume:\s*([\d.]+)", out)
    if code or not m:
        return {"available": False, "why": "PipeWire isn't running (tarn-kiosk starts it)."}
    return {"available": True, "volume": round(float(m.group(1)) * 100), "muted": "[MUTED]" in out,
            "outputs": _sinks()}


def _sinks() -> list[dict]:
    """Output devices from `wpctl status`: id, name, and which one is the default."""
    code, out = _run("wpctl", "status")
    sinks, inside = [], False
    for line in out.splitlines():
        if re.search(r"Sinks:", line):
            inside = True
            continue
        if inside:
            m = re.search(r"(\*)?\s+(\d+)\.\s+(.+?)(\s+\[vol:.*\])?\s*$", line)
            if not m:
                break
            sinks.append({"id": m.group(2), "name": m.group(3).strip(), "default": bool(m.group(1))})
    return sinks


def set_volume(percent: int) -> None:
    _run("wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{max(0, min(100, percent)) / 100:.2f}")


def set_mute(muted: bool) -> None:
    _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0")


def set_output(sink_id: str) -> None:
    _run("wpctl", "set-default", sink_id)


# ------------------------------------------------------------------ screen

def brightness() -> dict:
    if not shutil.which("brightnessctl"):
        return {"available": False, "why": "Install brightnessctl: doas apk add brightnessctl"}
    code, out = _run("brightnessctl", "-m")
    parts = out.split(",")
    if code or len(parts) < 5:
        return {"available": False, "why": out or "No backlight found."}
    return {"available": True, "percent": int(parts[3].rstrip("%"))}


def set_brightness(percent: int) -> None:
    _run("brightnessctl", "-q", "set", f"{max(1, min(100, percent))}%")  # 1%: never fully black by accident


def screen_off_now() -> str | None:
    """Power the screen off until the next key or touch (OLED care). Needs wlopm, inside the kiosk."""
    if not shutil.which("wlopm"):
        return "Install wlopm: doas apk add wlopm"
    code, out = _run("wlopm", "--off", "*")
    return out if code else None


# ------------------------------------------------------------------ battery

def battery() -> dict:
    p = host.power_status()
    b = p["batteries"][0] if p["batteries"] else None
    path = next(iter(glob.glob("/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode")), None)
    return {"present": b is not None, "percent": b and b["capacity"], "status": b and (b["status"] or "").lower(),
            "ac": p["ac_online"], "conservation": p["battery_conservation"],
            "conservation_writable": bool(path and os.access(path, os.W_OK))}


def set_conservation(on: bool) -> None:
    path = next(iter(glob.glob("/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode")), None)
    if not path or not os.access(path, os.W_OK):
        raise RuntimeError(f"Switching conservation mode needs setup: {SETUP_HINT}")
    with open(path, "w") as fh:
        fh.write("1" if on else "0")


def summary() -> dict:
    """One poll for the panel's icons."""
    return {"wifi": wifi_status(), "ethernet": ethernet(), "bluetooth": bluetooth_summary(),
            "sound": sound(), "brightness": brightness(), "battery": battery()}


# ------------------------------------------------------------------ Bluetooth (BlueZ over D-Bus)

def _bus():
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def _objects() -> dict:
    from gi.repository import Gio, GLib
    reply = _bus().call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                             None, GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 5000, None)
    return reply.unpack()[0]


def _bt_error(exc: Exception) -> str:
    text = str(exc)
    for marker in ("ServiceUnknown", "Could not connect", "No such file", "FileNotFound"):
        if marker in text:
            return f"Bluetooth needs setup: {SETUP_HINT}"
    return re.sub(r"^.*?: ", "", text.replace("GDBus.Error:", ""))


def bluetooth() -> dict:
    """Adapter state and every known or discovered device."""
    radio = radios().get("bluetooth")
    if radio is None:
        return {"available": False, "why": "No Bluetooth adapter."}
    try:
        objects = _objects()
    except Exception as exc:  # GLib.Error: no system bus, or bluetoothd not running
        return {"available": False, "why": _bt_error(exc)}
    adapter = next(((p, i["org.bluez.Adapter1"]) for p, i in objects.items() if "org.bluez.Adapter1" in i), None)
    if adapter is None:
        return {"available": False, "why": "bluetoothd sees no adapter (is it blocked?)."}
    devices = []
    for path, ifaces in objects.items():
        d = ifaces.get("org.bluez.Device1")
        if d is None or not path.startswith(adapter[0]):
            continue
        devices.append({"path": path, "name": d.get("Alias") or d.get("Name") or d.get("Address"),
                        "address": d.get("Address"), "paired": d.get("Paired", False),
                        "connected": d.get("Connected", False), "icon": d.get("Icon", ""),
                        "rssi": d.get("RSSI"), "named": bool(d.get("Name"))})
    devices.sort(key=lambda d: (not d["connected"], not d["paired"], -(d["rssi"] or -200)))
    return {"available": True, "adapter": adapter[0], "on": bool(adapter[1].get("Powered")) and not radio["blocked"],
            "discovering": bool(adapter[1].get("Discovering")), "devices": devices}


def bluetooth_summary() -> dict:
    bt = bluetooth()
    if not bt["available"]:
        return bt
    connected = [d["name"] for d in bt["devices"] if d["connected"]]
    return {"available": True, "on": bt["on"], "connected": connected}


def _call(path: str, iface: str, method: str, args=None, timeout_ms: int = 30000):
    from gi.repository import Gio
    try:
        return _bus().call_sync("org.bluez", path, iface, method, args, None, Gio.DBusCallFlags.NONE,
                                timeout_ms, None)
    except Exception as exc:
        raise RuntimeError(_bt_error(exc)) from exc


def _set_prop(path: str, iface: str, name: str, value) -> None:
    from gi.repository import GLib
    _call(path, "org.freedesktop.DBus.Properties", "Set", GLib.Variant("(ssv)", (iface, name, value)))


def set_bluetooth(on: bool) -> None:
    from gi.repository import GLib
    if on:
        set_radio("bluetooth", True)
        time.sleep(1)  # bluetoothd re-adds the adapter after the radio comes back
    bt = bluetooth()
    if not bt["available"]:
        raise RuntimeError(bt["why"])
    _set_prop(bt["adapter"], "org.bluez.Adapter1", "Powered", GLib.Variant("b", on))


def bluetooth_discover(seconds: float = 12) -> None:
    """Look for new devices for a while; they show up in bluetooth()['devices']."""
    bt = bluetooth()
    if not bt["available"]:
        raise RuntimeError(bt["why"])
    if not bt["discovering"]:
        _call(bt["adapter"], "org.bluez.Adapter1", "StartDiscovery")
    time.sleep(seconds)
    try:
        _call(bt["adapter"], "org.bluez.Adapter1", "StopDiscovery")
    except RuntimeError:
        pass


def bluetooth_pair(path: str) -> None:
    """Pair, trust (so it reconnects by itself) and connect a device."""
    from gi.repository import GLib
    AGENT.expect(path)
    try:
        _call(path, "org.bluez.Device1", "Pair", timeout_ms=60000)
    finally:
        AGENT.expect(None)
    _set_prop(path, "org.bluez.Device1", "Trusted", GLib.Variant("b", True))
    _call(path, "org.bluez.Device1", "Connect")


def bluetooth_connect(path: str, on: bool) -> None:
    _call(path, "org.bluez.Device1", "Connect" if on else "Disconnect")


def bluetooth_forget(path: str) -> None:
    from gi.repository import GLib
    adapter = path.rsplit("/", 1)[0]
    _call(adapter, "org.bluez.Adapter1", "RemoveDevice", GLib.Variant("(o)", (path,)))


class _Agent:
    """A pairing agent for devices with no screen or keypad (headphones, speakers, mice).

    It answers BlueZ's "Just Works" questions with yes, but only for the device
    the user just chose to pair, so nothing in range can pair itself.
    """

    PATH = "/org/tarn/agent"
    XML = """<node><interface name="org.bluez.Agent1">
      <method name="Release"/><method name="Cancel"/>
      <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
      <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
      <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
      <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
      <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
      <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/>
        <arg type="q" direction="in"/></method>
      <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    </interface></node>"""

    def __init__(self):
        self.target: str | None = None
        self.registered = False

    def expect(self, path: str | None):
        self.target = path

    def register(self) -> str | None:
        """Call once on the GTK thread; returns an error message or None."""
        if self.registered:
            return None
        from gi.repository import Gio, GLib
        try:
            bus = _bus()
            info = Gio.DBusNodeInfo.new_for_xml(self.XML).interfaces[0]
            bus.register_object(self.PATH, info, self._method, None, None)
            for method, args in (("RegisterAgent", GLib.Variant("(os)", (self.PATH, "NoInputNoOutput"))),
                                 ("RequestDefaultAgent", GLib.Variant("(o)", (self.PATH,)))):
                bus.call_sync("org.bluez", "/org/bluez", "org.bluez.AgentManager1", method, args, None,
                              Gio.DBusCallFlags.NONE, 5000, None)
            self.registered = True
            return None
        except Exception as exc:
            return _bt_error(exc)

    def _method(self, _conn, _sender, _path, _iface, method, params, invocation):
        device = params.unpack()[0] if params.n_children() else None
        if method in ("Release", "Cancel", "DisplayPasskey", "DisplayPinCode"):
            invocation.return_value(None)
        elif method in ("RequestAuthorization", "RequestConfirmation", "AuthorizeService") and device == self.target:
            invocation.return_value(None)
        else:
            invocation.return_dbus_error("org.bluez.Error.Rejected", "Not expecting this device")


AGENT = _Agent()
