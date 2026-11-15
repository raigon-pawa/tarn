#!/bin/sh
# Build the static Tarn demo for GitHub Pages.
#
#   sh demo/build.sh [OUT_DIR]      (default: _site)
#
# Copies the real web UI from tarn/static and swaps the server for
# demo/mock-api.js, so the demo always matches the shipped UI.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-"$ROOT/_site"}
SRC="$ROOT/tarn/static"
DEMO="$ROOT/demo"

rm -rf "$OUT"
mkdir -p "$OUT/static/vendor"
cp "$SRC/app.css" "$OUT/static/"
cp -R "$SRC/vendor/fonts" "$OUT/static/vendor/"
cp "$DEMO/mock-api.js" "$DEMO/console.html" "$OUT/"
: > "$OUT/.nojekyll"

# app.js: the console iframe points at the demo console page.
sed 's#`/console?uuid=#`console.html?uuid=#' "$SRC/app.js" > "$OUT/static/app.js"
grep -q '`console.html?uuid=' "$OUT/static/app.js" || { echo "build: console path not found in app.js" >&2; exit 1; }

# index.html: relative asset paths (Pages serves under /tarn/), the mock
# before app.js, and the demo banner at the top of the page.
awk -v banner="$DEMO/banner.html" '
  { gsub("\"/static/", "\"static/") }
  /<title>/ { sub("<title>Tarn</title>", "<title>Tarn demo</title>") }
  /<link rel="stylesheet"/ { print "  <script src=\"mock-api.js\"></script>" }
  { print }
  /<body>/ { while ((getline l < banner) > 0) print l }
' "$SRC/index.html" > "$OUT/index.html"
grep -q 'src="mock-api.js"' "$OUT/index.html" || { echo "build: failed to inject mock-api.js" >&2; exit 1; }
grep -q 'demo-banner' "$OUT/index.html" || { echo "build: failed to inject banner" >&2; exit 1; }
if grep -q '"/static/' "$OUT/index.html"; then echo "build: absolute /static/ path left in index.html" >&2; exit 1; fi

echo "Built demo in $OUT"
