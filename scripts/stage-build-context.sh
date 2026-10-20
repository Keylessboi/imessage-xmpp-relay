#!/usr/bin/env bash
# Stage a Docker build context for deploy/docker/server.Dockerfile.
#
#   scripts/stage-build-context.sh OUTDIR [RUSTPUSH_DIR]
#
# Copies this repository and the patched rustpush checkout into OUTDIR as
#   OUTDIR/openbubbles-relay/   and   OUTDIR/imessage-lab/rustpush/
# which is the layout the crate's path dependencies expect. RUSTPUSH_DIR defaults to
# ../imessage-lab/rustpush next to this repository.
#
# Secrets never enter the context: data/ (the IDS registration), profiles.json, .env files,
# the relay secret, keystores, OMEMO stores, virtualenvs, build output and .git are excluded.
# The script then refuses to finish if anything secret-shaped slipped in.
set -euo pipefail

out="${1:?usage: stage-build-context.sh OUTDIR [RUSTPUSH_DIR]}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rustpush="${2:-$here/../imessage-lab/rustpush}"
[ -f "$rustpush/Cargo.toml" ] || { echo "rustpush checkout not found at $rustpush" >&2; exit 1; }
[ -d "$rustpush/certs/fairplay" ] || { echo "rustpush is missing certs/fairplay (it will not compile)" >&2; exit 1; }

excludes="$(mktemp)"
trap 'rm -f "$excludes"' EXIT
cat >"$excludes" <<'EOF'
target
.git
.venv
__pycache__
data
profiles.json
.env*
relay-secret
*.keystore
omemo-store*.json
aka-oracle
anisette_back
state.plist
hwconfig.plist
keystore.plist
id_cache.plist
EOF

mkdir -p "$out/openbubbles-relay" "$out/imessage-lab/rustpush"
rsync -a --delete --exclude-from="$excludes" "$here/" "$out/openbubbles-relay/"
rsync -a --delete --exclude-from="$excludes" "$rustpush/" "$out/imessage-lab/rustpush/"

bad="$(find "$out" \( -name state.plist -o -name hwconfig.plist -o -name keystore.plist -o -name profiles.json -o -name 'relay-secret' -o -name '.env*' \) -not -path '*/certs/*' | head -5)"
if [ -n "$bad" ]; then
  echo "refusing: secret-shaped files in the staged context:" >&2
  echo "$bad" >&2
  exit 1
fi
echo "staged $(du -sh "$out" | cut -f1) in $out"
