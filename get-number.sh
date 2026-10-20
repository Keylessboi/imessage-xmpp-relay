#!/bin/bash
# Full phone-number registration flow:
#   T-Mobile getAuthentication -> SIM AKA (via shell txn 33, authType 129)
#   -> EAP-AKA wrap (our oracle APK) -> postChallenge + getPhoneNumber
set -e
W=/tmp/ent
R=com.openbubbles.aka
RCV=com.bluebubbles.messaging.services.rustpush.eap_aka.AkaReceiver
mkdir -p $W

NAI="${NAI:-000000000000000@nai.epc.mnc240.mcc310.3gppnetwork.org}"
UA="Entitlement/2 (iPhone) iOS/16.1.1 (20B101) Carrier Settings/50.0.2"

post() { # $1 = json path, $2 = out path
  gzip -c "$1" > "$1.gz"
  curl -s --max-time 90 -X POST "https://eas3.msg.t-mobile.com" \
    -H 'Content-Type: application/json' -H 'Content-Encoding: gzip' \
    -H 'Accept: application/json' -H 'Accept-Encoding: gzip' \
    -H 'Accept-Language: en' -H 'x-country-iso-code: us' -H 'x-protocol-version: 2' \
    -H "User-Agent: $UA" --data-binary @"$1.gz" -o "$2" -w 'HTTP %{http_code}\n'
}

echo "=== 1. getAuthentication ==="
NAI="$NAI" python3 -c '
import json,os,uuid,base64
nai=os.environ["NAI"]
subid=base64.b64encode(b"\x02\x00\x00\x3b\x01"+nai.encode()).decode()
print(json.dumps([{"device-account-identifier":str(uuid.uuid4()).upper(),"auth-type":"EAP-AKA",
 "action-name":"getAuthentication","subscriber-id":subid,"request-id":3,"unique-id":"000000000000000"}]))
' > $W/getauth.json
post $W/getauth.json $W/getauth.bin
python3 -c "
import gzip,json
raw=open('$W/getauth.bin','rb').read()
try: raw=gzip.decompress(raw)
except Exception: pass
j=json.loads(raw)
print('  status', j[0].get('status'))
open('$W/challenge.txt','w').write(j[0]['challenge'])
"
CHAL=$(cat $W/challenge.txt)

echo "=== 2. app: parse challenge -> SIMREQ ==="
adb shell am broadcast -n $R/$RCV -a $R.RUN --es challenge "$CHAL" --receiver-foreground >/dev/null
sleep 6
SIMREQ=$(adb shell run-as $R cat files/result.txt | sed -n 's/^SIMREQ=//p' | tr -d '\r')
echo "  SIMREQ=$SIMREQ"

echo "=== 3. SIM AKA (shell, txn 33, authType 129) ==="
adb shell service call iphonesubinfo 33 i32 2 i32 2 i32 129 s16 "$SIMREQ" s16 com.android.shell s16 null > $W/aka_out.txt 2>&1
RAW=$(python3 parcel.py $W/aka_out.txt)
echo "  raw SIM response=$RAW"

echo "=== 4. app: wrap into EAP-AKA ==="
adb shell am broadcast -n $R/$RCV -a $R.RUN --es challenge "$CHAL" --es simresponse "$RAW" --receiver-foreground >/dev/null
sleep 6
adb shell run-as $R cat files/result.txt
RESP=$(adb shell run-as $R cat files/result.txt | sed -n 's/^RESPONSE=//p' | tr -d '\r')
if [ -z "$RESP" ]; then echo "WRAP FAILED"; exit 1; fi
echo "  EAP-AKA response=$RESP"

echo "=== 5. postChallenge + getPhoneNumber ==="
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
print(' ', raw.decode(errors='replace')[:1000])
open('$W/post.json.resp','w').write(raw.decode(errors='replace'))
"
