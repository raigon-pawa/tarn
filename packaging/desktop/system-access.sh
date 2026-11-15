#!/bin/sh
# Lets the Tarn desktop user manage Wi-Fi, Bluetooth, sound and battery from
# the app's top panel without root. Run as root (install.sh --desktop runs it):
#   doas sh packaging/desktop/system-access.sh USER
# Safe to run again.
#   - Wi-Fi: wpa_supplicant gets a control socket for the netdev group, and may
#     save networks added from the app (update_config). Its config holds Wi-Fi
#     passwords, so it becomes root-only.
#   - Wi-Fi/Bluetooth radio switches: /dev/rfkill for the netdev group.
#   - Bluetooth: BlueZ and the D-Bus system bus, started at boot.
#   - Sound: PipeWire (the kiosk starts it in the user's session).
#   - Battery conservation mode: writable by the netdev group.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root: doas sh $0 USER"; exit 1; }
USER_NAME=${1:?usage: $0 USER}

addgroup "$USER_NAME" netdev 2>/dev/null || true

apk add -q bluez dbus dbus-openrc bluez-openrc \
	pipewire wireplumber pipewire-pulse pipewire-alsa

# -- Wi-Fi: control socket for netdev, and let added networks be saved
conf=/etc/wpa_supplicant/wpa_supplicant.conf
touch "$conf"
changed=0
if ! grep -q '^ctrl_interface=' "$conf"; then
	sed -i '1i ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev' "$conf"
	changed=1
fi
if ! grep -q '^update_config=' "$conf"; then
	sed -i '/^ctrl_interface=/a update_config=1' "$conf"
	changed=1
fi
chmod 600 "$conf"
if [ "$changed" = 1 ] && rc-service --exists wpa_supplicant; then
	echo "Restarting wpa_supplicant: Wi-Fi drops for a few seconds."
	rc-service wpa_supplicant restart >/dev/null
fi

# -- radio switches
cat > /etc/udev/rules.d/70-tarn-rfkill.rules <<'EOF'
# Tarn's top panel switches Wi-Fi and Bluetooth on and off as the netdev group.
KERNEL=="rfkill", GROUP="netdev", MODE="0664"
EOF
chgrp netdev /dev/rfkill && chmod 0664 /dev/rfkill

# -- Bluetooth
rc-update add dbus default >/dev/null
rc-update add bluetooth default >/dev/null
rc-service dbus start >/dev/null 2>&1 || true
rc-service bluetooth start >/dev/null 2>&1 || true

# -- battery conservation mode (Lenovo), at every boot: sysfs permissions don't persist
cat > /etc/local.d/tarn-power-access.start <<'EOF'
#!/bin/sh
# Let the netdev group switch Lenovo battery conservation mode from Tarn's panel.
for f in /sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode; do
	[ -e "$f" ] && chgrp netdev "$f" && chmod g+w "$f"
done
exit 0
EOF
chmod 755 /etc/local.d/tarn-power-access.start
rc-update add local default >/dev/null 2>&1 || true
/etc/local.d/tarn-power-access.start

echo "Done. Log out and back in (or restart tarn-kiosk) if $USER_NAME was just added to netdev."
