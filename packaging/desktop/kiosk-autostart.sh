# Start Tarn full screen after the automatic login on tty1. Quitting it (Ctrl+Q)
# leaves a normal shell there. Installed by: install.sh --autostart
if [ "$(tty)" = /dev/tty1 ] && [ -z "${WAYLAND_DISPLAY:-}" ] && command -v tarn-kiosk >/dev/null; then
	tarn-kiosk
fi
