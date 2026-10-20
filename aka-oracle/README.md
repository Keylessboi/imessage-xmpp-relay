# aka-oracle

A tiny, Gradle-free helper APK for **iMessage phone-number activation** work on
the Pixel 3a XL test handset (codename `bonito`, adb serial `<adb-serial>`,
T-Mobile SIM, MCCMNC 310240/310260).

Package: `com.openbubbles.aka` (debuggable, minSdk 24, targetSdk 32).

It does three things, all driven from `adb`:

1. the original **EAP-AKA oracle** (`AkaReceiver`, unchanged from the first
   version — another workflow depends on it);
2. **sends** the carrier-gateway registration SMS, `REG-REQ?v=3;t=<token>;r=<id>;`
   (`RegReceiver`);
3. **captures** inbound SMS verbatim to a file — both **text** SMS
   (`SmsReceiver`, `SMS_RECEIVED`) and the **port-addressed data SMS** the
   carrier actually answers with (`PduReceiver`, `DATA_SMS_RECEIVED`) — plus a
   diagnostic that reports what the framework will say about the SIM
   (`InfoReceiver`).

Everything can be read back with `adb shell run-as com.openbubbles.aka cat ...`
because the app is `android:debuggable="true"`.

---

## 1. EAP-AKA oracle (existing, do not break)

```sh
# step 1
adb -s <adb-serial> shell am broadcast \
    -n com.openbubbles.aka/com.bluebubbles.messaging.services.rustpush.eap_aka.AkaReceiver \
    -a com.openbubbles.aka.RUN --es challenge <b64>
# step 2
adb -s <adb-serial> shell am broadcast \
    -n com.openbubbles.aka/com.bluebubbles.messaging.services.rustpush.eap_aka.AkaReceiver \
    -a com.openbubbles.aka.RUN --es challenge <b64> --es simresponse <b64>
```

Writes `files/result.txt` with keys `SIMREQ=` / `RESPONSE=` (and
`simOperator=`, `subscriberId=`, `NAI=`, `ERROR=`). These key names are
reserved for the AKA workflow; the SMS feature below only ever uses *different*
key names and its own files.

## 2. Send a REG-REQ to the gateway shortcode

```sh
adb -s <adb-serial> shell am broadcast \
    -n com.openbubbles.aka/com.openbubbles.aka.RegReceiver \
    -a com.openbubbles.aka.REGREQ \
    --es token <apns-push-token-hex> [--es gateway 22223333] [--es sub N]
```

| extra | meaning |
| --- | --- |
| `token` | APNs push token, hex, no separators. Required for a real attempt. |
| `gateway` | destination shortcode; default `22223333` (`PhoneNumberRegistrationGatewayAddress` for 310240/310260). |
| `sub` | subscription id override; default = the device's current subscription (2 on this handset). |
| `body` | *complete* body override (testing); when present `token`/`r` are ignored. |

Body sent is exactly `REG-REQ?v=3;t=<token>;r=<random-int>;`. The message is
sent with `SmsManager.getSmsManagerForSubscriptionId(sub).sendTextMessage()`,
with sent/delivered PendingIntents so the outcome is recorded.

### Output — `files/result.txt` (this file is rewritten by each send)

```
REGREQ=REG-REQ?v=3;t=<token>;r=491531591;     body that was sent
GATEWAY=22223333
SUB=2
SENT=submitted;r=491531591                    framework accepted it
SENT=ok;r=491531591                           network sent report (appended async)
SENT=err(rc=4:no-service);r=...               or a mapped SmsManager error
DELIVERED=ok;r=491531591                      gateway delivery report
SENDFAIL=<exception>                          sendTextMessage threw (no permission, radio off, ...)
```

Read it:

```sh
adb -s <adb-serial> shell run-as com.openbubbles.aka cat files/result.txt
```

## 3. Receive SMS

### 3a. The gateway reply — a data SMS (`PduReceiver`)

**This is the path the REG-RESP actually takes.** `PduReceiver` is registered for
`android.intent.action.DATA_SMS_RECEIVED` with `<data android:scheme="sms" />`
and **no port filter**, and writes:

* `files/result.txt`  `REGRESP=<body>` — the decoded reply, e.g.
  `REG-RESP?v=3;r=1224989370;n=+15551234567;s=<redacted>`
* `files/sms.txt`     `[pdu] src=DATA_SMS port=<port> body=<body>`
* `files/pdu.txt`     the raw unsigned PDU as base64 (always, so a parse
  failure can never lose the reply).

Decoding tries 3GPP, then 3GPP2, then falls back to US-ASCII on the raw PDU bytes
(the REG-RESP body is ASCII inside the PDU, so the fallback usually wins) —
a port of the reference app's `PDUReceiver.kt`. No adb command is needed: the
receiver fires by itself when the carrier answers.

```sh
adb -s <adb-serial> shell run-as com.openbubbles.aka cat files/result.txt
adb -s <adb-serial> shell run-as com.openbubbles.aka cat files/pdu.txt
```

### 3b. Plain text SMS (`SmsReceiver`)

`SmsReceiver` is registered in the manifest for the standard
`android.provider.Telephony.SMS_RECEIVED` broadcast (**used for normal text
SMS**; it is *not* how the gateway answers).
It parses the PDUs with `Telephony.Sms.Intents.getMessagesFromIntent()`; if that
throws or returns nothing it falls back to reading the raw `pdus` extra and
`SmsMessage.createFromPdu()`. Both are the `Telephony.Sms.Intents` path — a
non-default SMS app has no other supported receive path.

Every message is appended **verbatim** to `files/sms.txt`, one record per line:

```
[<iso8601-local>] src=SMS_RECEIVED from=<address> time=<sms-timestamp-millis> body=<body>
```

```sh
adb -s <adb-serial> shell run-as com.openbubbles.aka cat files/sms.txt
```

Two extra actions exist to exercise the plumbing without an inbound SMS:

```sh
# append a synthetic record (no radio involved)
adb -s <adb-serial> shell am broadcast \
    -n com.openbubbles.aka/com.openbubbles.aka.SmsReceiver \
    -a com.openbubbles.aka.SMS_TEST --es sender +15550001111 --es body hello

# dump content://sms/inbox (needs READ_SMS); only messages newer than the last
# dump are appended, the high-water mark is files/inbox_last.txt
adb -s <adb-serial> shell am broadcast \
    -n com.openbubbles.aka/com.openbubbles.aka.SmsReceiver \
    -a com.openbubbles.aka.SMS_INBOX_DUMP
```

## 4. SIM diagnostic

```sh
adb -s <adb-serial> shell am broadcast \
    -n com.openbubbles.aka/com.openbubbles.aka.InfoReceiver -a com.openbubbles.aka.INFO
adb -s <adb-serial> shell run-as com.openbubbles.aka cat files/info.txt
```

Example (this handset):

```
SDK=32
LINE1=15551234567
SIMOP=310240
SIMOPNAME=T-Mobile
NETOP=310260
NETOPNAME=T-Mobile
COUNTRY=us
ROAMING=false
SUBID=2
ICCID=****XXXX            (masked to last 4; the full value is never written)
SUBS=1
  sub id=2 slot=0 carrier=T-Mobile number=15551234567
```

## Permissions

Declared: `SEND_SMS`, `RECEIVE_SMS`, `READ_SMS` (inbox-dump fallback),
`READ_PHONE_STATE`, `READ_PHONE_NUMBERS`. All are runtime permissions and can be
granted non-interactively. `adb install -r` keeps them; re-grant after a fresh
install:

```sh
for p in SEND_SMS RECEIVE_SMS READ_SMS READ_PHONE_STATE READ_PHONE_NUMBERS; do
  adb -s <adb-serial> shell pm grant com.openbubbles.aka android.permission."$p"
done
adb -s <adb-serial> shell dumpsys package com.openbubbles.aka | grep -i sms
```

Expected:

```
android.permission.READ_SMS: granted=true, ...
android.permission.RECEIVE_SMS: granted=true, ...
android.permission.SEND_SMS: granted=true, ...
```

> The handset is a **debug** build of Android, so `pm grant` works without any
> user interaction. No notification listener or default-SMS-app role is needed
> for `SMS_RECEIVED`; the broadcast is delivered to any app holding
> `RECEIVE_SMS`.

## Rebuilding

```sh
./build.sh
adb -s <adb-serial> install -r build/aka-oracle.apk
```

There is **no Gradle**. The pipeline is
`aapt2 link -> javac -> d8 -> zip classes.dex -> zipalign -> apksigner`, the same
as the original APK. Build-tools are not on PATH on this machine; the script
finds an SDK via `ANDROID_SDK_ROOT`/`ANDROID_HOME` or one of
`~/android-sdk`, `~/Android/Sdk`, `/opt/android-sdk`, and needs
`build-tools/*/{aapt2,d8,zipalign,apksigner}` plus
`platforms/android-*/android.jar`.

The working install here was built with **build-tools 34.0.0** and
**Android platform 32** unpacked into `~/android-sdk` (kept out of the repo;
downloaded from `https://dl.google.com/android/repository/`:
`build-tools_r34-linux.zip`, `platform-32_r01.zip`).

Signing reuses `build/oracle.keystore` (alias `oracle`, store/key password
`android`) so `install -r` keeps working over the previous install. Override
with `KS`, `KS_PASS`, `KS_ALIAS`; the keystore is generated if missing. The
script never prints keystore contents.

### Build trap (already handled in `build.sh`)

`javac` must be run with `-parameters`. Without it, javac emits a
`MethodParameters` attribute whose names are `null` for the synthetic
constructor parameters of anonymous inner classes, and **d8 (R8 8.2.2,
build-tools 34.0.0) aborts** with:

```
Error in .../RegReceiver$1.class:
java.lang.NullPointerException: Cannot invoke "String.length()" because "<parameter1>" is null
```

`-parameters` fills the names in and d8 succeeds. This is why the flag is there
— it is not cosmetic.

## Field notes (2026-10-13, live on <adb-serial>)

* **Send is proven.** A message left the device and got a network delivery report
  (`SENT=ok`, `DELIVERED=ok`; logcat shows
  `QImsService: REQUEST_SEND_IMS_SMS` then `UNSOL_IMS_SMS_STATUS_REPORT`).
* **One** `REG-REQ` with an all-zero (invalid) token was sent to `22223333`.
  `SENT=ok`, `DELIVERED=ok`, and **no reply ever arrived** — no `REG-RESP`,
  nothing in `files/sms.txt`, no inbound-SMS activity in logcat. An invalid
  token being answered with silence is the expected result; do not retry in a
  loop (repeated bogus registrations to a carrier shortcode are exactly what gets
  a number flagged).
* **Self-send does not loop back on T-Mobile.** Sending an SMS from the
  handset to its own number (`+15551234567`) is accepted and gets
  `DELIVERED=ok`, and the message is inserted into the provider's *sent* box, but
  no inbound copy ever arrives. To prove receive you need an SMS from a
  **different** phone/line.
* **A real `REG-RESP` WAS received.** It is archived in
  `files/result.txt` and `files/sms.txt` / `files/pdu.txt`:

  ```
  REG-RESP?v=3;r=1224989370;n=+15551234567;s=<redacted-reg-resp-signature>
  ```

  `n=` matches the handset's own line-1 number; `s=` is a 32-byte carrier
  signature.
* **The gateway reply is NOT a text SMS on this carrier — it is a port-addressed
  data SMS.** The reply arrives as a binary SMS on **port 5497**
  (`sms://localhost:5497`), which does **not** raise
  `android.provider.Telephony.SMS_RECEIVED` and is **not** written to
  `content://sms/inbox`. An app listening only for the text broadcast (which is
  what the first build of this helper did) sees *nothing at all* — a valid-token
  `REG-REQ` looks exactly like silence. The fix is `PduReceiver`, registered for
  `android.intent.action.DATA_SMS_RECEIVED` with `scheme="sms"` and **no port
  filter** (the port is chosen by the carrier). This mirrors the reference app's
  `PDUReceiver.kt`, whose own comment notes that "on some carriers (i.e. AT&T),
  the REG-RESP message is sent as a data SMS (PDU) instead of a regular SMS".
  T-Mobile behaves the same way.
* **Earlier "no reply" observations were this bug, not gateway silence.** The
  07:05:32 inbound that was first misread as a mere carrier/Google config push
  (`CarrierServices: Received binary SMS on port 5497`, Bugle
  `ConfigSmsReceiver: port not matching, ignoring`) was in fact the reply
  channel. Do not conclude "no REG-RESP" from a text-only capture again.
* **Text SMS still works** and is still worth having: `SmsReceiver` records plain
  SMS from any sender. Two gotchas observed: (a) a self-send to the handset's own
  number is accepted with `DELIVERED=ok` but T-Mobile does **not** loop it
  back as an inbound message, so "text yourself" is not a receive test on this
  carrier; (b) Google Messages deletes rows from `content://sms/inbox` after
  importing them, so the `SMS_INBOX_DUMP` fallback can race — the manifest
  receivers are the reliable path.
* **An invalid token still gets no answer.** The all-zero-token `REG-REQ` was
  sent once, got `SENT=ok`/`DELIVERED=ok`, and produced no `REG-RESP`. Keep
  gateway traffic to a handful of messages.
* **The SIM's line-1 number is readable** on this handset:
  `+15551234567` (`InfoReceiver`; `TelephonyManager.getLine1Number()` and
  `SubscriptionInfo.getNumber()` agree). Note that
  `adb shell service call iphonesubinfo 1` returns null here — use the app.

## Layout

```
AndroidManifest.xml                                   package + receivers + permissions
src/com/bluebubbles/.../eap_aka/                      the original AKA oracle (untouched)
src/com/openbubbles/aka/RegReceiver.java              REG-REQ sender + result writer
src/com/openbubbles/aka/SmsReceiver.java              text SMS_RECEIVED capture + test/dump actions
src/com/openbubbles/aka/PduReceiver.java              DATA_SMS_RECEIVED capture -> the real REG-RESP
src/com/openbubbles/aka/SmsStatusReceiver.java        async sent/delivered status
src/com/openbubbles/aka/InfoReceiver.java             SIM/line-1 diagnostic
build.sh                                              reproducible build
build/                                                outputs + oracle.keystore (keep out of git)
```

Runtime files (in the app's private dir, `run-as` to read):

| file | written by | contents |
| --- | --- | --- |
| `files/result.txt` | RegReceiver, SmsStatusReceiver, PduReceiver, AkaReceiver | `REGREQ=`/`SENT=`/`SENDFAIL=`/`DELIVERED=`, `REGRESP=<body>`, and the AKA `SIMREQ=`/`RESPONSE=` |
| `files/sms.txt` | SmsReceiver, PduReceiver | one verbatim record per inbound SMS / data SMS (appended) |
| `files/pdu.txt` | PduReceiver | base64 of every unsigned data-SMS PDU, so bytes survive a parse failure |
| `files/info.txt` | InfoReceiver | SIM/line-1 diagnostic (`LINE1=`, `SIMOP=`, ...) |
| `files/inbox_last.txt` | SmsReceiver | high-water mark for `SMS_INBOX_DUMP` |
