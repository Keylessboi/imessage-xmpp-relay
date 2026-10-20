# openbubbles-relay — self-hosted registration relay (NAC / validation-data oracle).
#
# Build from the REPOSITORY ROOT:
#   docker build -f deploy/docker/relay.Dockerfile -t openbubbles-relay:latest .
#
# The relay replaces https://registration-relay.beeper.com for the OpenBubbles
# client and, more importantly, replaces a physical iPhone that would otherwise
# have to stay powered on and signed in to produce NAC validation data.
# It ships no Apple code: the IMDAppleServices binary is fetched as data at
# runtime (opensource mirror) and cached in the volume.

FROM rust:1-slim-bookworm AS builder

# unicorn-engine builds QEMU from source; ring builds its own C.
# cmake/make/cc/pkg-config are the build surface, libclang is not needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates/openbubbles-relay ./crates/openbubbles-relay
COPY vendor/open-absinthe ./vendor/open-absinthe
COPY certs ./certs

RUN cargo build --release --locked -p openbubbles-relay \
 && strip target/release/openbubbles-relay

FROM debian:bookworm-slim AS runtime

# ca-certificates: the relay talks to Apple over TLS (init.ess.apple.com).
# libgcc-s1 ships libatomic, which Unicorn/QEMU's 16-byte atomics need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libgcc-s1 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /data relay

COPY --from=builder /src/target/release/openbubbles-relay /usr/local/bin/openbubbles-relay

# The emulator downloads a ~2 MB Apple binary on first use and caches it here.
ENV XDG_CACHE_HOME=/data/cache \
    PROFILES_FILE=/data/profiles.json \
    LISTEN=0.0.0.0:8787 \
    RUST_LOG=info

RUN mkdir -p /data/cache && chown -R relay:relay /data
USER relay
WORKDIR /data

# profiles.json is a secret-ish config: mount it, don't bake it in.
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/health || exit 1

ENTRYPOINT ["/usr/local/bin/openbubbles-relay"]
