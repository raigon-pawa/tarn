"""The libvirt layer. Everything Tarn does to VMs goes through here.

All methods are blocking (libvirt-python is synchronous); the web layer
calls them via asyncio.to_thread so the event loop never stalls.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import xml.etree.ElementTree as ET

import libvirt

from . import domxml, host
from .domxml import HostCaps, SpecError

log = logging.getLogger("tarn.virt")

STATE_NAMES = {
    libvirt.VIR_DOMAIN_NOSTATE: "unknown",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "running",   # idle vCPUs, effectively running
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "stopping",
    libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
}


class NotFound(Exception):
    pass


# A fake battery for NVIDIA laptop GPUs (built by the GPU prep steps in docs/ALPINE-LEGION.md).
BATTERY_SSDT = "/var/lib/libvirt/acpi/ssdt-battery.aml"


class Conflict(Exception):
    pass


# Silence libvirt's default handler, which prints every error to stderr even
# when we catch and handle it.
libvirt.registerErrorHandler(lambda _ctx, _err: None, None)


class Hypervisor:
    def __init__(self, uri: str, storage_cfg: dict, network_cfg: dict):
        self.uri = uri
        self.storage_cfg = storage_cfg
        self.network_cfg = network_cfg
        self._conn: libvirt.virConnect | None = None
        self._lock = threading.Lock()
        self._caps: HostCaps | None = None
        self._ip_cache: dict[str, tuple[float, list]] = {}

    # -- connection ---------------------------------------------------------

    @property
    def conn(self) -> libvirt.virConnect:
        with self._lock:
            if self._conn is not None:
                try:
                    if self._conn.isAlive():
                        return self._conn
                except libvirt.libvirtError:
                    pass
                log.warning("libvirt connection lost, reconnecting")
            self._conn = libvirt.open(self.uri)
            self._caps = None
            return self._conn

    def dom(self, uuid: str) -> libvirt.virDomain:
        try:
            return self.conn.lookupByUUIDString(uuid)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                raise NotFound(f"No VM with UUID {uuid}") from exc
            raise

    # -- capabilities -------------------------------------------------------

    def caps(self) -> HostCaps:
        if self._caps is not None:
            return self._caps
        conn = self.conn
        caps = HostCaps()
        root = ET.fromstring(conn.getCapabilities())
        virt_types: list[str] = []
        for guest in root.findall("guest"):
            arch = guest.find("arch")
            if arch is None or arch.get("name") != "x86_64":
                continue
            caps.emulator = arch.findtext("emulator")
            virt_types = [d.get("type") for d in arch.findall("domain")]
        caps.virt_type = "kvm" if "kvm" in virt_types else "qemu"

        try:
            dc = ET.fromstring(conn.getDomainCapabilities(None, "x86_64", "q35", caps.virt_type, 0))
            fw_values = [v.text for v in dc.findall("os/enum[@name='firmware']/value")]
            loaders = [v.text for v in dc.findall("os/loader/value")]
            secure = [v.text for v in dc.findall("os/loader/enum[@name='secure']/value")]
            caps.efi = "efi" in fw_values or bool(loaders)
            caps.secure_boot = caps.efi and "yes" in secure
            tpm = dc.find("devices/tpm")
            backends = [v.text for v in dc.findall("devices/tpm/enum[@name='backendModel']/value")]
            caps.tpm = (
                tpm is not None and tpm.get("supported") == "yes"
                and ("emulator" in backends or not backends)
                and shutil.which("swtpm") is not None
            )
            gfx_types = [v.text for v in dc.findall("devices/graphics/enum[@name='type']/value")]
            caps.spice = "spice" in gfx_types
            machines = [v.text for v in dc.findall("machine")]
            caps.q35 = True if not machines else any("q35" in (m or "") for m in machines)
        except libvirt.libvirtError as exc:
            log.warning("domain capabilities unavailable: %s", exc)
        self._caps = caps
        return caps

    # -- bootstrapping pools and networks ------------------------------------

    def ensure_infrastructure(self) -> list[str]:
        """Create the image/ISO pools and the NAT network if missing."""
        notes = []
        for name, path in (
            (self.storage_cfg["images_pool"], self.storage_cfg["images_path"]),
            (self.storage_cfg["iso_pool"], self.storage_cfg["iso_path"]),
        ):
            try:
                notes += self._ensure_pool(name, path)
            except libvirt.libvirtError as exc:
                notes.append(f"pool {name}: {exc}")
        if self.network_cfg.get("create_default"):
            try:
                notes += self._ensure_network(self.network_cfg["default"])
            except libvirt.libvirtError as exc:
                notes.append(f"network {self.network_cfg['default']}: {exc}")
        return notes

    def _ensure_pool(self, name: str, path: str) -> list[str]:
        conn = self.conn
        notes = []
        try:
            pool = conn.storagePoolLookupByName(name)
        except libvirt.libvirtError:
            xml = f"<pool type='dir'><name>{name}</name><target><path>{path}</path></target></pool>"
            pool = conn.storagePoolDefineXML(xml, 0)
            pool.build(0)
            pool.setAutostart(1)
            notes.append(f"created storage pool '{name}' at {path}")
        if not pool.isActive():
            pool.create(0)
            notes.append(f"started storage pool '{name}'")
        return notes

    def _ensure_network(self, name: str) -> list[str]:
        conn = self.conn
        notes = []
        try:
            net = conn.networkLookupByName(name)
        except libvirt.libvirtError:
            xml = f"""<network>
  <name>{name}</name>
  <forward mode='nat'/>
  <bridge name='virbr0' stp='on' delay='0'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp><range start='192.168.122.2' end='192.168.122.254'/></dhcp>
  </ip>
</network>"""
            net = conn.networkDefineXML(xml)
            net.setAutostart(1)
            notes.append(f"created NAT network '{name}' (virbr0, 192.168.122.0/24)")
        if not net.isActive():
            net.create()
            notes.append(f"started network '{name}'")
        return notes

    # -- listing --------------------------------------------------------------

    def list_vms(self) -> list[dict]:
        out = []
        for dom in self.conn.listAllDomains(0):
            try:
                out.append(self._summary(dom))
            except libvirt.libvirtError as exc:  # domain vanished mid-listing
                log.debug("skipping domain: %s", exc)
        out.sort(key=lambda v: v["name"].lower())
        return out

    def _summary(self, dom: libvirt.virDomain) -> dict:
        state, _reason = dom.state()
        info = domxml.parse_domain(dom.XMLDesc(0))
        return {
            "uuid": dom.UUIDString(),
            "name": dom.name(),
            "state": STATE_NAMES.get(state, "unknown"),
            "vcpus": info["vcpus"],
            "memory": info["memory"],
            "os_type": info["os_type"],
            "autostart": bool(dom.autostart()) if dom.isPersistent() else False,
            "persistent": bool(dom.isPersistent()),
            "has_console": bool(info["graphics"] and info["graphics"]["type"] in ("vnc",)),
            "looking_glass": info["looking_glass"],
        }

    def vm_detail(self, uuid: str) -> dict:
        dom = self.dom(uuid)
        summary = self._summary(dom)
        info = domxml.parse_domain(dom.XMLDesc(0))
        running = summary["state"] in ("running", "paused")
        for disk in info["disks"]:
            disk["capacity"] = disk["allocation"] = None
            if disk["device"] == "disk" and disk["target"]:
                try:
                    cap, alloc, _phys = dom.blockInfo(disk["target"], 0)
                    disk["capacity"], disk["allocation"] = cap, alloc
                except libvirt.libvirtError:
                    pass
        summary.update({
            "description": info["description"],
            "firmware": info["firmware"],
            "secure_boot": info["secure_boot"],
            "machine": info["machine"],
            "virt_type": info["virt_type"],
            "cpu_mode": info["cpu_mode"],
            "disks": info["disks"],
            "nics": info["nics"],
            "video": info["video"],
            "tpm": info["tpm"],
            "hostdevs": info["hostdevs"],
            "pinning": info["pinning"],
            "looking_glass": info["looking_glass"],
            "ips": self._addresses(dom) if running else [],
            "guest": self._guest_info(dom) if running and "org.qemu.guest_agent.0" in dom.XMLDesc(0) else None,
            "snapshot_count": self._snapshot_count(dom),
        })
        return summary

    def _snapshot_count(self, dom) -> int:
        try:
            return dom.snapshotNum(0)
        except libvirt.libvirtError:
            return 0

    def _addresses(self, dom) -> list[dict]:
        """IPs from the DHCP lease table first, then the guest agent.

        Cached for 15 s: the UI polls every few seconds, and asking a guest
        with no agent each time fills libvirtd's log with errors.
        """
        uuid = dom.UUIDString()
        cached = self._ip_cache.get(uuid)
        if cached and time.monotonic() - cached[0] < 15:
            return cached[1]
        ips = self._lookup_addresses(dom)
        self._ip_cache[uuid] = (time.monotonic(), ips)
        return ips

    def _guest_info(self, dom) -> dict | None:
        """What the guest agent reports: OS, signed-in users, filesystems. None if it isn't answering.

        Cached for 30 s like the addresses: a guest that is still booting
        makes each call wait for the agent's timeout.
        """
        key = "guest:" + dom.UUIDString()
        cached = self._ip_cache.get(key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        types = (libvirt.VIR_DOMAIN_GUEST_INFO_OS | libvirt.VIR_DOMAIN_GUEST_INFO_USERS
                 | libvirt.VIR_DOMAIN_GUEST_INFO_FILESYSTEM | libvirt.VIR_DOMAIN_GUEST_INFO_HOSTNAME)
        try:
            info = parse_guest_info(dom.guestInfo(types, 0))
        except libvirt.libvirtError:
            info = None
        self._ip_cache[key] = (time.monotonic(), info)
        return info

    def _lookup_addresses(self, dom) -> list[dict]:
        sources = [libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
                   libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT]
        for src in sources:
            try:
                ifaces = dom.interfaceAddresses(src, 0)
            except libvirt.libvirtError:
                continue
            ips = []
            for name, data in (ifaces or {}).items():
                if name == "lo":
                    continue
                for addr in data.get("addrs") or []:
                    if addr.get("addr", "").startswith(("127.", "::1", "fe80")):
                        continue
                    ips.append({"iface": name, "addr": addr["addr"], "prefix": addr.get("prefix")})
            if ips:
                return ips
        return []

    def vm_xml(self, uuid: str, inactive: bool = True) -> str:
        flags = libvirt.VIR_DOMAIN_XML_INACTIVE if inactive else 0
        return self.dom(uuid).XMLDesc(flags | libvirt.VIR_DOMAIN_XML_SECURE)

    def define_xml(self, uuid: str, xml: str) -> None:
        root = ET.fromstring(xml)  # raises on malformed XML before libvirt sees it
        if root.findtext("uuid") not in (None, uuid):
            raise SpecError("The XML's <uuid> doesn't match this VM.")
        self.conn.defineXML(xml)

    # -- lifecycle ------------------------------------------------------------

    def action(self, uuid: str, action: str) -> None:
        dom = self.dom(uuid)
        state = STATE_NAMES.get(dom.state()[0])
        if action == "start":
            if state == "paused":
                dom.resume()
            elif state == "suspended":
                dom.pMWakeup(0)
            else:
                self._check_exclusive(dom)
                dom.create()
        elif action == "shutdown":
            dom.shutdown()  # ACPI power button; the guest decides
        elif action == "reboot":
            dom.reboot(0)
        elif action == "reset":
            dom.reset(0)
        elif action == "poweroff":
            dom.destroy()
        elif action == "pause":
            dom.suspend()
        elif action == "resume":
            dom.resume()
        else:
            raise SpecError(f"Unknown action '{action}'")

    def _check_exclusive(self, dom) -> None:
        """Refuse to start a VM whose GPU or Looking Glass memory another running VM holds.

        libvirt would refuse the GPU too, but with a less friendly message; two
        VMs on /dev/kvmfr0 would both start and draw into one frame buffer.
        """
        mine = domxml.parse_domain(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        if not mine["hostdevs"] and not mine["looking_glass"]:
            return
        for other in self.conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE):
            if other.UUIDString() == dom.UUIDString():
                continue
            theirs = domxml.parse_domain(other.XMLDesc(0))
            if set(mine["hostdevs"]) & set(theirs["hostdevs"]):
                raise Conflict(f"{other.name()} is running with the same graphics card. Shut it down first.")
            if mine["looking_glass"] and theirs["looking_glass"]:
                raise Conflict(f"{other.name()} is running with Looking Glass's shared memory. "
                               "Shut it down first: two VMs can't share it.")

    def set_autostart(self, uuid: str, on: bool) -> None:
        self.dom(uuid).setAutostart(1 if on else 0)

    def set_resources(self, uuid: str, vcpus: int | None, memory_mib: int | None) -> dict:
        dom = self.dom(uuid)
        if vcpus is not None and not 1 <= vcpus <= 256:
            raise SpecError("vCPUs must be between 1 and 256")
        if memory_mib is not None and not 128 <= memory_mib <= 1024 * 1024:
            raise SpecError("Memory must be between 128 MiB and 1 TiB")
        xml = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE)
        self.conn.defineXML(domxml.set_resources(xml, vcpus, memory_mib))
        pending = STATE_NAMES.get(dom.state()[0]) != "stopped"
        return {"applies_after_restart": pending}

    def rename(self, uuid: str, new_name: str) -> None:
        if not domxml.NAME_RE.match(new_name):
            raise SpecError("Name must be 1-63 characters: letters, digits, '.', '_' or '-'.")
        dom = self.dom(uuid)
        if STATE_NAMES.get(dom.state()[0]) != "stopped":
            raise Conflict("Shut the VM down before renaming it.")
        dom.rename(new_name, 0)

    def change_media(self, uuid: str, target: str, path: str | None) -> None:
        dom = self.dom(uuid)
        info = domxml.parse_domain(dom.XMLDesc(0))
        drive = next((d for d in info["disks"] if d["target"] == target and d["device"] == "cdrom"), None)
        if drive is None:
            raise NotFound(f"No CD/DVD drive '{target}'")
        flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
        if dom.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
        dom.updateDeviceFlags(domxml.cdrom_xml(target, drive["bus"] or "sata", path), flags)

    def set_boot_disk_first(self, uuid: str) -> None:
        """After installing: boot from the hard disk and eject install media."""
        dom = self.dom(uuid)
        root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE))
        order = 1
        for disk in root.findall("devices/disk"):
            boot = disk.find("boot")
            if boot is not None:
                disk.remove(boot)
        for disk in root.findall("devices/disk"):
            if disk.get("device") == "disk":
                ET.SubElement(disk, "boot", order=str(order))
                order += 1
        self.conn.defineXML(ET.tostring(root, encoding="unicode"))

    def pci_candidates(self) -> list[dict]:
        """Host PCI cards worth passing through (GPUs for now), one entry per card, all functions together."""
        groups = {d["address"]: g for g in host.iommu_groups() for d in g["devices"]}
        used: dict[str, list[str]] = {}  # host PCI address -> VMs it is assigned to
        running = set()
        for dom in self.conn.listAllDomains(0):
            try:
                for addr in domxml.parse_domain(dom.XMLDesc(0))["hostdevs"]:
                    used.setdefault(addr, []).append(dom.name())
                if dom.isActive():
                    running.add(dom.name())
            except libvirt.libvirtError:
                continue
        cards = []
        for gpu in host.gpus():
            slot = gpu["address"].rsplit(".", 1)[0]
            group = groups.get(gpu["address"], {"id": None, "devices": []})
            functions = [d for d in group["devices"] if d["address"].startswith(slot + ".")] or [gpu]
            cards.append({
                "slot": slot,
                "name": gpu["name"],
                "vendor_id": gpu["vendor_id"],
                "functions": [{"address": d["address"], "name": d["name"], "driver": d["driver"]} for d in functions],
                "iommu_group": group["id"],
                "isolated": all(d.get("isolated", False) for d in functions),
                # Only a card parked on vfio-pci can be handed to a VM without unbinding the host's display.
                "ready": all(d["driver"] == "vfio-pci" for d in functions),
                # A card can be assigned to several VMs; only one of them can run at a time.
                "assigned_to": sorted({n for d in functions for n in used.get(d["address"], [])}),
            })
            card = cards[-1]
            card["used_by"] = card["assigned_to"][0] if card["assigned_to"] else None
            card["running_on"] = next((n for n in card["assigned_to"] if n in running), None)
        return cards

    def attach_card(self, uuid: str, slot: str) -> dict:
        """Pass every function of a card (e.g. GPU + its HDMI audio) to the VM, in one guest slot.

        GPU drivers expect the functions together, the way they sit on real
        hardware, so they go on a dedicated PCIe root port as functions 0, 1, ...
        """
        dom = self.dom(uuid)
        if dom.isActive():
            raise Conflict("Shut the VM down before adding a PCI device.")
        card = next((c for c in self.pci_candidates() if c["slot"] == slot), None)
        if card is None:
            raise NotFound(f"No card at {slot}")
        if dom.name() in card["assigned_to"]:
            raise Conflict("That card is already attached to this VM.")
        root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE))
        devs = root.find("devices")
        index = max([int(c.get("index")) for c in devs.findall("controller[@type='pci']")] or [0]) + 1
        ET.SubElement(devs, "controller", type="pci", index=str(index), model="pcie-root-port")
        for n, fn in enumerate(card["functions"]):
            h = ET.fromstring(domxml.pci_hostdev_xml(fn["address"]))
            ET.SubElement(h, "rom", bar="off")  # laptop GPUs often can't serve their ROM through the BAR
            addr = {"type": "pci", "domain": "0x0000", "bus": f"0x{index:02x}", "slot": "0x00", "function": f"0x{n}"}
            if n == 0 and len(card["functions"]) > 1:
                addr["multifunction"] = "on"
            ET.SubElement(h, "address", **addr)
            devs.append(h)
        notes = []
        if card["vendor_id"] == "10de" and host.power_status()["batteries"]:
            # NVIDIA's laptop driver stops with Code 43 when the VM has no battery; an SSDT fakes one.
            if os.path.exists(BATTERY_SSDT):
                domxml.set_acpi_table(root, BATTERY_SSDT, True)
            else:
                notes.append(f"No fake battery table at {BATTERY_SSDT}: NVIDIA's laptop driver may stop "
                             "with Code 43 (see the GPU section of docs/ALPINE-LEGION.md).")
        self.conn.defineXML(ET.tostring(root, encoding="unicode"))
        others = card["assigned_to"]
        if others:
            notes.insert(0, f"The card is also assigned to {', '.join(others)}; only one of them can run at a time.")
        return {"note": " ".join(notes) or None}

    def detach_card(self, uuid: str, slot: str) -> None:
        """Remove every function of a card from the VM, and the root port Tarn added for it."""
        dom = self.dom(uuid)
        if dom.isActive():
            raise Conflict("Shut the VM down before removing a PCI device.")
        root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE))
        devs = root.find("devices")
        buses = set()
        for h in devs.findall("hostdev[@type='pci']"):
            a = h.find("source/address")
            if a is not None and f"{int(a.get('domain', '0'), 16):04x}:{int(a.get('bus'), 16):02x}:{int(a.get('slot'), 16):02x}" == slot:
                guest = h.find("address")
                if guest is not None and guest.get("bus"):
                    buses.add(int(guest.get("bus"), 16))
                devs.remove(h)
        for c in devs.findall("controller[@type='pci'][@model='pcie-root-port']"):
            if int(c.get("index")) in buses and not any(
                    (d.find("address") is not None and d.find("address").get("bus")
                     and int(d.find("address").get("bus"), 16) == int(c.get("index"))) for d in devs):
                devs.remove(c)
        left = domxml.parse_domain(ET.tostring(root, encoding="unicode"))["hostdevs"]
        if not any(host.pci_device(a)["vendor_id"] == "10de" for a in left):
            domxml.set_acpi_table(root, BATTERY_SSDT, False)  # the fake battery was only for NVIDIA's driver
        self.conn.defineXML(ET.tostring(root, encoding="unicode"))

    def set_looking_glass(self, uuid: str, enabled: bool) -> dict:
        """Map /dev/kvmfr0 into the VM for Looking Glass, plus a SPICE display for its input and sound.

        Turning it off removes only the shared memory; SPICE stays, since the
        desktop console uses it too.
        """
        dom = self.dom(uuid)
        if dom.isActive():
            raise Conflict("Shut the VM down first: the shared memory is added when it starts.")
        root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE))
        q = f"{{{domxml.QEMU_NS}}}"
        for cmd in root.findall(f"{q}commandline"):
            args = cmd.findall(f"{q}arg")
            values = [a.get("value") or "" for a in args]
            for i, v in enumerate(values):  # drop each "-device/-object" pair that belongs to Looking Glass
                if "looking-glass" in v or "/dev/kvmfr" in v:
                    cmd.remove(args[i])
                    if i and values[i - 1] in ("-device", "-object"):
                        cmd.remove(args[i - 1])
            if not len(cmd):
                root.remove(cmd)
        note = None
        if enabled:
            shm = host.kvmfr_device()
            if shm is None:
                raise SpecError("/dev/kvmfr0 is missing. Install the kvmfr module first "
                                "(see the Looking Glass section of docs/ALPINE-LEGION.md).")
            if not shm["size_mib"]:
                raise SpecError("Couldn't find kvmfr's size: add 'options kvmfr static_size_mb=64' "
                                "to /etc/modprobe.d/kvmfr.conf.")
            cmd = root.find(f"{q}commandline")
            if cmd is None:
                cmd = ET.SubElement(root, f"{q}commandline")
            for v in domxml.looking_glass_args(shm["path"], shm["size_mib"]):
                ET.SubElement(cmd, f"{q}arg", value=v)
            devs = root.find("devices")
            if devs.find("graphics[@type='spice']") is None:
                domxml.add_spice(devs)
            others = [d.name() for d in self.conn.listAllDomains(0) if d.UUIDString() != dom.UUIDString()
                      and "/dev/kvmfr" in d.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)]
            if others:
                note = f"{', '.join(others)} uses the same Looking Glass memory; only one of them can run at a time."
        self.conn.defineXML(ET.tostring(root, encoding="unicode"))
        return {"note": note}

    def set_cpu_pinning(self, uuid: str, enabled: bool) -> dict:
        """Pin the VM's vCPUs one per physical core on the last CPU cluster; QEMU's own threads on the rest.

        Keeping a VM inside one cluster (one CCD on Ryzen) keeps its threads on
        one L3 cache, which is what makes games and audio smooth. The vCPU count
        is capped at that cluster's core count.
        """
        dom = self.dom(uuid)
        root = ET.fromstring(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE))
        for old in root.findall("cputune"):
            root.remove(old)
        cpu = root.find("cpu")
        if cpu is not None:
            for old in cpu.findall("topology"):
                cpu.remove(old)
        note = None
        if enabled:
            clusters = host.cpu_clusters()
            if not clusters:
                raise SpecError("Couldn't read this host's CPU topology.")
            target = clusters[-1]
            vcpus = int(root.findtext("vcpu") or 1)
            cores = target["cores"]
            if vcpus > len(cores):
                vcpus = len(cores)
                root.find("vcpu").text = str(vcpus)
                note = f"vCPUs reduced to {vcpus}, the number of cores in one cluster."
            tune = ET.Element("cputune")
            for i in range(vcpus):
                ET.SubElement(tune, "vcpupin", vcpu=str(i), cpuset=str(cores[i][0]))
            others = sorted(c for cl in clusters[:-1] for c in cl["cpus"])
            if others:
                ET.SubElement(tune, "emulatorpin", cpuset=",".join(map(str, others)))
            root.insert(list(root).index(root.find("vcpu")) + 1, tune)
            if cpu is None:
                cpu = ET.SubElement(root, "cpu", mode="host-passthrough")
            ET.SubElement(cpu, "topology", sockets="1", dies="1", cores=str(vcpus), threads="1")
        self.conn.defineXML(ET.tostring(root, encoding="unicode"))
        return {"applies_after_restart": bool(dom.isActive()), "note": note}

    def attach_pci(self, uuid: str, address: str) -> None:
        dom = self.dom(uuid)
        if dom.isActive():
            raise Conflict("Shut the VM down before adding a PCI device.")
        dom.attachDeviceFlags(domxml.pci_hostdev_xml(address), libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    def detach_pci(self, uuid: str, address: str) -> None:
        dom = self.dom(uuid)
        if dom.isActive():
            raise Conflict("Shut the VM down before removing a PCI device.")
        dom.detachDeviceFlags(domxml.pci_hostdev_xml(address), libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    # -- create / delete ------------------------------------------------------

    def host_report(self) -> dict:
        """The Host page: versions, readiness checklist, power, GPUs (shared by the web and desktop UIs)."""
        conn = self.conn
        caps = self.caps()
        nets = self.networks()
        default_net = self.network_cfg["default"]
        net_active = next((n["active"] for n in nets["networks"] if n["name"] == default_net), False)
        pool_names = {self.storage_cfg["images_pool"], self.storage_cfg["iso_pool"]}
        pools_active = True
        for pool in conn.listAllStoragePools(0):
            if pool.name() in pool_names and not pool.isActive():
                pools_active = False
        lib = conn.getLibVersion()
        hyp = conn.getVersion()
        fmt = lambda v: f"{v // 1000000}.{(v // 1000) % 1000}.{v % 1000}"  # noqa: E731
        info = host.host_info({
            "libvirt_version": fmt(lib),
            "qemu_version": fmt(hyp) if hyp else None,
            "virt_type": caps.virt_type,
            "uri": self.uri,
        })
        info["readiness"] = host.readiness(caps, net_active, pools_active)
        info["power"] = host.power_status()
        info["gpus"] = host.gpus()
        info["caps"] = {"efi": caps.efi, "secure_boot": caps.secure_boot, "tpm": caps.tpm,
                        "virt_type": caps.virt_type}
        return info

    def create_meta(self) -> dict:
        """Everything a create form needs: host capabilities, ISOs, free images, pools, networks."""
        caps = self.caps()
        storage = self.storage()
        isos, images = [], []
        for pool in storage:
            for vol in pool["volumes"]:
                item = {"pool": pool["name"], **vol}
                # Anything in the ISO pool counts, even if a long upload name lost its ".iso".
                if vol["name"].lower().endswith(".iso") or pool["name"] == self.storage_cfg["iso_pool"]:
                    isos.append(item)
                elif not vol["used_by"] and vol["format"] not in ("iso", "dir"):
                    images.append(item)
        return {
            "caps": {"efi": caps.efi, "secure_boot": caps.secure_boot, "tpm": caps.tpm,
                     "spice": caps.spice, "virt_type": caps.virt_type},
            "isos": isos,
            "images": images,
            "pools": [{"name": p["name"], "available": p.get("available"), "active": p["active"]}
                      for p in storage],
            "default_pool": self.storage_cfg["images_pool"],
            "iso_pool": self.storage_cfg["iso_pool"],
            **self.networks(),
            "default_network": self.network_cfg["default"],
            "cards": [{"slot": c["slot"], "name": c["name"], "assigned_to": c["assigned_to"]}
                      for c in self.pci_candidates() if c["ready"]],
            "looking_glass": host.kvmfr_device() is not None,
            "host_threads": os.cpu_count(),
            "host_memory": host.meminfo().get("MemTotal", 0),
        }

    def create_vm(self, raw_spec: dict) -> dict:
        caps = self.caps()
        spec = domxml.normalize_spec(raw_spec, caps)
        conn = self.conn
        try:
            conn.lookupByName(spec["name"])
            raise Conflict(f"A VM named '{spec['name']}' already exists.")
        except libvirt.libvirtError:
            pass

        for iso in (spec["iso"], spec["driver_iso"]):
            if iso and not self._path_known(iso):
                raise SpecError(f"ISO not found in any storage pool: {iso}")

        created_vol = None
        disk_path, disk_format = None, "qcow2"
        disk = spec["disk"]
        if disk["mode"] == "new":
            pool = conn.storagePoolLookupByName(disk["pool"] or self.storage_cfg["images_pool"])
            vol_name = f"{spec['name']}-disk0.qcow2"
            vol_xml = (
                f"<volume><name>{vol_name}</name>"
                f"<capacity unit='bytes'>{int(disk['size_gib'] * 1024**3)}</capacity>"
                "<target><format type='qcow2'/></target></volume>"
            )
            try:
                created_vol = pool.createXML(vol_xml, 0)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_STORAGE_VOL_EXIST:
                    raise Conflict(f"A disk named {vol_name} already exists in pool {pool.name()}.") from exc
                raise
            disk_path = created_vol.path()
        elif disk["mode"] == "existing":
            disk_path = disk["path"]
            disk_format = disk.get("format") or self._volume_format(disk_path) or "qcow2"
            in_use = self._paths_in_use().get(disk_path)
            if in_use:
                raise Conflict(f"That disk is already attached to VM '{in_use}'.")

        xml = domxml.build_domain(spec, caps, disk_path, disk_format)
        try:
            dom = conn.defineXML(xml)
        except libvirt.libvirtError:
            if created_vol is not None:
                created_vol.delete(0)
            raise
        uuid = dom.UUIDString()
        notes = []
        try:  # gaming extras, applied the same way as from the VM's page
            if raw_spec.get("cpu_pinning"):
                notes.append(self.set_cpu_pinning(uuid, True)["note"])
            if raw_spec.get("card"):
                notes.append(self.attach_card(uuid, str(raw_spec["card"]))["note"])
            if raw_spec.get("looking_glass"):
                lg_note = self.set_looking_glass(uuid, True)["note"]
                if not raw_spec.get("card"):  # with a card, its note already names the VMs it is shared with
                    notes.append(lg_note)
        except Exception:
            self.delete_vm(uuid, delete_disks=created_vol is not None)
            raise
        result = {"uuid": uuid, "started": spec["start"], "note": " ".join(filter(None, notes)) or None}
        if spec["start"]:
            try:
                self.action(uuid, "start")
            except (libvirt.libvirtError, Conflict) as exc:
                result.update(started=False, warning=str(exc))
        return result

    def delete_vm(self, uuid: str, delete_disks: bool) -> list[str]:
        dom = self.dom(uuid)
        info = domxml.parse_domain(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        if dom.isActive():
            dom.destroy()
        # Snapshot metadata must go first or undefine refuses.
        try:
            for snap in dom.listAllSnapshots(0):
                try:
                    snap.delete(libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY)
                except libvirt.libvirtError:
                    pass
        except libvirt.libvirtError:
            pass
        flags = (libvirt.VIR_DOMAIN_UNDEFINE_NVRAM | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
                 | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
        flags |= getattr(libvirt, "VIR_DOMAIN_UNDEFINE_TPM", 0)
        flags |= getattr(libvirt, "VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA", 0)
        dom.undefineFlags(flags)

        removed = []
        if delete_disks:
            still_used = self._paths_in_use()
            for disk in info["disks"]:
                if disk["device"] != "disk" or not disk["path"] or disk["path"] in still_used:
                    continue
                try:
                    vol = self.conn.storageVolLookupByPath(disk["path"])
                    vol.delete(0)
                    removed.append(disk["path"])
                except libvirt.libvirtError as exc:
                    log.warning("could not delete %s: %s", disk["path"], exc)
        return removed

    # -- snapshots ------------------------------------------------------------

    def list_snapshots(self, uuid: str) -> list[dict]:
        dom = self.dom(uuid)
        out = []
        try:
            current = dom.snapshotCurrent(0).getName() if dom.hasCurrentSnapshot(0) else None
        except libvirt.libvirtError:
            current = None
        for snap in dom.listAllSnapshots(0):
            root = ET.fromstring(snap.getXMLDesc(0))
            parent = root.findtext("parent/name")
            out.append({
                "name": snap.getName(),
                "description": root.findtext("description") or "",
                "created": int(root.findtext("creationTime") or 0),
                "state": root.findtext("state"),
                "parent": parent,
                "current": snap.getName() == current,
            })
        out.sort(key=lambda s: s["created"])
        return out

    def create_snapshot(self, uuid: str, name: str, description: str) -> None:
        if not domxml.NAME_RE.match(name):
            raise SpecError("Snapshot name must be letters, digits, '.', '_' or '-'.")
        root = ET.Element("domainsnapshot")
        ET.SubElement(root, "name").text = name
        if description:
            ET.SubElement(root, "description").text = description[:500]
        self.dom(uuid).snapshotCreateXML(ET.tostring(root, encoding="unicode"), 0)

    def revert_snapshot(self, uuid: str, name: str) -> None:
        dom = self.dom(uuid)
        dom.revertToSnapshot(dom.snapshotLookupByName(name, 0), 0)

    def delete_snapshot(self, uuid: str, name: str) -> None:
        self.dom(uuid).snapshotLookupByName(name, 0).delete(0)

    # -- console ----------------------------------------------------------------

    def open_graphics_fd(self, uuid: str) -> int:
        """Ask libvirt to hand us a socket straight into QEMU's VNC server."""
        dom = self.dom(uuid)
        if not dom.isActive():
            raise Conflict("The VM is not running.")
        return dom.openGraphicsFD(0, libvirt.VIR_DOMAIN_OPEN_GRAPHICS_SKIPAUTH)

    def console_target(self, uuid: str) -> dict:
        """Where a native client should connect: SPICE when the VM has it, else VNC."""
        dom = self.dom(uuid)
        if not dom.isActive():
            raise Conflict("The VM is not running.")
        displays = domxml.parse_domain(dom.XMLDesc(0))["displays"]
        for kind in ("spice", "vnc"):
            d = next((d for d in displays if d["type"] == kind and d["port"] > 0), None)
            if d:
                host = d["listen"] if d["listen"] not in ("0.0.0.0", "::", "") else "127.0.0.1"
                return {"type": kind, "host": host, "port": d["port"]}
        raise Conflict("This VM has no SPICE or VNC display.")

    def vnc_address(self, uuid: str) -> tuple[str, int]:
        dom = self.dom(uuid)
        if not dom.isActive():
            raise Conflict("The VM is not running.")
        g = domxml.parse_domain(dom.XMLDesc(0))["graphics"]
        if not g or g["type"] != "vnc" or g["port"] <= 0:
            raise Conflict("This VM has no VNC display.")
        listen = g["listen"]
        if listen in ("0.0.0.0", "::", ""):
            listen = "127.0.0.1"
        return listen, g["port"]

    # -- storage ------------------------------------------------------------------

    def _paths_in_use(self) -> dict[str, str]:
        used = {}
        for dom in self.conn.listAllDomains(0):
            try:
                for disk in domxml.parse_domain(dom.XMLDesc(0))["disks"]:
                    if disk["path"]:
                        used[disk["path"]] = dom.name()
            except libvirt.libvirtError:
                continue
        return used

    def _path_known(self, path: str) -> bool:
        try:
            self.conn.storageVolLookupByPath(path)
            return True
        except libvirt.libvirtError:
            return os.path.exists(path)

    def _volume_format(self, path: str) -> str | None:
        try:
            vol = self.conn.storageVolLookupByPath(path)
            fmt = ET.fromstring(vol.XMLDesc(0)).find("target/format")
            value = fmt.get("type") if fmt is not None else None
            return value if value in ("qcow2", "raw", "vmdk", "vdi", "vpc", "qed") else None
        except libvirt.libvirtError:
            return None

    def storage(self) -> list[dict]:
        used = self._paths_in_use()
        pools = []
        for pool in self.conn.listAllStoragePools(0):
            entry = {
                "name": pool.name(),
                "active": bool(pool.isActive()),
                "autostart": bool(pool.autostart()),
                "path": ET.fromstring(pool.XMLDesc(0)).findtext("target/path"),
                "volumes": [],
            }
            if entry["active"]:
                try:
                    pool.refresh(0)  # picks up files copied in by hand
                except libvirt.libvirtError:
                    pass
                _state, cap, alloc, avail = pool.info()
                entry.update(capacity=cap, allocation=alloc, available=avail)
                for vol in pool.listAllVolumes(0):
                    try:
                        vroot = ET.fromstring(vol.XMLDesc(0))
                        _t, vcap, valloc = vol.info()
                    except libvirt.libvirtError:
                        continue
                    fmt = vroot.find("target/format")
                    path = vol.path()
                    entry["volumes"].append({
                        "name": vol.name(),
                        "path": path,
                        "capacity": vcap,
                        "allocation": valloc,
                        "format": fmt.get("type") if fmt is not None else None,
                        "used_by": used.get(path),
                    })
                entry["volumes"].sort(key=lambda v: v["name"].lower())
            pools.append(entry)
        pools.sort(key=lambda p: p["name"])
        return pools

    def create_volume(self, pool_name: str, name: str, size_gib: float, fmt: str) -> str:
        if not domxml.NAME_RE.match(name):
            raise SpecError("Volume name must be letters, digits, '.', '_' or '-'.")
        if fmt not in ("qcow2", "raw"):
            raise SpecError("Format must be qcow2 or raw")
        pool = self.conn.storagePoolLookupByName(pool_name)
        xml = (f"<volume><name>{name}</name><capacity unit='bytes'>{int(size_gib * 1024**3)}</capacity>"
               f"<target><format type='{fmt}'/></target></volume>")
        return pool.createXML(xml, 0).path()

    def delete_volume(self, pool_name: str, name: str) -> None:
        vol = self.conn.storagePoolLookupByName(pool_name).storageVolLookupByName(name)
        user = self._paths_in_use().get(vol.path())
        if user:
            raise Conflict(f"In use by VM '{user}'. Detach or delete the VM first.")
        vol.delete(0)

    def begin_upload(self, pool_name: str, name: str, size: int):
        """Create a raw volume and return (stream, volume) ready for upload."""
        if not domxml.NAME_RE.match(name):
            raise SpecError("File name must be letters, digits, '.', '_' or '-'.")
        if size <= 0:
            raise SpecError("Empty upload")
        pool = self.conn.storagePoolLookupByName(pool_name)
        try:
            pool.storageVolLookupByName(name)
            raise Conflict(f"'{name}' already exists in pool {pool_name}.")
        except libvirt.libvirtError:
            pass
        vol = pool.createXML(
            f"<volume><name>{name}</name><capacity unit='bytes'>{size}</capacity>"
            "<target><format type='raw'/></target></volume>", 0)
        stream = self.conn.newStream(0)
        vol.upload(stream, 0, size, 0)
        return stream, vol

    def networks(self) -> dict:
        nets = []
        for net in self.conn.listAllNetworks(0):
            root = ET.fromstring(net.XMLDesc(0))
            fwd = root.find("forward")
            ip = root.find("ip")
            subnet = None
            if ip is not None and ip.get("address"):
                import ipaddress
                prefix = ip.get("prefix") or ip.get("netmask") or "24"
                try:
                    subnet = str(ipaddress.ip_interface(f"{ip.get('address')}/{prefix}").network)
                except ValueError:
                    subnet = ip.get("address")
            nets.append({
                "name": net.name(),
                "active": bool(net.isActive()),
                "autostart": bool(net.autostart()),
                "mode": fwd.get("mode") if fwd is not None else "isolated",
                "bridge": root.find("bridge").get("name") if root.find("bridge") is not None else None,
                "subnet": subnet,
            })
        bridges = []
        try:
            for name in sorted(os.listdir("/sys/class/net")):
                if os.path.isdir(f"/sys/class/net/{name}/bridge") and not name.startswith("virbr"):
                    bridges.append(name)
        except OSError:
            pass
        return {"networks": nets, "bridges": bridges}

    # -- stats ------------------------------------------------------------------

    def sample(self) -> tuple[float, dict[str, dict]]:
        """One raw stats sample for all running domains."""
        now = time.monotonic()
        out: dict[str, dict] = {}
        conn = self.conn
        stats_flags = (libvirt.VIR_DOMAIN_STATS_STATE | libvirt.VIR_DOMAIN_STATS_CPU_TOTAL
                       | libvirt.VIR_DOMAIN_STATS_BALLOON | libvirt.VIR_DOMAIN_STATS_VCPU
                       | libvirt.VIR_DOMAIN_STATS_INTERFACE | libvirt.VIR_DOMAIN_STATS_BLOCK)
        try:
            records = conn.getAllDomainStats(stats_flags, libvirt.VIR_CONNECT_GET_ALL_DOMAINS_STATS_ACTIVE)
        except libvirt.libvirtError:
            records = None
        if records is not None:
            for dom, st in records:
                out[dom.UUIDString()] = _flatten_stats(st)
            return now, out
        # Fallback for drivers without bulk stats.
        for dom in conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE):
            try:
                _state, maxmem, mem, ncpu, cputime = dom.info()
            except libvirt.libvirtError:
                continue
            out[dom.UUIDString()] = {"cpu_time": cputime, "vcpus": ncpu, "mem_used": mem * 1024,
                                     "mem_max": maxmem * 1024, "rd": 0, "wr": 0, "rx": 0, "tx": 0}
        return now, out


def parse_guest_info(raw: dict) -> dict:
    """libvirt's flat guest-info params ("fs.0.mountpoint", ...) as a small dict for the UIs."""
    users = sorted({raw.get(f"user.{i}.name") for i in range(int(raw.get("user.count", 0)))} - {None})
    filesystems, seen = [], set()
    for i in range(int(raw.get("fs.count", 0))):
        total, used = raw.get(f"fs.{i}.total-bytes"), raw.get(f"fs.{i}.used-bytes")
        mount = raw.get(f"fs.{i}.mountpoint")
        if not total or mount in seen or (raw.get(f"fs.{i}.fstype") or "").lower() in ("cdfs", "iso9660", "udf"):
            continue  # recovery and EFI partitions have no size; CD drives aren't storage
        seen.add(mount)
        filesystems.append({"mount": mount, "type": raw.get(f"fs.{i}.fstype"), "total": total, "used": used})
    filesystems.sort(key=lambda f: -f["total"])
    return {
        "os": raw.get("os.pretty-name") or raw.get("os.name"),
        "hostname": raw.get("hostname"),
        "users": users,
        "filesystems": filesystems[:4],
    }


def _flatten_stats(st: dict) -> dict:
    n_vcpu = st.get("vcpu.current") or st.get("vcpu.maximum") or 1
    mem_max = st.get("balloon.maximum", 0) * 1024
    if "balloon.available" in st and "balloon.unused" in st:
        used = (st["balloon.available"] - st["balloon.unused"]) * 1024
    elif "balloon.rss" in st:
        used = st["balloon.rss"] * 1024
    else:
        used = st.get("balloon.current", 0) * 1024
    rd = wr = rx = tx = 0
    for i in range(st.get("block.count", 0)):
        rd += st.get(f"block.{i}.rd.bytes", 0)
        wr += st.get(f"block.{i}.wr.bytes", 0)
    for i in range(st.get("net.count", 0)):
        rx += st.get(f"net.{i}.rx.bytes", 0)
        tx += st.get(f"net.{i}.tx.bytes", 0)
    return {"cpu_time": st.get("cpu.time", 0), "vcpus": n_vcpu, "mem_used": used,
            "mem_max": mem_max, "rd": rd, "wr": wr, "rx": rx, "tx": tx}
