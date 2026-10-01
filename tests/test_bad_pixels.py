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
      "all %d stuck pixels found (found %d pixels total)"
      % (len(STUCK), int(mask.sum())))

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

print("\n5. it works on a bank of FLICKERING defects")
# This is the real hardware character: the six confirmed defects do not sit at
# a stable offset, they fluctuate. Their offset changed from +1144..+1679 DL in
# one reference to +707..+910 DL in the next, while remaining present in 100%
# of live frames. So a bank whose defects swing hard is the case that matters,
# and it is the one a single-image test fails.
bank = []
for i in range(16):
    a = clean + rng.normal(0, 6, (H, W)).astype(np.float32)
    for y, x in STUCK:
        a[y, x] += 1100.0 if (i + y + x) % 2 == 0 else -900.0
    bank.append(a)
bank = np.stack(bank)
m3 = detect(bank)
got = set(zip(*[a.tolist() for a in np.where(m3)]))
check(all(p in got for p in STUCK),
      "all %d flickering defects found in the bank (%d px flagged)"
      % (len(STUCK), int(m3.sum())))

clean_bank = np.stack([clean + rng.normal(0, 6, (H, W)).astype(np.float32)
                       for _ in range(16)])
check(int(detect(clean_bank).sum()) == 0,
      "a clean 16-frame bank yields %d flagged px"
      % int(detect(clean_bank).sum()))

print("\n6. why flicker, not offset")
print("   the fixed pattern is identical in every frame, so subtracting the")
print("   temporal median cancels it and leaves temporal noise as the spread")
print("   estimate. A defect is then simply one that fluctuates far more.")
print("   On the hardware the single-image spread is about 180 DL, so 4 sigma")
print("   is 720 DL - and the six confirmed defects deviate only +392 to")
print("   +797 DL. An offset-based test cannot separate them from the scene at")
print("   any threshold: raise it and they vanish, lower it and the pattern")
print("   floods in. Flicker has no such ambiguity.")

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")