#!/bin/sh
# Build both container images with rootless podman and import them into a k3s node's containerd.
# Run ON the node that will run the pods (the cluster has no registry; pods use
# imagePullPolicy: Never and are pinned to this node).
#
#   scripts/nas-build.sh [TAG]            # default 0.1.0
#
# Expects the staged context from scripts/stage-build-context.sh at $BUILD_DIR/ctx
# (default ~/build/imessage), i.e. ctx/openbubbles-relay and ctx/imessage-lab/rustpush:
#
#   scripts/stage-build-context.sh /tmp/ctx ../imessage-lab/rustpush
#   rsync -a --delete /tmp/ctx/ node:build/imessage/ctx/
#   ssh node 'sh -s' < scripts/nas-build.sh 0.1.1
#
# Needs passwordless `doas` (or edit to sudo) for the containerd import. The tarballs are
# also copied to /var/lib/rancher/k3s/agent/images/ so k3s re-imports them after a restart.
# Bump the tag in the Deployment on every build.
set -eu
ver="${1:-0.1.0}"
dir="${BUILD_DIR:-$HOME/build/imessage}"
cd "$dir"
echo "== bridge image"
podman build -t "localhost/imessage-bridge:$ver" ctx/openbubbles-relay/bridge
echo "== server image (long Rust compile)"
podman build -f ctx/openbubbles-relay/deploy/docker/server.Dockerfile -t "localhost/imessage-server:$ver" ctx
for img in imessage-bridge imessage-server; do
  echo "== import $img into k3s containerd"
  podman save "localhost/$img:$ver" -o "$dir/$img-$ver.tar"
  doas k3s ctr -n k8s.io images import "$dir/$img-$ver.tar"
  doas cp "$dir/$img-$ver.tar" /var/lib/rancher/k3s/agent/images/
done
doas k3s ctr -n k8s.io images ls -q | grep imessage
echo BUILD-DONE
