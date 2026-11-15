# Tarn: notes for Claude Code

Tarn is a small Proxmox-style web manager for one KVM/libvirt host. The target
is Alpine Linux (musl, OpenRC) on the owner's Lenovo Legion Pro 5 laptop. It
also runs on Fedora for testing. Read `docs/HANDOFF.md` (kept locally, not in the repo) for the project's
history, the decisions made so far, and what's still open.

## Hardware this is for

- Lenovo Legion Pro 5: AMD Ryzen 9 9955HX, AMD integrated graphics, NVIDIA RTX
  5070 Laptop GPU (Blackwell, PCI ID `10de:2d58`), Wi-Fi plus an Ethernet port.
- Runs Alpine 3.24 (linux-lts) now; Fedora is gone. The host uses the AMD iGPU
  and the RTX 5070 is parked on `vfio-pci`. It is passed through to the
  `win10ENT` VM and works (no Code 43, survives a VM stop/start), with a fake
  battery SSDT and Looking Glass B7 over `/dev/kvmfr0`. Backups of that VM's
  XML before each change are in `~/vm-backups/`.

## Stack and layout

Python 3.11+ with `aiohttp` and `libvirt-python`. Plain ES-module JavaScript
with no build step. noVNC 1.7.0 and the Overpass fonts are vendored in
`tarn/static/vendor/`.

```
tarn/__main__.py   CLI: serve | passwd | check | desktop | looking-glass (Super+L in the kiosk)
tarn/web.py        routes, middlewares (errors, headers, auth), app factory
tarn/virt.py       Hypervisor class: every libvirt call (VMs, storage, nets, snapshots, stats)
tarn/domxml.py     VM templates (normalize_spec -> build_domain) and XML parsing
tarn/console.py    WebSocket <-> VNC relay (openGraphicsFD, TCP fallback)
tarn/stats.py      2 s background sampler, rates and 150-point history
tarn/host.py       /proc and /sys: readiness checks, IOMMU groups, battery, temps, GPUs
tarn/auth.py       scrypt password file, in-memory sessions, CSRF/origin middleware
tarn/config.py     TOML config with defaults (tarn.example.toml documents every key)
tarn/desktop/      native GTK3 app (`tarn desktop`): app.py window and navigation, overview.py, create.py,
                   vmpage.py (one VM: hardware, drives, GPU, snapshots), hostpage.py (readiness, GPUs,
                   power, storage, IOMMU), console.py (spice-gtk / gtk-vnc), lookingglass.py
                   (one client at a time, on the running VM's SPICE port),
                   panel.py + system.py (top bar: Wi-Fi via wpa_cli, Bluetooth via BlueZ D-Bus,
                   sound via wpctl, brightness, battery), backend.py worker threads,
                   theme.py palettes + GTK CSS, widgets.py ridgelines and Cairo tray icons
tarn/static/       index.html + app.js + app.css (SPA, hash routes), console.html + console.js
install.sh         Alpine (apk, OpenRC) and Fedora (dnf, systemd) installer, idempotent;
                   --desktop USER adds the native app + labwc kiosk, --autostart logs USER in on tty1
packaging/         OpenRC script, systemd unit, desktop/ (tarn-kiosk, labwc config, profile.d snippets),
                   lid-screen/ (root daemon: panel off while the lid is shut, via wlopm or fbcon blank)
tests/             api_test.py (REST + WebSocket), ui_test.py (Playwright), make-demo-iso.sh
docs/              ALPINE-LEGION.md (install guide for the laptop), HANDOFF.md, screenshots
```

## Running and testing

```sh
cp dev.example.toml dev.toml
python3 -m tarn --config dev.toml passwd
python3 -m tarn --config dev.toml serve        # http://127.0.0.1:8006
TARN_PASSWORD=... python3 tests/api_test.py    # expect every line [ok ]
TARN_PASSWORD=... DEMO_ISO=/tmp/alpine-demo.iso python3 tests/ui_test.py   # empty host only
python3 -m tarn --config dev.toml check        # host readiness checklist
```

The tests create and delete real VMs through libvirt, but only their own
(`alpine-test`, `win11-test`, `bios-test`, `test-install.iso`), so they are
safe next to other VMs. api_test.py passes in full on the Legion (Alpine 3.24,
libvirt 12.3, QEMU 11.0, real KVM) as of 2026-10-20, run as a normal user in
the libvirt group against a dev server. The desktop app is tested headless:
`cage` with `WLR_BACKENDS=headless WLR_RENDERER=pixman` plus `grim` for
screenshots, driving the window from a small Python harness.

## Conventions and gotchas

- **Desktop app**: reuses `Hypervisor`/`Sampler` in-process, no web server
  needed. Every libvirt call goes through `Backend.call`/`poll` on a thread
  and comes back via `GLib.idle_add`; never call libvirt on the GTK thread.
  Runs under labwc on the iGPU via `tarn-kiosk` (cage has no touchpad
  settings, so tap-to-click never worked there); Alpine needs seatd, eudev (not mdev, or
  libinput finds no devices), mesa-dri-gallium, py3-gobject3, py3-cairo,
  gtk+3.0, font-overpass, and the user in the libvirt and seat groups.
  GTK3 CSS: font weights only in steps of 100; a RadioButton drawn as a
  tile (`set_mode(False)`) has CSS node `button`, not `radiobutton`.
  Build widgets fresh on each render: re-parenting a widget kept from an
  old render (out of a container being torn down) segfaulted GTK once.
  Overpass has no ✓/✕ glyphs; status marks are CSS dots (`.dot.running`).
  The top panel needs no root at runtime: `packaging/desktop/system-access.sh`
  gives the netdev group wpa_supplicant's control socket and /dev/rfkill,
  installs BlueZ + D-Bus and PipeWire. Wi-Fi networks are added with hex
  SSID and a PBKDF2 PSK (no wpa_cli quoting issues). The Bluetooth agent
  (NoInputNoOutput) only accepts the device the user chose to pair.
- **Blocking libvirt calls go through `asyncio.to_thread`** (`run()` in
  web.py). libvirt-python is synchronous, and a direct call stalls every
  WebSocket.
- **Errors**: raise `SpecError` (400), `NotFound` (404) or `Conflict` (409)
  with a message that's safe to show the user. The error middleware maps them
  to JSON. `libvirtError` messages pass through as 400 because they're useful.
- **Auth**: every mutating request needs the `X-Tarn: 1` header, and any
  `Origin` must match the host, WebSocket handshakes included. Keep this when
  adding routes. Public paths are listed in `auth.PUBLIC_*`.
- **VNC listens on 127.0.0.1 only.** The console must go through Tarn's
  authenticated WebSocket. Don't change the `listen` address in the templates.
- **Windows template defaults**: SATA disk, e1000e NIC and `vga` video, so
  setup works without drivers. VirtIO is opt-in when `virtio-win.iso` is
  attached. Linux defaults to VirtIO everything. Keep these defaults.
- **Frontend helpers** in app.js: `h()` builds elements and `put(el, ...)`
  replaces children while skipping null. Never call `replaceChildren` with
  values that might be null, because it renders the literal text "null".
  `busy(btn, fn)` wraps async button actions.
- **Event handlers**: capture `ev.currentTarget` into a variable *before* any
  `await`, because it's null afterwards. This broke dialog-confirm buttons once.
- **Routing**: `route()` uses a sequence counter so a slow view can't paint
  over a newer one, and each view returns a stop function for its pollers.
- **Design**: an "alpine tarn" look, with glacial teal accent `#0e6e78`, a
  snowfield background, and CPU history drawn as filled ridgelines (`setRidge`).
  Colours are CSS variables in app.css with a dark theme. Lists use rows
  rather than cards. Keep labels in sentence case without all-caps eyebrows.
- **Alpine packaging**: install Python deps with apk (`py3-libvirt`,
  `py3-aiohttp`), never pip, to avoid musl wheel problems. OVMF/edk2 comes
  with `qemu-system-x86_64`, including a secure-boot build and JSON firmware
  descriptors, and libvirt picks it via `<os firmware='efi'>`. libvirtd group
  access is set in install.sh (`unix_sock_group`, `auth_unix_rw = "none"`).
- **Shared VM tweaks live in virt.py**, not in a UI: `attach_card`/`detach_card`
  (all functions of a GPU on one guest root port, `rom bar='off'`),
  `set_cpu_pinning` (one vCPU per core of the last L3 cluster from
  `host.cpu_clusters()`, emulator threads on the rest), `pci_candidates`,
  `host_report` (the Host page payload) and `create_meta`, `set_looking_glass`
  (kvmfr `qemu:commandline` + SPICE). A card may be assigned to several VMs
  (`assigned_to`, `running_on` in `pci_candidates`); `action("start")` runs
  `_check_exclusive` so two VMs never hold the GPU or `/dev/kvmfr0` at once.
  `vm_detail()["guest"]` is guest-agent info (OS, users, drives), cached 30 s.
  `attach_card` adds the fake-battery SSDT (`BATTERY_SSDT`) for NVIDIA cards
  on a laptop and `detach_card` removes it; `create_vm` accepts
  `cpu_pinning`, `card` and `looking_glass` and applies them through the
  same methods before the first start (rolling the VM back if one fails).
  domxml registers the `qemu:` namespace so Looking Glass's
  `qemu:commandline` survives XML rewrites.
- **Known limitation**: snapshots of a *running* UEFI VM can fail on some
  libvirt versions. The UI warns about this, and shutting the VM down first
  works.

## Working on this

The owner chose to build Tarn from scratch as a learning project rather than
use Cockpit or WebVirtCloud. Explain trade-offs honestly, especially around
GPU passthrough. Test changes against real libvirt rather than mocks, and
check the UI by taking screenshots.
