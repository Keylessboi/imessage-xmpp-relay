# Multi-stage build for the headless iMessage server.
#
# Build context is THIS directory (the crate is standalone: no workspace, no
# imessage-lab path dependencies):
#
#   docker build -t imessage-server crates/imessage-server
#
# This builds the STUB backend binary. The rustpush-backed binary
# (crates/imessage-server-rustpush) needs git submodules that are not vendored
# here; see ../imessage-server-rustpush/README.md.

# --------------------------------------------------------------- builder ----
FROM rust:1-bookworm AS builder

WORKDIR /build

# Manifests first so the dependency layer is cached across source edits.
COPY Cargo.toml ./
COPY src ./src
COPY tests ./tests

RUN cargo build --release --bin imessage-server

# --------------------------------------------------------------- runtime ----
FROM debian:bookworm-slim AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 imessage \
 && useradd --uid 10001 --gid 10001 --home-dir /data --shell /usr/sbin/nologin imessage \
 && mkdir -p /data \
 && chown imessage:imessage /data

COPY --from=builder /build/target/release/imessage-server /usr/local/bin/imessage-server

ENV IMESSAGE_DATA_DIR=/data \
    LISTEN=0.0.0.0:8790 \
    RUST_LOG=info

VOLUME ["/data"]
EXPOSE 8790

USER imessage

# /data must be bind-mounted or a named volume: it holds hwconfig.plist,
# gsa.plist, state.plist and the keystore.
ENTRYPOINT ["/usr/local/bin/imessage-server"]
