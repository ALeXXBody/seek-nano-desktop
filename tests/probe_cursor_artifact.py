"""Does the scanline band come from the cursor / line-profile overlay?

The artifact is a single band of corrupted image data spanning the full width.
The overlay is not the hot-spot boxes: it is the line profile the app draws
along the cursor, which is exactly one or two rows tall and spans the frame.

Decisive test - same scene, same instant, cursor moved OFF the canvas and then
back ON, capturing the video panel each time. If the band tracks the cursor it
is drawn; if it survives with the cursor parked outside the panel it is data.
"""
import ctypes
import ctypes.wintypes as wt
import time

import numpy as np
from PIL import ImageGrab

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

u = ctypes.windll.user32
u.FindWindowW.restype = wt.HWND
u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wt.UINT]
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

h = u.FindWindowW(None, "Seek Nano Viewer")
if not h:
    raise SystemExit("viewer not running")
u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0040)
time.sleep(0.8)
r = wt.RECT()
u.GetWindowRect(h, ctypes.byref(r))
PX0, PY0, PX1, PY1 = 22, 88, 722, 655

# a point inside the canvas, and one far outside the window
INSIDE = (r.left + 350, r.top + 350)
OUTSIDE = (r.left + r.right - r.left - 40, r.top + 690)
print("cursor inside canvas : %s" % (INSIDE,))
print("cursor parked outside: %s" % (OUTSIDE,))


def grab(tag):
    im = np.asarray(ImageGrab.grab(
        bbox=(r.left + PX0, r.top + PY0, r.left + PX1, r.top + PY1),
        all_screens=True).convert("RGB"), dtype=np.float32)
    ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom),
                   all_screens=True).convert("RGB").save(
                       r"C:\a\_cursor_%s.png" % tag)
    step = np.abs(np.diff(im, axis=1)).mean(axis=2)
    prof = step.mean(axis=1)
    med = float(np.median(prof))
    mad = float(np.median(np.abs(prof - med)))
    scale = max(med + 3 * mad, 1.0)
    bad = [(i, float(prof[i])) for i in range(len(prof)) if prof[i] > 2.5 * scale]
    return bad


for tag, pt, n in (("off", OUTSIDE, 5), ("on", INSIDE, 5),
                   ("off2", OUTSIDE, 5), ("on2", INSIDE, 5)):
    u.SetCursorPos(*pt)
    time.sleep(0.9)
    res = [grab(tag) for _ in range(n)]
    counts = [len(x) for x in res]
    worst = max(counts)
    rows = sorted({i for x in res for i, _ in x})
    print("  cursor %-5s flagged rows per frame %s  total %d  at %s"
          % (tag, counts, sum(counts), rows[:8]))