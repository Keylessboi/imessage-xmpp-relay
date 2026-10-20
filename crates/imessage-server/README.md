# imessage-server

A headless Linux iMessage server. It reads a config directory, logs in to an
Apple ID, registers the Apple ID as an IDS user, keeps an APNs connection open
and exposes a **frozen** local HTTP API for sending messages and streaming
inbound ones over SSE. The goal is to let an iPhone be retired.

```
crates/imessage-server/            this crate: config, state, HTTP API, SSE bus
crates/imessage-server-rustpush/   real Apple IDS backend (opt-in, see below)
```

## Status (read this first)

| Piece | State |
| --- | --- |
| `cargo build` / `cargo test` this crate | **green** (verified) |
| Config parsing, state file, all endpoints, SSE framing | **verified** (tests + a live boot) |
| Dockerfile | written, **not built** (per instructions) |
| APS, phone registration, send | **verified live through the real backend**: the number is registered as a `P:` IDS user and `/im/send` is delivered; see `../imessage-server-rustpush/README.md` |
| Inbound receive / SSE | **written, not exercised**: no message has arrived at the number yet |

The real backend lives in `crates/imessage-server-rustpush`, which depends on
`imessage-lab/rustpush` (submodules now present). It is excluded from this
workspace so that `cargo build` at the repository root stays green:

```sh
cd crates/imessage-server-rustpush
cargo build
```

See that crate's README for the live evidence. Two former blockers are fixed
history: `-80045` was a client-info / OS mismatch (an Android client-info string
fixes it) and the SMS 2FA code POST needed the same akd header set as the
challenge. The working deployment does **not** log in to an Apple ID — it
registers a `P:<number>` user from a carrier signature — and the server still
degrades gracefully (`registered:false`, retry backoff) instead of aborting if a
login is attempted and fails.

## Config

Default data directory `/data`; override with `IMESSAGE_DATA_DIR`. A `.env` is
loaded from the working directory and from the data directory. Values containing
spaces must be quoted (`dotenvy` rejects them otherwise, and the server logs a
warning instead of silently continuing).

| File | Contents | Alternative env vars |
| --- | --- | --- |
| `hwconfig.plist` | `MacOSConfig` (Mac hardware identity, extracted from Mac validation data) | `IMESSAGE_HWCONFIG_PLIST_BASE64` (base64 of the plist) |
| `gsa.plist` | `{ user: string, pass: <data: sha256(password)> }` | `IMESSAGE_APPLE_ID` + `IMESSAGE_APPLE_PASSWORD` (plaintext) or `IMESSAGE_APPLE_PASSWORD_SHA256` (hex) |
| `state.plist` | written by the server: apple id, `registered`, handles, APNs token, opaque backend blob | - |

Other environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LISTEN` | `127.0.0.1:8790` | HTTP bind address |
| `RUST_LOG` | `info` | tracing filter |
| `IMESSAGE_2FA_CODE` | - | 6-digit code for the first login (else see below) |
| `IMESSAGE_LOGIN_RETRY_SECS` | `1800` | base delay for the Apple ID login retry backoff (cap 3600 s; `0` disables) — rustpush backend only |
| `IMESSAGE_ADB_SERIAL` | `<adb-serial>` | handset the SMS 2FA code is read from over adb — rustpush backend only |

Secrets are never logged. `state.plist` and the keystore are written mode `0600`.

## Endpoints (frozen)

| Method | Path | Response |
| --- | --- | --- |
| GET | `/health` | `{"ok":true,"registered":<bool>}` |
| GET | `/im/status` | `{"registered":bool,"handles":[string],"apple_id":string,"login_error":string\|null,"last_login_attempt":int\|null}` |
| POST | `/im/send` | body `{"to":"+15551234567","text":"hi","reply_to":null}` → `{"ok":true,"guid":"..."}`; `to` may be omitted when `chat_guid` names an inbound group; non-2xx → `{"error":"..."}` |
| POST | `/im/facetime-call` | body `{"to":"+15551234567","video":true}` → `{"ok":true,"url":...,"rang":bool,"note":str|null}`; starts a FaceTime call to `to` and returns the owner's join link; `rang:false` = fell back to a plain link, nobody rung |
| POST | `/im/facetime-link` | body `{"handle":"+15551234567"}` → `{"ok":true,"url":"https://facetime.apple.com/join#..."}`; the handle is context only |
| GET | `/im/events` | `text/event-stream`, one JSON object per `data:` line |
| POST | `/im/register-phone` | body `{"number":"+1555...","signature":"<hex from REG-RESP>"}` → `{"ok":true,"handle":"tel:+1555..."}` |
| GET | `/apns-token` | `{"token":"<hex>"}`, or 503 while APS has no token |

SSE payloads:

```json
{"type":"ready"}
{"type":"message","guid":"...","chat_guid":"iMessage;-;+15551234567","from":"+15551234567","to":["me@example.com"],"text":"hi","timestamp":1700000000000,"is_group":false,"sender_name":null}
```

`{"type":"ready"}` is emitted once IDS registration completes, and is also sent
immediately to a client that connects after registration.
`chat_guid` is the conversation's `sender_guid` when Apple supplies one,
otherwise `iMessage;-;<peer>` for 1:1 and a stable hash for groups.

Registration-gated endpoints (`/im/send`, `/im/register-phone`) answer
`503 {"error":"not registered"}` until `/im/status` reports registered. That
gate lives in the HTTP layer, not the backend.

One exception: a backend whose
[`Backend::phone_registration_available`](src/backend.rs) returns true
(`imessage-server-rustpush` does) may receive `/im/register-phone` before
registration, because a `P:<number>` IDS user needs only the APNs token and the
carrier `REG-RESP` signature — no Apple ID. The stub keeps the default `false`, so
the 503 above still applies to it.

## First boot flow (real backend)

1. Read `.env` + the config directory; parse `hwconfig.plist` into `MacOSConfig`
   and `gsa.plist` into an Apple ID + SHA-256 password.
2. Load `state.plist`. With saved IDS users, skip to step 5 — no login, no 2FA.
3. First boot only: initialise the software keystore, open APS, build an
   anisette client, `AppleAccount::login` (SRP; may ask for 2FA — see below),
   then `login_apple_delegates` → `authenticate_apple` to mint the
   `mailto:<apple id>` IDS user.
4. `register(...)` the IDS users with Apple and persist the new state.
5. Build the `IMClient`, publish the APNs token and handles, mark the server
   registered (emits `{"type":"ready"}`), and start the push loop that forwards
   every inbound text message to the SSE bus.
6. `POST /im/register-phone` mints the `P:<number>` user from an SMS
   `REG-RESP` signature: `authenticate_phone(number, AuthPhone { push_token,
   sigs: [sig] }, config)`, then re-`register`s and rebuilds the `IMClient`.

Steps 3–4 are **not fatal**. APS opens and the backend is marked running before
the Apple ID login runs; if the login fails, the server logs it, stays
`registered:false`, and retries on a slow bounded backoff
(`IMESSAGE_LOGIN_RETRY_SECS`, base default 1800 s = 30 min, doubling to a 3600 s
cap, one attempt and one log line at a time; `0` disables). `/health`,
`/apns-token` and `/im/register-phone` keep working throughout, and
`/im/status` exposes `login_error` + `last_login_attempt` so a lifted block is
visible without reading the log.

### Two-factor authentication

`rustpush`'s login takes a synchronous code callback, and nobody is around to
type a code. The rustpush backend's real path reads a **new inbound SMS** off the
handset holding the Apple ID's SIM over adb (default serial `<adb-serial>`,
override `IMESSAGE_ADB_SERIAL`): the helper app mirrors inbound texts to its
private `files/sms.txt`, that file is snapshotted *before* the login asks Apple
for a code, and the first *new* line carrying a 6-digit code is fed back.
`IMESSAGE_2FA_CODE`, or a code written to `<data_dir>/2fa.txt` (deleted once
read), still overrides it for a manual run. This is the SMS (`secondaryAuth`)
flow, not the trusted-device one. The two bugs that broke it are fixed (the code
POST must reuse the challenge's akd header set; Apple now answers HTTP 200), but
the working deployment does not use the Apple ID path.

## Running

```sh
# stub backend (this crate): serves the API, never registers
cargo build -p imessage-server
IMESSAGE_DATA_DIR=/data LISTEN=127.0.0.1:8790 ./target/debug/imessage-server

# real backend (see crates/imessage-server-rustpush/README.md)
cd crates/imessage-server-rustpush && cargo build
./target/debug/imessage-server-rustpush
```

Docker (not built in this repo):

```sh
docker build -t imessage-server crates/imessage-server
docker run --rm -p 127.0.0.1:8790:8790 -v /srv/imessage:/data imessage-server
```

## Tests

```sh
cargo test -p imessage-server
```

19 tests: hex helpers, config validation, state persistence + the ready
transition, the frozen JSON shapes, `/im/status` login diagnostics, and eight
end-to-end HTTP tests (status codes, error shape, SSE `content-type`, ready +
message frames, group send by `chat_guid`, FaceTime link).
