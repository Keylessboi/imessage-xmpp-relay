#!/usr/bin/env python3
"""Decode a 'service call' reply Parcel containing a String16.

The hex dump prints 32-bit words; the underlying bytes are little-endian, so
each word is byte-reversed. The reply parcel is then:
    int32 exceptionCode | int32 length | length*2 bytes UTF-16LE
"""
import re, struct, sys

txt = open(sys.argv[1]).read()
words = []
for line in txt.splitlines():
    m = re.search(r"0x[0-9a-f]{8}: ([0-9a-f ]+?) '", line)
    if m:
        words += m.group(1).split()
if not words:
    sys.exit("no parcel data")

data = bytearray()
for w in words:
    data += bytes(reversed(bytes.fromhex(w)))

exc = struct.unpack_from("<i", data, 0)[0]
length = struct.unpack_from("<i", data, 4)[0]
sys.stderr.write(f"exception={exc} length={length}\n")

if exc != 0:
    sys.exit(f"remote exception code {exc}")
if length < 0:
    sys.exit("null string")
s = data[8:8 + length * 2].decode("utf-16-le", errors="replace")
sys.stdout.write(s)
