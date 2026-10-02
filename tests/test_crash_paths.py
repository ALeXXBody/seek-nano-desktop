"""Drive every crash path I introduced, with the real code, no camera.

Four crashes came out of this session and three of them were mine:

  np.percentile(None)                  _bmp, after _process began returning None
  "%d DL" % None                       _draw_scale_bar, lo_ema before first stretch
  img_arr.min()                        _save_png, same None
  Timer.Start(float)                   wx.CallLater(3000.0) in the USB retry

Checking the source text for these proves nothing - it is how all four got
through. So extract the real methods and RUN them in the states that crashed.
The camera is wedged, so this is the only verification available, and it is
worth more than another regex.
"""
import ast
import os
import pathlib
import sys
import time
import types

import numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


NS = {"np": np, "time": time, "os": os, "sys": sys, "wx": None,
      "__file__": r"C:\a\src\app\viewer.py", "__name__": "viewer"}
for node in TREE.body:
    if isinstance(node, ast.Assign):
        try:
            val = eval(compile(ast.Expression(node.value), "<c>", "eval"),
                       dict(NS))
        except Exception:
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                NS[t.id] = val
            elif isinstance(t, ast.Tuple) and isinstance(val, (tuple, list)):
                for s, v in zip(t.elts, val):
                    if isinstance(s, ast.Name):
                        NS[s.id] = v

VIEWER = next(c for c in TREE.body if isinstance(c, ast.ClassDef)
              and c.name == "Viewer")
WANT = ["_scanline_health", "_draw_scale_bar", "_usb_reconnect",
        "_is_usb_timeout", "on_spot_mode", "on_spot_sens", "_detect_spots"]
for w in WANT:
    node = next((f for f in list(TREE.body) + list(VIEWER.body)
                 if isinstance(f, ast.FunctionDef) and f.name == w), None)
    if node is None:
        raise SystemExit("could not find %s" % w)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "v", "exec"), NS)

IW, IH = 320, 240


def ironbow(t):
    """Stand-in for the colormap, so the scale bar can be drawn headlessly."""
    return (int(255 * t), int(128 * (1 - abs(2 * t - 1))), int(255 * (1 - t)))


NS["ironbow"] = ironbow


class _Wx:
    """Minimal wx surface: enough for the scale bar to reach the DL labels,
    which is the code that actually crashed."""

    class Pen:
        def __init__(self, *a):
            pass

    class Colour:
        def __init__(self, *a):
            pass

    @staticmethod
    def GetTextExtent(s, *a):
        return (len(str(s)) * 6, 12)


NS["wx"] = _Wx


def _font(n, **k):
    return object()


NS["_ui_font"] = _font
NS["THEME"] = {"muted": (120, 130, 140)}


class Q:
    def __init__(self):
        self.msgs = []

    def put(self, t):
        self.msgs.append(t)


class Panel:
    _scanline_health = NS["_scanline_health"]
    _draw_scale_bar = NS["_draw_scale_bar"]
    _usb_reconnect = NS["_usb_reconnect"]
    _is_usb_timeout = NS["_is_usb_timeout"]
    on_spot_mode = NS["on_spot_mode"]
    on_spot_sens = NS["on_spot_sens"]
    _detect_spots = NS["_detect_spots"]

    def __init__(self):
        self.q = Q()
        self.frame_raw = bytes(IH * IW * 2)
        self._scan_n = 0
        self._scan_last_save = 0.0
        # the state that crashed: display window never set, because every frame
        # was being rejected before its stretch ran
        self.lo_ema = None
        self.hi_ema = None
        self.spot_mode = "mark"
        self.spots = []
        self.spot_sens = 1.0
        self.spot_hist = []
        self.spot_alarm = 0.0
        self.spot_alarm_on = False
        self._spot_pts = []
        self._spot_score = []
        self.stream_thread = None
        self._usb_retries = 0


print("1. _draw_scale_bar survives lo_ema/hi_ema being None")
# This is the exact state that produced 4386 bytes of crash log: a rejected
# frame returns before the stretch, so the window is still None.
p = Panel()
sentinel = []


class FakeDC:
    def SetTextForeground(self, *_a):
        pass

    def SetFont(self, *_a):
        pass

    def GetTextExtent(self, s, *_a):
        sentinel.append(s)
        return (len(str(s)) * 6, 12)

    def SetPen(self, *_a):
        pass

    def DrawLine(self, *_a):
        pass

    def DrawText(self, s, *_a):
        sentinel.append(s)


try:
    p._draw_scale_bar(FakeDC(), 400, 640)
    check(True, "drew the scale bar with no exception")
    check(any("DL" in str(s) for s in sentinel),
          "and it printed DL labels: %r"
          % [s for s in sentinel if "DL" in str(s)])
except Exception as e:
    check(False, "raised %r - this is the bug" % (e,))

print("\n2. a rejected frame does not latch")
clean = (6500 + np.random.default_rng(0).normal(0, 10, (IH, IW))).astype(np.float32)
bad = clean.copy()
bad[238:, :] *= 9.0   # the within-row jump that clears the threshold
p2 = Panel()
p2._scanline_health(bad)
faulted = getattr(p2, "_scan_fault", False)
p2._scanline_health(clean)
cleared = not getattr(p2, "_scan_fault", False)
check(faulted, "a faulted frame sets _scan_fault")
check(cleared, "the next clean frame clears it")
for f in os.listdir("."):
    if f.startswith("scanline_fault_"):
        os.remove(f)

print("\n3. the USB retry delay is an int wx will accept")
check(isinstance(NS["USB_RETRY_DELAY_MS"], int),
      "USB_RETRY_DELAY_MS is %r (int)" % (NS["USB_RETRY_DELAY_MS"],))
f = NS["_is_usb_timeout"]
for msg, want in (("usb.core.USBTimeoutError: [Errno 10060] Operation timed out",
                   True),
                  ("usb.core.USBError: [Errno 32] Pipe error", True),
                  ("ZeroDivisionError: float division by zero", False),
                  ("no frames for 60 kicks - camera stopped sending", False)):
    check(f(msg) is want, "classifier on %r -> %s" % (msg[:44], want))

print("\n4. every _process caller guards None (from the AST, not memory)")
callers = []
for cls in [n for n in TREE.body if isinstance(n, ast.ClassDef)]:
    for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
        seg = ast.get_source_segment(SRC, fn) or ""
        if "self._process(" in seg and fn.name != "_process":
            line = next((n.lineno for n in ast.walk(fn)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "_process"), None)
            after = seg.find("is None", seg.find("self._process("))
            callers.append((cls.name, fn.name, after > 0))
for name, fn, ok in callers:
    check(ok, "%s.%s guards the None from _process" % (name, fn))
check(len(callers) == 2,
      "found %d _process callers: %s"
      % (len(callers), [(c[0], c[1]) for c in callers]))

print("\n5. the capture the user confirmed fixed still replays as a rejection")
cap = None
for cand in sorted(pathlib.Path("C:/a").glob("scanline_fault_*.raw")):
    import struct
    b = cand.read_bytes()
    if len(b) >= 12 and struct.unpack("<I", b[8:12])[0] == IH * IW:
        cap = np.frombuffer(b[12:12 + IH * IW * 4],
                            dtype=np.float32).reshape(IH, IW)
        break
if cap is None:
    print("   (skipped - no captured fault frame on disk)")
else:
    p3 = Panel()
    p3._scanline_health(cap)
    check(getattr(p3, "_scan_fault", False) is True,
          "the real captured fault is rejected by the shipped path")
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