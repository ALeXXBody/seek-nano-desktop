"""Read the whole USB transport, not one function.

Tracing 0x12e3c linearly is slow and the generic key writer may not even be the
path. The transport itself is tiny: libseekusb.so is 21,576 bytes. That is
around 5,400 Thumb instructions - small enough to take apart wholesale rather
than by sampling.

Two passes:

1. CONSTANT HARVEST. Every immediate that looks like a vendor request code is
   collected with the function it sits in. Calibrated against the codes this
   project already knows work on real hardware - 0x36, 0x37, 0x3C, 0x3D, 0x3E,
   0x53, 0x54, 0x55, 0x56, 0x58 - so the encoding style is identified from ground
   truth rather than guessed. If the known codes appear in the harvest, the
   unknown neighbours in the same functions are the AGC/shutter ones.

2. STRUCTURE. Every exported function, its size, and what it calls. A function
   that takes a request code and a buffer is the control-transfer wrapper; from
   there the callers that pass 0x20xx-style command words become visible.

Also sweeps libseekcamera.so's writable data for a table of small integers,
because a command-id table is the other likely shape and it is cheap to check.
"""
import re
import struct
from collections import defaultdict

from capstone import (CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs,
                       CS_MODE_LITTLE_ENDIAN)

KNOWN = {0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x53, 0x54, 0x55, 0x56, 0x58}
md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
md_a = Cs(CS_ARCH_ARM, CS_MODE_ARM | CS_MODE_LITTLE_ENDIAN)


class Elf:
    def __init__(self, path):
        self.path = path
        self.d = open(path, "rb").read()
        d = self.d
        e_shoff, = struct.unpack_from("<I", d, 0x20)
        ent, num, shstrndx = struct.unpack_from("<HHH", d, 0x2E)
        self.secs = []
        for i in range(num):
            o = e_shoff + i * ent
            v = struct.unpack_from("<10I", d, o)
            self.secs.append(dict(name=v[0], typ=v[1], addr=v[3], off=v[4],
                                  size=v[5], link=v[6], entsize=v[9]))
        so = self.secs[shstrndx]["off"]
        for s in self.secs:
            e = d.index(b"\0", so + s["name"])
            s["sname"] = d[so + s["name"]:e].decode("latin1")
        self.segs = [(s["addr"], s["off"], s["size"]) for s in self.secs
                     if s["typ"] == 1 and s["addr"] != 0]
        self.syms = self._dynsym()

    def _dynsym(self):
        out = {}
        ds = next((s for s in self.secs if s["sname"] == ".dynsym"), None)
        st = next((s for s in self.secs if s["sname"] == ".strtab"), None)
        if not ds or not st:
            return out
        for i in range(ds["size"] // ds["entsize"]):
            o = ds["off"] + i * ds["entsize"]
            noff, val, size, info, other, shndx = struct.unpack_from(
                "<IIIBBH", self.d, o)
            e = self.d.index(b"\0", st["off"] + noff)
            n = self.d[st["off"] + noff:e].decode("latin1")
            if n and val:
                out[n] = (val, size)
        return out

    def off(self, v):
        for a, o, sz in self.segs:
            if a <= v < a + sz:
                return o + (v - a)
        return None

    def sec(self, name):
        return next((s for s in self.secs if s["sname"] == name), None)

    def funcs(self):
        """(name, vaddr, size, thumb) for named functions, sorted by address."""
        out = []
        for n, (v, sz) in self.syms.items():
            if v & 1:
                out.append((n, v & ~1, sz, True))
            else:
                out.append((n, v, sz, False))
        out.sort(key=lambda t: t[1])
        return out


def analyse(path, label):
    e = Elf(path)
    text = e.sec(".text")
    print("=" * 74)
    print("%s   %s   %d bytes" % (label, path.rsplit("\\", 1)[-1], len(e.d)))
    print("=" * 74)
    fs = e.funcs()
    print("functions with symbols: %d" % len(fs))
    print()
    known_hits = defaultdict(list)
    rows = []
    for name, addr, size, thumb in fs:
        o = e.off(addr)
        if o is None:
            continue
        n = size if size else 128
        code = e.d[o:o + n]
        md = md_t if thumb else md_a
        calls = []
        consts = set()
        for ins in md.disasm(code, addr):
            if ins.mnemonic.startswith("bl"):
                calls.append(ins.op_str)
            for m in re.finditer(r"#(0x[0-9a-fA-F]+|\d+)", ins.op_str):
                try:
                    v = int(m.group(1), 0)
                except ValueError:
                    continue
                if 0 < v < 0x100 and v in KNOWN:
                    consts.add(v)
        rows.append((name, addr, n, thumb, len(calls), sorted(consts)))
        for c in consts:
            known_hits[c].append(name)

    print("request-code constants found, with the functions containing them:")
    print("  (0x36..0x58 are CONFIRMED working on this hardware by this project)")
    print()
    for c in sorted(KNOWN):
        fs_ = known_hits.get(c, [])
        print("  0x%02X  %-2d occurrence(s)  %s"
              % (c, len(fs_), ", ".join(fs_[:4])))
    print()
    print("%-52s %-9s %5s %s" % ("function", "addr", "bytes", "calls"))
    print("-" * 74)
    for name, addr, n, thumb, nc, consts in rows:
        if nc == 0 and not consts:
            continue
        print("%-52s 0x%06x %5d %s%s"
              % (name[:52], addr, n, "%d" % nc,
                 ("  consts " + ",".join(hex(c) for c in consts)) if consts else ""))
    return e, known_hits


analyse(r"C:\a\_apk\x\libseekusb.so", "USB transport")

print()
print("=" * 74)
print("command-word table hunt in libseekcamera.so .data / .rodata")
print("=" * 74)
c = Elf(r"C:\a\_apk\x\libseekcamera.so")
for sname in (".data", ".data.rel.ro", ".rodata"):
    s = c.sec(sname)
    if not s:
        continue
    base, off, size = s["addr"], s["off"], s["size"]
    print()
    print("--- %s  addr 0x%06x  %d bytes ---" % (sname, base, size))
    # look for runs of 16-bit values that look like request codes
    best = []
    i = 0
    while i + 8 <= size:
        vals = struct.unpack_from("<4H", c.d, off + i)
        if all(0x30 <= v <= 0x7F for v in vals) and len(set(vals)) >= 3:
            j = i
            run = []
            while j + 2 <= size:
                v, = struct.unpack_from("<H", c.d, off + j)
                if 0x30 <= v <= 0x7F:
                    run.append(v)
                    j += 2
                else:
                    break
            if len(run) >= 4:
                best.append((base + i, run))
            i = j
        else:
            i += 2
    if best:
        for addr, run in best[:8]:
            print("   0x%06x  %s" % (addr, " ".join("%02x" % v for v in run[:24])))
    else:
        print("   no run of 4+ request-code-shaped 16-bit values found")