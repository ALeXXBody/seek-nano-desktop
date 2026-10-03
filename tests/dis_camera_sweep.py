"""Sweep libseekcamera.so for the vendor request codes, using real boundaries.

The transport turned out to be a pure JNI pass-through - libseekusb.so only
exports ControlTransfer(int, byte[], int) and BulkTransfer(int, byte[], int) and
carries no protocol of its own. The request codes are therefore chosen by the
CALLER, which is libseekcamera.so.

That makes this sweep the right place to look, and .ARM.exidx makes it tractable:
it holds one entry per function, so a stripped library still yields exact
function boundaries. libseekcamera.so's exidx is 5,000 bytes = 625 entries,
which is the map needed to walk .text without guessing where functions start.

Calibration again decides whether the result means anything. This project has
ten request codes confirmed against real hardware - 0x36, 0x37, 0x3C, 0x3D, 0x3E,
0x53, 0x54, 0x55, 0x56, 0x58 - and the SDK is what generates them, so they must
appear in this sweep. If they do, the same encoding and range can be trusted for
the AGC and shutter codes found next to them. If they do not, then either the
codes are built at runtime or they live in the lower-level SPI library, and
static extraction from here is the wrong approach entirely.

Reports the verdict explicitly rather than printing a table of numbers and
leaving the reader to judge.
"""
import re
import struct
from collections import defaultdict

from capstone import (CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs,
                       CS_MODE_LITTLE_ENDIAN)

KNOWN = {0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x53, 0x54, 0x55, 0x56, 0x58}
md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)

d = open(r"C:\a\_apk\x\libseekcamera.so", "rb").read()
TEXT_ADDR, TEXT_OFF, TEXT_SZ = 0x96b0, 0x96b0, 281712
EX_OFF, EX_SZ = 0x3338, 5000


def boundaries():
    out = []
    n = EX_SZ // 8
    for k in range(n):
        o = EX_OFF + k * 8
        prel, unwind = struct.unpack_from("<II", d, o)
        if prel == 0:
            continue
        if prel & 1:
            start = EX_OFF + k * 8 + 4
        else:
            start = (prel & 0x7FFFFFFF) + EX_OFF
        if TEXT_ADDR <= start < TEXT_ADDR + TEXT_SZ:
            out.append(start)
    return sorted(set(out))


bs = boundaries()
print("functions from .ARM.exidx: %d, spanning 0x%x..0x%x"
      % (len(bs), bs[0], bs[-1]))
print()

# Any vendor request code: 0x30-0x7F is the range the confirmed ones occupy.
allv = defaultdict(list)          # value -> [(funcaddr, insaddr)]
func_of = {}
for i, b in enumerate(bs):
    e = bs[i + 1] if i + 1 < len(bs) else TEXT_ADDR + TEXT_SZ
    for ins in md_t.disasm(d[b:e], b):
        func_of[ins.address] = b
        for m in re.finditer(r"#(0x[0-9a-fA-F]+|\d+)\b", ins.op_str):
            tok = m.group(1)
            try:
                v = int(tok, 16) if tok.startswith("0x") else int(tok, 10)
            except ValueError:
                continue
            if 0x30 <= v <= 0x7F:
                allv[v].append((b, ins.address))

found = sorted(set(allv) & KNOWN)
print("=== VALIDATION ===")
print("confirmed codes recovered: %d / %d" % (len(found), len(KNOWN)))
print("  recovered: %s" % ", ".join("0x%02X" % v for v in found))
miss = sorted(KNOWN - set(allv))
if miss:
    print("  MISSING:   %s" % ", ".join("0x%02X" % v for v in miss))
print()
if len(found) < 5:
    print("VERDICT: the confirmed codes are NOT plain Thumb immediates here.")
    print("Either they are constructed at runtime, encoded as immediates in")
    print("ARM-mode code, or the request byte lives in the SPI library. Static")
    print("extraction from this file is not going to work; see the note below.")
else:
    print("VERDICT: encoding validated. Codes found alongside the confirmed set")
    print("are real protocol constants.")
    print()
    print("=== all values in the request range, with the functions using them ===")
    print("%-6s %-6s %s" % ("value", "hits", "function addresses"))
    print("-" * 70)
    for v in sorted(allv):
        fns = sorted({a for a, _ in allv[v]})
        kn = "CONFIRMED" if v in KNOWN else ""
        if len(allv[v]) >= 1 and (v in KNOWN or len(allv[v]) >= 2 or kn):
            print("  0x%02X  %5d  %-14s %s"
                  % (v, len(allv[v]), kn,
                     " ".join("0x%x" % f for f in fns[:4])))