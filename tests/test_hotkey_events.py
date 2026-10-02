# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""Hotkeys must work from BOTH event kinds, and paint must never black out.

Two regressions this pins down:

1. The frame accelerator table emits a wx.CommandEvent, which has no
   GetKeyCode. _hotkey_run called it unconditionally, so every accelerator
   press raised AttributeError - every hotkey silently did nothing, including
   F to capture the flat-field reference. It went unnoticed because a --windowed
   build sends the traceback to a stderr nobody reads; see
   seeknano_crash.log, which is how it was finally caught.

2. on_paint cleared the window to black BEFORE building the bitmap. If there
   was no frame to build from, it returned with the window already black and
   nothing drawn - indistinguishable on screen from the camera dropping out.
   It must build first and leave the previous picture alone if it has nothing.
"""
import ast
import pathlib
import types

SRC = VIEWER
TEXT = SRC.read_text(encoding="utf-8")
tree = ast.parse(TEXT)
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


class FakeWx:
    WXK_ESCAPE = 27
    WXK_SPACE = 32
    Pen = staticmethod(lambda *a, **k: None)


ns = {"wx": FakeWx()}
for name in ("_hotkey_run",):
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)

import re as _re
for _line in TEXT.splitlines():
    _m = _re.match(r"^([A-Z_][A-Z_0-9]*)\s*=\s*(\{.*\}|[\d.]+|\(.*\))\s*$", _line)
    if _m:
        try:
            ns[_m.group(1)] = eval(_m.group(2), {"wx": FakeWx}, {})
        except Exception:
            pass
# ACCEL_KEY is a multi-line dict, so pull it straight from the AST
for _node in tree.body:
    if isinstance(_node, ast.Assign) and any(
            getattr(t, "id", None) == "ACCEL_KEY" for t in _node.targets):
        exec(compile(ast.Module(body=[_node], type_ignores=[]), str(SRC),
                     "exec"), ns)
check("ACCEL_KEY" in ns, "ACCEL_KEY was found and loaded")
ns["wx"] = FakeWx()


class KeyEvent(object):
    """Has GetKeyCode - the CHAR_HOOK path."""

    def __init__(self, code):
        self._code = code

    def GetKeyCode(self):
        return self._code


class CommandEvent(object):
    """No GetKeyCode at all - the accelerator path."""

    def __init__(self, name=""):
        self._name = name

    def GetId(self):
        return self._name


ACTIONS = {"s": "on_toggle", "c": "on_cmap", "p": "on_snapshot",
           "d": "on_raw", "f": "on_capture_flat"}
ACCEL_NAMES = {"Start/stop stream": "s", "Next colormap": "c",
               "Save PNG snapshot": "p", "Dump raw frame": "d",
               "Capture flat (wall)": "f"}

print("1. accelerator path: a CommandEvent has no GetKeyCode")
check(not hasattr(CommandEvent("x"), "GetKeyCode"),
      "the fake CommandEvent really has no GetKeyCode, as wx's does not")

panel = types.SimpleNamespace()
panel.fired = []
panel.paused = False
panel.push_status = lambda s: None
for meth in set(ACTIONS.values()) | {"Close"}:
    setattr(panel, meth, (lambda n: lambda *a: panel.fired.append(n))(meth))

for name, key in ACCEL_NAMES.items():
    panel.fired.clear()
    try:
        ns["_hotkey_run"](panel, CommandEvent(name), name)
        ok = ACTIONS[key] in panel.fired
    except AttributeError as e:
        ok = False
        print("      raised: %r" % e)
    check(ok, "accelerator %-22r reaches %s" % (name, ACTIONS[key]))

panel.fired.clear()
ns["_hotkey_run"](panel, CommandEvent("Quit"), "Quit")
check("Close" in panel.fired, "accelerator Quit closes the window")

panel.fired.clear()
ns["_hotkey_run"](panel, CommandEvent("Pause"), "Pause")
check(panel.paused, "accelerator Pause toggles pause")

print("\n2. CHAR_HOOK path: a KeyEvent still works")
for key, meth in sorted(ACTIONS.items()):
    panel.fired.clear()
    ns["_hotkey_run"](panel, KeyEvent(ord(key.upper())), "key")
    check(meth in panel.fired, "key %r reaches %s" % (key, meth))

panel.fired.clear()
ns["_hotkey_run"](panel, KeyEvent(FakeWx.WXK_ESCAPE), "key")
check("Close" in panel.fired, "escape closes the window")

panel.fired.clear()
panel.paused = False
ns["_hotkey_run"](panel, KeyEvent(FakeWx.WXK_SPACE), "key")
check(panel.paused, "space toggles pause")

print("\n3. paint must not clear before it knows it has something to draw")
# Pick Viewer.on_paint specifically. Taking the first on_paint in the file
# matched GlassButton's once the custom title bar, telemetry block and glass
# buttons were added - all four classes define on_paint, and Viewer is last.
# The test then inspected a button's paint handler, found no _bmp() in it, and
# reported six failures against code that was correct.
paint = next(n for n in next(c for c in tree.body
                             if isinstance(c, ast.ClassDef)
                             and c.name == "Viewer").body
             if isinstance(n, ast.FunctionDef) and n.name == "on_paint")
src = ast.get_source_segment(TEXT, paint) or ""
check("_bmp()" in src,
      "found Viewer.on_paint (L%s), the one that draws the frame"
      % paint.lineno)
build_at = src.find("self._bmp()")
clear_at = src.find("dc.Clear()")
check(build_at > 0 and clear_at > 0, "on_paint builds a bitmap and clears")
check(build_at < clear_at,
      "the bitmap is built BEFORE dc.Clear(), so a missing frame cannot "
      "leave the window black")
check("if rgb is None" in src or "if bmp is None" in src,
      "and it returns without clearing when there is nothing to draw")

print("\n4. a throw anywhere in the draw must be recorded, not silent")
# The blank-panel symptom was silent because only _bmp() was wrapped; the
# wx.Image/Scale/Bitmap/DrawBitmap calls that were actually failing were not,
# and in a windowed build an event-handler exception goes to a stderr nobody
# sees - so the panel went black with no error anywhere.
draw_at = src.find("dc.DrawBitmap")
check(draw_at > 0, "on_paint draws the bitmap")
wrapped = src[:draw_at]
check(wrapped.count("try:") >= 2,
      "the draw is inside its own try, not just the bitmap build "
      "(found %d try blocks before DrawBitmap)" % wrapped.count("try:"))
check("_show_fatal(\"draw\"" in src,
      "and a failure in the draw is logged via _show_fatal")

print("\n5. the cache must hold a numpy array, never a wx object")
bmp_fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_bmp")
bsrc = ast.get_source_segment(TEXT, bmp_fn) or ""
check("self._bmp_cache = (self.frame_raw, rgb)" in bsrc,
      "_bmp caches the numpy array")
# only the body of _bmp - get_source_segment can over-reach on a function whose
# last statement is a long expression, so bound it by its own line range
_b0, _b1 = bmp_fn.lineno, bmp_fn.end_lineno
# strip comments: the explanatory comment in _bmp names wx.Bitmap and wx.Image
# when describing what the code USED to do, and matching that prose would make
# this check pass or fail for the wrong reason
_body = "\n".join(l for l in TEXT.splitlines()[_b0 - 1:_b1]
                  if not l.strip().startswith("#"))
check("wx.Bitmap" not in _body and "wx.Image" not in _body,
      "_bmp creates no wx objects - the cached GDI object was the suspect")
tob = next((n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_to_bitmap"), None)
check(tob is not None, "_to_bitmap builds the wx.Bitmap per paint")
dump = next((n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "_dump_pngs"), None)
check(dump is not None, "the PNG writes live in their own method")
if tob is not None and dump is not None:
    check("im.save" not in bsrc,
          "and _bmp no longer encodes a PNG - an 8.00 ms encode ran inside "
          "WM_PAINT once per paint")

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")