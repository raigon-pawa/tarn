# Wayland apps (labwc, GTK) need a private runtime dir. Without elogind nothing
# creates one on Alpine, so make it at login. Installed by: install.sh --desktop
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
	XDG_RUNTIME_DIR="/tmp/$(id -u)-runtime-dir"
	mkdir -pm 0700 "$XDG_RUNTIME_DIR"
	export XDG_RUNTIME_DIR
fi
