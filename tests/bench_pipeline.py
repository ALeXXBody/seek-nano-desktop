"""Where does the per-frame main-thread time go at 25 fps?

The stream delivers 25.57 fps but paints only reach 18/s, so something on the
main thread is losing the race - most likely the numpy pipeline in _bmp plus
GIL contention with a stream thread now doing 4.4 MB/s of USB reads.

Times each stage of the real _bmp on a real-sized frame, so any win is aimed
at something that actually costs something.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"C:\a\src\app")

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240


def bench(label, fn, n=40):
    fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = (time.perf_counter() - t0) / n * 1000
    print("  %-42s %7.3f ms" % (label, dt))
    return dt


rng = np.random.default_rng(3)
# a realistic scene: structure + sensor-like fixed pattern
scene = np.zeros((IH, IW), np.float32)
yy, xx = np.mgrid[0:IH, 0:IW]
scene += 400 * np.exp(-(((xx - 110) ** 2 + (yy - 130) ** 2) / 2500.0))
scene += 150 * np.sin(xx / 11.0)
fpn = np.full((IH, IW), 240.0, np.float32)
fpn += rng.normal(0, 60, (IH, IW)).astype(np.float32)
img0 = scene + 6000 + fpn
img = img0 - fpn + float(fpn.mean())          # after flat-field

import ast
import pathlib
SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
TEXT = SRC.read_text(encoding="utf-8")
tree = ast.parse(TEXT)
ns = {"np": np}
for nm in ("_boxblur", "_nuc2d", "_apply_ffc", "_roi_u16"):
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == nm), None)
    if fn is not None:
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)

import re as _re
for line in TEXT.splitlines():
    m = _re.match(r"^([A-Z_][A-Z_0-9]*(?:\s*,\s*[A-Z_][A-Z_0-9]*)*)\s*=\s*"
                  r"([\d.]+(?:\s*,\s*[\d.]+)*)", line)
    if m:
        names = [t.strip() for t in m.group(1).split(",")]
        vals = [float(v) for v in m.group(2).split(",")]
        if len(names) == len(vals):
            for n_, v_ in zip(names, vals):
                ns[n_] = v_

boxblur = ns["_boxblur"]
SMOOTH = int(ns.get("DISPLAY_SMOOTH", 3))

print("per-frame main-thread cost, %dx%d ROI\n" % (IW, IH))
tot = 0.0
tot += bench("flat-field subtract", lambda: img - fpn + float(fpn.mean()))
tot += bench("display box blur (k=%d, separable)" % SMOOTH,
             lambda: boxblur(img0, SMOOTH))
b = boxblur(img0, SMOOTH)
tot += bench("2/98 percentiles", lambda: (np.percentile(b, 2), np.percentile(b, 98)))
lo, hi = np.percentile(b, 2), np.percentile(b, 98)
tot += bench("stretch to 0..1", lambda: np.clip((b - lo) / (hi - lo), 0, 1))
t = np.clip((b - lo) / (hi - lo), 0, 1)


def overlay():
    tt = t.copy()
    tt[:42, :210] = 0.0
    for i in range(3):
        tt[6 + i * 12, 4:4 + 180] = 1.0
    return tt


tot += bench("overlay band", overlay)


def lut():
    return (t[..., None] * np.array([255, 128, 64], np.float32)
            ).astype(np.uint8)


tot += bench("colormap -> uint8", lut)
rgb = lut()
tot += bench("rgb.tobytes() (wx.Image source)", lambda: np.ascontiguousarray(rgb).tobytes())
print()
print("  %-42s %7.3f ms" % ("TOTAL numpy pipeline per frame", tot))
print("  at 25 fps that is %.0f ms/s = %.0f%% of the main thread"
      % (tot * 25, tot * 25 / 10))
print()
print("  the app also pays ~1.5 ms per paint (measured: Scale 0.89 + Bitmap 0.66)")
print("  so a frame costs about %.1f ms end to end on the main thread"
      % (tot + 1.5))
print("  25 fps x %.1f ms = %.0f ms/s = %.0f%% of one core"
      % (tot + 1.5, (tot + 1.5) * 25, (tot + 1.5) * 25 / 10))
print()
print("  if that exceeds 100%%, the display cannot reach 25 fps no matter what,")
print("  because every one of those stages is on the main thread")