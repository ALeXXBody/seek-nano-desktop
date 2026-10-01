"""One bad frame must not be able to steer the contrast window.

This is the pulsing. Measured on this hardware: raw p98 sat steady at 7114 DL
while the window's upper bound jumped to 7850 DL and took ~12 s to decay - the
contrast width went 425 -> 1180 DL, a 2.8x rescale of the whole picture. An EMA
with alpha 0.03 cannot move that far against a stable input, so a transient
frame drove it: MIN_WINDOW only rejected spans BELOW 20 DL, so an explosive
span passed straight through and the average absorbed 3% of it.

Drives the real _bmp with a steady scene plus injected outliers and asserts the
displayed window stays put.
"""
import ast
import collections
import pathlib
import sys
import types

import numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
TEXT = SRC.read_text(encoding="utf-8")
tree = ast.parse(TEXT)
ns = {"np": np, "collections": collections, "deque": collections.deque,
      "wx": types.SimpleNamespace(Image=types.SimpleNamespace(),
                                  Bitmap=lambda *a: None),
      "time": None, "os": None}
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


for name in ("_bmp", "_process", "_overlay", "_text", "_boxblur", "_apply_ffc",
             "_roi_u16", "_validate_ffc", "_nuc2d", "_neighbour_median",
             "_apply_bad_pixels", "_detect_spots", "_draw_hotspots"):
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        continue
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)

# constants the extracted code needs
# constants: take them straight from the module-level assignments rather than
# exec'ing individual nodes, which misses multi-target forms like
# "RAW_W, RAW_H = 342, 260"
import re as _re

for _line in TEXT.splitlines():
    _m = _re.match(r"^([A-Z_][A-Z_0-9]*(?:\s*,\s*[A-Z_][A-Z_0-9]*)*)\s*=\s*(.+?)\s*(?:#.*)?$",
                   _line)
    if not _m:
        continue
    _targets = [t.strip() for t in _m.group(1).split(",")]
    try:
        _vals = eval(_m.group(2), {"np": np}, {})
    except Exception:
        continue
    if not isinstance(_vals, tuple):
        _vals = (_vals,)
    if len(_vals) != len(_targets):
        continue
    for _t, _v in zip(_targets, _vals):
        if _t not in ("COLORMAPS",):
            ns[_t] = _v

for _need in ("IMG_W", "IMG_H", "RAW_W", "RAW_H", "ROI_X", "ROI_Y",
              "MIN_WINDOW", "STRETCH_EMA", "WINDOW_MEDIAN"):
    if _need not in ns:
        raise SystemExit("constant %s not picked up from viewer.py" % _need)

ns["COLORMAPS"] = [("grey", lambda t: np.clip(np.stack([t] * 3, -1) * 255,
                                                0, 255).astype(np.uint8))]
ns["BURST"] = 0
ns["LATEST_PNG"] = "_t_window_guard.png"


class _FakeImage(object):
    def __init__(self, *a):
        self._buf = a[-1] if a else b""

    def Scale(self, *a):
        return self


class _FakeWx(object):
    Image = _FakeImage
    IMAGE_QUALITY_NEAREST = 1

    @staticmethod
    def Bitmap(*a):
        return "bitmap"


ns["wx"] = _FakeWx()
import time as _time
ns["time"] = _time

Panel = type("Panel", (), {k: ns[k] for k in ns if callable(ns[k])})
Panel._raw_p = None


def make_panel():
    p = Panel()
    p.frame_raw = None
    p.ffc = None
    p.ffc_ok = None
    p.ffc_gain = None
    p.last_gain = 3
    p.shown_gain = 3
    p.shown_frames = 0
    p.bad_frames = 0
    p.held_gain = 0
    p.saturated = 0
    p.bg_frames = [base.copy()]
    p.ffc_collect = None
    p.lo_ema = None
    p.hi_ema = None
    p.prev_t = None
    p.stretch_skips = 0
    p.paint_count = 0
    p._paint_seq = None
    p.paused = False
    p.bg_hp = None
    p.bad = None
    p.spots = []
    p.spot_mode = "off"
    p.spot_sens = 1.0
    p.spot_hist = []
    p.spot_alarm = 0.0
    p.spot_alarm_on = False
    p.lut_i = 0
    p.W = IW
    p.H = IH
    p.log = types.SimpleNamespace(AppendText=lambda s: None)
    p.q = types.SimpleNamespace(put=lambda *a: None)
    p._plo = collections.deque(maxlen=ns.get("WINDOW_MEDIAN", 9))
    p._phi = collections.deque(maxlen=ns.get("WINDOW_MEDIAN", 9))
    p._bmp_cache = None
    p._last_dump = 0.0
    p._burst_i = 0
    return p


IW, IH = ns.get("IMG_W", 320), ns.get("IMG_H", 240)
RX, RY = ns.get("ROI_X", 2), ns.get("ROI_Y", 12)
RW, RH = ns.get("RAW_W", 342), ns.get("RAW_H", 260)


def frame(img):
    """Wrap a ROI image back into a full raw transfer buffer."""
    buf = np.zeros((RH, RW), dtype="<u2")
    buf[RY:RY + IH, RX:RX + IW] = img.astype("<u2")
    return buf.astype("<u2").tobytes()


rng = np.random.default_rng(7)
base = np.zeros((IH, IW), np.float32)
base[:, :] = 6000.0
base[:, IW // 3:IW // 3 + 40] = 6300.0     # a warm object, so the span is real
base[:, 2 * IW // 3:] = 6600.0

print("1. a steady scene must hold a steady window")
p = make_panel()
lo0 = hi0 = None
for i in range(40):
    noisy = base + rng.normal(0, 2.0, base.shape).astype(np.float32)
    p.frame_raw = frame(noisy)
    p._bmp_cache = None
    p._bmp()
    if i == 25:
        lo0, hi0 = p.lo_ema, p.hi_ema
check(lo0 is not None, "window was seeded")
check((hi0 - lo0) > 20.0, f"seeded window has a real span ({hi0 - lo0:.1f} DL)")

print("\n2. one explosive frame must not move it")
moves = []
for i in range(12):
    noisy = base + rng.normal(0, 2.0, base.shape).astype(np.float32)
    if i == 4:
        # the transient: a saturated frame, span enormous, p98 far above
        bad = noisy.copy()
        bad[:, IW // 2:] = 64000.0
        noisy = bad
    p.frame_raw = frame(noisy)
    p._bmp_cache = None
    p._bmp()
    moves.append((p.hi_ema - p.lo_ema, p.stretch_skips))
spans = [m[0] for m in moves]
worst = max(abs(s - (hi0 - lo0)) for s in spans)
check(worst < 0.25 * (hi0 - lo0),
      f"window span stayed within 25% through a saturated frame "
      f"(worst excursion {worst:.1f} DL on a {(hi0 - lo0):.1f} DL window)")
# The outlier is neutralised by the median, which sits upstream of the ratio
# guard - a median over 9 frames cannot be moved by one sample at all. So
# stretch_skips is expected to stay 0 (nothing discarded, so no freezing);
# what matters is that the median the window tracked ignored the bad frame.
med_hi_now = float(np.median(p._phi))
check(med_hi_now < 20000,
      f"the median ignored the saturated frame (median p98 {med_hi_now:.0f} DL, "
      f"not the 64000 the bad frame carried)")
check(moves[-1][1] == 0,
      "no frame had to be discarded - the median absorbed it upstream, so the "
      "picture never freezes waiting for a good frame")

print("\n3. and the old EMA path really would have been moved by it")
# alpha 0.03 straight onto the same transient, for comparison
lo, hi = lo0, hi0
worst_ema = 0.0
for i in range(12):
    noisy = base + rng.normal(0, 2.0, base.shape).astype(np.float32)
    p_lo, p_hi = (np.percentile(noisy, 2), np.percentile(noisy, 98))
    if i == 4:
        bad = noisy.copy()
        bad[:, IW // 2:] = 64000.0
        p_lo, p_hi = (np.percentile(bad, 2), np.percentile(bad, 98))
    a = ns.get("STRETCH_EMA", 0.03)
    lo += a * (p_lo - lo)
    hi += a * (p_hi - hi)
    worst_ema = max(worst_ema, abs((hi - lo) - (hi0 - lo0)))
print(f"   plain EMA would have moved the window by up to {worst_ema:.1f} DL;")
print(f"   the guarded path moved it by {worst:.1f} DL")
check(worst_ema > worst,
      "the guard is doing real work (old path moved further than the new one)")

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")
