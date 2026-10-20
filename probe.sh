#!/bin/bash
# Try postChallenge and getPhoneNumber separately/with different nonces.
set -e
W=/tmp/ent
R=com.openbubbles.aka
RCV=com.bluebubbles.messaging.services.rustpush.eap_aka.AkaReceiver
mkdir -p $W
NAI="${NAI:-000000000000000@nai.epc.mnc240.mcc310.3gppnetwork.org}"
UA="Entitlement/2 (iPhone) iOS/16.1.1 (20B101) Carrier Settings/50.0.2"
post() {
  gzip -c "$1" > "$1.gz"
  curl -s --max-time 90 -X POST "https://eas3.msg.t-mobile.com" \
    -H 'Content-Type: application/json' -H 'Content-Encoding: gzip' \
    -H 'Accept: application/json' -H 'Accept-Encoding: gzip' \
    -H 'Accept-Language: en' -H 'x-country-iso-code: us' -H 'x-protocol-version: 2' \
    -H "User-Agent: $UA" --data-binary @"$1.gz" -o "$2" -w '' 
  python3 -c "
import gzip,json,sys
raw=open('$2','rb').read()
try: raw=gzip.decompress(raw)
except Exception: pass
print('   ->', raw.decode(errors='replace')[:400])
"
}

fresh_aka() {
  NAI="$NAI" python3 -c '
import json,os,uuid,base64
subid=base64.b64encode(b"\x02\x00\x00\x3b\x01"+os.environ["NAI"].encode()).decode()
print(json.dumps([{"device-account-identifier":str(uuid.uuid4()).upper(),"auth-type":"EAP-AKA",
 "action-name":"getAuthentication","subscriber-id":subid,"request-id":3,"unique-id":"000000000000000"}]))
' > $W/ga.json
  post $W/ga.json $W/ga.bin >/dev/null
  python3 -c "
import gzip,json
raw=open('$W/ga.bin','rb').read()
try: raw=gzip.decompress(raw)
except Exception: pass
open('$W/ch.txt','w').write(json.loads(raw)[0]['challenge'])"
  CH=$(cat $W/ch.txt)
  adb shell am broadcast -n $R/$RCV -a $R.RUN --es challenge "$CH" --receiver-foreground >/dev/null
  sleep 6
  SREQ=$(adb shell run-as $R cat files/result.txt | sed -n 's/^SIMREQ=//p' | tr -d '\r')
  adb shell service call iphonesubinfo 33 i32 2 i32 2 i32 129 s16 "$SREQ" s16 com.android.shell s16 null > $W/ao.txt 2>&1
  RAW=$(python3 parcel.py $W/ao.txt 2>/dev/null)
  adb shell am broadcast -n $R/$RCV -a $R.RUN --es challenge "$CH" --es simresponse "$RAW" --receiver-foreground >/dev/null
  sleep 6
  RESP=$(adb shell run-as $R cat files/result.txt | sed -n 's/^RESPONSE=//p' | tr -d '\r')
  echo "$RESP"
}

echo "=== A) postChallenge ALONE (id 10) ==="
A=$(fresh_aka)
echo "   aka=$A"
A="$A" python3 -c 'import json,os; print(json.dumps([{"payload":os.environ["A"],"action-name":"postChallenge","request-id":10}]))' > $W/p1.json
post $W/p1.json $W/p1.bin

echo "=== B) getPhoneNumber ALONE (id 11, zero nonce) ==="
fresh_aka >/dev/null
python3 -c 'import json,base64; print(json.dumps([{"client-nonce":base64.b64encode(b"\x00"*32).decode(),"action-name":"getPhoneNumber","request-id":11}]))' > $W/p2.json
post $W/p2.json $W/p2.bin

echo "=== C) batched, random nonce ==="
B=$(fresh_aka)
B="$B" python3 -c 'import json,os,base64; print(json.dumps([{"payload":os.environ["B"],"action-name":"postChallenge","request-id":10},{"client-nonce":base64.b64encode(os.urandom(32)).decode(),"action-name":"getPhoneNumber","request-id":11}]))' > $W/p3.json
post $W/p3.json $W/p3.bin
