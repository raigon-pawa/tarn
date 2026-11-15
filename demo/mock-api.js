// Tarn demo: an in-browser stand-in for the Tarn server.
//
// Loaded as a classic script before app.js, it answers every /api request
// from a made-up host so the real UI runs unchanged on a static site (GitHub
// Pages). Nothing leaves the browser. State that the visitor changes (VMs,
// snapshots, disks) is kept in sessionStorage so the console iframe and page
// reloads see the same host; "Reset demo" clears it.
(() => {
  "use strict";

  const KEY = "tarn-demo-state-v1";
  const GiB = 1024 ** 3;
  const MiB = 1024 ** 2;
  const POINTS = 150;
  const now = () => Date.now() / 1000;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);

  class HttpError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }
  const notFound = (what) => new HttpError(404, `${what} not found`);
  const conflict = (msg) => new HttpError(409, msg);
  const bad = (msg) => new HttpError(400, msg);

  // ------------------------------------------------------------ fixed host

  const HOST = {
    hostname: "tarn-demo",
    os: "Alpine Linux v3.24",
    kernel: "6.18.57-0-lts",
    cpu_model: "AMD Ryzen 9 9955HX 16-Core Processor",
    threads: 32,
    memory_total: 32 * GiB,
    virt_type: "kvm",
    libvirt_version: "12.3.0",
    qemu_version: "11.0.0",
    uri: "qemu:///system",
    booted: now() - (3 * 86400 + 5 * 3600 + 17 * 60),
  };

  const CARD = {
    slot: "0000:01:00",
    name: "NVIDIA GeForce RTX 5070 Laptop GPU",
    functions: [
      { address: "0000:01:00.0", driver: "vfio-pci" },
      { address: "0000:01:00.1", driver: "vfio-pci" },
    ],
    ready: true,
    isolated: true,
  };
  const CARD_ADDRS = CARD.functions.map((f) => f.address);

  const IOMMU = [
    { id: 0, devices: [{ address: "0000:00:00.0", name: "Root Complex", class: "Host bridge", class_code: "060000", driver: null }] },
    { id: 12, devices: [
      { address: "0000:01:00.0", name: "NVIDIA GB206M [GeForce RTX 5070 Laptop GPU]", class: "VGA compatible controller", class_code: "030000", driver: "vfio-pci" },
      { address: "0000:01:00.1", name: "NVIDIA GB206 High Definition Audio Controller", class: "Audio device", class_code: "040300", driver: "vfio-pci" },
    ] },
    { id: 13, devices: [{ address: "0000:02:00.0", name: "Samsung NVMe SSD Controller PM9C1a", class: "Non-Volatile memory controller", class_code: "010802", driver: "nvme" }] },
    { id: 14, devices: [{ address: "0000:03:00.0", name: "Realtek RTL8125 2.5GbE Controller", class: "Ethernet controller", class_code: "020000", driver: "r8169" }] },
    { id: 15, devices: [{ address: "0000:04:00.0", name: "MediaTek MT7925 Wi-Fi 7", class: "Network controller", class_code: "028000", driver: "mt7925e" }] },
    { id: 16, devices: [
      { address: "0000:05:00.0", name: "AMD Raphael integrated graphics", class: "VGA compatible controller", class_code: "030000", driver: "amdgpu" },
      { address: "0000:05:00.1", name: "AMD Rembrandt Radeon High Definition Audio", class: "Audio device", class_code: "040300", driver: "snd_hda_intel" },
    ] },
    { id: 17, devices: [{ address: "0000:06:00.0", name: "AMD USB 3.2 xHCI Controller", class: "USB controller", class_code: "0c0330", driver: "xhci_hcd" }] },
  ].map((g) => ({ ...g, devices: g.devices.map((d) => ({ ...d, isolated: g.devices.length === 1 || g.id === 12 || g.id === 16 })) }));

  // ------------------------------------------------------------ initial state

  let macSeq = 0x10;
  const mac = () => `52:54:00:3a:7e:${(macSeq++).toString(16).padStart(2, "0")}`;
  const uuid4 = () => (crypto.randomUUID ? crypto.randomUUID()
    : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    }));

  function makeVm(o) {
    const linux = o.os_type === "linux";
    const win = o.os_type === "windows";
    return {
      uuid: o.uuid || uuid4(),
      name: o.name,
      os_type: o.os_type,
      state: o.state || "stopped",
      vcpus: o.vcpus,
      memory: o.memory_mib * MiB,
      firmware: o.firmware || "efi",
      secure_boot: !!o.secure_boot,
      tpm: o.tpm ? { version: "2.0", model: "tpm-crb" } : null,
      machine: "pc-q35-11.0",
      virt_type: "kvm",
      cpu_mode: "host-passthrough",
      video: win ? "vga" : linux ? "virtio" : "vga",
      autostart: !!o.autostart,
      disks: o.disks,
      nics: o.nics || [{ model: linux || o.virtio ? "virtio" : win ? "e1000e" : "e1000", type: "network", source: "default", mac: mac() }],
      hostdevs: o.hostdevs || [],
      pinning: o.pinning || null,
      looking_glass: !!o.looking_glass,
      ip: o.ip || null,
      guest: o.guest || null,
      snapshots: o.snapshots || [],
      started: o.state === "running" ? now() - rand(600, 40000) : null,
      xml: null,
      // per-VM personality for the made-up load
      load: o.load || { base: 8, swing: 6, mem: 0.45 },
    };
  }

  const disk = (target, bus, path, capGiB, boot) => ({ device: "disk", target, bus, path, capacity: capGiB * GiB, boot_order: boot || null });
  const cdrom = (target, bus, path, boot) => ({ device: "cdrom", target, bus, path: path || null, capacity: null, boot_order: boot || null });

  function initialState() {
    const t = now();
    const vms = [
      makeVm({
        name: "win11-gaming", os_type: "windows", state: "running", vcpus: 8, memory_mib: 16384,
        secure_boot: true, tpm: true, autostart: false, looking_glass: true,
        hostdevs: CARD_ADDRS.slice(),
        pinning: { vcpus: { 0: 8, 1: 9, 2: 10, 3: 11, 4: 12, 5: 13, 6: 14, 7: 15 }, emulator: "0-7,16-23" },
        disks: [disk("sda", "sata", "/var/lib/libvirt/images/win11-gaming.qcow2", 256, 1),
          cdrom("sdb", "sata", "/var/lib/libvirt/iso/virtio-win-0.1.285.iso")],
        nics: [{ model: "virtio", type: "network", source: "default", mac: mac() }],
        ip: "192.168.122.48",
        guest: { os: "Microsoft Windows 11 Pro", hostname: "WIN11-GAMING", users: ["player"],
          filesystems: [{ mount: "C:\\", total: 255 * GiB, used: 141 * GiB }] },
        snapshots: [
          { name: "fresh-install", created: t - 21 * 86400, state: "shutoff", description: "Clean Windows 11, drivers only" },
          { name: "before-nvidia-driver", created: t - 9 * 86400, state: "shutoff", description: "Before installing the GPU driver" },
        ],
        load: { base: 34, swing: 22, mem: 0.71 },
      }),
      makeVm({
        name: "alpine-dev", os_type: "linux", state: "running", vcpus: 2, memory_mib: 2048, autostart: true,
        disks: [disk("vda", "virtio", "/var/lib/libvirt/images/alpine-dev.qcow2", 32, 1)],
        ip: "192.168.122.21",
        guest: { os: "Alpine Linux v3.24", hostname: "alpine-dev", users: ["dev"],
          filesystems: [{ mount: "/", total: 31 * GiB, used: 4.2 * GiB }] },
        snapshots: [{ name: "base", created: t - 4 * 86400, state: "running", description: "Toolchain installed" }],
        load: { base: 6, swing: 9, mem: 0.38 },
      }),
      makeVm({
        name: "debian-nas", os_type: "linux", state: "running", vcpus: 4, memory_mib: 4096, autostart: true,
        disks: [disk("vda", "virtio", "/var/lib/libvirt/images/debian-nas.qcow2", 24, 1),
          disk("vdb", "virtio", "/var/lib/libvirt/images/nas-data.qcow2", 500)],
        ip: "192.168.122.30",
        guest: { os: "Debian GNU/Linux 13 (trixie)", hostname: "nas", users: [],
          filesystems: [{ mount: "/", total: 23 * GiB, used: 6.8 * GiB }, { mount: "/srv/data", total: 492 * GiB, used: 318 * GiB }] },
        load: { base: 3, swing: 4, mem: 0.55 },
      }),
      makeVm({
        name: "fedora-test", os_type: "linux", state: "stopped", vcpus: 4, memory_mib: 4096,
        disks: [disk("vda", "virtio", "/var/lib/libvirt/images/fedora-test.qcow2", 40, 2),
          cdrom("sda", "sata", "/var/lib/libvirt/iso/Fedora-Workstation-Live-43.iso", 1)],
        load: { base: 12, swing: 10, mem: 0.5 },
      }),
      makeVm({
        name: "freebsd-14", os_type: "other", state: "stopped", vcpus: 2, memory_mib: 1024, firmware: "bios",
        disks: [disk("sda", "sata", "/var/lib/libvirt/images/freebsd-14.qcow2", 16, 1)],
        load: { base: 4, swing: 5, mem: 0.3 },
      }),
    ];
    return {
      vms,
      isos: [
        { name: "alpine-virt-3.24.2-x86_64.iso", size: 68 * MiB },
        { name: "Fedora-Workstation-Live-43.iso", size: 2.4 * GiB },
        { name: "virtio-win-0.1.285.iso", size: 754 * MiB },
        { name: "Win11_25H2_English_x64.iso", size: 7.2 * GiB },
      ],
      extraImages: [{ name: "old-laptop-windows.qcow2", capacity: 476 * GiB, allocation: 212 * GiB }],
      signedIn: true,
    };
  }

  // ------------------------------------------------------------ persistence

  let state;
  function load() {
    try {
      const raw = sessionStorage.getItem(KEY);
      if (raw) { state = JSON.parse(raw); return; }
    } catch { /* storage blocked: fall back to memory */ }
    state = initialState();
  }
  function save() {
    try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch { /* ignore */ }
  }
  load();
  save();

  // ------------------------------------------------------------ live stats

  const vmHist = new Map();   // uuid -> [{cpu, mem_used, rd, wr, rx, tx}]
  const hostHist = [];
  let tick = 0;

  function sampleVm(vm, i) {
    const L = vm.load;
    const phase = (tick + i) / 9 + vm.name.length;
    const cpu = clamp(L.base + L.swing * Math.sin(phase) + L.swing * 0.6 * Math.sin(phase * 2.7) + rand(-3, 3), 0.3, 99);
    const burst = Math.random() < 0.08 ? rand(8, 40) * MiB : 0;
    return {
      cpu,
      mem_used: vm.memory * clamp(L.mem + 0.03 * Math.sin(phase / 3), 0.05, 0.98),
      rd: Math.max(0, rand(0, 900) * 1024 + burst),
      wr: Math.max(0, rand(50, 1500) * 1024 + burst * 0.4),
      rx: Math.max(0, rand(1, 60) * 1024 * (cpu / 10)),
      tx: Math.max(0, rand(1, 25) * 1024 * (cpu / 12)),
    };
  }

  function sampleAll(n = 1) {
    for (let k = 0; k < n; k++) {
      tick++;
      let hostCpu = 2.5 + rand(0, 2);
      let memUsed = 2.1 * GiB;
      state.vms.forEach((vm, i) => {
        let h = vmHist.get(vm.uuid);
        if (!h) { h = []; vmHist.set(vm.uuid, h); }
        if (vm.state === "running") {
          const s = sampleVm(vm, i);
          h.push(s);
          hostCpu += (s.cpu * vm.vcpus) / HOST.threads;
          memUsed += s.mem_used;
        } else if (vm.state === "paused") {
          const last = h[h.length - 1];
          h.push({ cpu: 0, mem_used: last ? last.mem_used : 0, rd: 0, wr: 0, rx: 0, tx: 0 });
          if (last) memUsed += last.mem_used;
        } else {
          h.length = 0;
        }
        if (h.length > POINTS) h.splice(0, h.length - POINTS);
      });
      hostHist.push({ cpu: clamp(hostCpu, 0, 100), mem_used: memUsed });
      if (hostHist.length > POINTS) hostHist.splice(0, hostHist.length - POINTS);
    }
  }
  sampleAll(POINTS);  // full five-minute charts from the first paint
  setInterval(() => sampleAll(1), 2000);

  // ------------------------------------------------------------ views of state

  const vmBy = (id) => {
    const vm = state.vms.find((v) => v.uuid === id);
    if (!vm) throw notFound("VM");
    return vm;
  };
  const isoPath = (name) => `/var/lib/libvirt/iso/${name}`;
  const imgPath = (name) => `/var/lib/libvirt/images/${name}`;

  function currentStats(vm) {
    const h = vmHist.get(vm.uuid) || [];
    const c = h[h.length - 1];
    if (!c || vm.state !== "running") return null;
    return { ...c, mem_max: vm.memory };
  }

  function vmSummary(vm) {
    const st = currentStats(vm);
    return {
      uuid: vm.uuid, name: vm.name, state: vm.state, vcpus: vm.vcpus, memory: vm.memory, os_type: vm.os_type,
      stats: st ? { cpu: st.cpu, mem_used: st.mem_used, mem_max: st.mem_max } : null,
      cpu_history: (vmHist.get(vm.uuid) || []).map((p) => p.cpu),
    };
  }

  function vmDetail(vm) {
    const running = vm.state === "running";
    return {
      uuid: vm.uuid, name: vm.name, state: vm.state, os_type: vm.os_type,
      vcpus: vm.vcpus, memory: vm.memory, firmware: vm.firmware, secure_boot: vm.secure_boot,
      tpm: vm.tpm, machine: vm.machine, virt_type: vm.virt_type, cpu_mode: vm.cpu_mode,
      video: vm.video, autostart: vm.autostart, snapshot_count: vm.snapshots.length,
      disks: vm.disks, nics: vm.nics, hostdevs: vm.hostdevs, pinning: vm.pinning,
      looking_glass: vm.looking_glass,
      ips: running && vm.ip ? [{ addr: vm.ip, prefix: 24, iface: vm.os_type === "windows" ? "Ethernet" : "eth0" }] : [],
      guest: running ? vm.guest : null,
    };
  }

  function domainXml(vm) {
    if (vm.xml) return vm.xml;
    const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
    const lines = [
      `<domain type='kvm'${vm.looking_glass ? " xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'" : ""}>`,
      `  <name>${esc(vm.name)}</name>`,
      `  <uuid>${vm.uuid}</uuid>`,
      `  <memory unit='KiB'>${vm.memory / 1024}</memory>`,
      `  <currentMemory unit='KiB'>${vm.memory / 1024}</currentMemory>`,
      `  <vcpu placement='static'>${vm.vcpus}</vcpu>`,
    ];
    if (vm.pinning) {
      lines.push("  <cputune>");
      for (const [v, c] of Object.entries(vm.pinning.vcpus)) lines.push(`    <vcpupin vcpu='${v}' cpuset='${c}'/>`);
      lines.push(`    <emulatorpin cpuset='${vm.pinning.emulator}'/>`, "  </cputune>");
    }
    lines.push(
      `  <os${vm.firmware === "efi" ? " firmware='efi'" : ""}>`,
      `    <type arch='x86_64' machine='${vm.machine}'>hvm</type>`);
    if (vm.firmware === "efi") {
      lines.push("    <firmware>", `      <feature enabled='${vm.secure_boot ? "yes" : "no"}' name='secure-boot'/>`,
        `      <feature enabled='${vm.secure_boot ? "yes" : "no"}' name='enrolled-keys'/>`, "    </firmware>");
    }
    lines.push("  </os>", "  <features>", "    <acpi/>", "    <apic/>");
    if (vm.os_type === "windows") {
      lines.push("    <hyperv mode='custom'>", "      <relaxed state='on'/>", "      <vapic state='on'/>",
        "      <spinlocks state='on' retries='8191'/>", "      <vpindex state='on'/>", "      <synic state='on'/>",
        "      <stimer state='on'/>", "    </hyperv>");
    }
    if (vm.secure_boot) lines.push("    <smm state='on'/>");
    lines.push("  </features>", `  <cpu mode='${vm.cpu_mode}' check='none' migratable='on'/>`,
      `  <clock offset='${vm.os_type === "windows" ? "localtime" : "utc"}'/>`,
      "  <on_poweroff>destroy</on_poweroff>", "  <on_reboot>restart</on_reboot>", "  <devices>");
    for (const d of vm.disks) {
      lines.push(`    <disk type='file' device='${d.device}'>`,
        `      <driver name='qemu' type='${d.device === "cdrom" ? "raw" : "qcow2"}'/>`);
      if (d.path) lines.push(`      <source file='${esc(d.path)}'/>`);
      lines.push(`      <target dev='${d.target}' bus='${d.bus}'/>`);
      if (d.device === "cdrom") lines.push("      <readonly/>");
      if (d.boot_order) lines.push(`      <boot order='${d.boot_order}'/>`);
      lines.push("    </disk>");
    }
    for (const n of vm.nics) {
      lines.push(`    <interface type='${n.type}'>`, `      <mac address='${n.mac}'/>`,
        `      <source ${n.type === "bridge" ? "bridge" : "network"}='${esc(n.source)}'/>`,
        `      <model type='${n.model}'/>`, "    </interface>");
    }
    if (vm.os_type === "linux") {
      lines.push("    <channel type='unix'>", "      <target type='virtio' name='org.qemu.guest_agent.0'/>", "    </channel>");
    }
    lines.push("    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>",
      `    <video><model type='${vm.video}'/></video>`);
    if (vm.tpm) lines.push(`    <tpm model='${vm.tpm.model}'>`, `      <backend type='emulator' version='${vm.tpm.version}'/>`, "    </tpm>");
    for (const a of vm.hostdevs) {
      const [dom, bus, rest] = a.split(":");
      const [slot, fn] = rest.split(".");
      lines.push("    <hostdev mode='subsystem' type='pci' managed='yes'>",
        `      <source><address domain='0x${dom}' bus='0x${bus}' slot='0x${slot}' function='0x${fn}'/></source>`,
        "      <rom bar='off'/>", "    </hostdev>");
    }
    lines.push("  </devices>");
    if (vm.looking_glass) {
      lines.push("  <qemu:commandline>",
        "    <qemu:arg value='-device'/>",
        "    <qemu:arg value='{\"driver\":\"ivshmem-plain\",\"id\":\"shmem0\",\"memdev\":\"looking-glass\"}'/>",
        "    <qemu:arg value='-object'/>",
        "    <qemu:arg value='{\"qom-type\":\"memory-backend-file\",\"id\":\"looking-glass\",\"mem-path\":\"/dev/kvmfr0\",\"size\":134217728,\"share\":true}'/>",
        "  </qemu:commandline>");
    }
    lines.push("</domain>");
    return lines.join("\n");
  }

  function usedBy(path) {
    const vm = state.vms.find((v) => v.disks.some((d) => d.path === path));
    return vm ? vm.name : null;
  }

  function storage() {
    const images = [];
    for (const vm of state.vms) {
      for (const d of vm.disks) {
        if (d.device !== "disk" || !d.path) continue;
        const name = d.path.split("/").pop();
        images.push({ name, format: "qcow2", capacity: d.capacity, allocation: Math.round(d.capacity * (vm.load.mem * 0.6 + 0.1)), used_by: vm.name });
      }
    }
    for (const im of state.extraImages) images.push({ name: im.name, format: "qcow2", capacity: im.capacity, allocation: im.allocation, used_by: usedBy(imgPath(im.name)) });
    images.sort((a, b) => a.name.localeCompare(b.name));
    const isos = state.isos.map((i) => ({ name: i.name, format: "raw", capacity: i.size, allocation: i.size, used_by: usedBy(isoPath(i.name)) }));
    const pool = (name, path, capacity, vols) => {
      const allocation = vols.reduce((n, v) => n + v.allocation, 0) + 18 * GiB;
      return { name, path, active: true, capacity, allocation, available: Math.max(0, capacity - allocation), volumes: vols };
    };
    return {
      pools: [
        pool("default", "/var/lib/libvirt/images", 1800 * GiB, images),
        pool("iso", "/var/lib/libvirt/iso", 1800 * GiB, isos),
      ],
      roles: { images: "default", iso: "iso" },
    };
  }

  function cards() {
    const assigned = state.vms.filter((v) => CARD_ADDRS.some((a) => v.hostdevs.includes(a)));
    const running = assigned.find((v) => v.state === "running");
    return [{ ...CARD, assigned_to: assigned.map((v) => v.name), running_on: running ? running.name : null }];
  }

  function meta() {
    const st = storage();
    const images = st.pools[0].volumes.filter((v) => !v.used_by)
      .map((v) => ({ name: v.name, path: imgPath(v.name), format: v.format, capacity: v.capacity }));
    return {
      caps: { efi: true, secure_boot: true, tpm: true, virt_type: "kvm" },
      isos: state.isos.map((i) => ({ name: i.name, path: isoPath(i.name) })),
      pools: st.pools.map((p) => ({ name: p.name, active: p.active, available: p.available })),
      default_pool: "default",
      images,
      host_threads: HOST.threads,
      host_memory: HOST.memory_total,
      networks: [{ name: "default", mode: "nat", subnet: "192.168.122.0/24" }],
      default_network: "default",
      bridges: ["br0"],
      cards: cards(),
      looking_glass: true,
    };
  }

  function power() {
    const t = now();
    return {
      batteries: [{ capacity: Math.round(clamp(78 + 4 * Math.sin(t / 900), 0, 100)), status: "Charging" }],
      ac_online: true,
      battery_conservation: true,
      platform_profile: "balanced",
      platform_profile_choices: ["low-power", "balanced", "performance"],
      temps: [
        { label: "Tctl", sensor: "k10temp", celsius: 58 + 9 * Math.sin(t / 37) + rand(-1, 1) },
        { label: "edge", sensor: "amdgpu", celsius: 47 + 3 * Math.sin(t / 51) },
        { label: "Composite", sensor: "nvme", celsius: 41 + 2 * Math.sin(t / 73) },
      ],
    };
  }

  function hostInfo() {
    return {
      ...HOST,
      uptime: now() - HOST.booted,
      readiness: [
        { level: "ok", label: "KVM acceleration", detail: "/dev/kvm is present and usable (kvm_amd loaded)." },
        { level: "ok", label: "libvirtd is running", detail: `libvirt ${HOST.libvirt_version}, QEMU ${HOST.qemu_version}.` },
        { level: "ok", label: "UEFI firmware", detail: "edk2 builds with Secure Boot found via QEMU's firmware descriptors." },
        { level: "ok", label: "TPM emulation", detail: "swtpm is installed, so Windows 11 VMs can have a TPM 2.0." },
        { level: "ok", label: "IOMMU", detail: "AMD-Vi is on with iommu=pt; 17 IOMMU groups." },
        { level: "ok", label: "Graphics card parked on vfio-pci", detail: "0000:01:00.0 and 0000:01:00.1 are bound to vfio-pci and ready to pass through." },
        { level: "ok", label: "Looking Glass", detail: "/dev/kvmfr0 exists (128 MiB) and is writable by the libvirt group." },
        { level: "warn", label: "Hugepages", detail: "No hugepages are reserved. Large gaming VMs run a little smoother with them.",
          fix: "echo 'vm.nr_hugepages = 8192' | doas tee /etc/sysctl.d/hugepages.conf && doas sysctl -p /etc/sysctl.d/hugepages.conf" },
        { level: "ok", label: "Default network", detail: "libvirt NAT network 'default' is active (virbr0, 192.168.122.0/24)." },
      ],
      power: power(),
      gpus: [
        { address: "0000:05:00.0", name: "AMD Raphael integrated graphics", driver: "amdgpu", power: "D0" },
        { address: "0000:01:00.0", name: "NVIDIA GeForce RTX 5070 Laptop GPU", driver: "vfio-pci",
          power: state.vms.some((v) => v.state === "running" && v.hostdevs.includes("0000:01:00.0")) ? "D0 (in a VM)" : "D3cold" },
      ],
    };
  }

  // ------------------------------------------------------------ actions

  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;

  function checkExclusive(vm) {
    for (const other of state.vms) {
      if (other === vm || other.state !== "running") continue;
      if (vm.hostdevs.some((a) => other.hostdevs.includes(a))) {
        throw conflict(`${other.name} is running with the same graphics card. Shut it down first.`);
      }
      if (vm.looking_glass && other.looking_glass) {
        throw conflict(`${other.name} is using /dev/kvmfr0 for Looking Glass. Shut it down first.`);
      }
    }
  }

  function setState(vm, st) {
    vm.state = st;
    vm.started = st === "running" ? now() : st === "paused" ? vm.started : null;
    save();
  }

  function doAction(vm, action) {
    const st = vm.state;
    const need = (ok, msg) => { if (!ok) throw bad(msg || `Can't ${action} a VM that is ${st}.`); };
    switch (action) {
      case "start":
        need(st === "stopped" || st === "crashed", "The VM is already running.");
        checkExclusive(vm);
        setState(vm, "running");
        break;
      case "shutdown":
        need(st === "running");
        setState(vm, "stopping");
        setTimeout(() => { if (vm.state === "stopping") setState(vm, "stopped"); }, 3500);
        break;
      case "reboot":
      case "reset":
        need(st === "running");
        vmHist.set(vm.uuid, []);
        break;
      case "poweroff":
        need(st === "running" || st === "paused" || st === "stopping", "The VM is already off.");
        setState(vm, "stopped");
        break;
      case "pause":
        need(st === "running");
        setState(vm, "paused");
        break;
      case "resume":
        need(st === "paused");
        setState(vm, "running");
        break;
      default:
        throw bad(`Unknown action ${action}`);
    }
    return {};
  }

  function patchVm(vm, b) {
    const running = vm.state !== "stopped" && vm.state !== "crashed";
    let applies_after_restart = false, note = null;
    if (b.name !== undefined && b.name !== vm.name) {
      if (running) throw conflict("Shut the VM down before renaming it.");
      if (!NAME_RE.test(b.name)) throw bad("Names use letters, digits, dots, dashes and underscores.");
      if (state.vms.some((v) => v.name === b.name)) throw conflict(`A VM called ${b.name} already exists.`);
      vm.name = b.name;
    }
    if (b.vcpus !== undefined) {
      if (!(b.vcpus >= 1 && b.vcpus <= HOST.threads)) throw bad(`vCPUs must be between 1 and ${HOST.threads}.`);
      vm.vcpus = b.vcpus;
      if (vm.pinning) vm.pinning = pinFor(vm.vcpus);
      applies_after_restart ||= running;
    }
    if (b.memory_mib !== undefined) {
      if (!(b.memory_mib >= 256 && b.memory_mib * MiB <= HOST.memory_total)) throw bad("Memory must be between 0.25 GiB and the host's RAM.");
      vm.memory = b.memory_mib * MiB;
      applies_after_restart ||= running;
    }
    if (b.autostart !== undefined) vm.autostart = !!b.autostart;
    if (b.boot_from_disk) {
      let order = 1;
      for (const d of vm.disks) d.boot_order = d.device === "disk" ? order++ : null;
      const cd = vm.disks.find((d) => d.device === "cdrom");
      if (cd) cd.boot_order = order;
    }
    if (b.cpu_pinning !== undefined) {
      vm.pinning = b.cpu_pinning ? pinFor(vm.vcpus) : null;
      if (b.cpu_pinning && vm.vcpus > 8) note = "Only 8 vCPUs fit on one cluster; the rest float.";
      applies_after_restart ||= running;
    }
    if (b.looking_glass !== undefined) {
      if (running) throw conflict("Shut the VM down to change Looking Glass.");
      vm.looking_glass = !!b.looking_glass;
      if (vm.looking_glass) note = "Install the Looking Glass host app (B7) inside the guest.";
    }
    vm.xml = null;
    save();
    return { applies_after_restart, note };
  }

  function pinFor(n) {
    const vcpus = {};
    for (let i = 0; i < Math.min(n, 8); i++) vcpus[i] = 8 + i;
    return { vcpus, emulator: "0-7,16-23" };
  }

  function createVm(spec) {
    const name = (spec.name || "").trim();
    if (!NAME_RE.test(name)) throw bad("Give the VM a name: letters, digits, dots, dashes and underscores.");
    if (state.vms.some((v) => v.name === name)) throw conflict(`A VM called ${name} already exists.`);
    if (!(spec.vcpus >= 1 && spec.vcpus <= HOST.threads)) throw bad(`vCPUs must be between 1 and ${HOST.threads}.`);
    if (!(spec.memory_mib >= 256)) throw bad("Give the VM at least 0.25 GiB of memory.");
    const win = spec.os_type === "windows";
    const virtio = !!spec.virtio;
    const bus = virtio ? "virtio" : "sata";
    const dev = (i) => (virtio ? "vd" : "sd") + "abcdefgh"[i];
    const disks = [];
    let di = 0;
    if (spec.disk.mode === "new") {
      if (!(spec.disk.size_gib >= 1)) throw bad("The disk needs at least 1 GiB.");
      disks.push(disk(dev(di++), bus, imgPath(`${name}.qcow2`), spec.disk.size_gib, spec.iso ? 2 : 1));
    } else if (spec.disk.mode === "existing") {
      if (!spec.disk.path) throw bad("Choose a disk image.");
      const im = state.extraImages.find((i) => imgPath(i.name) === spec.disk.path);
      disks.push({ device: "disk", target: dev(di++), bus, path: spec.disk.path, capacity: im ? im.capacity : 64 * GiB, boot_order: spec.iso ? 2 : 1 });
    }
    let ci = virtio ? 0 : di;
    if (spec.iso) disks.push(cdrom("sd" + "abcdefgh"[ci++], "sata", spec.iso, 1));
    if (win && spec.driver_iso) disks.push(cdrom("sd" + "abcdefgh"[ci++], "sata", spec.driver_iso));
    const net = spec.network || {};
    const nics = net.type === "none" ? [] : [{
      model: virtio ? "virtio" : win ? "e1000e" : "e1000",
      type: net.type === "bridge" ? "bridge" : "network",
      source: net.name || "default", mac: mac(),
    }];
    const vm = makeVm({
      name, os_type: spec.os_type, vcpus: spec.vcpus, memory_mib: spec.memory_mib,
      firmware: spec.firmware, secure_boot: spec.secure_boot && spec.firmware === "efi", tpm: spec.tpm,
      virtio, disks, nics,
      hostdevs: spec.card ? CARD_ADDRS.slice() : [],
      pinning: spec.cpu_pinning ? pinFor(spec.vcpus) : null,
      looking_glass: !!spec.looking_glass,
      ip: nics.length && net.type !== "bridge" ? `192.168.122.${60 + state.vms.length}` : null,
      load: { base: rand(10, 30), swing: rand(5, 15), mem: rand(0.3, 0.6) },
    });
    vm.video = virtio && !win ? "virtio" : "vga";
    state.vms.push(vm);
    let started = false, warning = null;
    if (spec.start) {
      try { checkExclusive(vm); vm.state = "running"; vm.started = now(); started = true; } catch (e) { warning = e.message; }
    }
    save();
    return { uuid: vm.uuid, started, warning, note: spec.card ? "The graphics card is attached." : null };
  }

  function deleteVm(vm, withDisks) {
    state.vms = state.vms.filter((v) => v !== vm);
    vmHist.delete(vm.uuid);
    const deleted_disks = withDisks ? vm.disks.filter((d) => d.device === "disk" && d.path).map((d) => d.path) : [];
    if (!withDisks) {
      for (const d of vm.disks) {
        if (d.device === "disk" && d.path && d.path.startsWith("/var/lib/libvirt/images/")) {
          state.extraImages.push({ name: d.path.split("/").pop(), capacity: d.capacity, allocation: Math.round(d.capacity * 0.3) });
        }
      }
    }
    save();
    return { deleted_disks };
  }

  // ------------------------------------------------------------ router

  const routes = [];
  const route = (method, pattern, fn) => {
    const keys = [];
    const re = new RegExp("^" + pattern.replace(/\{(\w+)\}/g, (_, k) => { keys.push(k); return "([^/]+)"; }) + "$");
    routes.push({ method, re, keys, fn });
  };

  const requireAuth = () => { if (!state.signedIn) throw new HttpError(401, "Sign in first"); };

  route("GET", "/api/me", () => { requireAuth(); return { user: "admin" }; });
  route("POST", "/api/login", (_p, b) => {
    if (!b || !b.password) throw new HttpError(401, "Type any password: this is a demo");
    state.signedIn = true; save(); return {};
  });
  route("POST", "/api/logout", () => { state.signedIn = false; save(); return {}; });

  route("GET", "/api/host", () => hostInfo());
  route("GET", "/api/host/stats", () => {
    const c = hostHist[hostHist.length - 1] || {};
    return { current: { cpu: c.cpu, mem_used: c.mem_used, mem_total: HOST.memory_total }, history: hostHist.slice(), power: power() };
  });
  route("GET", "/api/host/iommu", () => ({ groups: IOMMU }));
  route("GET", "/api/pci/cards", () => ({ cards: cards() }));
  route("GET", "/api/meta", () => meta());

  route("GET", "/api/vms", () => ({ vms: state.vms.map(vmSummary) }));
  route("POST", "/api/vms", (_p, b) => createVm(b || {}));
  route("GET", "/api/vms/{id}", (p) => vmDetail(vmBy(p.id)));
  route("PATCH", "/api/vms/{id}", (p, b) => patchVm(vmBy(p.id), b || {}));
  route("DELETE", "/api/vms/{id}", (p, _b, q) => deleteVm(vmBy(p.id), q.get("disks") === "1"));
  route("POST", "/api/vms/{id}/action", (p, b) => doAction(vmBy(p.id), (b || {}).action));
  route("GET", "/api/vms/{id}/stats", (p) => {
    const vm = vmBy(p.id);
    return { current: currentStats(vm), history: (vmHist.get(vm.uuid) || []).slice() };
  });
  route("POST", "/api/vms/{id}/media", (p, b) => {
    const vm = vmBy(p.id);
    const d = vm.disks.find((x) => x.target === b.target && x.device === "cdrom");
    if (!d) throw notFound("Drive");
    d.path = b.path || null;
    vm.xml = null; save();
    return {};
  });
  route("POST", "/api/vms/{id}/cards", (p, b) => {
    const vm = vmBy(p.id);
    if (vm.state !== "stopped" && vm.state !== "crashed") throw conflict("Shut the VM down to change its graphics card.");
    if (b.slot !== CARD.slot) throw notFound("Card");
    for (const a of CARD_ADDRS) if (!vm.hostdevs.includes(a)) vm.hostdevs.push(a);
    vm.xml = null; save();
    return { note: vm.os_type === "windows" ? "Added a fake battery so the NVIDIA driver doesn't stop with Code 43." : null };
  });
  route("DELETE", "/api/vms/{id}/cards/{slot}", (p) => {
    const vm = vmBy(p.id);
    if (vm.state !== "stopped" && vm.state !== "crashed") throw conflict("Shut the VM down to change its graphics card.");
    vm.hostdevs = vm.hostdevs.filter((a) => !CARD_ADDRS.includes(a));
    vm.xml = null; save();
    return {};
  });
  route("POST", "/api/vms/{id}/pci", (p, b) => {
    const vm = vmBy(p.id);
    if (vm.state !== "stopped" && vm.state !== "crashed") throw conflict("Shut the VM down to add devices.");
    if (!vm.hostdevs.includes(b.address)) vm.hostdevs.push(b.address);
    vm.xml = null; save();
    return {};
  });
  route("DELETE", "/api/vms/{id}/pci/{addr}", (p) => {
    const vm = vmBy(p.id);
    if (vm.state !== "stopped" && vm.state !== "crashed") throw conflict("Shut the VM down to remove devices.");
    vm.hostdevs = vm.hostdevs.filter((a) => a !== p.addr);
    vm.xml = null; save();
    return {};
  });
  route("GET", "/api/vms/{id}/snapshots", (p) => {
    const vm = vmBy(p.id);
    const last = vm.snapshots[vm.snapshots.length - 1];
    return { snapshots: vm.snapshots.map((s) => ({ ...s, current: s === last })) };
  });
  route("POST", "/api/vms/{id}/snapshots", (p, b) => {
    const vm = vmBy(p.id);
    const name = (b.name || "").trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) throw bad("Snapshot names use letters, digits, dots, dashes and underscores.");
    if (vm.snapshots.some((s) => s.name === name)) throw conflict(`There's already a snapshot called ${name}.`);
    vm.snapshots.push({ name, created: now(), state: vm.state === "running" ? "running" : "shutoff", description: b.description || "" });
    save();
    return {};
  });
  route("POST", "/api/vms/{id}/snapshots/{name}/revert", (p) => {
    const vm = vmBy(p.id);
    const sn = vm.snapshots.find((s) => s.name === p.name);
    if (!sn) throw notFound("Snapshot");
    vm.snapshots = vm.snapshots.filter((s) => s !== sn).concat(sn);
    setState(vm, sn.state === "running" ? "running" : "stopped");
    return {};
  });
  route("DELETE", "/api/vms/{id}/snapshots/{name}", (p) => {
    const vm = vmBy(p.id);
    const before = vm.snapshots.length;
    vm.snapshots = vm.snapshots.filter((s) => s.name !== p.name);
    if (vm.snapshots.length === before) throw notFound("Snapshot");
    save();
    return {};
  });
  route("GET", "/api/vms/{id}/xml", (p) => ({ xml: domainXml(vmBy(p.id)) }));
  route("PUT", "/api/vms/{id}/xml", (p, b) => {
    const vm = vmBy(p.id);
    const xml = (b && b.xml) || "";
    let doc;
    try { doc = new DOMParser().parseFromString(xml, "application/xml"); } catch { doc = null; }
    if (!doc || doc.querySelector("parsererror") || !doc.documentElement || doc.documentElement.nodeName !== "domain") {
      throw bad("XML error: this isn't a well-formed <domain> definition.");
    }
    const nm = doc.querySelector("domain > name");
    if (nm && nm.textContent && nm.textContent !== vm.name) {
      if (!NAME_RE.test(nm.textContent)) throw bad("XML error: invalid VM name.");
      vm.name = nm.textContent;
    }
    vm.xml = xml;
    save();
    return {};
  });

  route("GET", "/api/storage", () => storage());
  route("POST", "/api/storage/{pool}/volumes", (p, b) => {
    const name = (b.name || "").trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/.test(name)) throw bad("Volume names use letters, digits, dots, dashes and underscores.");
    if (p.pool !== "default") throw bad("Create disks in the default pool.");
    if (storage().pools[0].volumes.some((v) => v.name === name)) throw conflict(`${name} already exists.`);
    state.extraImages.push({ name, capacity: (b.size_gib || 1) * GiB, allocation: 196 * 1024 });
    save();
    return {};
  });
  route("DELETE", "/api/storage/{pool}/volumes/{name}", (p) => {
    const path = p.pool === "iso" ? isoPath(p.name) : imgPath(p.name);
    const user = usedBy(path);
    if (user) throw conflict(`${p.name} is used by ${user}.`);
    if (p.pool === "iso") state.isos = state.isos.filter((i) => i.name !== p.name);
    else state.extraImages = state.extraImages.filter((i) => i.name !== p.name);
    save();
    return {};
  });

  async function handle(method, url, body) {
    const u = new URL(url, location.href);
    const path = u.pathname.replace(/^.*?(\/api\/)/, "/api/");
    for (const r of routes) {
      if (r.method !== method) continue;
      const m = path.match(r.re);
      if (!m) continue;
      const params = {};
      r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
      await sleep(method === "GET" ? rand(25, 90) : rand(180, 480));
      try {
        if (!["/api/login", "/api/logout", "/api/me"].includes(path)) requireAuth();
        return { status: 200, data: (await r.fn(params, body, u.searchParams)) ?? {} };
      } catch (e) {
        if (e instanceof HttpError) return { status: e.status, data: { error: e.message } };
        console.error(e);
        return { status: 500, data: { error: "Something went wrong in the demo" } };
      }
    }
    return { status: 404, data: { error: `No demo route for ${method} ${path}` } };
  }

  const isApi = (url) => {
    try { return new URL(url, location.href).pathname.includes("/api/"); } catch { return false; }
  };

  // ------------------------------------------------------------ fetch shim

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    if (!isApi(url)) return realFetch(input, init);
    const method = (init.method || (typeof input === "string" ? "GET" : input.method) || "GET").toUpperCase();
    let body;
    if (init.body) { try { body = JSON.parse(init.body); } catch { body = undefined; } }
    const { status, data } = await handle(method, url, body);
    return new Response(JSON.stringify(data), {
      status,
      statusText: status === 200 ? "OK" : "Error",
      headers: { "Content-Type": "application/json" },
    });
  };

  // ------------------------------------------------------------ upload shim
  // The UI uploads ISOs with XMLHttpRequest for progress events. The demo
  // reads nothing from the file: it only pretends to send it.

  const RealXHR = window.XMLHttpRequest;
  class DemoXHR {
    constructor() {
      this.upload = { onprogress: null };
      this.onload = null; this.onerror = null;
      this.status = 0; this.responseText = "";
      this._real = null;
    }
    open(method, url) {
      this._method = method; this._url = url;
      if (!isApi(url)) { this._real = new RealXHR(); this._real.open(method, url); }
    }
    setRequestHeader(k, v) { if (this._real) this._real.setRequestHeader(k, v); }
    async send(file) {
      if (this._real) {
        const r = this._real;
        r.onload = () => { this.status = r.status; this.responseText = r.responseText; this.onload && this.onload(); };
        r.onerror = () => this.onerror && this.onerror();
        r.send(file);
        return;
      }
      const u = new URL(this._url, location.href);
      const name = u.searchParams.get("name") || "upload.iso";
      const size = (file && file.size) || 0;
      if (!state.signedIn) return this._finish(401, { error: "Sign in first" });
      if (state.isos.some((i) => i.name === name)) return this._finish(409, { error: `${name} already exists.` });
      const steps = 24;
      for (let i = 1; i <= steps; i++) {
        await sleep(90);
        if (this.upload.onprogress) this.upload.onprogress({ lengthComputable: true, loaded: (size * i) / steps, total: size || 1 });
      }
      state.isos.push({ name, size });
      save();
      this._finish(200, { name });
    }
    _finish(status, data) {
      this.status = status;
      this.responseText = JSON.stringify(data);
      if (this.onload) this.onload();
    }
  }
  window.XMLHttpRequest = DemoXHR;

  // ------------------------------------------------------------ for the demo console

  window.TarnDemo = {
    vm(id) { try { return vmDetail(vmBy(id)); } catch { return null; } },
    reset() {
      try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
      location.hash = "#/";
      location.reload();
    },
  };
})();
