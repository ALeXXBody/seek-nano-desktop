"""Validated request-code sweep of the arm64 libseekcamera.so.

This is the binary actually running on the phone, pulled from the installed
arm64 split - not the armeabi-v7a copy from the APK mirror. Two reasons it is
better than what came before: AArch64 has fixed 4-byte instructions and no
Thumb/ARM interworking, so there is no mode guessing and no bogus decode; and
it is the exact build the phone runs, so any code recovered is the code in use.

Same discipline as before - the sweep is only meaningful if it recovers the ten
request codes this project has confirmed against real hardware. That check runs
first and gates everything else.

Then: boundaries from .ARM.exidx as before, BL targets to build the call graph,
and reachability from the AGC and shutter entry points. A request code reachable
from set_agc_mode but not from the frame path is an AGC command.
"""
import re
import struct
from collections import defaultdict, deque

from capstone import CS_ARCH_ARM64, CS_MODE_ARM, Cs

KNOWN = {0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x53, 0x54, 0x55, 0x56, 0x58}

path = r"C:\a\_apk\arm64\x\libseekcamera.so"
d = open(path, "rb").read()
print("libseekcamera.so (arm64)  %d bytes" % len(d))
assert d[:4] == b"\x7fELF" and d[4] == 2, "expected a 64-bit ELF"
machine, = struct.unpack_from("<H", d, 18)
print("e_machine=%d (183=AArch64)" % machine)

e_shoff, = struct.unpack_from("<I", d, 0x28)
e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x3A)
secs = []
for i in range(e_shnum):
    o = e_shoff + i * e_shentsize
    nm, typ, fl, addr, off, size, link, info, al, ent = \
        struct.unpack_from("<IIQQQQIIQQ", d, o)
    secs.append(dict(nameoff=nm, typ=typ, addr=addr, off=off, size=size,
                     link=link, entsize=ent))
so = secs[e_shstrndx]["off"]


def sname(x):
    e = d.index(b"\0", so + x)
    return d[so + x:e].decode("latin1")


for s in secs:
    s["sname"] = sname(s["nameoff"])

text = next(s for s in secs if s["sname"] == ".text")
print(".text  addr 0x%x  size %d" % (text["addr"], text["size"]))

# ---------------------------------------------------------------- symbols
dynsym = next((s for s in secs if s["sname"] == ".dynsym"), None)
dynstr = next((s for s in secs if s["sname"] == ".dynstr"), None)
byaddr = {}
if dynsym and dynstr:
    for i in range(dynsym["size"] // dynsym["entsize"]):
        o = dynsym["off"] + i * dynsym["entsize"]
        noff, info, other, shndx, val, sz = struct.unpack_from(
            "<IBBHQQ", d, o)
        e = d.index(b"\0", dynstr["off"] + noff)
        nm = d[dynstr["off"] + noff:e].decode("latin1")
        if nm and val:
            byaddr.setdefault(val, nm)
print("dynsym exports: %d" % len(byaddr))
for want in ("seekcamera_set_agc_mode", "seekcamera_set_shutter_mode",
             "seekcamera_shutter_trigger",
             "seekcamera_set_linear_agc_lock_mode",
             "seekcamera_capture_session_start",
             "seekcamera_get_agc_mode"):
    if want in byaddr:
        print("   %-42s 0x%x" % (want, byaddr and
                                 [a for a, n in byaddr.items() if n == want][0]))
print()

md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)


def segoff(v):
    for s in secs:
        if s["typ"] == 1 and s["addr"] and s["addr"] <= v < s["addr"] + s["size"]:
            return s["off"] + (v - s["addr"])
    return None


# boundaries: exidx entries + exports
exidx = next((s for s in secs if s["sname"] == ".ARM.exidx"), None)
starts = set(byaddr)
if exidx:
    ent = 8 if exidx["entsize"] == 0 else exidx["entsize"]
    for k in range(exidx["size"] // ent):
        prel, _ = struct.unpack_from("<II", d, exidx["off"] + k * ent)
        if prel == 0:
            continue
        a = (exidx["off"] + k * ent + 4) if (prel & 1) \
            else ((prel & 0x7FFFFFFF) + exidx["off"])
        if text["addr"] <= a < text["addr"] + text["size"]:
            starts.add(a)
starts = sorted(starts)
print("function starts: %d" % len(starts))

edges = defaultdict(set)
consts = defaultdict(set)
for rounds in range(8):
    before = len(starts)
    for i, b in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else text["addr"] + text["size"]
        o = segoff(b)
        if o is None or end <= b:
            continue
        newt = set()
        for ins in md.disasm(d[o:o + (end - b)], b):
            if ins.mnemonic == "bl":
                m = re.match(r"#(0x[0-9a-fA-F]+)", ins.op_str)
                if m:
                    t = int(m.group(1), 16)
                    edges[b].add(t)
                    newt.add(t)
            for m in re.finditer(r"#(0x[0-9a-fA-F]+)\b", ins.op_str):
                v = int(m.group(1), 16)
                if 0x20 <= v <= 0x7F:
                    consts[b].add(v)
        for t in newt:
            if text["addr"] <= t < text["addr"] + text["size"] and t not in starts:
                starts.append(t)
    starts.sort()
    if len(starts) == before:
        break
print("after fixed point: %d starts, %d edges"
      % (len(starts), sum(len(v) for v in edges.values())))
print()

# ---------------------------------------------------------------- validation
allv = defaultdict(list)
for f, vs in consts.items():
    for v in vs:
        allv[v].append(f)
found = sorted(set(allv) & KNOWN)
print("=== VALIDATION ===")
print("confirmed codes recovered: %d / %d   %s"
      % (len(found), len(KNOWN), " ".join("0x%02X" % v for v in found)))
if len(found) < 8:
    print("  encoding NOT validated - treat any new number as noise")
else:
    print("  encoding validated")
print()
print("%-6s %-6s %s" % ("value", "hits", "functions"))
for v in sorted(allv):
    if v in KNOWN or len(allv[v]) <= 4:
        print("  0x%02X  %5d  %s%s"
              % (v, len(allv[v]), "CONFIRMED" if v in KNOWN else "",
                 "  " + " ".join("0x%x" % f for f in sorted(allv[v])[:5])))

print()
print("=== reachability from the AGC / shutter entry points ===")


def reach(names):
    seen, q, roots = set(), deque(), []
    for nm in names:
        a = next((a for a, n in byaddr.items() if n == nm), None)
        if a is None:
            print("  root missing:", nm)
            continue
        roots.append((nm, a))
        seen.add(a)
        q.append(a)
    while q:
        c = q.popleft()
        for t in edges.get(c, ()):
            if t not in seen:
                seen.add(t)
                q.append(t)
    return roots, seen


GROUPS = {
    "set_agc_mode": ["seekcamera_set_agc_mode"],
    "set_agc_lock": ["seekcamera_set_linear_agc_lock_mode"],
    "set_shutter_mode": ["seekcamera_set_shutter_mode"],
    "shutter_trigger": ["seekcamera_shutter_trigger"],
    "frame_path": ["seekcamera_capture_session_start"],
}
sets = {}
for g, r in GROUPS.items():
    rr, s = reach(r)
    sets[g] = s
    print("  %-20s root 0x%-6x reaches %d functions" % (g, rr[0][1], len(s)))
print()
print("=== codes in the AGC/shutter closure ===")
agcg = ("set_agc_mode", "set_agc_lock", "set_shutter_mode", "shutter_trigger")
for v in sorted(allv):
    hits = {g: sorted(f for f in sets[g] if v in consts.get(f, ())) for g in GROUPS}
    ina = any(hits[g] for g in agcg)
    inf = bool(hits["frame_path"])
    if ina:
        print("  0x%02X %-10s agc/shutter:%s frame_path:%s   %s"
              % (v, "CONFIRMED" if v in KNOWN else "candidate",
                 "YES" if ina else "-", "yes" if inf else "no",
                 " ".join("0x%x" % f for f in sorted(set(sum((hits[g] for g in agcg), [])))[:4])))
print()
print("=== unique to the AGC/shutter closure (not on the frame path) ===")
for v in sorted(allv):
    ina = any(v in consts.get(f, ()) for g in agcg for f in sets[g])
    inf = v in consts.get(next(iter([f for f in sets["frame_path"]
                                     if v in consts.get(f, ())]), 0), ()) \
        if sets["frame_path"] else False
    if ina and not inf:
        fns = sorted(set(f for g in agcg for f in sets[g]
                         if v in consts.get(f, ())))
        print("  0x%02X  %d fn(s): %s"
              % (v, len(fns), ", ".join("%s@0x%x" % (byaddr.get(f, "?"), f)
                                        for f in fns[:5])))