# syntax=docker/dockerfile:1
# imessage-server-rustpush: the real Apple IDS backend (HTTP/SSE API on :8790).
#
# The crate path-depends on ../../../imessage-lab/rustpush (see its Cargo.toml), so the build
# context must hold BOTH trees side by side. Do not build from a plain checkout: stage the
# context with scripts/stage-build-context.sh, which also guarantees no secret (data/,
# profiles.json, .env, plists, keystores) ever enters it:
#
#   scripts/stage-build-context.sh /tmp/ctx
#   docker build -f /tmp/ctx/openbubbles-relay/deploy/docker/server.Dockerfile -t imessage-server:0.1.0 /tmp/ctx
#
# Build on a machine with cores to spare (rustpush + QEMU/unicorn is a long compile).
FROM rust:1-bookworm AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends cmake clang protobuf-compiler pkg-config libssl-dev libatomic1 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY imessage-lab/rustpush /src/imessage-lab/rustpush
COPY openbubbles-relay /src/openbubbles-relay
WORKDIR /src/openbubbles-relay/crates/imessage-server-rustpush
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libatomic1 libssl3 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 imessage \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin imessage \
 && mkdir /data && chown 10001:10001 /data
COPY --from=build /src/openbubbles-relay/crates/imessage-server-rustpush/target/release/imessage-server-rustpush /usr/local/bin/imessage-server-rustpush
USER 10001:10001
# /data holds the IDS registration (state.plist): mount a persistent volume there.
ENV IMESSAGE_DATA_DIR=/data \
    LISTEN=127.0.0.1:8790
ENTRYPOINT ["/usr/local/bin/imessage-server-rustpush"]
