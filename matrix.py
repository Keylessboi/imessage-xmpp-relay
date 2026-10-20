#!/usr/bin/env python3
"""Vary the postChallenge request shape to find what the carrier rejects."""
import os, subprocess, json, base64, uuid, gzip, time, sys

W = "/tmp/ent"
R = "com.openbubbles.aka"
RCV = "com.bluebubbles.messaging.services.rustpush.eap_aka.AkaReceiver"
NAI = os.environ.get("NAI", "000000000000000@nai.epc.mnc240.mcc310.3gppnetwork.org")
UA = "Entitlement/2 (iPhone) iOS/16.1.1 (20B101) Carrier Settings/50.0.2"
URL = "https://eas3.msg.t-mobile.com"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def post(obj):
    open(W + "/req.gz", "wb").write(gzip.compress(json.dumps(obj).encode()))
    out = subprocess.run([
        "curl", "-s", "--max-time", "60", "-X", "POST", URL,
        "-H", "Content-Type: application/json", "-H", "Content-Encoding: gzip",
        "-H", "Accept: application/json", "-H", "Accept-Encoding: gzip",
        "-H", "Accept-Language: en", "-H", "x-country-iso-code: us",
        "-H", "x-protocol-version: 2", "-H", "User-Agent: " + UA,
        "--data-binary", "@" + W + "/req.gz"], capture_output=True)
    raw = out.stdout
    try:
        raw = gzip.decompress(raw)
    except Exception:
        pass
    try:
        return json.loads(raw)
    except Exception:
        return raw.decode(errors="replace")


def wait_marker(name, tries=80):
    for _ in range(tries):
        v = sh("adb shell run-as %s cat files/result.txt 2>/dev/null" % R)
        for line in v.splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
        time.sleep(0.15)
    return None


def flow():
    """fresh challenge + SIM AKA + EAP wrap. returns (devid, chal, simreq, raw, resp)"""
    devid = str(uuid.uuid4()).upper()
    subid = base64.b64encode(b"\x02\x00\x00\x3b\x01" + NAI.encode()).decode()
    r = post([{"device-account-identifier": devid, "auth-type": "EAP-AKA",
               "action-name": "getAuthentication", "subscriber-id": subid,
               "request-id": 3, "unique-id": "000000000000000"}])
    if not isinstance(r, list) or "challenge" not in r[0]:
        return None
    chal = r[0]["challenge"]

    sh("adb shell run-as %s rm -f files/result.txt" % R)
    sh('adb shell am broadcast -n %s/%s -a %s.RUN --es challenge "%s" --receiver-foreground'
       % (R, RCV, R, chal))
    simreq = wait_marker("SIMREQ")
    if not simreq:
        return None

    sh("adb shell service call iphonesubinfo 33 i32 2 i32 2 i32 129 s16 '%s' s16 com.android.shell s16 null > %s/ao.txt 2>&1"
       % (simreq, W))
    raw = sh("python3 %s/../parcel.py %s/ao.txt" % (W, W))
    sh("adb shell run-as %s rm -f files/result.txt" % R)
    sh('adb shell am broadcast -n %s/%s -a %s.RUN --es challenge "%s" --es simresponse "%s" --receiver-foreground'
       % (R, RCV, R, chal, raw))
    resp = wait_marker("RESPONSE")
    return devid, chal, simreq, raw, resp


VARIANTS = [
    ("baseline (id10)",        lambda d, c, rw, rp: [{"payload": rp, "action-name": "postChallenge", "request-id": 10}]),
    ("request-id 3",           lambda d, c, rw, rp: [{"payload": rp, "action-name": "postChallenge", "request-id": 3}]),
    ("with devid+authtype",    lambda d, c, rw, rp: [{"device-account-identifier": d, "auth-type": "EAP-AKA", "payload": rp, "action-name": "postChallenge", "request-id": 10}]),
    ("raw sim response",       lambda d, c, rw, rp: [{"payload": rw, "action-name": "postChallenge", "request-id": 10}]),
    ("payload+challenge",      lambda d, c, rw, rp: [{"payload": rp, "challenge": c, "action-name": "postChallenge", "request-id": 10}]),
]

for name, mk in VARIANTS:
    f = flow()
    if not f:
        print(f"{name:26s} flow failed")
        continue
    devid, chal, simreq, raw, resp = f
    r = post(mk(devid, chal, raw, resp))
    print(f"{name:26s} -> {json.dumps(r)[:160]}")
