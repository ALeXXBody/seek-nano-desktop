"""Disassemble the SDK's AGC/NUC entry points and recover the USB protocol.

Have: libseekcamera.so, armeabi-v7a, 367 KB, from the official APK
(sha256 of the APK matched the recorded value).

Want: what `seekcamera_set_agc_mode`, `seekcamera_set_linear_agc_lock_mode`
and `seekcamera_shutter_trigger` actually send on the wire. The app currently
has no idea - it locks gain by discarding frames - so this is the piece that
would let it lock gain properly, or run the camera's own AGC.

Method: parse the ELF symbol table to get each function's address and size, pull
the .rodata strings it points at, and disassemble with capstone so the
immediates - which is where a USB request code and its payload live - can be
read directly. ARM loads addresses via literal pools, so the disassembly is
traced for PC-relative loads to recover string references.
"""
import re
import struct
import sys

from capstone import (CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs,
                       CS_MODE_LITTLE_ENDIAN)

SO = r"C:\a\_apk\x\libseekcamera.so"
d = open(SO, "rb").read()

# ---------------------------------------------------------------- ELF parse
assert d[:4] == b"\x7fELF", "not an ELF"
e_shoff, = struct.unpack_from("<I", d, 0x20)
e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x2E)


def sh(i):
    o = e_shoff + i * e_shentsize
    name, typ, flags, addr, off, size, link, info, align, entsize = \
        struct.unpack_from("<10I", d, o)
    return dict(name=name, typ=typ, addr=addr, off=off, size=size,
                link=link, entsize=entsize)


secs = [sh(i) for i in range(e_shnum)]
stroff = secs[e_shstrndx]["off"]


def sname(x):
    e = d.index(b"\0", stroff + x)
    return d[stroff + x:e].decode("latin1")


for s in secs:
    s["sname"] = sname(s["name"])

# symtab / strtab
symtab = next((s for s in secs if s["sname"] == ".symtab"), None)
strtab = next((s for s in secs if s["sname"] == ".strtab"), None)
syms = {}
if symtab and strtab:
    n = symtab["size"] // symtab["entsize"]
    for i in range(n):
        o = symtab["off"] + i * symtab["entsize"]
        nameoff, value, size, info, other, shndx = \
            struct.unpack_from("<IIIBBH", d, o)
        e = d.index(b"\0", strtab["off"] + nameoff)
        nm = d[strtab["off"] + nameoff:e].decode("latin1")
        if nm:
            syms[nm] = (value, size, info & 0xF, info >> 4, shndx)

print("ELF: %d bytes, %d symbols" % (len(d), len(syms)))

# ---------------------------------------------------------------- strings
def cstrings(lo, hi, minlen=4):
    out = []
    i = lo
    while i < hi:
        j = i
        while j < hi and 32 <= d[j] < 127:
            j += 1
        if j - i >= minlen:
            out.append((i, d[i:j].decode("latin1")))
        i = max(j + 1, i + 1)
    return out


ro = next((s for s in secs if s["sname"] in (".rodata",)), None)
allstr = []
if ro:
    allstr = cstrings(ro["off"], ro["off"] + ro["size"])
print("rodata strings: %d\n" % len(allstr))

KEY = re.compile(r"agc|nuc|shutter|flat|gain|pipeline|filter|histeq|"
                 r"lock|plateau|roi|trim|emissivity", re.I)
hits = [(o, s) for o, s in allstr if KEY.search(s)]
print("=== config-shaped strings in .rodata ===")
for o, s in hits[:70]:
    print("   0x%06x  %s" % (o, s[:96]))
print()

# ---------------------------------------------------------------- disasm
def thumb_for(name):
    v = syms.get(name)
    if not v:
        return None
    val, size, stype, bind, shndx = v
    thumb = (bind == 1)          # STB_GLOBAL usually Thumb on Android
    return val, size, thumb


md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)
md_t.detail = False
md_a.detail = False

# file offset for a vaddr
segs = [(s["addr"], s["off"], s["size"], s["sname"]) for s in secs
        if s["typ"] == 1 and s["addr"] != 0]


def to_off(vaddr):
    for a, o, sz, nm in segs:
        if a <= vaddr < a + sz:
            return o + (vaddr - a)
    return None


def disasm(name, maxins=90):
    t = thumb_for(name)
    if not t:
        print("  %-42s NOT FOUND" % name)
        return
    addr, size, thumb = t
    off = to_off(addr)
    if off is None:
        print("  %-42s addr 0x%x unmapped" % (name, addr))
        return
    n = size if size else 256
    code = d[off:off + n]
    md = md_t if thumb else md_a
    print("=== %s   @0x%06x  %d bytes  %s ==="
          % (name, addr, n, "Thumb" if thumb else "ARM"))
    count = 0
    for ins in md.disasm(code, addr):
        line = "  0x%06x  %-10s %s" % (ins.address, ins.mnemonic, ins.op_str)
        print(line)
        count += 1
        if count >= maxins:
            print("   ... (truncated)")
            break
    print()


TARGETS = [
    "seekcamera_set_agc_mode",
    "seekcamera_get_agc_mode",
    "seekcamera_set_linear_agc_lock_mode",
    "seekcamera_set_pipeline_mode",
    "seekcamera_shutter_trigger",
]
for t in TARGETS:
    disasm(t)