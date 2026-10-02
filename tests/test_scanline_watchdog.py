"""The scanline watchdog must fire on a real corrupted row and stay quiet on a
clean one, or it is just noise in the log.

Builds a clean frame, then injects the fault as it actually appears on screen:
one row of alternating extreme values. Checks the detector scores the bad row
far above the clean one, and that the threshold sits where clean live frames
stay under it.
"""
import ast
import os
import pathlib
import re
import sys
import time

import numpy as np

sys.path.insert(0, r"C:\a\src\app")
SRC = pathlib.Path(r"C:\a\src\app\viewer.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
NS = {"np": np, "time": time, "os": os, "sys": sys,
      "__file__": r"C:\a\src\app\viewer.py", "__name__": "viewer",
      "struct": __import__("struct")}


def _vlog(*_a, **_k):
    pass


NS["_vlog"] = _vlog
for node in TREE.body:
    if isinstance(node, ast.Assign):
        try:
            val = eval(compile(ast.Expression(node.value), "<c>", "eval"),
                       dict(NS))
        except Exception:
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                NS[tgt.id] = val
            elif isinstance(tgt, ast.Tuple) and isinstance(val, (tuple, list)):
                for sub, v in zip(tgt.elts, val):
                    if isinstance(sub, ast.Name):
                        NS[sub.id] = v
_fn = next(n for c in TREE.body if isinstance(c, ast.ClassDef)
           for n in c.body
           if isinstance(n, ast.FunctionDef) and n.name == "_scanline_health")
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "v", "exec"), NS)

IW, IH = 320, 240
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


class Q:
    def __init__(self):
        self.msgs = []

    def put(self, t):
        self.msgs.append(t)


class P:
    _scanline_health = NS["_scanline_health"]

    def __init__(self):
        self.q = Q()
        self.frame_raw = np.zeros((IH, IW), np.uint16)
        self._scan_n = 0
        self._scan_last_save = 0.0


def zscore(img):
    prof = np.abs(np.diff(img, axis=1)).mean(axis=1)
    med = float(np.median(prof))
    mad = float(np.median(np.abs(prof - med)))
    return prof / max(med + 3.0 * mad, 1e-3)


rng = np.random.default_rng(7)
clean = (6000 + 40 * np.sin(np.mgrid[0:IH, 0:IW][1] / 11.0)
         + rng.normal(0, 8, (IH, IW))).astype(np.float32)
# a realistic textured scene too, since the fault was seen on a busy one
busy = (6000 + 160 * np.sin(np.mgrid[0:IH, 0:IW][1] / 11.0)
        + rng.normal(0, 20, (IH, IW))).astype(np.float32)

print("1. a clean frame scores low and does NOT save a dump")
for name, img in (("smooth", clean), ("textured", busy)):
    z = zscore(img)
    p = P()
    p._scanline_health(img)
    saved = [f for f in os.listdir(".") if f.startswith("scanline_fault_")]
    check(z.max() < NS["SCAN_FAULT_Z"],
          "%s frame max z %.1f < threshold %.1f"
          % (name, z.max(), NS["SCAN_FAULT_Z"]))
    check(not saved, "%s frame saved no dump (%d present)" % (name, len(saved)))

print("\n2. a corrupted row scores far above the threshold and IS saved")
bad = clean.copy()
bad[118, :] = np.where(np.arange(IW) % 2 == 0, 200.0, 14000.0)
z = zscore(bad)
check(z[118] > NS["SCAN_FAULT_Z"],
      "the corrupted row scores z %.1f > threshold %.1f"
      % (z[118], NS["SCAN_FAULT_Z"]))
check(z[118] > 5 * max(z[np.arange(IH) != 118].max(), 1e-6),
      "and dwarfs every clean row (next highest z %.1f)"
      % z[np.arange(IH) != 118].max())
p = P()
# frame_raw is the raw USB payload: bytes, not an array. Passing an ndarray
# here let the dump code be written against the wrong type and every real
# capture failed with UFuncTypeError.
p.frame_raw = np.clip(bad, 0, 65535).astype(np.uint16).tobytes()
p._scanline_health(bad)
dumps = sorted(f for f in os.listdir(".") if f.startswith("scanline_fault_"))
check(len(dumps) == 1, "a dump was written (%s)" % (dumps or "none"))
check(not any("save failed" in m[1] for m in p.q.msgs),
      "and the save did not fail (%r)" % ([m[1] for m in p.q.msgs][:2]))
check(any("scanline fault" in m[1] for m in p.q.msgs),
      "and the fault was reported on screen (%r)"
      % ([m[1] for m in p.q.msgs][:1]))
if dumps:
    blob = open(dumps[0], "rb").read()
    import struct
    n, zint, row = struct.unpack("<IHH", blob[:12][:8])
    npx = struct.unpack("<I", blob[8:12])[0]
    scored = np.frombuffer(blob[12:12 + npx * 4], dtype=np.float32)
    raw = blob[12 + npx * 4:]
    check(scored.size == IH * IW,
          "the dump carries the SCORED image (%d px), which is where the "
          "fault was measured" % scored.size)
    check(scored.size == IH * IW and np.isfinite(scored).all(),
          "the scored image is complete and finite")
    check(len(raw) > 1000,
          "and the raw payload alongside it (%d bytes), for comparison" % len(raw))
    check(row == 118, "it names the offending row (%d)" % row)
    # the row that scored high must be the row that is extreme in the DUMP -
    # this is the check that would have caught saving frame_raw instead
    s = scored.reshape(IH, IW)
    prof = np.abs(np.diff(s, axis=1)).mean(axis=1)
    md = float(np.median(prof))
    mad = float(np.median(np.abs(prof - md)))
    zr = prof[118] / max(md + 3 * mad, 1e-3)
    check(zr > 8,
          "row 118 is still extreme in the dumped image (z %.1f) - the dump "
          "and the score agree" % zr)
    os.remove(dumps[0])

print("\n3. a 5-second rate limit stops a persistent fault flooding the disk")
p = P()
before = len([f for f in os.listdir(".") if f.startswith("scanline_fault_")])
p._scan_last_save = time.time()
for i in range(60):
    p._scan_n = i
    p._scanline_health(bad)
after = len([f for f in os.listdir(".") if f.startswith("scanline_fault_")])
check(after - before <= 1,
      "60 consecutive faulty frames produced %d dump(s), not 60"
      % (after - before))
for f in os.listdir("."):
    if f.startswith("scanline_fault_"):
        os.remove(f)

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")