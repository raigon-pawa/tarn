"""Host facts: hardware, readiness checks, IOMMU groups, laptop power/thermals.

Everything here reads /proc and /sys directly, so it works the same on
Alpine (musl, OpenRC) as on any other distro.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import time

PCI_VENDORS = {
    "10de": "NVIDIA", "1002": "AMD/ATI", "1022": "AMD", "8086": "Intel", "10ec": "Realtek",
    "14c3": "MediaTek", "144d": "Samsung", "15b7": "Sandisk/WD", "1e0f": "KIOXIA", "1987": "Phison",
    "1c5c": "SK hynix", "126f": "Silicon Motion", "17aa": "Lenovo", "1b21": "ASMedia", "1d6b": "Linux Foundation",
}
PCI_CLASSES = {
    "0100": "SCSI storage", "0106": "SATA controller", "0108": "NVMe controller", "0200": "Ethernet",
    "0280": "Wireless network", "0300": "VGA / display", "0302": "3D controller", "0380": "Display controller",
    "0403": "Audio device", "0600": "Host bridge", "0601": "ISA bridge", "0604": "PCI bridge",
    "0c03": "USB controller", "0c05": "SMBus", "1080": "Encryption controller", "0880": "System peripheral",
    "1101": "Signal processing", "1180": "Signal processing", "1200": "Processing accelerator",
    "0c80": "Serial bus", "0c0a": "USB4/Thunderbolt",
}


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def os_release() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def meminfo() -> dict[str, int]:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) * 1024
    return out


def uptime_seconds() -> float:
    try:
        return float(_read("/proc/uptime").split()[0])
    except (IndexError, ValueError):
        return 0.0


def cpu_times() -> tuple[int, int]:
    """(busy, total) jiffies from /proc/stat."""
    fields = _read("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(v) for v in fields]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals[:8])
    return total - idle, total


def _cpu_list(text: str) -> list[int]:
    """Expand a sysfs CPU list like "0-7,16-23" into [0, 1, ..., 23]."""
    out = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out.extend(range(int(lo), int(hi) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def cpu_clusters() -> list[dict]:
    """CPUs grouped by shared L3 cache: one group per CCD on Ryzen, usually one on Intel.

    Each group lists its physical cores, each as the host threads (SMT siblings)
    that make it up, so a VM can be pinned one vCPU per core.
    """
    groups: dict[str, set[int]] = {}
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cache/index3/shared_cpu_list"):
        shared = _read(path)
        if shared:
            groups.setdefault(shared, set()).update(_cpu_list(shared))
    clusters = []
    for cpus in sorted(groups.values(), key=min):
        cores: dict[str, list[int]] = {}
        for cpu in sorted(cpus):
            siblings = _read(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list") or str(cpu)
            cores.setdefault(siblings, []).append(cpu)
        clusters.append({"cpus": sorted(cpus), "cores": sorted(cores.values(), key=min)})
    return clusters


# --------------------------------------------------------------------------
# Laptop bits: battery, AC, temperatures, platform profile
# --------------------------------------------------------------------------

def power_status() -> dict:
    batteries = []
    ac_online = None
    for supply in sorted(glob.glob("/sys/class/power_supply/*")):
        kind = _read(f"{supply}/type")
        if kind == "Battery":
            cap = _read(f"{supply}/capacity")
            batteries.append({
                "name": os.path.basename(supply),
                "capacity": int(cap) if cap.isdigit() else None,
                "status": _read(f"{supply}/status") or None,
            })
        elif kind == "Mains":
            ac_online = _read(f"{supply}/online") == "1"

    temps = []
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = _read(f"{hw}/name")
        if name not in ("k10temp", "coretemp", "amdgpu", "nvme", "acpitz", "zenpower"):
            continue
        for inp in sorted(glob.glob(f"{hw}/temp*_input")):
            label = _read(inp.replace("_input", "_label")) or name
            raw = _read(inp)
            if raw.lstrip("-").isdigit():
                temps.append({"sensor": name, "label": label, "celsius": int(raw) / 1000})
                if name in ("nvme", "acpitz"):
                    break  # first reading is enough for these

    profile = _read("/sys/firmware/acpi/platform_profile") or None
    choices = _read("/sys/firmware/acpi/platform_profile_choices").split() or None

    conservation = None
    for path in glob.glob("/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode"):
        conservation = _read(path) == "1"
        break

    return {
        "batteries": batteries,
        "ac_online": ac_online,
        "temps": temps,
        "platform_profile": profile,
        "platform_profile_choices": choices,
        "battery_conservation": conservation,
    }


# --------------------------------------------------------------------------
# PCI / IOMMU
# --------------------------------------------------------------------------

_lspci_cache: dict[str, str] | None = None


def _lspci_names() -> dict[str, str]:
    global _lspci_cache
    if _lspci_cache is not None:
        return _lspci_cache
    names: dict[str, str] = {}
    if shutil.which("lspci"):
        try:
            out = subprocess.run(["lspci", "-D", "-mm"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                # 0000:01:00.0 "VGA compatible controller" "NVIDIA Corporation" "GB206M ..." ...
                parts = [p for p in line.split('"') if p.strip()]
                if len(parts) >= 4:
                    names[parts[0].strip()] = f"{parts[2]} {parts[3]}"
        except (OSError, subprocess.SubprocessError):
            pass
    _lspci_cache = names
    return names


def pci_device(addr: str) -> dict:
    base = f"/sys/bus/pci/devices/{addr}"
    vendor = _read(f"{base}/vendor").replace("0x", "")
    device = _read(f"{base}/device").replace("0x", "")
    cls = _read(f"{base}/class").replace("0x", "")[:4]
    driver_link = f"{base}/driver"
    driver = os.path.basename(os.readlink(driver_link)) if os.path.islink(driver_link) else None
    name = _lspci_names().get(addr) or f"{PCI_VENDORS.get(vendor, vendor)} {PCI_CLASSES.get(cls, 'device')} [{vendor}:{device}]"
    return {
        "address": addr,
        "vendor_id": vendor,
        "device_id": device,
        "class": PCI_CLASSES.get(cls, cls),
        "class_code": cls,
        "name": name,
        "driver": driver,
        "power": _read(f"{base}/power/runtime_status") or None,
    }


def iommu_groups() -> list[dict]:
    groups = []
    for gdir in glob.glob("/sys/kernel/iommu_groups/*"):
        try:
            gid = int(os.path.basename(gdir))
        except ValueError:
            continue
        devices = [pci_device(os.path.basename(d)) for d in sorted(glob.glob(f"{gdir}/devices/*"))]
        groups.append({"id": gid, "devices": devices})
    groups.sort(key=lambda g: g["id"])
    for g in groups:
        # A device is a clean passthrough candidate if everything else in its
        # group is either a bridge or another function of the same card.
        for dev in g["devices"]:
            slot = dev["address"].rsplit(".", 1)[0]
            others = [d for d in g["devices"] if d["address"] != dev["address"]]
            dev["isolated"] = all(o["address"].startswith(slot) or o["class_code"].startswith("06") for o in others)
    return groups


def gpus() -> list[dict]:
    out = []
    for path in sorted(glob.glob("/sys/bus/pci/devices/*")):
        cls = _read(f"{path}/class").replace("0x", "")[:4]
        if cls.startswith("03"):
            out.append(pci_device(os.path.basename(path)))
    return out


def short_gpu_name(name: str) -> str:
    """'NVIDIA Corporation GB206M [GeForce RTX 5070 Max-Q / Mobile]' -> 'RTX 5070'."""
    m = re.search(r"\b(RTX|GTX|RX|Arc|Quadro)\s?[A-Z]?\d{3,4}\w*(\s(XTX|XT|Ti|SUPER))?", name)
    return m.group(0) if m else "GPU"


def passthrough_gpu_status(address: str, running_user: str | None) -> dict:
    """Health of a GPU parked on vfio-pci, without waking it from sleep.

    Reading config space powers a sleeping card up, so it is only read when
    the card is already awake with no VM using it. A card that stays awake
    and returns 0xffff there has fallen off the bus: the Blackwell reset bug,
    which only a host reboot clears.
    """
    base = f"/sys/bus/pci/devices/{address}"
    if running_user:
        return {"status": "in use", "detail": running_user}
    power = _read(f"{base}/power_state") or "unknown"
    if power.startswith("D3"):
        return {"status": "ready", "detail": "asleep"}
    try:
        with open(f"{base}/config", "rb") as fh:
            vendor = fh.read(2)
    except OSError:
        vendor = b""
    if vendor in (b"", b"\xff\xff"):
        return {"status": "stuck", "detail": "reboot the host"}
    return {"status": "ready", "detail": power}


def kvmfr_device() -> dict | None:
    """The first Looking Glass shared-memory device (/dev/kvmfr0) and its size, or None if it isn't set up.

    The kvmfr module keeps its size parameter to itself, so it is read from
    the modprobe options that set it (static_size_mb=64, or a list for several devices).
    """
    if not os.path.exists("/dev/kvmfr0"):
        return None
    size = None
    for conf in sorted(glob.glob("/etc/modprobe.d/*.conf")):
        for line in _read(conf).splitlines():
            m = re.search(r"^\s*options\s+kvmfr\b.*\bstatic_size_mb=(\d+)", line)
            if m:
                size = int(m.group(1))
    return {"path": "/dev/kvmfr0", "size_mib": size}


# --------------------------------------------------------------------------
# Readiness checks
# --------------------------------------------------------------------------

def readiness(caps, net_active: bool | None, pools_active: bool | None) -> list[dict]:
    """A virt-host-validate style checklist with Alpine-flavoured fixes."""
    checks = []

    def add(key, label, ok, detail, fix=None, level="error"):
        checks.append({"key": key, "label": label, "ok": ok, "detail": detail,
                       "fix": None if ok else fix, "level": "ok" if ok else level})

    vendor = "amd" if "AMD" in cpu_model().upper() else "intel"
    kvm_mod = f"kvm_{vendor}"
    has_kvm = os.path.exists("/dev/kvm")
    add("kvm", "Hardware virtualisation (/dev/kvm)", has_kvm,
        "KVM acceleration available" if has_kvm else "VMs will run in slow software emulation",
        f"Enable {'SVM Mode' if vendor == 'amd' else 'Intel VT-x'} in the BIOS, then: "
        f"modprobe {kvm_mod} && echo {kvm_mod} >> /etc/modules")
    add("kvm_module", f"{kvm_mod} module", os.path.isdir(f"/sys/module/{kvm_mod}"),
        "loaded" if os.path.isdir(f"/sys/module/{kvm_mod}") else "not loaded",
        f"modprobe {kvm_mod} && echo {kvm_mod} >> /etc/modules")
    add("virt_type", "Accelerator in use", caps.virt_type == "kvm",
        "KVM" if caps.virt_type == "kvm" else "TCG (software emulation)",
        "Restart libvirtd after /dev/kvm appears: rc-service libvirtd restart")
    groups = glob.glob("/sys/kernel/iommu_groups/*")
    add("iommu", "IOMMU (needed only for PCI passthrough)", bool(groups),
        f"{len(groups)} IOMMU groups" if groups else "no IOMMU groups",
        "Enable IOMMU (AMD-Vi) in the BIOS, then add 'iommu=pt' to the kernel command line"
        if vendor == "amd" else
        "Enable VT-d in the BIOS, then add 'intel_iommu=on iommu=pt' to the kernel command line",
        level="warn")
    add("vhost_net", "vhost-net (fast VM networking)", os.path.exists("/dev/vhost-net"),
        "available" if os.path.exists("/dev/vhost-net") else "missing",
        "modprobe vhost_net && echo vhost_net >> /etc/modules", level="warn")
    add("tun", "TUN/TAP devices", os.path.exists("/dev/net/tun"),
        "available" if os.path.exists("/dev/net/tun") else "missing",
        "modprobe tun && echo tun >> /etc/modules")
    add("uefi", "UEFI firmware for guests", caps.efi, "available" if caps.efi else "missing",
        "apk add ovmf (QEMU's bundled edk2 also works)")
    add("secureboot", "Secure-boot capable UEFI (Windows 11)", caps.secure_boot,
        "available" if caps.secure_boot else "missing",
        "Needs a secure-boot edk2 build; Windows 11 can still be installed with the LabConfig bypass",
        level="warn")
    add("tpm", "TPM 2.0 emulation (swtpm)", caps.tpm, "available" if caps.tpm else "missing",
        "apk add swtpm, then restart libvirtd", level="warn")
    if net_active is not None:
        add("network", "Default NAT network", net_active, "active" if net_active else "inactive",
            "apk add dnsmasq nftables iptables, then restart Tarn so it can create the network")
    if pools_active is not None:
        add("pools", "Storage pools", pools_active, "active" if pools_active else "not active",
            "Check /var/lib/libvirt permissions and libvirtd logs")
    guests = guest_shutdown()
    if guests is not None:
        add("guests", "VMs shut down cleanly with the host", guests["ok"], guests["detail"], guests["fix"],
            level="warn")
    power = power_status()
    if power["batteries"]:
        on_ac = power["ac_online"]
        add("ac", "Laptop on AC power", bool(on_ac), "charger connected" if on_ac else "running on battery",
            "Plug in the charger; VMs will drain the battery fast", level="warn")
    return checks


def _shell_var(path: str, key: str) -> str | None:
    """KEY's value in a shell-style config file (conf.d, sysconfig), or None when unset."""
    value = None
    for line in _read(path).splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip().strip("\"'")
    return value


def guest_shutdown() -> dict | None:
    """Whether running VMs get a clean shutdown when the host powers off (the libvirt-guests service).

    Without it, power-off kills QEMU: a pulled plug for the guest. Its default,
    managedsave, can't save a VM with a passed-through GPU either.
    """
    if os.path.exists("/etc/init.d/libvirt-guests"):  # OpenRC
        enabled = bool(glob.glob("/etc/runlevels/*/libvirt-guests"))
        mode = _shell_var("/etc/conf.d/libvirt-guests", "LIBVIRT_SHUTDOWN") or "managedsave"
        fix_mode = ("sed -i -E 's/^#?LIBVIRT_SHUTDOWN=.*/LIBVIRT_SHUTDOWN=\"shutdown\"/' "
                    "/etc/conf.d/libvirt-guests")
        fix_enable = "rc-update add libvirt-guests default && rc-service libvirt-guests start"
    elif os.path.exists("/usr/lib/systemd/system/libvirt-guests.service"):
        enabled = os.path.exists("/etc/systemd/system/multi-user.target.wants/libvirt-guests.service")
        on_shutdown = _shell_var("/etc/sysconfig/libvirt-guests", "ON_SHUTDOWN") or "suspend"
        mode = "managedsave" if on_shutdown == "suspend" else on_shutdown
        fix_mode = "sed -i -E 's/^#?ON_SHUTDOWN=.*/ON_SHUTDOWN=shutdown/' /etc/sysconfig/libvirt-guests"
        fix_enable = "systemctl enable --now libvirt-guests"
    else:
        return None
    if mode == "managedsave":
        # Saving fails for a VM with a passed-through GPU, so ask guests to shut down instead.
        fix = fix_mode if enabled else f"{fix_mode} && {fix_enable}"
        detail = ("VMs get saved to disk, which fails for one with a passed-through GPU" if enabled
                  else "no: powering off the host pulls the plug on running VMs")
        return {"ok": False, "detail": detail, "fix": fix}
    if not enabled:
        return {"ok": False, "detail": "no: powering off the host pulls the plug on running VMs", "fix": fix_enable}
    return {"ok": True, "detail": f"yes, guests get {mode}", "fix": None}


def host_info(conn_info: dict) -> dict:
    mem = meminfo()
    return {
        "hostname": platform.node(),
        "os": os_release(),
        "kernel": platform.release(),
        "cpu_model": cpu_model(),
        "threads": os.cpu_count(),
        "memory_total": mem.get("MemTotal", 0),
        "uptime": uptime_seconds(),
        "boot_time": time.time() - uptime_seconds(),
        **conn_info,
    }
