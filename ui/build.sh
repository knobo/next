#!/usr/bin/env bash
# Builds the board's stylesheet. The CSS is checked IN (../board.css) — the board is
# stdlib-only Python in production, and the CSP (default-src 'none') lets no CDN in. Node
# is therefore a development tool here, not a runtime dependency.
#   cd ui && npm install && ./build.sh
set -euo pipefail
cd "$(dirname "$0")"
[ -d node_modules ] || { echo "run npm install first" >&2; exit 1; }
npx @tailwindcss/cli -i input.css -o ../board.css --minify
# The CSP has no font-src, img-src or connect-src. A NETWORK reference in the output
# would be blocked SILENTLY in the browser, so it is a build error, not a warning.
# data: URIs are fine: they fetch nothing (daisyUI leaves one behind for `.glass`, a class
# the board does not use).
if grep -oE "@import|url\\(\\s*[\"']?(https?:)?//" ../board.css | grep -q .; then
  echo "board.css fetches something over the network — the CSP blocks it silently:" >&2
  grep -oE "@import[^;]*|url\\(\\s*[\"']?(https?:)?//[^)]*" ../board.css >&2
  exit 1
fi
# daisyUI puts a data: URI texture in --fx-noise on :root. The CSP (default-src 'none',
# no img-src) blocks data: too, so the browser logged a violation on every page view.
# input.css overrides it — if that override ever falls out of the output, the build must
# say so here and not in somebody's browser console.
grep -q -- '--fx-noise:none' ../board.css || {
  echo "the --fx-noise override is missing from board.css — daisyUI's data: texture will" >&2
  echo "cause a CSP violation on every page view. Check the :root rule at the bottom of input.css." >&2
  exit 1
}
printf 'board.css: %s bytes\n' "$(wc -c < ../board.css)"
