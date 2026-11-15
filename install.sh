#!/bin/sh
# Tarn installer.
#
# Alpine Linux (OpenRC) is the target. Fedora (systemd) also works, so you
# can try Tarn on your current install before wiping anything.
#
#   sh install.sh                      install or upgrade
#   sh install.sh --no-pkgs            skip package installation (offline upgrades)
#   sh install.sh --desktop USER       also set up the native app on this machine's
#                                      screen for USER (Alpine): run it with tarn-kiosk
#   sh install.sh --desktop USER --autostart
#                                      and log USER in on tty1 at boot, straight into Tarn
#
# Safe to run again: it never overwrites your config, password or TLS cert.
set -eu

PREFIX=/opt/tarn
ETC=/etc/tarn
HERE=$(cd "$(dirname "$0")" && pwd)
INSTALL_PKGS=1
DESKTOP_USER=
AUTOSTART=0
while [ $# -gt 0 ]; do
	case "$1" in
	--no-pkgs) INSTALL_PKGS=0 ;;
	--desktop) DESKTOP_USER=${2:-}; shift ;;
	--autostart) AUTOSTART=1 ;;
	*) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
	esac
	shift
done

host_ips() {
	{ ip -4 -o addr show scope global 2>/dev/null || ip -4 -o addr show 2>/dev/null; } |
		awk '{print $4}' | cut -d/ -f1 | grep -v -e '^127\.' -e '^192\.168\.122\.' || true
}
say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (doas sh install.sh)"
if [ -n "$DESKTOP_USER" ]; then
	id "$DESKTOP_USER" >/dev/null 2>&1 || die "--desktop: no user named '$DESKTOP_USER'"
fi
[ "$AUTOSTART" = 1 ] && [ -z "$DESKTOP_USER" ] && die "--autostart needs --desktop USER"
[ -f "$HERE/tarn/__main__.py" ] || die "run this from the unpacked tarn folder"

if [ -f /etc/alpine-release ]; then
	DISTRO=alpine
elif [ -f /etc/fedora-release ]; then
	DISTRO=fedora
else
	die "this script supports Alpine (and Fedora for testing). See README.md for manual steps."
fi

# ---------------------------------------------------------------- packages
if [ "$INSTALL_PKGS" = 1 ]; then
	case "$DISTRO" in
	alpine)
		say "Enabling the community repository"
		if ! grep -Eq '^[^#].*/community/?$' /etc/apk/repositories; then
			sed -i -E 's@^#[[:space:]]*(.*/community/?)$@\1@' /etc/apk/repositories
		fi
		grep -Eq '^[^#].*/community/?$' /etc/apk/repositories ||
			die "couldn't enable the community repo; uncomment it in /etc/apk/repositories"
		apk update

		say "Installing QEMU/KVM, libvirt, swtpm and Python packages"
		# Only the QEMU modules a headless VM host needs, not the GTK/SDL UIs.
		apk add \
			libvirt-daemon libvirt-qemu libvirt-client \
			qemu-system-x86_64 qemu-img \
			qemu-hw-display-virtio-vga qemu-hw-display-virtio-gpu qemu-hw-display-virtio-gpu-pci \
			qemu-hw-usb-host qemu-hw-usb-redirect \
			qemu-ui-spice-core qemu-chardev-spice qemu-audio-spice \
			swtpm dnsmasq nftables iptables ip6tables bridge-utils \
			python3 py3-libvirt py3-aiohttp \
			openssl pciutils iproute2
		;;
	fedora)
		say "Installing the virtualization group and Python packages"
		dnf install -y @virtualization swtpm swtpm-tools python3-libvirt python3-aiohttp openssl pciutils
		;;
	esac
fi

# ---------------------------------------------------------------- kernel modules
say "Loading KVM and networking kernel modules"
if grep -q AuthenticAMD /proc/cpuinfo; then KVM_MOD=kvm_amd; else KVM_MOD=kvm_intel; fi
for mod in "$KVM_MOD" tun vhost_net; do
	if modprobe "$mod" 2>/dev/null; then
		note "loaded $mod"
	else
		note "could not load $mod (for $KVM_MOD: is virtualisation enabled in the BIOS?)"
	fi
	if [ "$DISTRO" = alpine ] && ! grep -qx "$mod" /etc/modules 2>/dev/null; then
		echo "$mod" >> /etc/modules
	fi
done
[ -e /dev/kvm ] || note "warning: /dev/kvm is missing; VMs will run in slow software emulation"

# ---------------------------------------------------------------- libvirt
if [ "$DISTRO" = alpine ]; then
	say "Configuring libvirtd so the 'libvirt' group can manage VMs"
	addgroup -S libvirt 2>/dev/null || true
	conf=/etc/libvirt/libvirtd.conf
	set_conf() {
		key=$1 value=$2
		if grep -Eq "^[#[:space:]]*${key}[[:space:]]*=" "$conf"; then
			sed -i -E "s@^[#[:space:]]*${key}[[:space:]]*=.*@$key = $value@" "$conf"
		else
			echo "$key = $value" >> "$conf"
		fi
	}
	set_conf unix_sock_group '"libvirt"'
	set_conf unix_sock_rw_perms '"0770"'
	# Group membership is the access control; polkit isn't running on a
	# minimal Alpine box.
	set_conf auth_unix_rw '"none"'

	rc-update add virtlogd default >/dev/null 2>&1 || true
	rc-update add libvirtd default >/dev/null
	rc-service libvirtd restart >/dev/null 2>&1 || rc-service libvirtd start
else
	say "Starting libvirt"
	systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket 2>/dev/null ||
		systemctl enable --now libvirtd
fi

# Shut running VMs down cleanly when the host powers off, instead of killing
# QEMU (a pulled plug for the guest). The default, managedsave, can't save a
# VM with a passed-through GPU, so ask the guest to shut down and wait for it.
say "Making running VMs shut down cleanly with the host"
set_kv() {  # set_kv FILE KEY VALUE: KEY="VALUE", replacing a commented-out default
	if grep -Eq "^[#[:space:]]*$2=" "$1" 2>/dev/null; then
		sed -i -E "s@^[#[:space:]]*$2=.*@$2=\"$3\"@" "$1"
	else
		echo "$2=\"$3\"" >> "$1"
	fi
}
if [ "$DISTRO" = alpine ]; then
	guests=/etc/conf.d/libvirt-guests
	set_kv $guests LIBVIRT_SHUTDOWN shutdown
	set_kv $guests LIBVIRT_MAXWAIT 120
	set_kv $guests LIBVIRT_START no  # VMs marked "start with the host" still start
	rc-update add libvirt-guests default >/dev/null 2>&1 || true
	rc-service libvirt-guests start >/dev/null 2>&1 || true  # OpenRC only stops what it started
else
	guests=/etc/sysconfig/libvirt-guests
	set_kv $guests ON_SHUTDOWN shutdown
	set_kv $guests SHUTDOWN_TIMEOUT 120
	set_kv $guests ON_BOOT ignore
	systemctl enable --now libvirt-guests >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- user and files
say "Creating the tarn service user"
if ! id tarn >/dev/null 2>&1; then
	if [ "$DISTRO" = alpine ]; then
		adduser -S -D -H -h /var/lib/tarn -s /sbin/nologin -G libvirt tarn
	else
		useradd --system --no-create-home --home-dir /var/lib/tarn --shell /sbin/nologin -g libvirt tarn
	fi
fi

say "Installing Tarn to $PREFIX"
rm -rf "$PREFIX.new"
mkdir -p "$PREFIX.new"
cp -R "$HERE/tarn" "$PREFIX.new/"
find "$PREFIX.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$PREFIX.old"
[ -d "$PREFIX" ] && mv "$PREFIX" "$PREFIX.old"
mv "$PREFIX.new" "$PREFIX"
rm -rf "$PREFIX.old"
chown -R root:root "$PREFIX"
chmod -R u=rwX,go=rX "$PREFIX"

cat > /usr/local/bin/tarn <<EOF
#!/bin/sh
PYTHONPATH=$PREFIX exec python3 -m tarn "\$@"
EOF
chmod 755 /usr/local/bin/tarn

mkdir -p "$ETC/tls"
chown root:libvirt "$ETC" "$ETC/tls"
chmod 750 "$ETC" "$ETC/tls"
if [ ! -f "$ETC/tarn.toml" ]; then
	cp "$HERE/tarn.example.toml" "$ETC/tarn.toml"
	note "wrote $ETC/tarn.toml"
fi

if [ ! -f "$ETC/tls/cert.pem" ]; then
	say "Creating a self-signed TLS certificate"
	host=$(hostname)
	san="DNS:$host,DNS:$host.local,DNS:localhost,IP:127.0.0.1"
	for ip in $(host_ips); do
		san="$san,IP:$ip"
	done
	openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
		-subj "/CN=$host" -addext "subjectAltName=$san" \
		-keyout "$ETC/tls/key.pem" -out "$ETC/tls/cert.pem" 2>/dev/null
	note "covers: $san"
fi
chown root:libvirt "$ETC/tls/key.pem" "$ETC/tls/cert.pem"
chmod 640 "$ETC/tls/key.pem"
chmod 644 "$ETC/tls/cert.pem"

if [ ! -s "$ETC/passwd" ]; then
	say "Set the admin password for the web UI"
	if [ -t 0 ]; then
		/usr/local/bin/tarn --config "$ETC/tarn.toml" passwd
	else
		note "no terminal; run 'tarn passwd' before signing in"
	fi
fi
[ -f "$ETC/passwd" ] && chown root:libvirt "$ETC/passwd" && chmod 640 "$ETC/passwd"

# ---------------------------------------------------------------- service
say "Installing and starting the service"
if [ "$DISTRO" = alpine ]; then
	install -m 755 "$HERE/packaging/openrc/tarn" /etc/init.d/tarn
	rc-update add tarn default >/dev/null
	rc-service tarn restart >/dev/null 2>&1 || rc-service tarn start
else
	install -m 644 "$HERE/packaging/systemd/tarn.service" /etc/systemd/system/tarn.service
	systemctl daemon-reload
	systemctl enable tarn >/dev/null 2>&1
	systemctl restart tarn
	if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
		firewall-cmd --permanent --add-port=8006/tcp >/dev/null && firewall-cmd --reload >/dev/null
		note "opened port 8006 in firewalld"
	fi
fi

# ---------------------------------------------------------------- desktop (optional)
if [ -n "$DESKTOP_USER" ]; then
	[ "$DISTRO" = alpine ] || die "--desktop is set up for Alpine only"
	D="$HERE/packaging/desktop"
	if [ "$INSTALL_PKGS" = 1 ]; then
		say "Installing the desktop app: GTK, labwc, seatd, Mesa, SPICE and VNC viewers"
		apk add \
			py3-gobject3 py3-cairo gtk+3.0 font-overpass \
			labwc seatd mesa-dri-gallium brightnessctl \
			eudev udev-init-scripts udev-init-scripts-openrc \
			spice-gtk gtk-vnc gst-plugins-base gst-plugins-good \
			pipewire wireplumber pipewire-pulse pipewire-alsa dbus \
			wlopm swayidle
	fi

	# libinput finds input devices through udev's database; Alpine's default mdev has none.
	if rc-service --exists udev && ! rc-update show sysinit 2>/dev/null | grep -q ' udev '; then
		say "Switching device management from mdev to udev"
		setup-devd udev
		NEED_REBOOT=1
	fi
	rc-update add seatd boot >/dev/null 2>&1 || true
	rc-service seatd start >/dev/null 2>&1 || true

	say "Giving $DESKTOP_USER access to VMs, the screen and input"
	for g in libvirt seat video input; do
		addgroup "$DESKTOP_USER" "$g" 2>/dev/null || true
	done

	say "Letting $DESKTOP_USER manage Wi-Fi, Bluetooth, sound and battery from the app's top panel"
	if [ "$INSTALL_PKGS" = 1 ]; then
		sh "$D/system-access.sh" "$DESKTOP_USER"
	else
		note "skipped with --no-pkgs; run: sh $D/system-access.sh $DESKTOP_USER"
	fi

	install -m 755 "$D/tarn-kiosk" /usr/local/bin/tarn-kiosk
	install -m 644 "$D/xdg-runtime.sh" /etc/profile.d/tarn-xdg-runtime.sh
	mkdir -p /etc/xdg/labwc
	if [ ! -f /etc/xdg/labwc/rc.xml ]; then
		install -m 644 "$D/labwc-rc.xml" /etc/xdg/labwc/rc.xml
		note "wrote /etc/xdg/labwc/rc.xml (a user's ~/.config/labwc/rc.xml takes precedence)"
	fi
	note "start it on the laptop's screen with: tarn-kiosk"

	# The laptop keeps running with its lid shut, but the panel shouldn't stay lit (OLED burn-in).
	if ls /proc/acpi/button/lid/*/state >/dev/null 2>&1; then
		say "Turning the screen off while the lid is closed"
		mkdir -p /usr/local/sbin
		install -m 755 "$HERE/packaging/lid-screen/lid-screen" /usr/local/sbin/lid-screen
		install -m 755 "$HERE/packaging/lid-screen/lid-screen.initd" /etc/init.d/lid-screen
		rc-update add lid-screen default >/dev/null
		rc-service lid-screen restart >/dev/null 2>&1 || rc-service lid-screen start
	fi

	if [ "$AUTOSTART" = 1 ]; then
		say "Logging $DESKTOP_USER in on tty1 at boot, straight into Tarn"
		# busybox getty -n -l: skip the login prompt and run this program instead.
		mkdir -p /usr/local/libexec
		cat > /usr/local/libexec/tarn-autologin <<AUTOLOGIN
#!/bin/sh
exec /bin/login -f $DESKTOP_USER
AUTOLOGIN
		chmod 755 /usr/local/libexec/tarn-autologin
		[ -e /etc/inittab.bak-tarn ] || cp -p /etc/inittab /etc/inittab.bak-tarn
		sed -i 's|^tty1::respawn:.*|tty1::respawn:/sbin/getty -n -l /usr/local/libexec/tarn-autologin 38400 tty1|' /etc/inittab
		install -m 644 "$D/kiosk-autostart.sh" /etc/profile.d/tarn-kiosk-autostart.sh
		note "tty1 logs in automatically at the next boot; /etc/inittab.bak-tarn has the original"
		note "anyone at the keyboard gets $DESKTOP_USER's session, so lock the BIOS if that matters"
	fi
fi

say "Host check"
/usr/local/bin/tarn --config "$ETC/tarn.toml" check || true

ip=$(host_ips | head -n1)
say "Done. Open https://${ip:-<this-host>}:8006"
note "The certificate is self-signed, so your browser will ask you to accept it once."
note "Logs: $( [ "$DISTRO" = alpine ] && echo /var/log/tarn/tarn.log || echo 'journalctl -u tarn')"
[ "${NEED_REBOOT:-0}" = 1 ] && note "Reboot once so udev takes over from mdev (the desktop needs it)."
exit 0
