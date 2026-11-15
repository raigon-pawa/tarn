#!/bin/sh
# Build the demo and publish it to the gh-pages branch (GitHub Pages).
#
#   sh demo/publish.sh
#
# Run it after changing tarn/static or demo/. The site is live at
# https://raigon-pawa.github.io/tarn/ a minute or so later.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
REMOTE=$(git -C "$ROOT" remote get-url origin)
REV=$(git -C "$ROOT" rev-parse --short HEAD)
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

sh "$ROOT/demo/build.sh" "$OUT"
cd "$OUT"
git init -q -b gh-pages
git add -A
git -c user.name="$(git -C "$ROOT" config user.name)" -c user.email="$(git -C "$ROOT" config user.email)" \
	commit -q -m "Demo built from $REV"
git push -q -f "$REMOTE" gh-pages
echo "Published demo from $REV to gh-pages"
