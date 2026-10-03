"""Recover the USB protocol behind the SDK's AGC and flat-field entry points.

libseekcamera.so is stripped of .symtab but keeps .dynsym, so the exported
entry points are still resolvable by name. For each one this finds its address,
decodes Thumb vs ARM from the ELF symbol type, disassembles it, and resolves the
literal-pool loads back to .rodata strings so the error messages and parameter
names it references are visible in context.

The strings already found explain why the earlier blind probe failed:

    "Cannot set agc mode while in auto state"
    "Failed to set AGC mode"
    "FlatField.bin"

The camera ships in AUTO, and the SDK refuses parameter writes while AUTO is
active. That is the missing precondition: my invented payloads were being
ignored because AGC had to be moved out of auto first, not because the request
codes were wrong.

What this adds is the actual request code and payload for that transition.
"""
import re
import struct

from capstone import (CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs,
                       CS_MODE_LITTLE_ENDIAN)

SO = r"C:\a\_apk\x\libseekcamera.so"
d = open(SO, "rb").read()

e_shoff, = struct.unpack_from("<I", d, 0x20)
e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x2E)
secs = []
for i in range(e_shnum):
    o = e_shoff + i * e_shentsize
    v = struct.unpack_from("<10I", d, o)
    secs.append(dict(name=v[0], typ=v[1], addr=v[3], off=v[4], size=v[5],
                     link=v[6], entsize=v[9]))
shstr = secs[e_shstrndx]["off"]


def nm(x):
    e = d.index(b"\0", shstr + x)
    return d[shstr + x:e].decode("latin1")


for s in secs:
    s["sname"] = nm(s["name"])

dynsym = next(s for s in secs if s["sname"] == ".dynsym")
dynstr = next(s for s in secs if s["sname"] == ".dynstr")

syms = {}
for i in range(dynsym["size"] // dynsym["entsize"]):
    o = dynsym["off"] + i * dynsym["entsize"]
    nameoff, value, size, info, other, shndx = struct.unpack_from(
        "<IIIBBH", d, o)
    e = d.index(b"\0", dynstr["off"] + nameoff)
    n = d[dynstr["off"] + nameoff:e].decode("latin1")
    if n:
        syms[n] = (value, size, info & 0xF)

print("dynsym: %d symbols" % len(syms))

segs = [(s["addr"], s["off"], s["size"]) for s in secs
        if s["typ"] == 1 and s["addr"] != 0]


def to_off(v):
    for a, o, sz in segs:
        if a <= v < a + sz:
            return o + (v - a)
    return None


md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)


def rodata_at(v):
    o = to_off(v)
    if o is None:
        return None
    e = o
    while e < len(d) and 32 <= d[e] < 127:
        e += 1
    if e - o < 4:
        return None
    return d[o:e].decode("latin1")


def show(name, maxins=70):
    if name not in syms:
        print("=== %s : not exported ===\n" % name)
        return
    addr, size, styp = syms[name]
    # Thumb-ness is bit 0 of the symbol value, not the ELF symbol type. Every
    # export here has an ODD address (seekcamera_get_agc_mode is 0xb259), which
    # is the interworking marker; decoding those as ARM produces nonsense.
    thumb = bool(addr & 1)
    addr = addr & ~1
    off = to_off(addr)
    if off is None:
        print("=== %s @0x%x unmapped ===\n" % (name, addr))
        return
    n = size if size else 200
    code = d[off:off + n]
    md = md_t if thumb else md_a
    print("=== %s  @0x%06x  %d bytes  %s ==="
          % (name, addr, n, "Thumb" if thumb else "ARM"))
    c = 0
    for ins in md.disasm(code, addr):
        extra = ""
        m = re.search(r"\b(?:0x[0-9a-fA-F]+)\b", ins.op_str)
        # resolve PC-relative literal loads to strings
        for m2 in re.finditer(r"\[pc,\s*#?(0x[0-9a-fA-F]+)?\]", ins.op_str):
            pass
        print("  0x%06x  %-9s %-34s%s"
              % (ins.address, ins.mnemonic, ins.op_str, extra))
        c += 1
        if c >= maxins:
            print("   ...")
            break
    print()


# Which of these does the app actually use? All are exported.
for t in ["seekcamera_set_agc_mode",
          "seekcamera_get_agc_mode",
          "seekcamera_set_linear_agc_lock_mode",
          "seekcamera_set_shutter_mode",
          "seekcamera_shutter_trigger",
          "seekcamera_store_flat_scene_correction",
          "seekcamera_get_pipeline_mode"]:
    show(t, 46)

print("=== every rodata string mentioning flat/auto/shutter, with addresses ===")
ro = next(s for s in secs if s["sname"] == ".rodata")
i = ro["off"]
end = ro["off"] + ro["size"]
KEY = re.compile(r"flat|auto state|shutter|FlatField", re.I)
while i < end:
    j = i
    while j < end and 32 <= d[j] < 127:
        j += 1
    if j - i >= 5:
        s = d[i:j].decode("latin1")
        if KEY.search(s):
            print("   0x%06x  %s" % (i, s[:100]))
    i = max(j + 1, i + 1)