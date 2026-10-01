"""Capture the ACTUAL screen, not what the app writes to disk.

Every measurement so far has been of the app's output - burst PNGs, processed
dumps, window counters. All of them say the picture is stable. The user says
the screen flickers. Those two facts can only be reconciled by looking at the
screen, and PrintWindow cannot do it: wx draws via wx.PaintDC and does not
answer WM_PRINTCLIENT, so PrintWindow returned 234 byte-identical frames while
the app was streaming.

So capture the framebuffer instead: make the window TOPMOST so nothing
occludes it, then grab the screen rectangle. That is what the user sees,
including anything Windows does on top of the bitmap.

Sanity-checks that the capture is live before trusting it - a uniform region
means something is occluding the window, which is exactly what happened the
first time this was attempted from a console.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

import numpy as np
from PIL import ImageGrab

TITLE = "Seek Nano Viewer"
u = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    u.SetProcessDPIAware()

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040

u.FindWindowW.restype = wt.HWND
u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wt.UINT]
u.SetForegroundWindow.argtypes = [wt.HWND]
u.GetForegroundWindow.restype = wt.HWND
u.GetWindowThreadProcessId.restype = wt.DWORD
u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.c_void_p]
k32 = ctypes.windll.kernel32
k32.GetCurrentThreadId.restype = wt.DWORD
u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
u.BringWindowToTop.argtypes = [wt.HWND]
u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]


def rect(h):
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


hwnd = u.FindWindowW(None, TITLE)
if not hwnd:
    raise SystemExit("window not found - is SeekNano running?")

u.ShowWindow(hwnd, 9)
u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
               SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
fg = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
me = k32.GetCurrentThreadId()
u.AttachThreadInput(me, fg, True)
u.BringWindowToTop(hwnd)
u.SetForegroundWindow(hwnd)
u.AttachThreadInput(me, fg, False)
time.sleep(1.2)
print("window made TOPMOST; rect", rect(hwnd))

W = np.array([0.299, 0.587, 0.114], np.float32)
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0


def grab():
    r = rect(hwnd)
    im = ImageGrab.grab(bbox=r, all_screens=True)
    return np.asarray(im.convert("RGB")).astype(np.float32)


probe = grab()
lum = (probe * W).sum(axis=2)
print("probe: mean %.1f  std %.2f  shape %s" % (lum.mean(), lum.std(),
                                                 lum.shape))
if lum.std() < 4.0:
    raise SystemExit(
        "capture is a flat region (std %.2f) - the window is occluded or "
        "blank; refusing to draw conclusions from it." % lum.std())

means, stds, mins, times = [], [], [], []
uniq = set()
t0 = time.perf_counter()
while time.perf_counter() - t0 < DUR:
    a = grab()
    l = (a * W).sum(axis=2)
    t = time.perf_counter() - t0
    means.append(float(l.mean()))
    stds.append(float(l.std()))
    mins.append(float(l.min()))
    times.append(t)
    # cheap fingerprint so we can tell a live capture from a frozen one
    uniq.add(hash(a[::8, ::8].tobytes()))
    time.sleep(0.01)

m = np.array(means)
s = np.array(stds)
mn = np.array(mins)
tt = np.array(times)
n = len(m)
print("\n%d grabs in %.0f s -> %.1f Hz" % (n, DUR, n / DUR))
print("distinct fingerprints: %d  (1 would mean a frozen capture)" % len(uniq))
print()
print("screen mean : %.2f  min %.2f  max %.2f  p-p %.2f/255"
      % (m.mean(), m.min(), m.max(), m.max() - m.min()))
print("screen std  : %.2f  min %.2f  max %.2f" % (s.mean(), s.min(), s.max()))
print("darkest px  : %.2f  min %.2f" % (mn.mean(), mn.min()))
d = np.abs(np.diff(m))
print("frame-to-frame |step|: mean %.3f  max %.3f" % (d.mean(), d.max()))

dark = int((mn < 12).sum())
print("\ncaptures with a near-black pixel: %d of %d" % (dark, n))
verydark = int((m < 20).sum())
print("captures with overall mean < 20/255: %d" % verydark)

# where in the frame does the flicker live?
if len(uniq) > 3:
    rows = []
    for a in ():
        pass
print("\nmeans over time (every 6th):")
for i in range(0, n, 6):
    bar = "#" * int(max(0.0, (m[i] - m.min()) / max(1e-6, m.max() - m.min())
                        * 46))
    print("  %5.2fs  %7.2f  std %6.2f  %s" % (tt[i], m[i], s[i], bar))
pp = m.max() - m.min()
print("\np-p %.2f/255 = %.1f%% - %s" % (pp, 100 * pp / 255,
                                       "VISIBLE" if pp > 5 else "subtle"))