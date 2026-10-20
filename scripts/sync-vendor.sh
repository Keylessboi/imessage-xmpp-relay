#!/usr/bin/env bash
# Re-vendor open-absinthe from the upstream checkout so the Docker build context
# stays self-contained (no path dependency reaching outside this repository).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-$HERE/../imessage-lab/bubbles/crates/open-absinthe}"
DST="$(cd "$(dirname "$0")/.." && pwd)/vendor/open-absinthe"
[ -d "$SRC" ] || { echo "no such upstream: $SRC" >&2; exit 1; }
rm -rf "$DST"; mkdir -p "$DST"
tar cf - -C "$SRC" --exclude=target --exclude=.git . | tar xf - -C "$DST"
echo "re-vendored open-absinthe from $SRC"
git -C "$(dirname "$DST")" status --short 2>/dev/null || true
