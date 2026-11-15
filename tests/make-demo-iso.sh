#!/bin/sh
# Build a tiny bootable GRUB ISO (about 12 MB) for testing the console without
# downloading a real installer. Needs grub-mkrescue, xorriso and mtools
# (Fedora: grub2-tools-extra xorriso mtools; Debian/Ubuntu: grub-common
# grub-pc-bin grub-efi-amd64-bin xorriso mtools).
set -eu
out=${1:-/tmp/alpine-demo.iso}
dir=$(mktemp -d)
mkdir -p "$dir/boot/grub"
cat > "$dir/boot/grub/grub.cfg" <<'CFG'
set timeout=-1
menuentry "Install Alpine Linux (demo ISO built for testing Tarn)" { echo "This is a test ISO."; sleep 5 }
menuentry "Boot from first hard disk" { exit }
menuentry "Memory test" { echo "nothing here"; sleep 3 }
CFG
mkrescue=$(command -v grub-mkrescue || command -v grub2-mkrescue)
"$mkrescue" -o "$out" "$dir" >/dev/null 2>&1
rm -rf "$dir"
echo "wrote $out"
