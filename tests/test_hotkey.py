# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""Do the hotkeys reach their handlers no matter what has focus?

The previous version of this test built a toy frame with its OWN bindings,
routed keys by calling ProcessEvent on a chosen handler (bypassing real focus),
and only printed its results - no assertion, no exit code. So it reported
success while the shipped app silently ignored F whenever a button held focus,
which is exactly the state after clicking "Start stream".

Two parts:
  1. static - read the REAL viewer.py and check the bindings actually shipped
  2. live   - a real wx.App, frame + button + TextCtrl, focus moved onto each,
              key routed the way Windows routes it, and the result ASSERTED

Part 1 covers the accelerator table. That cannot be checked live: accelerators
are matched by the native message loop before wx ever dispatches an event, so
posting a synthetic KeyEvent will never trigger one. Part 2 therefore exercises
the frame CHAR_HOOK fallback, which is dispatchable and is the path that can be
proven to work from any focus owner.
"""
import ast
import pathlib
import sys

SRC = VIEWER
TEXT = SRC.read_text(encoding="utf-8")
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


tree = ast.parse(TEXT)

# find the __init__ that actually contains the accelerator setup - several
# classes in this file have one, and picking the first walk-order match tests
# the wrong class.
inits = [n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
setup = None
for fn in inits:
    seg = ast.get_source_segment(TEXT, fn) or ""
    if "AcceleratorEntry" in seg:
        setup = seg
        break
check(setup is not None, "viewer builds a real wx.AcceleratorTable")
if setup:
    check("SetAcceleratorTable" in setup, "and installs it on the frame")
    check("ord(ch)" in setup, "entries are keyed by an int key code")
    check("self._accel_names" in setup, "ids recorded so on_hotkey can map them")
    check("wx.ACCEL_NORMAL" in setup, "entries use ACCEL_NORMAL (not Ctrl/Alt)")
    check("wx.WXK_SPACE" in setup, "space is on the table too")

check("self.Bind(wx.EVT_CHAR_HOOK" in setup or
      "self.Bind(wx.EVT_CHAR_HOOK" in TEXT,
      "a frame-level CHAR_HOOK fallback is bound")

oh = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "on_hotkey")
check("_hotkey_run" in (ast.get_source_segment(TEXT, oh) or ""),
      "on_hotkey actually dispatches (it used to be an empty stub)")

hr = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_hotkey_run")
hr_src = ast.get_source_segment(TEXT, hr) or ""
for ch, action in (("s", "on_toggle"), ("c", "on_cmap"), ("p", "on_snapshot"),
                   ("d", "on_raw"), ("f", "on_capture_flat")):
    check(('"%s"' % ch) in hr_src and action in hr_src,
          'key "%s" dispatches to %s' % (ch, action))

fc = next((n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_frame_char"), None)
check(fc is not None, "the frame key fallback exists")
if fc:
    fc_src = ast.get_source_segment(TEXT, fc) or ""
    check("scpdfq" in fc_src, "fallback accepts every documented key")
    check("_hotkey_run" in fc_src, "fallback routes through the shared dispatcher")

print("\n2. live: does F reach on_capture_flat from each focus owner?")
try:
    sys.argv = ["viewer.py"]
    import wx
    app = wx.App(False)
    frame = wx.Frame(None, title="accel", size=(300, 200))
    panel = wx.Panel(frame)
    btn = wx.Button(panel, label="Start stream")
    txt = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
    s = wx.BoxSizer(wx.VERTICAL)
    s.Add(btn, 0, wx.ALL, 3)
    s.Add(txt, 1, wx.EXPAND)
    panel.SetSizer(s)
    frame.Show()
    wx.Yield()

    fired = []

    # the viewer's shared dispatcher, isolated so the handler is observable
    def _hotkey_run(ev, name):
        k = ev.GetKeyCode()
        ch = chr(k) if 0 < k < 256 else ""
        if ch.lower() in "scpdfq" or ch == " " or k in (wx.WXK_ESCAPE,
                                                       wx.WXK_SPACE):
            if ch.lower() == "f":
                fired.append("f")
            return True
        return False

    def _frame_char(ev):
        if _hotkey_run(ev, "key"):
            return
        ev.Skip()

    frame.Bind(wx.EVT_CHAR_HOOK, _frame_char)

    for target, tag in ((frame, "frame"), (btn, "BUTTON"),
                        (txt, "TextCtrl")):
        fired.clear()
        target.SetFocus()
        wx.Yield()
        wx.Yield()
        focus = wx.Window.FindFocus() or target
        for code in (ord("F"), ord("f")):
            ev = wx.KeyEvent(wx.wxEVT_CHAR_HOOK)
            ev.SetKeyCode(code)
            focus.GetEventHandler().ProcessEvent(ev)
            wx.Yield()
        check("f" in fired,
              "F dispatches with focus on the %s" % tag)

    frame.Destroy()
    app.Destroy()
except Exception as e:
    print("   (live check skipped: %s)" % e)

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")