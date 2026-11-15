# Alpine + Tarn on the Lenovo Legion Pro 5

This walks through turning the Legion Pro 5 (Ryzen 9 9955HX, RTX 5070 Laptop
GPU, AMD integrated graphics) into a lean VM host: Alpine Linux on bare metal,
libvirt/KVM underneath, Tarn in the browser on top.

What you end up with: the laptop boots to a text console in a few seconds,
idles at a few hundred MB of RAM, and you manage everything from a browser on
your phone or another PC at `https://<laptop-ip>:8006`.

A few things are different from a normal laptop install, and worth knowing up
front:

- **No NVIDIA driver on Alpine.** NVIDIA's driver is built against glibc and
  Alpine uses musl, so the RTX 5070 can't be used by the host. The plan below
  parks it on `vfio-pci`, which keeps it idle and ready for passing through to a
  VM later.
- **Wi-Fi can't be bridged.** VMs use libvirt's NAT network (they get
  `192.168.122.x` addresses and reach the internet through the laptop). If you
  want VMs to appear directly on your LAN, use the Ethernet port and a bridge.
- **Secure Boot must be off.** Alpine's kernel isn't signed for it.

---

## 1. Before you wipe anything

1. **Back up** anything on the drive you'll install to. Installing Alpine erases
   that disk.
2. **Keep the Windows image.** If you made `windows.vhdx` with Disk2vhd for the
   VM migration, make sure it's on the external drive and not on the disk
   you're about to wipe.
3. **Copy Tarn somewhere safe**: put `tarn-0.1.1.tar.gz` on a USB stick or the
   external drive.
4. If the laptop has two NVMe drives, decide which one gets Alpine. The other
   can hold VM disks later (mount it at `/var/lib/libvirt/images`).

## 2. BIOS settings

Press **F2** at power-on (or use the Novo button's menu). Menu names vary by
BIOS version; look under *Configuration* and *Security*.

| Setting | Value | Why |
|---|---|---|
| AMD SVM / Virtualization Technology | **Enabled** | KVM needs it (`/dev/kvm`) |
| IOMMU / AMD-Vi (if listed) | **Enabled** | Only needed for PCI passthrough |
| Secure Boot | **Disabled** | Alpine's kernel isn't signed |
| GPU mode / Hybrid mode | **Hybrid** (not dGPU-only) | The AMD iGPU must drive the screen, since Alpine can't use the RTX 5070 |

If the GPU-mode switch isn't in the BIOS on your model, it's the Hybrid Mode
toggle in Legion Space under Windows; set it before wiping Windows.

## 3. Install Alpine

1. Download the **x86_64 Extended** ISO from alpinelinux.org/downloads (it
   carries more drivers and firmware than Standard) and write it to a USB stick
   with Fedora Media Writer, `dd`, or Ventoy.
2. Plug in **Ethernet** for the install; it just works. Wi-Fi needs firmware
   you'll add afterwards.
3. Boot the stick with **F12**, log in as `root` (no password), and run:

   ```sh
   setup-alpine
   ```

   Suggested answers: hostname `legion`; interface `eth0` with `dhcp`; your
   timezone; the fastest mirror (`f`); create a user and allow it `doas`; SSH
   server `openssh`; disk: the NVMe you chose, mode **sys**. It uses GRUB on
   UEFI machines.
4. Reboot, remove the stick, and log in.
5. From another machine you can now `ssh` in, which is nicer than the laptop's
   console for the rest of this.

## 4. Firmware and Wi-Fi

Enable the community repository and add firmware for the AMD graphics and CPU
microcode:

```sh
doas sed -i -E 's@^#(.*/community)$@\1@' /etc/apk/repositories
doas apk update
doas apk add linux-firmware-amdgpu linux-firmware-amd-ucode pciutils
```

Find out which Wi-Fi and Ethernet chips this unit has:

```sh
lspci -nn | grep -Ei 'network|ethernet'
```

Then add the matching firmware: `linux-firmware-mediatek` for MediaTek
(MT79xx), `linux-firmware-rtw89` for Realtek Wi-Fi 6E/7, and
`linux-firmware-rtl_nic` for the Realtek Ethernet port. Reboot, then run
`setup-interfaces` again and pick the `wlan0` interface; it asks for the
network name and password.

## 5. Kernel command line

Edit `/etc/default/grub` and add to `GRUB_CMDLINE_LINUX_DEFAULT`:

- `iommu=pt` for PCI passthrough (the AMD IOMMU is on by default once it's
  enabled in the BIOS; there's no `amd_iommu=on` to add)
- `consoleblank=120` so the screen goes dark after two minutes instead of
  burning power all day

```sh
doas vi /etc/default/grub
doas grub-mkconfig -o /boot/grub/grub.cfg
```

## 6. Install Tarn

Copy the tarball over (USB stick or `scp`), then:

```sh
tar xzf tarn-0.1.1.tar.gz
cd tarn
doas sh install.sh
```

It installs QEMU/KVM, libvirt, swtpm and the Python packages from Alpine's
repos, loads `kvm_amd`, `tun` and `vhost_net` (and adds them to
`/etc/modules`), sets up the service user, TLS certificate and admin password,
and starts Tarn. At the end it prints the host checklist, which should show
`OK` for KVM. Open the address it prints from any browser on your network.

The **Host** page in Tarn repeats the checklist, with the exact command to fix
anything that's red.

## 7. Make the laptop a good server

### Keep it running with the lid closed

Close the lid and keep pinging the laptop from another machine. A bare Alpine
install usually ignores the lid. If it suspends:

- with **elogind** installed: set `HandleLidSwitch=ignore` and
  `HandleLidSwitchExternalPower=ignore` in `/etc/elogind/logind.conf`;
- with **acpid**: remove or edit the lid handler under `/etc/acpi/`.

The screen, though, stays lit with the lid shut, which burns in an OLED
panel. `install.sh --desktop` adds a `lid-screen` service that powers the
panel off (DPMS) while the lid is closed and back on when it opens, whether
the text console or the Tarn kiosk is showing. The kiosk also powers the
screen off after 10 idle minutes (`TARN_IDLE_SECONDS` changes that); any key
or touchpad press wakes it.

### Battery and power profile

A laptop that lives on its charger is happier kept around 60–80%. Many Legion
models expose a conservation-mode switch and a platform power profile; Tarn's
Host page shows both if the files exist. To set them at every boot:

```sh
doas tee /etc/local.d/legion.start >/dev/null <<'EOF'
#!/bin/sh
f=$(ls /sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode 2>/dev/null | head -n1)
[ -n "$f" ] && echo 1 > "$f"
[ -w /sys/firmware/acpi/platform_profile ] && echo balanced > /sys/firmware/acpi/platform_profile
exit 0
EOF
doas chmod +x /etc/local.d/legion.start
doas rc-update add local default
```

Check the valid profile names with `cat /sys/firmware/acpi/platform_profile_choices`
and use `performance` if you run heavy VMs. Fan control tools like
LenovoLegionLinux need an out-of-tree kernel module; skip them unless thermals
become a problem.

### Shut VMs down cleanly with the host

`poweroff` with a VM running kills QEMU, which is a pulled plug for Windows.
The `libvirt-guests` service asks running VMs to shut down first. Its default
(`managedsave`, saving VM memory to disk) fails for a VM with a
passed-through GPU, so `install.sh` sets it to a clean shutdown with a
two-minute limit. To do it by hand:

```sh
doas sed -i -E 's/^#?LIBVIRT_SHUTDOWN=.*/LIBVIRT_SHUTDOWN="shutdown"/' /etc/conf.d/libvirt-guests
doas rc-update add libvirt-guests default && doas rc-service libvirt-guests start
```

Tarn's Host page flags it when this isn't set up.

### Park the RTX 5070 on vfio-pci

With no driver bound, the dGPU may never power down. Binding it to `vfio-pci`
keeps it idle and is also step one of passing it through to a VM.

Find its IDs (the GPU and its HDMI audio function):

```sh
lspci -nn -d 10de:
```

Your RTX 5070 Laptop GPU should be `10de:2d58`; the audio function is the other
`10de:` entry. Then:

```sh
doas tee /etc/modprobe.d/vfio.conf >/dev/null <<'EOF'
blacklist nouveau
options vfio-pci ids=10de:2d58,10de:XXXX
EOF
echo vfio-pci | doas tee -a /etc/modules
doas reboot
```

Replace `XXXX` with the audio function's device ID. After the reboot, Tarn's
Host page should list the GPU with driver `vfio-pci`, and its power state
should read `suspended` when idle. If `lspci -k -d 10de:` still shows
`nouveau`, rebuild the initramfs so it picks up the blacklist
(`doas mkinitfs`) and reboot again.

Actually passing a *laptop* GPU into a Windows VM is an experiment, not a
recipe: it depends on how the HDMI port is wired, whether the GPU's video BIOS
is reachable, and the IOMMU grouping (the IOMMU table on the Host page shows
whether the GPU sits in a clean group). Tarn's Hardware tab can attach it once
you're ready to try.

### Remote access

Keep port 8006 on your LAN. To reach Tarn from outside, use a VPN such as
Tailscale (`apk add tailscale`) or an SSH tunnel:

```sh
ssh -L 8006:localhost:8006 you@legion
# then browse to https://localhost:8006
```

### Tarn on the laptop's own screen

The web UI is for other devices. On the laptop itself, Tarn has a native app:

```sh
doas sh install.sh --desktop "$USER"      # add --autostart to boot straight into it
tarn-kiosk                                 # from the text console (tty1)
```

`--desktop` installs GTK, the labwc compositor, seatd, Mesa and the SPICE/VNC
viewers, switches device management from mdev to udev (labwc's input library
needs udev; reboot once afterwards), and adds you to the `libvirt`, `seat`,
`video` and `input` groups. In the app: Ctrl+Q quits, F11 toggles full screen,
Super+L starts Looking Glass, and Fn+F5/F6 change the brightness. "Host
details" on the overview shows the readiness checklist, GPU state,
temperatures and storage (with deletion of unused ISOs and disks).

The bar along the top works like a desktop's system tray: Wi-Fi (join,
disconnect, forget networks), Bluetooth (pair headphones, mice and the like),
volume and output device, brightness, battery and conservation mode. It
runs as your user; `install.sh --desktop` grants the access it needs
(`packaging/desktop/system-access.sh`).

A VM's page has a Looking Glass switch that adds the `/dev/kvmfr0` shared
memory and a SPICE display, so a second gaming VM (say Windows 11 next to
Windows 10) needs no hand-edited XML. One graphics card can be attached to
several VMs; Tarn refuses to start a second one while the card or the shared
memory is in use.

`--autostart` logs you in on tty1 at boot and starts the app. Anyone with the
laptop in hand then has your session (though `doas` still asks for your
password), so set a BIOS password if that matters.

## 8. Bring the Windows install over

If the Disk2vhd image exists, convert it on the laptop (it takes a while):

```sh
doas qemu-img convert -p -O qcow2 /media/usb/windows.vhdx /var/lib/libvirt/images/windows.qcow2
```

In Tarn: **Create VM → Windows**, name it, choose **Use an existing image**
and pick `windows.qcow2`, no installer ISO. Keep UEFI, Secure Boot and TPM
ticked. Leave **Use VirtIO disk and network** unticked unless you installed
the VirtIO drivers in Windows before imaging; SATA always boots, and you can
switch to VirtIO later in the XML tab once the drivers are in.

Expect Windows to spend a few minutes detecting new hardware on first boot. If
BitLocker was on, have the recovery key ready. Activation may need the
troubleshooter if the licence was tied to the laptop's hardware.

## 9. When something's wrong

| Symptom | Look at |
|---|---|
| Tarn page won't load | `rc-service tarn status`, `/var/log/tarn/tarn.log` |
| "No KVM acceleration" | BIOS SVM setting, then `doas modprobe kvm_amd`, then `doas rc-service libvirtd restart` |
| VM won't start | the error in Tarn's toast, and `/var/log/libvirt/qemu/<vm>.log` |
| VM has no network | `virsh net-list --all` (the `default` network must be active); `apk add dnsmasq nftables iptables` |
| Console stays black | the VM may be at a text prompt; press a key. Windows guests use the `vga` adapter until VirtIO drivers are installed |
| Forgot the password | `doas tarn passwd` (takes effect at the next sign-in) |

`tarn check` prints the readiness checklist from the command line.
