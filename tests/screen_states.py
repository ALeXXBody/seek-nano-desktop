"""Find out WHAT differs between the two screen states.

screen_capture.py showed the screen alternating between two appearances, one of
them bit-identical every time (mean 141.72, std 120.34) and one varying around
mean 184 (std ~90). 17.1% peak-to-peak.

Saves an example of each state and reports the bounding box of the difference,
so the flickering region is identified rather than guessed at.
"""
import ctypes
import ctypes.wintypes as wt
import time

import numpy as np
from PIL import Image, ImageGrab

TITLE = "Seek Nano Viewer"
u = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    u.SetProcessDPIAware()
HWND_TOPMOST = -1
u.FindWindowW.restype = wt.HWND
u.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wt.UINT]
u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]


def rect(h):
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


hwnd = u.FindWindowW(None, TITLE)
if not hwnd:
    raise SystemExit("window not found")
u.ShowWindow(hwnd, 9)
u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0040)
time.sleep(1.0)
r = rect(hwnd)
bw, bh = r[2] - r[0], r[3] - r[1]
print("window rect %s  (%dx%d)" % (str(r), bw, bh))

W = np.array([0.299, 0.587, 0.114], np.float32)
shots = []
t0 = time.perf_counter()
while time.perf_counter() - t0 < 20.0:
    a = np.asarray(ImageGrab.grab(bbox=r, all_screens=True)
                   .convert("RGB")).astype(np.float32)
    shots.append(a)
    time.sleep(0.01)
print("captured %d" % len(shots))

means = np.array([(s * W).sum(axis=2).mean() for s in shots])
lo = means < np.median(means)
print("mean range %.1f .. %.1f, median %.1f" % (means.min(), means.max(),
                                                 np.median(means)))
A = [s for s, l in zip(shots, lo) if l]
B = [s for s, l in zip(shots, lo) if not l]
print("state A (low mean): %d captures, mean %.2f" % (len(A), means[lo].mean()))
print("state B (high mean): %d captures, mean %.2f"
      % (len(B), means[~lo].mean()))

if not A or not B:
    raise SystemExit("only one state seen; nothing to compare")
# the most representative of each
a = A[int(np.argmin([abs((s * W).sum(axis=2).mean() - means[lo].mean())
                     for s in A]))]
b = B[int(np.argmin([abs((s * W).sum(axis=2).mean() - means[~lo].mean())
                     for s in B]))]
Image.fromarray(a.astype(np.uint8)).save(r"C:\a\_state_A.png")
Image.fromarray(b.astype(np.uint8)).save(r"C:\a\_state_B.png")

diff = np.abs(a - b).max(axis=2)
print("\nper-pixel max difference between the two states:")
print("  overall max %.1f  mean %.2f  fraction differing >8: %.3f"
      % (diff.max(), diff.mean(), float((diff > 8).mean())))
rows = np.where(diff.max(axis=1) > 8)[0]
cols = np.where(diff.max(axis=0) > 8)[0]
if len(rows) and len(cols):
    print("  differing region: rows %d..%d of %d, cols %d..%d of %d"
          % (rows[0], rows[-1], a.shape[0], cols[0], cols[-1], a.shape[1]))
    print("  -> %s"
          % ("FULL FRAME" if (rows[-1] - rows[0] > a.shape[0] * 0.8 and
                              cols[-1] - cols[0] > a.shape[1] * 0.8)
             else "a sub-region"))

# is one state simply the image plus a black margin?
for tag, s in (("A", a), ("B", b)):
    lum = (s * W).sum(axis=2)
    # count fully-black columns and rows
    darkcols = np.where(lum.max(axis=0) < 10)[0]
    darkrows = np.where(lum.max(axis=1) < 10)[0]
    print("\nstate %s: mean %7.2f  std %6.2f" % (tag, lum.mean(), lum.std()))
    print("  fully black columns: %d  (first %s, last %s)"
          % (len(darkcols), darkcols[0] if len(darkcols) else "-",
             darkcols[-1] if len(darkcols) else "-"))
    print("  fully black rows   : %d  (first %s, last %s)"
          % (len(darkrows), darkrows[0] if len(darkrows) else "-",
             darkrows[-1] if len(darkrows) else "-"))
    q = np.percentile(lum, [1, 25, 50, 75, 99])
    print("  percentiles 1/25/50/75/99: %s"
          % " ".join("%.0f" % v for v in q))
print("\nsaved C:\\a\\_state_A.png and C:\\a\\_state_B.png")