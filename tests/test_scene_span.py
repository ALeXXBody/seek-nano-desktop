"""The content gate must reject flat AND clipped frames.

The gate only had a lower bound, so a fully clipped frame (1-99% span 65535 DL
- the entire 16-bit range) passed it. Those were only kept off screen because
the gain-hold happened to reject them for being at an unfamiliar gain; the
content gate itself was not sound.

Driving the real _frame_has_content with frames at the spans measured from
every gain the AGC visits on this hardware.
"""
import ast
import pathlib

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


# constants FIRST: the functions below use them as default argument values,
# which are evaluated at def time. Handles multi-target forms like
# "RAW_W, RAW_H = 342, 260".
import re as _re
for _line in TEXT.splitlines():
    _m = _re.match(r"^([A-Z_][A-Z_0-9]*(?:\s*,\s*[A-Z_][A-Z_0-9]*)*)\s*=\s*"
                   r"([\d.]+(?:\s*,\s*[\d.]+)*)\s*(?:#.*)?$", _line)
    if not _m:
        continue
    names = [t.strip() for t in _m.group(1).split(",")]
    vals = [float(v) for v in _m.group(2).split(",")]
    if len(names) == len(vals):
        for n_, v_ in zip(names, vals):
            ns[n_] = v_

# the extracted _roi_u16 uses these as slice indices and reshape dims, so they
# must be ints in its globals
for _dim in ("RAW_W", "RAW_H", "IMG_W", "IMG_H", "ROI_X", "ROI_Y"):
    if _dim in ns:
        ns[_dim] = int(ns[_dim])


for name in ("_roi_u16", "_frame_has_content"):
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)

RW, RH, RX, RY = (int(ns["RAW_W"]), int(ns["RAW_H"]),
                   int(ns["ROI_X"]), int(ns["ROI_Y"]))
IW, IH = int(ns["IMG_W"]), int(ns["IMG_H"])
has = ns["_frame_has_content"]
print("gate: %.0f DL <= span <= %.0f DL\n" % (ns["MIN_SCENE_SPAN"],
                                              ns["MAX_SCENE_SPAN"]))


def frame_with_span(span, seed=0):
    """A frame whose 1-99 percentile span is as close to `span` as we can get."""
    rng = np.random.default_rng(seed)
    base = 20000.0
    buf = np.zeros((RH, RW), dtype="<u2")
    # half the pixels at the low end, half at the high end -> 1-99 span ~= span
    lo = base - span / 2.0
    hi = base + span / 2.0
    half = IW // 2
    roi = np.full((IH, IW), base, dtype=np.float64)
    roi[:, :half] = lo
    roi[:, half:] = hi
    roi += rng.normal(0, 1.0, roi.shape)
    buf[RY:RY + IH, RX:RX + IW] = np.clip(roi, 0, 65535).astype("<u2")
    return buf.astype("<u2").tobytes()


# the spans actually measured on the camera, per gain
REAL = {1: 675.2, 3: 914.0, 6: 679.2, 8: 1252.0, 27: 754.0, 28: 1300.0}
FLAT = {7: 24.0, 20: 24.0, 25: 50.0}
CLIPPED = {4: 9973.0, 9: 34178.4, 14: 65535.0, 26: 65532.0}

print("real pictures must SHOW")
for g, span in sorted(REAL.items()):
    check(has(frame_with_span(span, seed=g)),
          "gain %-3d span %8.1f DL shows" % (g, span))

print("\nflat frames must be REJECTED (no picture to show)")
for g, span in sorted(FLAT.items()):
    check(not has(frame_with_span(span, seed=g)),
          "gain %-3d span %8.1f DL rejected" % (g, span))

print("\nclipped frames must be REJECTED (would paint solid white)")
for g, span in sorted(CLIPPED.items()):
    check(not has(frame_with_span(span, seed=g)),
          "gain %-3d span %8.1f DL rejected" % (g, span))

print("\nboundaries")
check(has(frame_with_span(ns["MIN_SCENE_SPAN"] * 3)),
      "a frame 3x the minimum shows")
check(not has(frame_with_span(ns["MAX_SCENE_SPAN"] * 1.5)),
      "a frame 1.5x the maximum is rejected")

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")