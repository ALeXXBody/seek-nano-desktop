"""Screenshot the live window and look for the pulsing.

Everything measured so far says the picture is stable: the processed output
varies by 0.2/255 frame to frame, the repaint costs ~11 ms against a 70 ms
timer, and the draw is a straight 1:1 blit. Yet the user sees the brightness
pulse. So measure the SCREEN - the only thing that includes whatever Windows
does on top of the bitmap.

Uses PrintWindow(PW_RENDERFULLCONTENT) rather than a screen grab: a screen
grab only sees whatever is in front, and running this from a console captured
the console (a uniform dark rectangle, zero variance) rather than the app.
"""
import ctypes
import ctypes.wintypes as wt
import time

import numpy as np

TITLE = "Seek Nano Viewer"
u = ctypes.windll.user32
g = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    u.SetProcessDPIAware()

u.GetWindowDC.restype = wt.HDC
u.GetWindowDC.argtypes = [wt.HWND]
g.CreateCompatibleDC.restype = wt.HDC
g.CreateCompatibleDC.argtypes = [wt.HDC]
g.CreateCompatibleBitmap.restype = wt.HBITMAP
g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
u.PrintWindow.restype = wt.BOOL
u.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
# handle types must be declared, or a 64-bit HBITMAP overflows a default int arg
g.SelectObject.restype = wt.HGDIOBJ
g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
g.GetDIBits.restype = ctypes.c_int
g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                        ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
g.DeleteObject.argtypes = [wt.HGDIOBJ]
g.DeleteDC.argtypes = [wt.HDC]
u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def rect(hwnd):
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def grab_window(hwnd):
    l, t, r, b = rect(hwnd)
    w, h = r - l, b - t
    hdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mdc, bmp)
    ok = u.PrintWindow(hwnd, mdc, 2)          # PW_RENDERFULLCONTENT
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h                # negative = top-down
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(hwnd, hdc)
    if not ok:
        return None
    return np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3].copy()


hwnd = u.FindWindowW(None, TITLE)
if not hwnd:
    raise SystemExit("window %r not found - is SeekNano running?" % TITLE)
print("window:", hwnd, "rect:", rect(hwnd))

# sanity: make sure we are actually looking at the app, not a blank buffer
probe = grab_window(hwnd)
if probe is None or probe.std() < 1.0:
    print("probe std %s - PrintWindow gave nothing; cannot judge the picture."
          % (None if probe is None else round(float(probe.std()), 3)))
    raise SystemExit(1)
print("probe looks like content: std %.2f, mean %.1f"
      % (probe.std(), probe.mean()))

means = []
stds = []
sizes = []
t0 = time.perf_counter()
GRAB = 8.0
while time.perf_counter() - t0 < GRAB:
    a = grab_window(hwnd)
    if a is None:
        break
    l, t, r, b = rect(hwnd)
    sizes.append((r - l, b - t))
    lum = (a.astype(np.float32)
           * np.array([0.299, 0.587, 0.114], np.float32)).sum(axis=2)
    means.append(float(lum.mean()))
    stds.append(float(lum.std()))

m = np.array(means)
n = len(m)
print(f"\n{GRAB:.1f} s, {n} grabs -> {n / GRAB:.1f} Hz")
print(f"screen mean {m.mean():.2f}  min {m.min():.2f}  max {m.max():.2f}  "
      f"peak-to-peak {m.max() - m.min():.2f}/255")
print(f"frame-to-frame |step| mean {np.abs(np.diff(m)).mean():.3f}  "
      f"max {np.abs(np.diff(m)).max():.3f}")
print(f"per-frame std: mean {np.mean(stds):.1f}  min {np.min(stds):.1f}  "
      f"max {np.max(stds):.1f}")

u2 = sorted(set(sizes))
print(f"\nwindow sizes seen: {u2}")
if len(u2) > 1:
    print("  -> THE WINDOW IS BEING RESIZED WHILE STREAMING")

d = m - m.mean()
if np.std(d) > 1e-6:
    f = np.fft.rfftfreq(len(d), GRAB / n)
    sp = np.abs(np.fft.rfft(d))
    top = [i for i in np.argsort(sp)[1:9][::-1] if f[i] > 0][:4]
    print("  dominant periods: "
          + ", ".join("%.2f s" % (1.0 / f[i]) for i in top))
    ac = np.correlate(d, d, "full")[n - 1:]
    ac /= ac[0]
    print("  autocorr lag 1,5,10,20,30: "
          + ", ".join("%.2f" % ac[l] for l in (1, 5, 10, 20, 30) if l < n))

print("\nmeans:")
for i in range(0, min(n, 100), 10):
    print("  " + " ".join("%6.1f" % v for v in m[i:i + 10]))
pp = 100 * (m.max() - m.min()) / 255
print(f"\npeak-to-peak is {pp:.2f}% of full scale - "
      + ("visible pulse" if pp > 2 else "below the visible threshold"))