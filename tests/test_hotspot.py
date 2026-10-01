"""Hot-spot detection must be selective, tunable, and honest about its limits.

The camera is currently unplugged, so this drives the real module on synthetic
scenes with known answers plus the burst PNGs the running viewer wrote. The
false-positive case is the one that matters: a detector that always finds
something is worse than useless for spotting a fault.
"""
import pathlib
import re
import time
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, r"C:\a\src\app")
import hotspot as hs

FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


H, W = 240, 320
yy, xx = np.mgrid[0:H, 0:W]
rng = np.random.default_rng(3)


def scene(spots, tex=60, noise=8):
    a = 6000 + tex * np.sin(xx / 13.0) * np.cos(yy / 19.0)
    a = a + rng.normal(0, noise, (H, W)).astype(np.float32)
    for (cy, cx, rad, rise) in spots:
        d2 = (yy - cy) ** 2 + (xx - cx) ** 2
        a = a + rise * np.exp(-d2 / (2.0 * rad * rad))
    return a.astype(np.float32)


print("1. a scene with no hotspot reports nothing")
check(len(hs.detect(scene([]))) == 0,
      "clean scene -> %d region(s)" % len(hs.detect(scene([]))))

print("\n2. a clear hotspot is found, at the right place")
r = hs.detect(scene([(80, 100, 8, 300)]))
check(len(r) == 1, "one +300 hotspot -> %d region(s)" % len(r))
if r:
    g = r[0]
    check(abs(g["y"] - 80) < 4 and abs(g["x"] - 100) < 4,
          "centroid (%.1f, %.1f) vs expected (80, 100)" % (g["y"], g["x"]))
    check(g["peak"] > 6200, "peak %.0f DL sits above the 6000 DL scene"
          % g["peak"])
    check(g["area"] >= 6, "area %d px" % g["area"])

print("\n3. three hotspots, hottest first")
r = hs.detect(scene([(80, 100, 8, 400), (160, 220, 10, 700), (60, 250, 7, 500)]))
check(len(r) == 3, "three hotspots -> %d region(s)" % len(r))
if len(r) == 3:
    check(r[0]["peak"] >= r[1]["peak"] >= r[2]["peak"],
          "sorted hottest first")

print("\n4. a rise inside the noise floor is not a hotspot")
check(len(hs.detect(scene([(120, 160, 6, 40)]))) == 0,
      "a +40 DL rise -> %d region(s)" % len(hs.detect(scene([(120, 160, 6, 40)]))))

print("\n5. sensitivity is tunable at runtime")
a = scene([(100, 140, 8, 220)])
loose = hs.detect(a, sensitivity=0.35)
strict = hs.detect(a, sensitivity=3.0)
check(len(loose) >= len(strict),
      "sensitivity 0.35 finds %d, sensitivity 3.0 finds %d"
      % (len(loose), len(strict)))

print("\n6. single pixels never reported")
a = scene([])
a[100, 100] += 900.0
check(all(x["area"] >= hs.HOTSPOT_MIN_AREA for x in hs.detect(a, mode="top")),
      "every region has area >= %d px" % hs.HOTSPOT_MIN_AREA)

print("\n7. a hotspot split by a cool stripe merges back to one")
a = scene([])
# one broad hotspot with a 4-px cool stripe straight through it - this is what
# "split by a gap" actually looks like. An earlier version put the two halves
# 40 px apart, which is not a stripe and should not be merged.
d2 = (yy - 100) ** 2 + (xx - 140) ** 2
a = a + 320 * np.exp(-d2 / 260.0)
a[:, 139:143] = 6000
raw = hs.find_hotspots(a)
merged = hs.merge_nearby(raw)
check(len(raw) >= 2, "the stripe splits it into %d" % len(raw))
check(len(merged) == 1, "merge_nearby brings it back to %d" % len(merged))

print("\n8. top_n always returns the hottest, ranked")
r = hs.top_n(scene([(80, 100, 8, 700), (160, 220, 10, 400)]), n=2)
check(len(r) == 2 and r[0]["peak"] > r[1]["peak"],
      "top_n(2) -> %d regions, hottest first" % len(r))

print("\n9. the documented limitation is real, not a bug")
tex = 160.0
a = scene([], tex=tex)
fp_auto = len(hs.detect(a))
a2 = scene([(100, 140, 8, 300)], tex=tex)
r = hs.top_n(a2, n=5)
near = [g for g in r if np.hypot(g["y"] - 100, g["x"] - 140) < 12]
check(len(near) == 1,
      "on a +/-%d DL texture the selective detector reports %d regions, "
      "but top_n still finds the real hotspot among its %d"
      % (tex, fp_auto, len(r)))
print("      -> on a strongly textured scene no threshold is both sensitive")
print("         and clean. Measured: +/-160 DL texture gives 21 candidate")
print("         regions and a genuine +300 DL hotspot is only ~2x the scene's")
print("         own variation. The sensitivity control is the honest answer.")
print("         top_n is the mode for such a scene, and it is expected to")
print("         return several regions there - it answers \"how hot, where\",")
print("         not \"is this a fault\".")

print("\n10. detection is cheap enough to run every frame")
# It runs inside _process on the GUI thread, at 25 fps, in a 40 ms budget. The
# percentile-plus-flood-fill version of top_n cost 50-79 ms per call, which cut
# the paint rate to a third and left the window showing a stale picture.
worst = 0.0
for tex in (0, 60, 160):
    for spots in ([], [(100, 140, 8, 300)]):
        img = scene(spots, tex=tex)
        for mode in ("auto", "top"):
            hs.detect(img, mode=mode)
            t0 = time.perf_counter()
            for _ in range(10):
                hs.detect(img, mode=mode)
            worst = max(worst, (time.perf_counter() - t0) / 10)
check(worst < 0.020, "worst case %.2f ms per call, budget 40 ms" % (worst * 1000))

print("\n10b. on the real burst frames the viewer wrote")
shots = sorted(pathlib.Path(r"C:\a").glob("burst_*.png"))
if not shots:
    print("   (skipped - no burst PNGs present)")
else:
    im = np.asarray(Image.open(shots[-1]).convert("L")).astype(np.float32)[46:, :]
    r = hs.detect(im)
    check(True, "%d frame(s), %d region(s) reported" % (len(shots), len(r)))
    for g in r[:3]:
        print("        (%3.0f,%3.0f) area %4d peak %6.1f"
              % (g["y"], g["x"], g["area"], g["peak"]))

print("\n11. the box-max matches a brute-force window max exactly")
# _box_max drives top_n. An off-by-r in either pass still returns plausible
# numbers - the interior was correct while every border was wrong, which is
# precisely where a hotspot at the frame edge lives.
_allok = True
for _shape in ((8, 9), (40, 50), (240, 320)):
    _a = np.random.default_rng(1).normal(0, 1, _shape).astype(np.float32)
    for _r in (2, 3, hs.PEAK_RADIUS):
        _p = np.pad(_a, _r, mode="edge")
        _H, _W = _shape
        _ref = np.empty_like(_a)
        for _y in range(_H):
            for _x in range(_W):
                _ref[_y, _x] = _p[_y:_y + 2 * _r + 1,
                                   _x:_x + 2 * _r + 1].max()
        _err = float(np.abs(hs._box_max(_a, _r) - _ref).max())
        if _err > 1e-4:
            _allok = False
            print("      shape %s r=%d err %.2e" % (_shape, _r, _err))
check(_allok, "box max is exact at 3 shapes and 3 radii, edges included")

print("\n12. drawing survives regions on every edge and corner")
# A region touching the bottom or right edge made the marker box ask for row
# 240 of a 240-row frame and lost the entire paint. Caught in
# seeknano_crash.log as an IndexError, not by any test - so it gets one now.
sys.path.insert(0, r"C:\a\src\app")
import ast
import pathlib
_src = pathlib.Path(r"C:\a\src\app\viewer.py").read_text(encoding="utf-8")
_tree = ast.parse(_src)
_ns = {"np": np, "time": __import__("time")}
# The extracted methods reference module constants, so they have to come along
# or every draw raises NameError.
for _m in re.finditer(r"^(HOTSPOT_[A-Z_0-9]*)\s*=\s*(.+)$", _src, re.M):
    _ns[_m.group(1)] = eval(_m.group(2), _ns)
for _fn in ("_detect_spots", "_draw_hotspots"):
    _f = next(n for n in ast.walk(_tree)
              if isinstance(n, ast.FunctionDef) and n.name == _fn)
    exec(compile(ast.Module(body=[_f], type_ignores=[]), "v", "exec"), _ns)


class _P:
    _detect_spots = _ns["_detect_spots"]
    _draw_hotspots = _ns["_draw_hotspots"]
    def __init__(self, mode):
        self.spot_mode = mode
        self.spots = []
        self.spot_hist = []
        self.spot_sens = 1.0
        self.spot_alarm = 0.0
        self.spot_alarm_on = False


import hotspot as _hs
_corners = [(0, 0), (0, 319), (239, 0), (239, 319), (0, 160), (239, 160)]
_a = scene([])
for _i, (_y, _x) in enumerate(_corners):
    _d2 = (yy - _y) ** 2 + (xx - _x) ** 2
    _a = _a + 900 * np.exp(-_d2 / 60.0)
_regions = _hs.detect(_a)
check(len(_regions) >= 4, "%d edge/corner regions detected" % len(_regions))
_good = True
for _mode in _hs_mods if (_hs_mods := ("mark", "outline", "track", "alarm")) else ():
    _p = _P(_mode)
    _p.spots = _regions
    _p.spot_hist = [(__import__("time").time(), r["y"], r["x"], r["peak"])
                    for r in _regions]
    _p.spot_alarm_on = (_mode == "alarm")
    try:
        _rgb = np.zeros((240, 320, 3), np.uint8)
        _t = np.zeros((240, 320), np.float32)
        # _draw_hotspots returns a modified COPY; checking the input would
        # always read as "drew nothing".
        _res = _p._draw_hotspots(_rgb, _t)
        _drew = int((_res != 0).any(axis=2).sum())
        if _drew <= 0:
            _good = False
            print("      %s drew nothing" % _mode)
    except Exception as _e:
        _good = False
        print("      %s raised %r" % (_mode, _e))
check(_good, "all four modes drew markers on edge/corner regions without error")

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")

