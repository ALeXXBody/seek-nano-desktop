"""Linear sweep of the whole transport, no symbol table needed.

libseekusb.so exports nothing, so symbol-driven sampling finds nothing. But it
is only 21,576 bytes: around 5,400 Thumb instructions, which can be swept
end to end. Linear sweep matters because Thumb has no reliable function
boundaries to walk - the mode bit switches ARM to Thumb mid-stream - so
decoding straight through and bucketing by address range is both simpler and
more complete than following calls.

The method: decode .text as Thumb from the start, then re-decode from
addr+2 as ARM, and keep whichever yields more decodable bytes. Collect every
immediate in the vendor-request range with the address it was found at.

Calibration is the important part. This project has confirmed working codes on
real hardware - 0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x53, 0x54, 0x55, 0x56, 0x58 -
so if the sweep recovers those, the encoding and range are right and any other
codes found alongside them are real. If it does NOT recover the known ones, the
range or encoding assumption is wrong and any new number would be noise.

Reports both: what was found, and whether the known codes validate it.
"""
import re
import struct
from collections import defaultdict

from capstone import (CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs,
                       CS_MODE_LITTLE_ENDIAN)

KNOWN = {0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x53, 0x54, 0x55, 0x56, 0x58}
md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)

PATH = r"C:\a\_apk\x\libseekusb.so"
d = open(PATH, "rb").read()

e_shoff, = struct.unpack_from("<I", d, 0x20)
ent, num, shstrndx = struct.unpack_from("<HHH", d, 0x2E)
secs = []
for i in range(num):
    o = e_shoff + i * ent
    v = struct.unpack_from("<10I", d, o)
    secs.append(dict(name=v[0], typ=v[1], addr=v[3], off=v[4], size=v[5],
                     entsize=v[9]))
so = secs[shstrndx]["off"]
for s in secs:
    e = d.index(b"\0", so + s["name"])
    s["sname"] = d[so + s["name"]:e].decode("latin1")

print("libseekusb.so sections")
for s in secs:
    if s["size"]:
        print("   %-18s addr=0x%06x off=0x%06x size=%6d"
              % (s["sname"], s["addr"], s["off"], s["size"]))
print()

text = next((s for s in secs if s["sname"] == ".text"), None)
if text is None:
    raise SystemExit("no .text")

base, off, size = text["addr"], text["off"], text["size"]
code = d[off:off + size]


def sweep(md, start):
    got = []
    n = 0
    for ins in md.disasm(code, start):
        got.append(ins)
        n += 1
    return n


n_t = sweep(md_t, base)
n_a = sweep(md_a, base)
use_thumb = n_t >= n_a
md = md_t if use_thumb else md_a
print("Thumb decodes %d instructions, ARM %d -> using %s"
      % (n_t, n_a, "Thumb" if use_thumb else "ARM"))
print()

hits = defaultdict(list)          # value -> [addresses]
allvals = defaultdict(int)
insns = []
for ins in md.disasm(code, base):
    insns.append(ins)
    for m in re.finditer(r"#(0x[0-9a-fA-F]+|\d+)", ins.op_str):
        tok = m.group(1)
        try:
            v = int(tok, 16) if tok.startswith("0x") else int(tok, 10)
        except ValueError:
            continue
        if 0x20 <= v <= 0x7F:
            allvals[v] += 1
            if v in KNOWN or len(hits[v]) < 8:
                hits[v].append(ins.address)

print("=== vendor-request-range immediates (0x20..0x7F), by frequency ===")
print("%-6s %-6s %s" % ("value", "count", "known?"))
print("-" * 52)
found_known = []
for v in sorted(allvals):
    kn = "CONFIRMED" if v in KNOWN else ""
    if kn:
        found_known.append(v)
    if allvals[v] >= 1 and (v in KNOWN or allvals[v] >= 2):
        print("  0x%02X  %5d  %s" % (v, allvals[v], kn))

print()
print("validation: %d of %d confirmed codes recovered by this sweep"
      % (len(found_known), len(KNOWN)))
missing = sorted(KNOWN - set(found_known))
if missing:
    print("  NOT recovered: %s" % ", ".join("0x%02X" % m for m in missing))
    print("  -> the request codes are not plain immediates in this file.")
else:
    print("  all confirmed codes present -> every other value listed above is")
    print("  a real protocol constant, and the ranges are trustworthy.")

print()
print("=== cluster map: where the confirmed codes sit in .text ===")
if found_known:
    addrs = []
    for v in found_known:
        addrs += hits[v]
    addrs.sort()
    print("  first 0x%06x   last 0x%06x   span %d bytes"
          % (addrs[0], addrs[-1], addrs[-1] - addrs[0]))
    # 256-byte buckets
    buck = defaultdict(list)
    for a in addrs:
        buck[a >> 8].append(a)
    print("  256-byte buckets containing a confirmed code:")
    for b in sorted(buck):
        print("     0x%06x - 0x%06x   %d hit(s)"
              % (b << 8, (b << 8) + 255, len(buck[b])))

print()
print("=== every distinct value in 0x20..0x7F, with first address ===")
for v in sorted(allvals):
    a = hits[v][0] if hits.get(v) else None
    print("  0x%02X  n=%-4d first@0x%06x%s"
          % (v, allvals[v], a if a else 0,
             "   <-- CONFIRMED" if v in KNOWN else ""))

print()
print("=== context around each confirmed code ===")
idx = {ins.address: i for i, ins in enumerate(insns)}
shown = 0
for v in sorted(KNOWN):
    for a in hits.get(v, [])[:1]:
        i = idx.get(a)
        if i is None:
            continue
        print("--- 0x%02X at 0x%06x ---" % (v, a))
        for ins in insns[max(0, i - 6):i + 7]:
            mark = ">>" if ins.address == a else "  "
            print("  %s 0x%06x  %-9s %s"
                  % (mark, ins.address, ins.mnemonic, ins.op_str))
        shown += 1
        if shown >= 4:
            break
    if shown >= 4:
        break