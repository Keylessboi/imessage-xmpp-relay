#!/usr/bin/env bash
# Register the SIM's phone number on iMessage — the whole chain, end to end.
#
#   1. read a fresh APNs push token from the running imessage-server
#   2. text REG-REQ to the carrier gateway from the SIM (one SMS, exactly one)
#   3. wait for the carrier's REG-RESP and pull out the number + signature
#   4. POST that signature back so the server mints P:<number>
#
# Needs: the imessage-server up on $API, the handset on adb, and the helper APK
# installed with SEND_SMS + RECEIVE_SMS granted. See aka-oracle/README.md.
#
# Safe to re-run: it refuses to send a second SMS while a REG-RESP is already
# on the handset, and it never loops. Sending bogus registrations to a carrier
# shortcode is a good way to get a number flagged, so this does one attempt.
set -euo pipefail

API="${API:-http://127.0.0.1:8790}"
SERIAL="${SERIAL:-0123456789ABCDEF}"
PKG="${PKG:-com.openbubbles.aka}"
REG_RCV="com.openbubbles.aka.RegReceiver"
ACTION="$PKG.REGREQ"
GATEWAY="${GATEWAY:-22223333}"   # PhoneNumberRegistrationGatewayAddress for MCCMNC 310240/310260
WAIT_SECS="${WAIT_SECS:-120}"

adb_() { adb -s "$SERIAL" "$@"; }
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v adb >/dev/null || die "adb not on PATH"
adb devices | grep -q "^$SERIAL[[:space:]]*device" || die "handset $SERIAL is not connected"

say "server health"
curl -fsS "$API/health" || die "imessage-server not answering on $API"
echo

say "APNs token"
TOKEN=$(curl -fsS "$API/apns-token" | sed -n 's/.*"token":"\([0-9a-fA-F]*\)".*/\1/p')
[ -n "$TOKEN" ] || die "/apns-token returned no token (APS not connected yet?)"
echo "token: ${#TOKEN} hex chars"

# Refuse to burn a second SMS if we already have a reply sitting on the phone.
EXISTING=$(adb_ shell run-as "$PKG" cat files/sms.txt 2>/dev/null || true)
if printf '%s' "$EXISTING" | grep -q 'REG-RESP'; then
    say "a REG-RESP is already on the handset; not sending another SMS"
else
    say "sending REG-REQ to $GATEWAY (exactly one SMS)"
    adb_ shell run-as "$PKG" rm -f files/result.txt >/dev/null 2>&1 || true
    adb_ shell am broadcast -n "$PKG/$REG_RCV" -a "$ACTION" \
        --es token "$TOKEN" --es gateway "$GATEWAY" --receiver-foreground >/dev/null
    sleep 2
    adb_ shell run-as "$PKG" cat files/result.txt 2>/dev/null | sed 's/^/    /' || true
fi

say "waiting up to ${WAIT_SECS}s for REG-RESP"
RESP=""
for _ in $(seq "$WAIT_SECS"); do
    # The manifest receiver writes files/sms.txt; the system store is the fallback
    # (independently proven against a real carrier-delivered inbound SMS).
    RESP=$(adb_ shell run-as "$PKG" cat files/sms.txt 2>/dev/null | grep -a 'REG-RESP' | tail -1 || true)
    if [ -z "$RESP" ]; then
        RESP=$(adb_ shell content query --uri content://sms/inbox 2>/dev/null | grep -a 'REG-RESP' | tail -1 || true)
    fi
    [ -n "$RESP" ] && break
    sleep 1
done
[ -n "$RESP" ] || die "no REG-RESP after ${WAIT_SECS}s — the gateway did not answer (bad token, or the line is not provisioned)"

echo "    $RESP"

# REG-RESP?v=3;r=<id>;n=<number>;s=<signature>;...
PAYLOAD="${RESP#*?}"
NUMBER=$(printf '%s' "$PAYLOAD" | tr ';' '\n' | sed -n 's/^n=//p' | head -1)
SIG=$(printf   '%s' "$PAYLOAD" | tr ';' '\n' | sed -n 's/^s=//p' | head -1)
[ -n "$NUMBER" ] || die "REG-RESP had no n= field: $RESP"
[ -n "$SIG" ]    || die "REG-RESP had no s= field: $RESP"

say "registering $NUMBER"
curl -fsS -X POST "$API/im/register-phone" \
    -H 'Content-Type: application/json' \
    -d "$(printf '{"number":"%s","signature":"%s"}' "$NUMBER" "$SIG")"
echo
say "status"
curl -fsS "$API/im/status"
echo
say "done — $NUMBER should now appear in handles above"
