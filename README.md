# openbubbles-relay

Self-hosted iMessage infrastructure that does not need an iPhone online.

Repository: [`imessage-xmpp-relay`](https://github.com/Keylessboi/imessage-xmpp-relay).
The crates and services keep their original names (`openbubbles-relay`,
`imessage-server`, `imessage-server-rustpush`, `bridge`). Licensed under
[AGPL-3.0](LICENSE).

The starting problem: an iPhone X was being kept powered on and signed in for exactly
one job — signing Apple's **NAC** (network authentication chip) challenge so another
device could hold an iMessage identity. That job is a signing oracle, nothing more, and
it can be done on a server. This project now registers a phone number as an Apple IDS
identity and keeps it alive from a headless Linux server. The phone is needed **once**
for registration; after that the server boots from saved IDS state with no Apple ID
login and no phone attached.

As of 2026-10-19:

- **+15551234567 is registered on Apple IDS** as a `P:` user, with no Apple ID login.
- **Outbound** — an iMessage sent from +15551234567 to +15557654321 was **delivered**
  (Apple's own receipt); `POST /im/send` is live.
- **Inbound** — a reply from that peer reached the relay, was decrypted, and was pushed to
  the XMPP side (`delivered iMessage to XMPP`). A message typed in the XMPP client then
  went back out as a correctly **threaded** iMessage (`reply_to` set to the inbound GUID).
  Replayed deliveries were deduplicated rather than shown twice.
- FaceTime join links are minted from the same phone-number-only identity.
- The phone is needed for **registration only**: with it powered off, the server boots from
  saved IDS state and stays registered.

Still **unproven**, and flagged as such throughout this README: a live **group** send
(group chat works in tests but needs an inbound group thread to exercise on the wire),
OMEMO 2 against a real client, and the audio-call bridge. The iOS-side pieces that would
make those work are described below.

## Components

| Path | What it is | State |
|---|---|---|
| [crates/openbubbles-relay](crates/openbubbles-relay/src/main.rs) | HTTP relay: hardware profile -> live Apple validation data, plus `emit-hwconfig` | **Working live**, but a *synthetic* profile is not enough to register a number — see [the synthetic-identity trap](#the-synthetic-identity-trap-the-most-useful-lesson-here) |
| [crates/imessage-server](crates/imessage-server/README.md) | Headless iMessage server; frozen HTTP/SSE API on :8790 | Builds, 19 tests, stub backend |
| [crates/imessage-server-rustpush](crates/imessage-server-rustpush/README.md) | The real Apple IDS backend (rustpush) | **+15551234567 registered; `/im/send` and `/im/facetime-link` live**; no Apple ID login |
| [bridge](bridge/README.md) | XMPP <-> iMessage bridge, client account or XEP-0114 component | 144 tests green; component mode **proven against ejabberd 26.9.0**; OMEMO (OMEMO 2 + legacy, per-peer identities, bridge-side PEP responder) implemented, not client-tested |
| [vendor/open-absinthe](vendor/open-absinthe/) | Emulated Apple NAC binary (Unicorn) — **vendored**, see below | AGPL-3.0, in-tree |
| [deploy/docker](deploy/docker/relay.Dockerfile) | Container image for the relay | Written, not built |
| [deploy/docker/server.Dockerfile](deploy/docker/server.Dockerfile) | Container image for `imessage-server-rustpush` (the :8790 server). Stage the build context with [scripts/stage-build-context.sh](scripts/stage-build-context.sh), which keeps every secret out of it | Built on a homelab node, 2026-10-20 |
| [bridge/Dockerfile](bridge/Dockerfile) | Container image for the XMPP bridge; secrets can come from files (`COMPONENT_SECRET_FILE`, `XMPP_PASSWORD_FILE`) | Built on a homelab node, 2026-10-20 |
| [deploy/gitops](deploy/gitops/README.md) | Example Kubernetes manifests for an ArgoCD-managed cluster | `kubectl kustomize` renders clean; **not applied** |
| [aka-oracle](aka-oracle/) | Android helper app: EAP-AKA over the real SIM | Working (SIM AKA proven) |

## How it fits together

At runtime there is no phone and no Apple ID in the loop:

```
  XMPP client                        one iMessage/SMS peer per JID
  (or XEP-0114 component)            in component mode
        │                                   ▲
        ▼                                   │
  ┌─────────────────────────────────────────────┐
  │ bridge (client mode or XEP-0114 component)  │
  └───────────────────┬─────────────────────────┘
                      │ HTTP + SSE
                      ▼
  ┌─────────────────────────────────────────────┐
  │ imessage-server-rustpush  (127.0.0.1:8790)  │── APS ──► Apple IDS
  └─────────────────────────────────────────────┘
```

Registration is a **separate, one-time** path, and it needs the phone:

```
  jailbroken iPhone X (iPhone10,6)          adb handset with the MVNO carrier SIM
  JJTech ValidationRelay                    REG-REQ / REG-RESP signature
        │ websocket                               │
        ▼                                         │
  self-hosted registration-relay (Docker)         │
        │ real NAC / validation data              │
        ▼                                         ▼
  imessage-server-rustpush  ── POST /im/register-phone ──►  Apple IDS
        │
        └─ saves P:+15551234567; later boots "reusing saved IDS state"
```

`crates/openbubbles-relay` + `open-absinthe` are **not** in that registration path.
They emulate a Mac NAC and produce valid validation data from a *synthetic* profile;
Apple accepts that for APS but rejects it when a phone number or an iCloud account is
attached. The real path above uses validation data from a real iPhone. The relay is
kept for the validation-data experiments and `emit-hwconfig`.

## Registering the number (one-time, and why it needs the phone)

The number is minted from three independent pieces: an iPhone-class device identity,
an APNs token, and a carrier signature. None of them is an Apple ID.

1. **Self-host beeper/registration-relay.** The container is
   `ghcr.io/beeper/registration-relay`. Leave `REGISTRATION_RELAY_VALIDATE_AUTH_URL`
   unset: self-hosted, that means no Beeper account or access token is needed. (The
   hosted `registration-relay.beeper.com` wraps the relay in Beeper auth and answers
   **401** to a phone's code.) The relay's own secret must stay constant across
   restarts — the phone's registration code is tied to it — and it lives outside the
   repo in `data/relay-secret` (mode 0600).
2. **Point the iPhone X at it.** The phone runs JJTech's ValidationRelay (jailbroken,
   iPhone10,6) with Relay=**Custom**, `ws://<lan-ip>:8000/api/v1/provider`, and shows a
   Registration Code. Plain `ws://` to a LAN IP works, so no TLS or public exposure is
   required.
3. **Give the server that code.** `crates/imessage-server-rustpush` builds rustpush's
   `RelayConfig` from `REGISTRATION_RELAY_URL` + `REGISTRATION_RELAY_CODE`. The device
   identity is then a real iPhone-class one (`iPhone10,6`), not the synthetic Mac.
4. **Send one carrier `REG-REQ`.** The the MVNO carrier SIM in the adb handset answers with
   `REG-RESP`; [scripts/activate-number.sh](scripts/activate-number.sh) drives this and
   posts the signature to `POST /im/register-phone`. That call mints the
   `P:+15551234567` IDS user. Do not spam the shortcode: one fresh signature is
   enough, and the APNs token is now persisted so a signature does not go stale
   between boots.
5. **The registration is persisted.** `data/state.plist` holds the `P:` user and its
   `com.apple.madrid` registration. A restart with the phone unplugged logs
   `reusing saved IDS state (no Apple ID login needed)` and comes up
   `registered: true`. Re-registration (only if Apple invalidates it) is the only
   reason to get the phone out again.

## What builds from a bare clone

Two of the three Rust crates build from a fresh clone without any extra source
checkout (a C toolchain is needed, because the NAC emulator links libunicorn and
libatomic):

```bash
cargo build --workspace        # crates/openbubbles-relay + crates/imessage-server
cargo test  --workspace
```

`crates/imessage-server-rustpush` — the crate that actually talks to Apple's IDS —
**does not build from a bare clone.** It is deliberately not a workspace member
because it depends on an external rustpush checkout at
`../imessage-lab/rustpush` (a sibling of this repository, i.e.
`../../../imessage-lab/rustpush` from inside the crate). To build it:

```bash
git clone https://github.com/JJTech0130/rustpush ../imessage-lab/rustpush
cd ../imessage-lab/rustpush && git submodule update --init --recursive
cd -                                   # back to this repository
cd crates/imessage-server-rustpush && cargo build --release
```

rustpush needs its six submodules (plus one nested) and its FairPlay certs
(`certs/fairplay/*.crt|*.pem`) before it compiles — see
[crates/imessage-server-rustpush/README.md](crates/imessage-server-rustpush/README.md)
for that and for the `[patch.crates-io] quinn` entry this crate must carry.
If your rustpush checkout lives elsewhere, edit the four path dependencies in
that crate's `Cargo.toml`.

## Configuration

Everything is read from the environment; nothing has a real default that would
work against a live account. Copy [.env.example](.env.example) to `.env` and fill
in your own values — it lists every variable the code reads, grouped by service.
The values in this repository are placeholders (`+15551234567`,
`you@example.org`, `xmpp.example.org`), never real identities.

## Quick start (validation-data relay only)

This path proves the NAC emulator works; on its own it **cannot register a number**
(see the trap below).

```bash
cargo build --release -p openbubbles-relay
PROFILES_FILE=./profiles.json ./target/release/openbubbles-relay
# POST /api/v1/bridge/get-validation-data with 'Authorization: Bearer <code>'
```

Generate the matching rustpush hardware config for the iMessage server:

```bash
openbubbles-relay emit-hwconfig TESTCODE /data/hwconfig.plist   # written mode 0600
```

Container:

```bash
docker build -f deploy/docker/relay.Dockerfile -t openbubbles-relay:0.1.0 .
```

## Things that will bite you

- **A synthetic hardware profile is not a real device identity.** This is the most
  expensive lesson in the repo. Apple's IDS accepted the synthetic Mac profile far
  enough to issue a real APNs token and reach `authenticate_phone`, then rejected the
  registration at `id-register` with **status 6004** ("this device class/identity
  cannot register this phone number") and at iCloud setup with
  **`ICLOUD_UNSUPPORTED_DEVICE`**. The **same** `REG-RESP` signature that got 6004 was
  accepted immediately once the config was a real `iPhone10,6` — no new SMS, no rate
  limit. If registration fails with 6004, look at the device identity first.
- **Registration is a one-time dependency on the phone, and the state is precious.**
  `data/state.plist` is the registration. Delete it (or lose `data/`) and the phone has
  to register again. `data/` is gitignored and must stay that way.
- **Do not lose the registration-relay secret.** It is tied to the phone's
  registration code; a new secret means a new code and a re-registration. Keep
  `data/relay-secret` constant and mode 0600.
- **If you return to the Apple ID login path, use one header set for the whole 2FA
  exchange.** The challenge and the code submission must both use the akd/Android
  identity. Mixing the legacy header set into the code POST makes Apple answer HTTP
  **401 with an empty body**, which looks exactly like a bad code. That bug cost two
  good codes.
- **`vendor/open-absinthe` is a copy.** The Docker build context must be self-contained,
  so the crate is vendored rather than referenced by a path outside the repo. Refresh it
  with [scripts/sync-vendor.sh](scripts/sync-vendor.sh) after upstream changes.
- **`imessage-lab/rustpush` needs its git submodules.** Six of them, plus a nested one,
  and two are pinned to SSH URLs that fail without a GitHub key. If `cargo metadata` in
  that tree fails, see the notes in [crates/imessage-server-rustpush](crates/imessage-server-rustpush/README.md).
- **The omnisette `panic!` fix is uncommitted.** `imessage-lab/rustpush`'s
  `third_party/apple-private-apis/omnisette` working tree has two local fixes
  (`remote_anisette_v3.rs` returns `Err` instead of `panic!()`, and `lib.rs`
  honours `OMNISETTE_URL_V3`). A submodule re-checkout or re-init will **silently discard
  them**, and the next anisette error will take the login task down instead of returning an
  error. `scripts/sync-vendor.sh` only refreshes `vendor/open-absinthe` and does not touch
  omnisette, but re-verify both hunks after any submodule operation (see the rustpush README).
- **FairPlay certs.** `rustpush/src/activation.rs` `include_bytes!`s
  `certs/fairplay/*.crt|.pem`, which are absent from a fresh checkout and must be copied
  in from another copy of the tree. Without them rustpush does not compile at all.
- **`profiles.json` is a secret** — it maps a bearer code to a device identity. It is
  gitignored and mounted, never baked into an image.
- **Apple's NAC binary is fetched as data**, never redistributed: ~2 MB from a community
  mirror on first use, sha1-pinned, cached under `$XDG_CACHE_HOME/open-absinthe`.
- **The number is fixed by the carrier signature.** To hold a *different* number, that
  number's SIM has to answer its own `REG-REQ`. The relay cannot port a number.
- **Print-previous-commit hygiene.** Commit from this directory only. The
  `.gitignore` here already excludes `.env`, `data/`, `profiles.json`,
  `relay-secret` and every `*.plist`; do not weaken it, and never commit
  anything under `data/`.
- **The iPhone X battery is end-of-life.** It works for registration, but the pack is at
  the swollen/explode end of its life: do not leave it charging unattended, and plan a
  replacement before the next re-registration.

## Status, honestly

**As of 2026-10-19: the number is registered and outbound works. Inbound is still
unproven.**

### The central result: registered, with no Apple ID login

`+15551234567` is a real Apple IDS identity, a `P:` user (`tel:+15551234567`) minted
by `POST /im/register-phone` from a carrier `REG-REQ`/`REG-RESP` signature. **No Apple
ID was logged in for any of it**, and none is needed at runtime.

The phone is a **one-time** dependency. A jailbroken iPhone X (iPhone10,6) running
JJTech's ValidationRelay connects over a websocket to a self-hosted
`ghcr.io/beeper/registration-relay` (Docker; `REGISTRATION_RELAY_VALIDATE_AUTH_URL`
unset, so no Beeper account is involved). The server builds rustpush's `RelayConfig`
from `REGISTRATION_RELAY_URL` + `REGISTRATION_RELAY_CODE`, so its device identity is
iPhone-class. The carrier `REG-RESP` signature comes from the the MVNO carrier SIM in the adb
handset, and `POST /im/register-phone` mints the `P:` user.

After that, the registration is persisted. Restarting the service **with the phone
unplugged** logs:

```
reusing saved IDS state (no Apple ID login needed) users=1
IDS registered handles=["tel:+15551234567"]
```

and `/im/status` reports `registered: true, login_error: null`. Validation data is
fetched again only if Apple invalidates the registration — i.e. only re-registration
needs the phone. This is the path that works; the synthetic-Mac path never did.

### The synthetic-identity trap (the most useful lesson here)

The earlier plan was to emulate Apple's NAC for a *synthetic* Mac hardware profile and
never touch a real device. That gets further than it looks, and then fails at exactly
the wrong moment:

- Apple's IDS **accepts** the synthetic profile for APNs and returns a real 32-byte
  token.
- `authenticate_phone` also gets through.
- `id-register` then rejects it with **status 6004**: "this device class/identity
  cannot register this phone number".
- iCloud setup rejects the same identity with **`ICLOUD_UNSUPPORTED_DEVICE`**
  ("Cannot create another iCloud account with this device").
- The **same `REG-RESP` signature** that failed with the synthetic Mac identity was
  accepted **immediately** — no new SMS — once the config was a real **iPhone10,6** from
  the registration relay.

So 6004 was not a rate limit or a bad signature; it was the device identity. A
synthetic profile can produce validation data Apple will accept for APS. It cannot
register a number or set up iCloud.

### Two real bugs that are fixed (history)

Neither is current: the working deployment does not log in to an Apple ID. They are
recorded because they were expensive, and because anyone who returns to that path will
meet them.

**SMS 2FA returned HTTP 401 with an empty body.** The challenge
(`PUT /auth/verify/phone`, `mode:"sms"`) was created with the **akd/Android** header
set, but the security code was posted with the **legacy** set. Apple answered 401 with
an empty body, so two perfectly good codes looked "bad". Fixed in the external
`icloud-auth` dependency by carrying the akd identity through `verify_sms_2fa`
(`imessage-lab/rustpush/third_party/apple-private-apis/icloud-auth/src/client.rs`;
outside this repo, a rustpush dependency). Apple now answers **HTTP 200** with
`x-apple-ak-auth-type: hsa2` and `x-apple-ak-action: complete`.

**`-80045` was a client-info / OS mismatch, not an Apple ban.** GrandSlam rejects an
anisette machine ID whose client OS does not match the client-info it is sent with.
Public anisette-v3 servers build the ADI machine ID with Apple's Android library, so
the client-info has to claim Android; Mac/Windows/iPhone strings produce `-80045`,
Android reaches the account check. With an Android client-info string the SRP init
passed. That is solved history and is not a current blocker.

The Apple ID login did get past 2FA after that fix, but with the **synthetic** hardware
identity it was still rejected at iCloud setup with `ICLOUD_UNSUPPORTED_DEVICE`. The
phone-number path above sidesteps that step entirely, so the deployment no longer
depends on the Apple ID login completing.

### Proven live

- **+15551234567 is registered** with Apple IDS, with `registered: true` and
  `handles: ["tel:+15551234567"]`, and survives a restart with no phone attached.
- **Outbound iMessage is delivered.** `POST /im/send` to +15557654321 returned
  `{"ok":true,"guid":"..."}`, and Apple's APS logged the receipt
  `recieved [tel:+15557654321] 'delivered'` with `sender: "tel:+15551234567"`.
- **`POST /im/facetime-link`** returns a real join link minted by the phone-only
  identity, with no Apple ID. The link is persisted and reused
  (`state.plist.backend_state.facetime`), so repeat calls return the same link.
  See [crates/imessage-server-rustpush/README.md](crates/imessage-server-rustpush/README.md).
- **Group chats map to XEP-0045 rooms in component mode** (create, rename, add/remove,
  leave, invites, history); unit-tested against a fake component and real slixmpp
  parsing, never run against real Apple or a real XMPP client. See
  [bridge/README.md](bridge/README.md#group-chats-as-rooms-component-mode).
- **Group replies are implemented.** `/im/send` accepts an optional `chat_guid`;
  participants and group id are learned from inbound messages and persisted. Not yet
  live-verified, because no inbound group message has ever arrived.
- **XMPP component mode was proven end to end against a real ejabberd 26.9.0**:
  handshake accepted, presence `subscribed`+`available`, an XMPP message became a
  delivered iMessage, and a non-owner JID was denied. Client mode (single bot account,
  prefixed text) also exists. OMEMO is implemented in the bridge (component mode answers
  the PEP lookups itself) but has **not** been exercised against a real client. See [bridge/README.md](bridge/README.md).
- **The bridge suite is green**: 144 tests, including component mode and OMEMO (OMEMO 2 + legacy).
  `imessage-server` has 19 tests and a stub backend.
- **The validation-data relay still works live** against Apple from a synthetic
  profile, and `emit-hwconfig` still produces a rustpush hardware config. That is a
  real capability; it is just not sufficient for registration (see the trap).

### Not proven / still open

- **Inbound iMessage -> XMPP has never been observed.** No message has arrived at
  +15551234567 yet, so the receive path, the SSE feed and the bridge's XMPP delivery
  are untested against real traffic.
- **Group reply is untested live.** There is no real inbound group message, so there
  is no real `gid` to reply into. An inbound group text to the number is the
  acceptance test.
- **Deployed to a k3s cluster on 2026-10-20** (server + bridge in one pod, the IDS registration on
  an encrypted NAS volume, the bridge linked to an ejabberd component host over XEP-0114). The
  server restored the registration (`registered: true`) and connected to Apple's push service from
  the cluster; federated message traffic through the new path has **not** been exercised yet.
  The manifests live in the cluster's GitOps repo, not here. Only the `registration-relay`
  container (re-registration) still runs on the development host, and the two systemd units there
  are stopped, because the registration must be live in exactly one place.
  [deploy/gitops](deploy/gitops/README.md) is the older example for the validation-data relay only.
- **Moving a service off a workstation hides requirements.** The server insists on an Apple ID at
  boot (a placeholder `gsa.plist` satisfies it; the phone-only number never logs in), and the APNs
  bag is fetched over plain HTTP on port 80, which a default-deny egress policy blocks.
- **OMEMO 2 has not been used against a real XMPP client** (Conversations/Dino/Gajim)
  or a live server; it is proven only between in-process sessions.
- **The iPhone X battery is end-of-life.** Registration works today, but a replacement
  is prudent before the next re-registration.
- **The Apple ID login path remains unproven end to end.** The `-80045` and 2FA bugs
  are fixed, but the last attempt with the synthetic identity died at iCloud setup,
  and the working phone-only path does not need it.
- **The number is fixed by the carrier signature**; holding a different number needs
  that number's SIM to answer the `REG-REQ`.

### Attachments (built, not proven against Apple)

Design and security notes: [docs/k8s-attachments.md](docs/k8s-attachments.md).

- **Inbound.** The rustpush backend downloads each attachment of an inbound
  message through MMCS (`Attachment::get_attachment`) into `IMESSAGE_ATTACH_DIR`
  (default `<data>/attachments`) and publishes an additive `attachment` SSE event
  `{guid, chat_guid, from, id, name, mime, size, path}`. The existing `message`
  and `message_meta` events are unchanged. Storage treats the sender as hostile:
  random ids, sanitised names, mime sniffed from the bytes, a per-file cap
  (`IMESSAGE_ATTACH_MAX_BYTES`, 100 MiB), a total quota (`IMESSAGE_ATTACH_QUOTA_BYTES`,
  2 GiB, oldest evicted first) and a maximum age (`IMESSAGE_ATTACH_MAX_AGE_SECS`, 7 days).
- **Serving.** A SEPARATE listener (`IMESSAGE_FILES_LISTEN`, default `0.0.0.0:8791`)
  serves only `GET /dl/<id>?exp=&sig=`: HMAC-SHA256 over `id`, newline, `exp` with the
  key read from the file named by `IMESSAGE_FILE_HMAC_KEY_FILE` (constant-time compare,
  signature checked before expiry, `IMESSAGE_FILE_URL_TTL_SECS` default 1800), always
  `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, a `sandbox` CSP,
  `no-store`. No key file means the feature is off: nothing is downloaded and the port
  is not opened.
- **Outbound.** `POST /im/attachment?to=|chat_guid=&name=&mime=` takes the raw file
  as the body (capped, spooled, sanitised, mime sniffed), uploads it with
  `MMCSFile::prepare_put` + `Attachment::new_mmcs` and sends it as a normal
  iMessage attachment part. The bridge feeds it from an XEP-0066 URL the owner posts.
- **Proven:** the pure parts (sanitising, sniffing, caps, eviction plan, HMAC against
  the RFC 4231 vectors, the `/dl` route including forged/expired/traversal/other-route
  cases, the upload route) by unit tests; the server crate and the rustpush crate
  compile and their tests pass on the NAS. **Not proven:** that Apple's MMCS accepts a
  download or an upload from this phone-only synthetic identity (no iCloud token;
  `dsid: None`), Live Photo / sticker / voice-message handling, and anything end to
  end. If MMCS refuses the identity the failure is logged per attachment
  (`attachment download failed: MMCS ...`) and the text placeholder still goes out.

### SMS 2FA, no human in the loop (dormant)

The working deployment does not log in to an Apple ID, so this path is not in use. It
stays in the code for anyone returning to the account route.

The trusted-device prompt is not an option here; the code is read **off the adb
handset**: `com.openbubbles.aka` mirrors inbound texts into its private
`files/sms.txt`, and [crates/imessage-server-rustpush/src/sms2fa.rs](crates/imessage-server-rustpush/src/sms2fa.rs)
snapshots that file *before* the login asks Apple for a code, then polls for the first
*new* line carrying a standalone 6-digit code. `IMESSAGE_ADB_SERIAL` overrides the
serial; `IMESSAGE_2FA_CODE` or `<data_dir>/2fa.txt` overrides the whole thing; nothing
ever *sends* an SMS. The read path is proven on-device; the full exchange against
Apple is not, because the deployment does not use it.

The full detail, evidence and dates live in
[crates/imessage-server-rustpush/README.md](crates/imessage-server-rustpush/README.md)
and [bridge/README.md](bridge/README.md).

## License

This project is licensed under the **GNU Affero General Public License, version 3
only** (AGPL-3.0-only). It is meant to be self-hosted, and the AGPL is the licence
that keeps it that way: anyone who runs a modified version and lets other people
use it over a network has to offer those users the corresponding source.

The full text is in [LICENSE](LICENSE); per-component notes (including the
vendored and derived code below) are in [NOTICE](NOTICE).

- `vendor/open-absinthe` is an original, vendored implementation of the Apple
  NAC challenge, also AGPL-3.0. It downloads Apple's `IMDAppleServices` binary at
  runtime and uses it as data; that binary is never redistributed here.
- `aka-oracle/src` contains code derived from The Android Open Source Project,
  Copyright (C) 2021 The Android Open Source Project, licensed under the Apache
  License, Version 2.0. See [NOTICE](NOTICE) for the required attribution.
