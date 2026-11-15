"""Domain XML: build new VMs from a simple spec, and parse existing ones.

The templates encode the choices you would otherwise click through in
virt-manager: Q35, UEFI, a TPM and Hyper-V enlightenments for Windows;
VirtIO everything plus a guest-agent channel for Linux.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

TARN_NS = "https://github.com/tarn-vm/tarn/xmlns/1"
ET.register_namespace("tarn", TARN_NS)
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
ET.register_namespace("qemu", QEMU_NS)  # keeps qemu:commandline intact when Tarn rewrites XML

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
OS_TYPES = ("linux", "windows", "other")


class SpecError(ValueError):
    """The user's VM spec is invalid; the message is safe to show."""


@dataclass
class HostCaps:
    """What this host's libvirt/QEMU can do, read from (domain)capabilities."""

    virt_type: str = "kvm"          # "kvm" or "qemu" (TCG, no hardware accel)
    emulator: str | None = None
    efi: bool = True                 # firmware autoselection with an EFI build
    secure_boot: bool = False        # a secure-boot-capable EFI build exists
    tpm: bool = False                # TPM emulator backend supported (swtpm)
    spice: bool = False              # QEMU has its SPICE modules (qemu-ui-spice-core, audio, chardev)
    q35: bool = True


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, {k.rstrip("_"): str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def _target_names(prefix: str):
    i = 0
    while True:
        yield prefix + "abcdefghijklmnopqrstuvwxyz"[i]
        i += 1


def normalize_spec(raw: dict, caps: HostCaps) -> dict:
    """Validate a create request and fill OS-specific defaults."""
    name = str(raw.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise SpecError("Name must be 1-63 characters: letters, digits, '.', '_' or '-'.")
    os_type = raw.get("os_type", "linux")
    if os_type not in OS_TYPES:
        raise SpecError(f"os_type must be one of {', '.join(OS_TYPES)}")

    windows = os_type == "windows"
    try:
        vcpus = int(raw.get("vcpus", 4 if windows else 2))
        memory_mib = int(raw.get("memory_mib", 8192 if windows else 2048))
    except (TypeError, ValueError) as exc:
        raise SpecError("vCPUs and memory must be numbers") from exc
    if not 1 <= vcpus <= 256:
        raise SpecError("vCPUs must be between 1 and 256")
    if not 128 <= memory_mib <= 1024 * 1024:
        raise SpecError("Memory must be between 128 MiB and 1 TiB")

    firmware = raw.get("firmware", "efi" if os_type != "other" else "bios")
    if firmware not in ("efi", "bios"):
        raise SpecError("firmware must be 'efi' or 'bios'")
    if firmware == "efi" and not caps.efi:
        raise SpecError("This host has no UEFI firmware for QEMU (install the ovmf package).")

    secure_boot = bool(raw.get("secure_boot", windows and caps.secure_boot))
    if secure_boot and (firmware != "efi" or not caps.secure_boot):
        raise SpecError("Secure Boot needs UEFI firmware with a secure-boot build on the host.")

    tpm = bool(raw.get("tpm", windows and caps.tpm))
    if tpm and not caps.tpm:
        raise SpecError("This host cannot emulate a TPM (install swtpm).")

    # Windows has no VirtIO drivers out of the box. Default to devices it can
    # drive itself unless the user says the drivers are available.
    virtio = bool(raw.get("virtio", not windows and os_type != "other"))

    disk = raw.get("disk") or {"mode": "none"}
    mode = disk.get("mode", "none")
    if mode == "new":
        try:
            size = float(disk.get("size_gib", 0))
        except (TypeError, ValueError) as exc:
            raise SpecError("Disk size must be a number") from exc
        if not 1 <= size <= 64 * 1024:
            raise SpecError("Disk size must be between 1 GiB and 64 TiB")
        disk = {"mode": "new", "size_gib": size, "pool": disk.get("pool") or None}
    elif mode == "existing":
        path = str(disk.get("path", "")).strip()
        if not path.startswith("/"):
            raise SpecError("Pick an existing disk image")
        disk = {"mode": "existing", "path": path, "format": disk.get("format") or None}
    elif mode == "none":
        disk = {"mode": "none"}
    else:
        raise SpecError("disk.mode must be new, existing or none")

    net = raw.get("network") or {"type": "network", "name": "default"}
    if net.get("type") not in ("network", "bridge", "none"):
        raise SpecError("network.type must be network, bridge or none")

    def _path(value):
        value = (value or "").strip() if isinstance(value, str) else ""
        return value or None

    return {
        "name": name,
        "os_type": os_type,
        "vcpus": vcpus,
        "memory_mib": memory_mib,
        "firmware": firmware,
        "secure_boot": secure_boot,
        "tpm": tpm,
        "virtio": virtio,
        "disk": disk,
        "iso": _path(raw.get("iso")),
        "driver_iso": _path(raw.get("driver_iso")),
        "network": net,
        "start": bool(raw.get("start", True)),
    }


def add_spice(dev: ET.Element) -> None:
    """A SPICE display next to VNC, for the desktop console and Looking Glass: sound, clipboard, input.

    Clipboard and resolution sync need spice-vdagent in the guest.
    """
    spice = _sub(dev, "graphics", type="spice", autoport="yes", listen="127.0.0.1")
    _sub(spice, "listen", type="address", address="127.0.0.1")
    _sub(spice, "image", compression="off")  # local only: CPU matters more than bandwidth
    if dev.find("channel[@type='spicevmc']") is None:
        vdagent = _sub(dev, "channel", type="spicevmc")
        _sub(vdagent, "target", type="virtio", name="com.redhat.spice.0")
    if dev.find("audio") is None:
        _sub(dev, "audio", id="1", type="spice")
        sound = dev.find("sound")
        if sound is None:
            sound = _sub(dev, "sound", model="ich9")  # Intel HDA: Windows and Linux have drivers built in
        if sound.find("audio") is None:
            _sub(sound, "audio", id="1")


def set_acpi_table(root: ET.Element, path: str, present: bool) -> None:
    """Add or remove a raw ACPI table (<os><acpi><table type='raw'>path</table>) in a domain's XML."""
    os_el = root.find("os")
    acpi = os_el.find("acpi")
    tables = [t for t in (acpi.findall("table") if acpi is not None else []) if (t.text or "").strip() == path]
    if present and not tables:
        if acpi is None:
            acpi = ET.SubElement(os_el, "acpi")
        ET.SubElement(acpi, "table", type="raw").text = path
    elif not present and tables:
        for t in tables:
            acpi.remove(t)
        if not len(acpi):
            os_el.remove(acpi)


def looking_glass_args(path: str, size_mib: int) -> list[str]:
    """qemu:commandline arguments that map a kvmfr device into the guest as an IVSHMEM PCI device."""
    return ["-device", json.dumps({"driver": "ivshmem-plain", "id": "shmem0", "memdev": "looking-glass"},
                                  separators=(",", ":")),
            "-object", json.dumps({"qom-type": "memory-backend-file", "id": "looking-glass", "mem-path": path,
                                   "size": size_mib * 1024 * 1024, "share": True}, separators=(",", ":"))]


def build_domain(spec: dict, caps: HostCaps, disk_path: str | None, disk_format: str = "qcow2") -> str:
    """Return domain XML for a normalized spec."""
    windows = spec["os_type"] == "windows"
    kvm = caps.virt_type == "kvm"

    dom = ET.Element("domain", type=caps.virt_type)
    _sub(dom, "name", spec["name"])
    meta = _sub(dom, "metadata")
    tm = ET.SubElement(meta, f"{{{TARN_NS}}}vm")
    ET.SubElement(tm, f"{{{TARN_NS}}}os").text = spec["os_type"]

    _sub(dom, "memory", spec["memory_mib"], unit="MiB")
    _sub(dom, "currentMemory", spec["memory_mib"], unit="MiB")
    _sub(dom, "vcpu", spec["vcpus"], placement="static")

    os_el = _sub(dom, "os", **({"firmware": "efi"} if spec["firmware"] == "efi" else {}))
    _sub(os_el, "type", "hvm", arch="x86_64", machine="q35" if caps.q35 else "pc")
    if spec["firmware"] == "efi":
        fw = _sub(os_el, "firmware")
        _sub(fw, "feature", enabled="yes" if spec["secure_boot"] else "no", name="secure-boot")

    feats = _sub(dom, "features")
    _sub(feats, "acpi")
    _sub(feats, "apic")
    if spec["secure_boot"]:
        _sub(feats, "smm", state="on")
    if windows and kvm:
        hv = _sub(feats, "hyperv", mode="custom")
        for flag in ("relaxed", "vapic", "vpindex", "runtime", "synic", "stimer",
                     "reset", "frequencies", "tlbflush", "ipi"):
            _sub(hv, flag, state="on")
        _sub(hv, "spinlocks", state="on", retries="8191")
    if kvm:
        _sub(feats, "vmport", state="off")

    if kvm:
        cpu = _sub(dom, "cpu", mode="host-passthrough", check="none", migratable="on")
    else:
        cpu = _sub(dom, "cpu", mode="maximum", check="none")
    _sub(cpu, "topology", sockets=1, dies=1, cores=spec["vcpus"], threads=1)

    clock = _sub(dom, "clock", offset="localtime" if windows else "utc")
    _sub(clock, "timer", name="rtc", tickpolicy="catchup")
    _sub(clock, "timer", name="pit", tickpolicy="delay")
    _sub(clock, "timer", name="hpet", present="no")
    if windows and kvm:
        _sub(clock, "timer", name="hypervclock", present="yes")

    _sub(dom, "on_poweroff", "destroy")
    _sub(dom, "on_reboot", "restart")
    _sub(dom, "on_crash", "destroy")
    pm = _sub(dom, "pm")
    _sub(pm, "suspend-to-mem", enabled="no")
    _sub(pm, "suspend-to-disk", enabled="no")

    dev = _sub(dom, "devices")
    if caps.emulator:
        _sub(dev, "emulator", caps.emulator)

    virtio = spec["virtio"]
    sata_names = _target_names("sd")
    boot = 1
    cdroms = [p for p in (spec["iso"], spec["driver_iso"]) if p]
    if spec["iso"]:
        # Install media boots first; once the guest is installed, eject it.
        cd = _sub(dev, "disk", type="file", device="cdrom")
        _sub(cd, "driver", name="qemu", type="raw")
        _sub(cd, "source", file=spec["iso"])
        _sub(cd, "target", dev=next(sata_names), bus="sata")
        _sub(cd, "readonly")
        _sub(cd, "boot", order=boot)
        boot += 1
        cdroms = cdroms[1:]

    if disk_path:
        d = _sub(dev, "disk", type="file", device="disk")
        drv = {"name": "qemu", "type": disk_format, "discard": "unmap"}
        if kvm:
            drv["cache"] = "none"
            drv["io"] = "native"
        _sub(d, "driver", **drv)
        _sub(d, "source", file=disk_path)
        _sub(d, "target", dev="vda" if virtio else next(sata_names), bus="virtio" if virtio else "sata")
        _sub(d, "boot", order=boot)
        boot += 1

    for extra in cdroms:  # e.g. virtio-win.iso for Windows drivers
        cd = _sub(dev, "disk", type="file", device="cdrom")
        _sub(cd, "driver", name="qemu", type="raw")
        _sub(cd, "source", file=extra)
        _sub(cd, "target", dev=next(sata_names), bus="sata")
        _sub(cd, "readonly")

    if not spec["iso"] and not cdroms:
        # An empty drive so an ISO can be inserted later from the UI.
        cd = _sub(dev, "disk", type="file", device="cdrom")
        _sub(cd, "driver", name="qemu", type="raw")
        _sub(cd, "target", dev=next(sata_names), bus="sata")
        _sub(cd, "readonly")

    net = spec["network"]
    if net["type"] in ("network", "bridge"):
        iface = _sub(dev, "interface", type=net["type"])
        if net["type"] == "network":
            _sub(iface, "source", network=net.get("name") or "default")
        else:
            _sub(iface, "source", bridge=net["name"])
        _sub(iface, "model", type="virtio" if virtio else "e1000e")

    _sub(dev, "controller", type="usb", model="qemu-xhci", ports="15")
    _sub(dev, "input", type="tablet", bus="usb")  # absolute pointer: noVNC stays in sync

    chan = _sub(dev, "channel", type="unix")
    _sub(chan, "target", type="virtio", name="org.qemu.guest_agent.0")

    # VNC stays first: the web console (noVNC) and parse_domain()["graphics"] use it.
    gfx = _sub(dev, "graphics", type="vnc", port="-1", autoport="yes", listen="127.0.0.1")
    _sub(gfx, "listen", type="address", address="127.0.0.1")  # only reachable via Tarn's proxy
    if caps.spice:
        add_spice(dev)
    video = _sub(dev, "video")
    if virtio:
        _sub(video, "model", type="virtio", heads="1", primary="yes")
    else:
        _sub(video, "model", type="vga", vram="16384", heads="1", primary="yes")

    if spec["tpm"]:
        tpm = _sub(dev, "tpm", model="tpm-crb")
        _sub(tpm, "backend", type="emulator", version="2.0")

    _sub(dev, "memballoon", model="virtio")
    rng = _sub(dev, "rng", model="virtio")
    _sub(rng, "backend", "/dev/urandom", model="random")

    ET.indent(dom)
    return ET.tostring(dom, encoding="unicode")


def cdrom_xml(target: str, bus: str, path: str | None) -> str:
    disk = ET.Element("disk", type="file", device="cdrom")
    _sub(disk, "driver", name="qemu", type="raw")
    if path:
        _sub(disk, "source", file=path)
    _sub(disk, "target", dev=target, bus=bus)
    _sub(disk, "readonly")
    return ET.tostring(disk, encoding="unicode")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_UNITS = {
    "b": 1, "bytes": 1, "k": 1024, "kib": 1024, "kb": 1000, "m": 1024**2, "mib": 1024**2,
    "mb": 1000**2, "g": 1024**3, "gib": 1024**3, "gb": 1000**3, "t": 1024**4, "tib": 1024**4,
}


def to_bytes(el: ET.Element | None, default_unit: str = "KiB") -> int:
    if el is None or el.text is None:
        return 0
    unit = (el.get("unit") or default_unit).lower()
    return int(el.text) * _UNITS.get(unit, 1024)


def parse_domain(xml: str) -> dict:
    root = ET.fromstring(xml)
    os_el = root.find("os")
    type_el = os_el.find("type") if os_el is not None else None
    loader = os_el.find("loader") if os_el is not None else None
    firmware = "bios"
    secure = False
    if os_el is not None and (os_el.get("firmware") == "efi" or (loader is not None and loader.get("type") == "pflash")):
        firmware = "efi"
        if loader is not None and loader.get("secure") == "yes":
            secure = True
        for feat in os_el.findall("firmware/feature"):
            if feat.get("name") == "secure-boot" and feat.get("enabled") == "yes":
                secure = True

    meta_os = root.find(f"metadata/{{{TARN_NS}}}vm/{{{TARN_NS}}}os")
    os_type = meta_os.text if meta_os is not None else None
    if os_type is None:
        os_type = "windows" if root.find("features/hyperv") is not None else "linux"

    disks = []
    for d in root.findall("devices/disk"):
        src = d.find("source")
        tgt = d.find("target")
        drv = d.find("driver")
        bootel = d.find("boot")
        path = None
        if src is not None:
            path = src.get("file") or src.get("dev") or src.get("volume")
        disks.append({
            "device": d.get("device", "disk"),
            "type": d.get("type"),
            "path": path,
            "target": tgt.get("dev") if tgt is not None else None,
            "bus": tgt.get("bus") if tgt is not None else None,
            "format": drv.get("type") if drv is not None else None,
            "readonly": d.find("readonly") is not None,
            "boot_order": int(bootel.get("order")) if bootel is not None else None,
        })

    nics = []
    for i in root.findall("devices/interface"):
        src = i.find("source")
        mac = i.find("mac")
        model = i.find("model")
        tgt = i.find("target")
        nics.append({
            "type": i.get("type"),
            "source": (src.get("network") or src.get("bridge") or src.get("dev")) if src is not None else None,
            "mac": mac.get("address") if mac is not None else None,
            "model": model.get("type") if model is not None else None,
            "ifname": tgt.get("dev") if tgt is not None else None,
        })

    gfx = root.find("devices/graphics")
    graphics = None
    if gfx is not None:
        graphics = {
            "type": gfx.get("type"),
            "port": int(gfx.get("port", "-1") or -1),
            "listen": gfx.get("listen") or "127.0.0.1",
        }

    displays = [{
        "type": g.get("type"),
        "port": int(g.get("port", "-1") or -1),
        "listen": g.get("listen") or "127.0.0.1",
    } for g in root.findall("devices/graphics")]

    tpm_el = root.find("devices/tpm")
    tpm = None
    if tpm_el is not None:
        backend = tpm_el.find("backend")
        tpm = {"model": tpm_el.get("model"), "version": backend.get("version") if backend is not None else None}

    hostdevs = []
    for h in root.findall("devices/hostdev"):
        addr = h.find("source/address")
        if addr is not None and h.get("type") == "pci":
            hostdevs.append("{:04x}:{:02x}:{:02x}.{:x}".format(
                int(addr.get("domain", "0"), 16), int(addr.get("bus", "0"), 16),
                int(addr.get("slot", "0"), 16), int(addr.get("function", "0"), 16)))

    cpu = root.find("cpu")
    video = root.find("devices/video/model")
    pins = {int(p.get("vcpu")): p.get("cpuset") for p in root.findall("cputune/vcpupin")}
    emulator = root.find("cputune/emulatorpin")
    qemu_args = [a.get("value") or "" for a in root.iter(f"{{{QEMU_NS}}}arg")]
    return {
        "name": root.findtext("name"),
        "uuid": root.findtext("uuid"),
        "description": root.findtext("description") or "",
        "os_type": os_type,
        "virt_type": root.get("type"),
        "machine": type_el.get("machine") if type_el is not None else None,
        "firmware": firmware,
        "secure_boot": secure,
        "vcpus": int(root.findtext("vcpu") or 1),
        "memory": to_bytes(root.find("memory")),
        "current_memory": to_bytes(root.find("currentMemory")),
        "cpu_mode": cpu.get("mode") if cpu is not None else None,
        "disks": disks,
        "nics": nics,
        "graphics": graphics,
        "displays": displays,
        "video": video.get("type") if video is not None else None,
        "tpm": tpm,
        "hostdevs": hostdevs,
        "pinning": {"vcpus": pins, "emulator": emulator.get("cpuset") if emulator is not None else None}
        if pins else None,
        # Looking Glass: shared memory from the kvmfr module, passed in with qemu:commandline.
        "looking_glass": any("/dev/kvmfr" in a for a in qemu_args),
    }


def set_resources(xml: str, vcpus: int | None, memory_mib: int | None) -> str:
    """Change vCPU count / memory in a persistent definition."""
    root = ET.fromstring(xml)
    if vcpus is not None:
        v = root.find("vcpu")
        v.text = str(vcpus)
        v.attrib.pop("current", None)
        topo = root.find("cpu/topology")
        if topo is not None:
            topo.set("sockets", "1")
            topo.set("dies", "1")
            topo.set("cores", str(vcpus))
            topo.set("threads", "1")
    if memory_mib is not None:
        for tag in ("memory", "currentMemory"):
            el = root.find(tag)
            if el is None:
                el = ET.SubElement(root, tag)
            el.text = str(memory_mib)
            el.set("unit", "MiB")
    return ET.tostring(root, encoding="unicode")


def pci_hostdev_xml(address: str) -> str:
    dom, bus, rest = address.split(":")
    slot, func = rest.split(".")
    h = ET.Element("hostdev", mode="subsystem", type="pci", managed="yes")
    src = _sub(h, "source")
    _sub(src, "address", domain=f"0x{dom}", bus=f"0x{bus}", slot=f"0x{slot}", function=f"0x{func}")
    return ET.tostring(h, encoding="unicode")
