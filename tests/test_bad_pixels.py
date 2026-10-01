"""Defective elements must be found and replaced, and only they.

The reported artefact was 3-4 small bright spots that stayed in the same place
while the scene moved around them. Confirmed on this hardware: six pixels that
deviate from their neighbours' median in 100% of 147 live frames, by +392 to
+797 DL.

The flat-field reference cannot remove these. It fixes the broad per-pixel
offsets, but six elements out of 76,800 move the aggregate noise by nothing
while being unmissable on screen - which is why the picture could be "the best
so far" and still show them.

Three things must hold:
  1. the detector finds all six validated positions at 6 x MAD
     (8 x MAD finds only the three most extreme and misses half of them)
  2. the correction removes the defect
  3. the correction touches nothing else
"""
import ast
import pathlib
import re

import numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
TEXT = SRC.read_text(encoding="utf-8")
tree = ast.parse(TEXT)
ns = {"np": np}
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


for line in TEXT.splitlines():
    m = re.match(r"^([A-Z_][A-Z_0-9]*)\s*=\s*([\d.]+)", line)
    if m:
        ns[m.group(1)] = float(m.group(2))
for nm in ("_neighbour_median", "_detect_bad_pixels", "_apply_bad_pixels"):
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == nm)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "v", "exec"), ns)

detect = ns["_detect_bad_pixels"]
apply_bad = ns["_apply_bad_pixels"]

# a synthetic sensor: smooth scene plus a per-pixel fixed pattern, so the
# detector has to cope with the same local spread the real one has
rng = np.random.default_rng(4)
H, W = 240, 320
yy, xx = np.mgrid[0:H, 0:W]
scene = 6000 + 300 * np.sin(xx / 17.0) * np.cos(yy / 23.0)
pattern = rng.normal(0, 60, (H, W))
clean = (scene + pattern).astype(np.float32)

STUCK = [(50, 60), (50, 61), (120, 200), (200, 31), (137, 31), (23, 300)]
defective = clean.copy()
for y, x in STUCK:
    defective[y, x] += 800.0

print("1. detection")
mask = detect(defective)
found = set(zip(*[a.tolist() for a in np.where(mask)]))
check(all(p in found for p in STUCK),
      "all %d stuck pixels found (found %d pixels total)" % (len(STUCK), int(mask.sum())))

print("\n2. no false positives on a clean frame")
clean_mask = detect(clean)
check(int(clean_mask.sum()) <= 1,
      "a frame with no defects yields %d flagged pixel(s)" % int(clean_mask.sum()))

print("\n3. correction removes the defect")
out = apply_bad(defective, mask)
err = [abs(out[y, x] - clean[y, x]) for y, x in STUCK]
check(max(err) < 120.0,
      "every stuck pixel pulled back to within %.0f DL of its true value "
      "(was +800)" % max(err))

print("\n4. correction touches nothing else")
changed = np.argwhere(np.abs(out - defective) > 1e-6)
check(len(changed) == len(STUCK),
      "exactly %d pixels changed, got %d" % (len(STUCK), len(changed)))
check(all((int(y), int(x)) in set(map(tuple, changed.tolist())) for y, x in STUCK),
      "and they are the stuck pixels")

print("\n5. it works on the real stored reference")
ref = np.fromfile(r"C:\a\ffc_latest.raw", dtype="<u2").reshape(H, W).astype(np.float32)
rmask = detect(ref)
VALIDATED = {(189, 241), (189, 36), (161, 209), (155, 298), (137, 31), (130, 135)}
rfound = set(zip(*[a.tolist() for a in np.where(rmask)]))
check(VALIDATED <= rfound,
      "all 6 live-validated bad pixels found in the real reference "
      "(%d px flagged)" % int(rmask.sum()))
rout = apply_bad(ref, rmask)
rerr = [abs(rout[y, x] - ns["_neighbour_median"](ref)[y, x]) for y, x in VALIDATED]
check(max(rerr) < 4000.0,
      "and each was pulled toward its neighbours (largest move %.0f DL)"
      % max(rerr))

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")