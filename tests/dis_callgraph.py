"""Name the AGC and shutter request codes by call-graph intersection.

Everything needed is in place: 621 exact function boundaries from .ARM.exidx,
a frequency-based discriminator that separates real request codes from ordinary
immediates, and ten confirmed codes for calibration.

What this adds is reachability. A request code only matters if the function
holding it is reachable from the entry point that uses it. So:

  1. Decode every function, resolve Thumb BL targets, build the call graph.
  2. Map PLT stubs to their imported symbols via .rel.plt, so a call out to
     the USB transport is visible by name rather than as an address.
  3. BFS from seekcamera_set_agc_mode, set_shutter_mode and shutter_trigger.
  4. Intersect the reachable set with the rare request-code candidates.

A candidate reachable from set_agc_mode but not from, say, the frame kick is an
AGC command. That is the inference, and it is stated as such - the report gives
the evidence (which entry points reach which candidate) rather than asserting a
mapping.

Also prints the PLT import table, because knowing which symbols the SDK pulls in
tells us where the real IO boundary is.
"""
import re
import struct
from collections import defaultdict, deque

from capstone import (CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs,
                       CS_MODE_LITTLE_ENDIAN)

KNOWN = {0x36, 0x37, 0x3C, 0x3D, 0x3E, 0x53, 0x54, 0x55, 0x56, 0x58}
CAND = [0x35, 0x3B, 0x43, 0x45, 0x47, 0x52, 0x59, 0x5A, 0x5E, 0x61, 0x6A]

d = open(r"C:\a\_apk\x\libseekcamera.so", "rb").read()

e_shoff, = struct.unpack_from("<I", d, 0x20)
ent, num, shstrndx = struct.unpack_from("<HHH", d, 0x2E)
secs = []
for i in range(num):
    o = e_shoff + i * ent
    v = struct.unpack_from("<10I", d, o)
    secs.append(dict(name=v[0], typ=v[1], addr=v[3], off=v[4], size=v[5],
                     link=v[6], entsize=v[9]))
so = secs[shstrndx]["off"]
for s in secs:
    e = d.index(b"\0", so + s["name"])
    s["sname"] = d[so + s["name"]:e].decode("latin1")

TEXT_A, TEXT_O, TEXT_S = 0x96B0, 0x96B0, 281712
PLT_A, PLT_O, PLT_S = 0x4E320, 0x4E320, 2256
EX_O, EX_S = 0x3338, 5000


def segoff(v):
    for s in secs:
        if s["typ"] == 1 and s["addr"] and s["addr"] <= v < s["addr"] + s["size"]:
            return s["off"] + (v - s["addr"])
    return None


# ------------------------------------------------------------ boundaries
bs = set()
n = EX_S // 8
for k in range(n):
    o = EX_O + k * 8
    prel, _u = struct.unpack_from("<II", d, o)
    if prel == 0:
        continue
    start = (EX_O + k * 8 + 4) if (prel & 1) else ((prel & 0x7FFFFFFF) + EX_O)
    if TEXT_A <= start < TEXT_A + TEXT_S:
        bs.add(start)
bs = sorted(bs)

# ------------------------------------------------------------ symbols
dynsym = next(s for s in secs if s["sname"] == ".dynsym")
# The library is stripped: there is no .strtab, only .dynstr.
dynstr = next(s for s in secs if s["sname"] == ".dynstr")
byaddr = {}
byval = {}
for i in range(dynsym["size"] // dynsym["entsize"]):
    o = dynsym["off"] + i * dynsym["entsize"]
    noff, val, size, info, other, shndx = struct.unpack_from("<IIIBBH", d, o)
    e = d.index(b"\0", dynstr["off"] + noff)
    nm = d[dynstr["off"] + noff:e].decode("latin1")
    if nm and val:
        real = val & ~1
        byval[real] = (nm, size)
        byaddr.setdefault(real, nm)

# ------------------------------------------------------------ PLT -> symbol
relplt = next((s for s in secs if s["sname"] == ".rel.plt"), None)
plt_map = {}
if relplt:
    got = next(s for s in secs if s["sname"] == ".got.plt")
    # PLT stubs are 16 bytes each on armeabi-v7
    for i in range(relplt["size"] // relplt["entsize"]):
        off, info = struct.unpack_from("<II", d, relplt["off"] + i * 8)
        symidx = info >> 8
        o = dynsym["off"] + symidx * dynsym["entsize"]
        noff, val, size, info2, other, shndx = struct.unpack_from("<IIIBBH", d, o)
        e = d.index(b"\0", dynstr["off"] + noff)
        nm = d[dynstr["off"] + noff:e].decode("latin1")
        stub = PLT_A + 0x14 + i * 0x10
        plt_map[stub] = nm

print("PLT imports (%d):" % len(plt_map))
for a in sorted(plt_map):
    if "seek" in plt_map[a] or "usb" in plt_map[a].lower() \
            or "io" in plt_map[a].lower() or "control" in plt_map[a].lower():
        print("   0x%06x  %s" % (a, plt_map[a]))
print()

# ------------------------------------------------------------ call graph
md_t = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
edges = defaultdict(set)
consts = defaultdict(set)
strs = defaultdict(set)

for i, b in enumerate(bs):
    end = bs[i + 1] if i + 1 < len(bs) else TEXT_A + TEXT_S
    o = segoff(b)
    if o is None:
        continue
    code = d[o:o + (end - b)]
    for ins in md_t.disasm(code, b):
        mn = ins.mnemonic
        if mn in ("bl", "blx"):
            m = re.match(r"#(0x[0-9a-fA-F]+)", ins.op_str)
            if m:
                edges[b].add(int(m.group(1), 16) & ~1)
        for m in re.finditer(r"#(0x[0-9a-fA-F]+|\d+)\b", ins.op_str):
            tok = m.group(1)
            try:
                v = int(tok, 16) if tok.startswith("0x") else int(tok, 10)
            except ValueError:
                continue
            if 0x30 <= v <= 0x7F:
                consts[b].add(v)

print("exidx boundaries: %d" % len(bs))
print("exports not covered by exidx: %d"
      % len([a for a in byaddr if a not in set(bs)]))

# .ARM.exidx only lists functions that carry unwind information, and it does
# NOT cover every export. The first pass reported "reaches 1 functions" for
# every root because the roots were never in the boundary list, so
# edges[root] was never created and the BFS had nothing to follow. Merging the
# exported addresses in gives each root a real extent, taken from the next
# boundary above it.
merged = sorted(set(bs) | set(byaddr))
edges.clear()
consts.clear()


def decode_all(starts):
    """Decode every known start; return the set of new BL targets seen."""
    newt = set()
    for i, b in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else TEXT_A + TEXT_S
        # .ARM.exidx contains compact-model entries whose computed address lands
        # MID-FUNCTION, so "next boundary" can be 4 bytes away and the decode
        # then misses every BL in the body - which is exactly what happened:
        # set_agc_mode reported one edge when it has two. Where .dynsym gives an
        # authoritative size, trust it over the boundary list.
        if b in byval:
            sym_end = b + byval[b][1]
            if sym_end > end:
                end = sym_end
        o = segoff(b)
        if o is None or end <= b:
            continue
        for ins in md_t.disasm(d[o:o + (end - b)], b):
            if ins.mnemonic in ("bl", "blx"):
                m = re.match(r"#(0x[0-9a-fA-F]+)", ins.op_str)
                if m:
                    t = int(m.group(1), 16) & ~1
                    edges[b].add(t)
                    newt.add(t)
            for m in re.finditer(r"#(0x[0-9a-fA-F]+|\d+)\b", ins.op_str):
                tok = m.group(1)
                try:
                    v = int(tok, 16) if tok.startswith("0x") else int(tok, 10)
                except ValueError:
                    continue
                if 0x30 <= v <= 0x7F:
                    consts[b].add(v)
    return newt


# Fixed point. Internal helpers are neither exported nor in exidx, so a single
# pass only ever decodes the outermost layer and the graph looks two deep. Each
# BL target becomes a start address in turn, until no new ones appear - that is
# what actually recovers the transitive closure.
rounds = 0
seen_starts = set(merged)
while rounds < 12:
    rounds += 1
    merged = sorted(seen_starts)
    newt = decode_all(merged)
    fresh = {t for t in newt
             if TEXT_A <= t < TEXT_A + TEXT_S and t not in seen_starts}
    if not fresh:
        break
    seen_starts |= fresh

print("decode rounds to fixed point: %d" % rounds)
print("merged boundaries: %d" % len(seen_starts))
print("call graph: %d nodes with edges, %d edges"
      % (len(edges), sum(len(v) for v in edges.values())))

# ------------------------------------------------------------ reachability
def reach(root_names):
    """Transitive callees of the named entry points."""
    seen = set()
    q = deque()
    roots = []
    for rn in root_names:
        hit = None
        for addr, nm in byaddr.items():
            if nm == rn:
                hit = addr
                break
        if hit is None:
            print("  root not found:", rn)
            continue
        roots.append((rn, hit))
        seen.add(hit)
        q.append(hit)
    while q:
        cur = q.popleft()
        for t in edges.get(cur, ()):
            if t not in seen:
                seen.add(t)
                q.append(t)
    return roots, seen


GROUPS = {
    "set_agc_mode": ["seekcamera_set_agc_mode"],
    "set_linear_agc_lock_mode": ["seekcamera_set_linear_agc_lock_mode"],
    "set_shutter_mode": ["seekcamera_set_shutter_mode"],
    "shutter_trigger": ["seekcamera_shutter_trigger"],
    "capture_session_start": ["seekcamera_capture_session_start"],
}

sets = {}
for g, roots in GROUPS.items():
    rr, seen = reach(roots)
    sets[g] = seen
    print("%-26s root 0x%06x   reaches %d functions" % (g, rr[0][1] if rr else 0, len(seen)))
print()

# ------------------------------------------------------------ intersection
print("=== candidate request codes per entry point ===")
print("%-8s %s" % ("code", "  ".join("%-24s" % g for g in GROUPS)))
print("-" * 110)
rows = {}
for v in sorted(set(CAND) | KNOWN):
    hits = {}
    for g in GROUPS:
        f = sorted(a for a in sets[g] if v in consts.get(a, ()))
        hits[g] = f
    if any(hits.values()):
        mark = "CONFIRMED" if v in KNOWN else "candidate"
        print("0x%02X %-9s %s" % (v, mark,
              "  ".join("%-24s" % ("%d fn" % len(hits[g]) if hits[g] else "-")
                        for g in GROUPS)))
        rows[v] = hits

print()
print("=== discriminating: reachable from AGC/shutter but NOT from capture_session_start ===")
for v in sorted(set(CAND) | KNOWN):
    h = rows.get(v)
    if not h:
        continue
    agcish = h["set_agc_mode"] or h["set_linear_agc_lock_mode"] \
        or h["set_shutter_mode"] or h["shutter_trigger"]
    if agcish and not h["capture_session_start"]:
        fns = sorted(set(sum((h[g] for g in
                              ("set_agc_mode", "set_linear_agc_lock_mode",
                               "set_shutter_mode", "shutter_trigger")), [])))
        print("  0x%02X  in %d function(s): %s"
              % (v, len(fns), ", ".join("0x%x" % f for f in fns[:6])))
        for f in fns[:3]:
            print("        0x%06x  %s" % (f, byaddr.get(f, "(internal)")))

print()
print("=== PLT calls made from the AGC/shutter closure ===")
calls = defaultdict(int)
for g in ("set_agc_mode", "set_shutter_mode", "shutter_trigger",
          "set_linear_agc_lock_mode"):
    for f in sets[g]:
        for t in edges.get(f, ()):
            if t in plt_map:
                calls[plt_map[t]] += 1
for nm, c in sorted(calls.items(), key=lambda kv: -kv[1])[:14]:
    print("   %-42s %d call site(s)" % (nm[:42], c))