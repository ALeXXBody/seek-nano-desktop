"""A flat reference must be judged on live data, even a freshly captured one.

The reported symptom was cyan speckle over the whole picture with `ffc on`.

Cause: _finish_flat set ffc_ok = True on every fresh capture, on the
reasonable-sounding assumption that a reference you just took must be good. It
is not - a capture taken while the camera was not on a uniform surface bakes the
scene into the reference, and subtracting it injects that scene as speckle.

Measured on the reference that caused it, 75 consecutive gain-3 frames:

    neighbour |dx| RAW         240.4 DL
    neighbour |dx| AFTER FFC    334.0 DL
    frames where it helps        0 of 75

So the correction was making the image worse on every single frame while being
reported as active. Two things must hold:

  1. _finish_flat must not pre-approve; it leaves the decision open
  2. the gate must demand a margin, not merely "no worse", so a break-even
     reference cannot flip on and off between frames
"""
import ast
import pathlib
import re

import numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
TEXT = SRC.read_text(encoding="utf-8")
tree = ast.parse(TEXT)
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def body_of(name):
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = TEXT.splitlines()[fn.lineno - 1:fn.end_lineno]
    return "\n".join(l for l in lines if not l.strip().startswith("#"))


print("1. a fresh capture must NOT be pre-approved")
fin = body_of("_finish_flat")
check("self.ffc_ok = True" not in fin,
      "_finish_flat does not set ffc_ok = True")
check("self.ffc_ok = None" in fin,
      "it leaves ffc_ok unset so _validate_ffc judges it on live data")

print("\n2. the gate must require a margin")
val = body_of("_validate_ffc")
check(re.search(r"fix_dx\s*<\s*raw_dx\s*\*\s*0\.9", val) is not None,
      "acceptance requires fix_dx < raw_dx * 0.90, not merely fix_dx < raw_dx")

print("\n3. behave like the real gate on a good and a bad reference")


class Fake:
    WXK_ESCAPE, WXK_SPACE = 27, 32


ns = {"wx": Fake, "np": np}
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_validate_ffc")
exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_apply_ffc")
exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)

rng = np.random.default_rng(11)
IW, IH = 320, 240
# a scene plus a strong per-pixel fixed pattern
scene = (6000 + 300 * np.sin(np.mgrid[0:IH, 0:IW][1] / 9.0)).astype(np.float32)
fpn = rng.normal(0, 120, (IH, IW)).astype(np.float32)

good_ref = fpn + 6000.0            # reference that is really just the pattern
# The bad case has to be genuinely bad: a reference whose own high-frequency
# content EXCEEDS the pattern it is supposed to remove. Anything milder is not
# a failure at all - subtracting it still reduces noise, so it should be
# accepted, and an earlier version of this test with sigma 90 wrongly expected
# rejection. The real reference raised neighbour difference from 240 to 334 DL,
# i.e. its own structure was worse than the sensor's.
bad_ref = fpn + 6000.0 + rng.normal(0, 260, (IH, IW)).astype(np.float32)

p = type("P", (), {})()
p.log = type("L", (), {"AppendText": staticmethod(lambda s: None)})()
p.q = type("Q", (), {"put": staticmethod(lambda *a: None)})()
p.push_status = lambda s: None


def decide(ref):
    p.ffc = ref
    p.ffc_ok = None
    p.ffc_gain = None
    p.shown_gain = 3
    ns["_validate_ffc"](p, scene + fpn)
    return p.ffc_ok


check(decide(good_ref) is True,
      "a genuine fixed-pattern reference is ACCEPTED")
check(decide(bad_ref) is False,
      "a reference carrying extra structure is REJECTED")
print()
print("   the bad case is the artefact: subtracting it raised neighbour")
print("   difference from 240 DL to 334 DL on live frames")

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")