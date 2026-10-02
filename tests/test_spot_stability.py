"""The stability filter must smooth jitter WITHOUT blinking, and must still
drop a spot that has genuinely gone away.

The bug it replaced (a hard-capped hit counter that deleted the key at 90) made
a motionless hotspot blink for 2 frames every 90 - 0.08 s of blackout repeating
every 3.6 s. The fix decays the counter instead. These checks exist so the fix
cannot silently regress into either failure: the original blink, or a filter so
sticky that vanished spots stay on screen forever.
"""
import ast
import os
import pathlib
import re
import sys
import time
import traceback

import numpy as np

sys.path.insert(0, r"C:\a\src\app")

SRC = pathlib.Path(r"C:\a\src\app\viewer.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
NS = {"np": np, "time": time, "os": os, "sys": sys,
      "__file__": r"C:\a\src\app\viewer.py", "__name__": "viewer"}
for m in re.finditer(
        r"^(HOTSPOT_[A-Z_0-9]*|SPOT_STABLE|SPOT_MOVE)\s*=\s*(.+)$", SRC, re.M):
    try:
        NS[m.group(1)] = eval(m.group(2), NS)
    except Exception:
        pass
_fn = next(n for c in TREE.body if isinstance(c, ast.ClassDef)
           for n in c.body
           if isinstance(n, ast.FunctionDef) and n.name == "_detect_spots")
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "v", "exec"), NS)

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
raw = np.fromfile(r"C:\a\src\data\frame_1790470060.raw",
                  dtype="<u2").reshape(RH, RW)
BASE = raw[RY:RY + IH, RX:RX + IW].astype(np.float32)
yy, xx = np.mgrid[0:IH, 0:IW]

# Auto threshold on this frame is median + max(6*sigma, 120); sigma is ~283 DL,
# so a real rise has to clear ~1700 DL for the detector to see it at all.
RISE = 2500.0


def scene(cy=100, cx=140):
    return BASE + RISE * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / 80.0)


class Q:
    def put(self, *_a):
        pass


def _fail(what, exc):
    print("  !! _show_fatal(%r):" % (what,))
    traceback.print_exception(*exc)


class Panel:
    _detect_spots = NS["_detect_spots"]

    def __init__(self, mode="mark"):
        self.spot_mode = mode
        self.spots = []
        self.spot_sens = 1.0
        self.spot_hist = []
        self.spot_alarm = 0.0
        self.spot_alarm_on = False
        self._spot_pts = []
        self._spot_score = []
        self.q = Q()
        self._show_fatal = _fail


FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def run(frames, img=None):
    p = Panel()
    out = []
    for i in range(frames):
        p._detect_spots(img if img is not None else scene())
        out.append(bool(p.spots))
    return out


print("1. a motionless hotspot never blinks")
d = run(400)
gaps = [i + 1 for i, v in enumerate(d) if not v]
# frames 1-2 are the intended warm-up; nothing after that may drop out
late = [g for g in gaps if g > 2]
check(not late,
      "no dropout in frames 3-400 (undrawn: %s)" % (late or "none"))
check(sum(d) >= 397, "drawn on %d of 400 frames" % sum(d))

print("\n2. jitter is still smoothed")
# Move the hotspot by a pixel each frame: the cell key changes constantly, so a
# naive filter would never qualify it. It should still settle and stay drawn.
p = Panel()
jitter = []
for i in range(60):
    p._detect_spots(scene(100 + (i % 2), 140))
    jitter.append(bool(p.spots))
check(sum(jitter[10:]) >= 45,
      "a 1-px jittering hotspot stays drawn on %d of the last 50 frames"
      % sum(jitter[10:]))

print("\n2b. a MOVING hotspot does not blink as it crosses cell boundaries")
# The stability key is an 8x8 grid cell, so a drifting hotspot enters a new cell
# and starts its counter from zero. That is the case most likely to look like
# strobing on a real target: the box is keyed on position, not identity.
p = Panel()
moving = []
for i in range(150):
    p._detect_spots(scene(100, 140 + i))       # walks right, one px/frame
    moving.append(bool(p.spots))
# ignore the first 6 frames of warm-up
holes = [i + 1 for i, v in enumerate(moving[6:], start=6) if not v]
check(not holes,
      "no dropout while the hotspot moves 150 px (undrawn: %s)" % (holes or "none"))
check(sum(moving) >= 144, "drawn on %d of 150 frames" % sum(moving))

print("\n3. a spot that vanishes is dropped, not held forever")
p = Panel()
for _ in range(40):
    p._detect_spots(scene())
after_on = len(p.spots) > 0
for _ in range(80):
    p._detect_spots(BASE)          # hotspot gone
gone = len(p.spots) == 0
check(after_on, "it was drawn while present")
check(gone, "and is gone 80 frames after the hotspot left the scene")
check(len(getattr(p, "_spot_pts", [])) <= 1,
      "the vanished region left %d tracked point(s), not a stale entry"
      % len(getattr(p, "_spot_pts", [])))

print("\n4. the old hard-cap bug cannot come back")
# Check the CODE, not the prose: the comment explaining the old bug names the
# expression it removed, so a substring search over the file gives a false
# failure. Parse _detect_spots and look for the prune in the AST instead.
_fn_src = ast.get_source_segment(SRC, _fn) or ""
check("_spot_seen" not in _fn_src,
      "the capped _spot_seen counter is gone entirely")
check(">= SPOT_STABLE" in _fn_src,
      "the draw threshold reads SPOT_STABLE, not a magic number")
check("SPOT_STABLE" in SRC and "SPOT_STABLE = 3" in SRC,
      "SPOT_STABLE is 3, so a spot still needs 3 frames of evidence")
# The original defect: state keyed on an 8x8 grid cell, so a moving hotspot
# restarted its count at every boundary. Positions, not cells, now.
check("SPOT_MOVE" in _fn_src,
      "regions are tracked by position within SPOT_MOVE, not by grid cell")
check("// 8" not in _fn_src,
      "no 8-px grid-cell keying left in _detect_spots")

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")