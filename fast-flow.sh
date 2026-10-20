#!/bin/bash
# Tight registration flow: pre-warm the oracle, poll instead of sleeping, reuse
# one device-account-identifier. Goal is challenge->response in ~2s.
set -e
W=/tmp/ent
R=com.openbubbles.aka
RCV=com.bluebubbles.messaging.services.rustpush.eap_aka.AkaReceiver
mkdir -p $W
NAI="${NAI:-000000000000000@nai.epc.mnc240.mcc310.3gppnetwork.org}"
UA="Entitlement/2 (iPhone) iOS/16.1.1 (20B101) Carrier Settings/50.0.2"
DEVID=$(python3 -c 'import uuid;print(str(uuid.uuid4()).upper())')

post() {
  gzip -c "$1" > "$1.gz"
  curl -s --max-time 60 -X POST "https://eas3.msg.t-mobile.com" \
    -H 'Content-Type: application/json' -H 'Content-Encoding: gzip' \
    -H 'Accept: application/json' -H 'Accept-Encoding: gzip' \
    -H 'Accept-Language: en' -H 'x-country-iso-code: us' -H 'x-protocol-version: 2' \
    -H "User-Agent: $UA" --data-binary @"$1.gz" -o "$2"
}

# poll for a marker in the oracle's result file; no fixed sleeps
poll_marker() { # $1 = marker
  local i v
  for i in $(seq 1 80); do
    v=$(adb shell run-as $R cat files/result.txt 2>/dev/null | sed -n "s/^$1=//p" | tr -d '\r')
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
    sleep 0.15
  done
  return 1
}

trigger() { # $@ = extra args
  adb shell run-as $R rm -f files/result.txt >/dev/null 2>&1 || true
  adb shell am broadcast -n $R/$RCV -a $R.RUN "$@" --receiver-foreground >/dev/null
}

echo "=== pre-warm the oracle process ==="
trigger --es warm 1 >/dev/null; poll_marker simOperator >/dev/null || true
echo "  warm"

echo "=== getAuthentication (device-account-identifier=$DEVID) ==="
DEVID="$DEVID" NAI="$NAI" python3 -c '
import json,os,base64
subid=base64.b64encode(b"\x02\x00\x00\x3b\x01"+os.environ["NAI"].encode()).decode()
print(json.dumps([{"device-account-identifier":os.environ["DEVID"],"auth-type":"EAP-AKA",
 "action-name":"getAuthentication","subscriber-id":subid,"request-id":3,"unique-id":"000000000000000"}]))
' > $W/ga.json
T0=$(date +%s.%N)
post $W/ga.json $W/ga.bin
python3 -c "
import gzip,json
raw=open('$W/ga.bin','rb').read()
try: raw=gzip.decompress(raw)
except Exception: pass
j=json.loads(raw)
assert j[0].get('status')==6302, j
open('$W/challenge.txt','w').write(j[0]['challenge'])"
CHAL=$(cat $W/challenge.txt)
T1=$(date +%s.%N)

echo "=== SIMREQ (app, polled) ==="
trigger --es challenge "$CHAL" >/dev/null
SIMREQ=$(poll_marker SIMREQ)
echo "  SIMREQ=$SIMREQ"
T2=$(date +%s.%N)

echo "=== SIM AKA (shell txn 33 / authType 129) ==="
adb shell service call iphonesubinfo 33 i32 2 i32 2 i32 129 s16 "$SIMREQ" s16 com.android.shell s16 null > $W/ao.txt 2>&1
RAW=$(python3 parcel.py $W/ao.txt 2>/dev/null)
T3=$(date +%s.%N)

echo "=== wrap into EAP-AKA (app, polled) ==="
trigger --es challenge "$CHAL" --es simresponse "$RAW" >/dev/null
RESP=$(poll_marker RESPONSE)
T4=$(date +%s.%N)
echo "  response=$RESP"

python3 -c "
t=[$T0,$T1,$T2,$T3,$T4]
print('  timing: challenge=%.1fs simreq=%.1fs aka=%.1fs wrap=%.1fs  TOTAL=%.1fs' % (t[1]-t[0],t[2]-t[1],t[3]-t[2],t[4]-t[3],t[4]-t[0]))"

echo "=== postChallenge + getPhoneNumber ==="
RESP="$RESP" python3 -c '
import json,os,base64
print(json.dumps([
 {"payload": os.environ["RESP"], "action-name":"postChallenge", "request-id":10},
 {"client-nonce": base64.b64encode(b"\x00"*32).decode(), "action-name":"getPhoneNumber", "request-id":11}]))
' > $W/post.json
post $W/post.json $W/post.bin
python3 -c "
import gzip,json
raw=open('$W/post.bin','rb').read()
try: raw=gzip.decompress(raw)
except Exception: pass
print(' ', raw.decode(errors='replace')[:800])"
