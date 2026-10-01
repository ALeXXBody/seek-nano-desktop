"""Post a key straight to the SeekNano window's message queue.

SendKeys needs the window to be genuinely foreground, and the console keeps
stealing it back, so the keystroke goes nowhere. Posting WM_KEYDOWN/WM_KEYUP to
the window handle puts it in that window's own queue, where
TranslateAccelerator picks it up - the same path a real keypress takes, with no
dependence on focus.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

u = ctypes.windll.user32
k = ctypes.windll.kernel32

u.FindWindowW.restype = wt.HWND
u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u.PostMessageW.restype = wt.BOOL
u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u.SetForegroundWindow.argtypes = [wt.HWND]
u.IsIconic.restype = wt.BOOL
u.IsIconic.argtypes = [wt.HWND]

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
VK = {"s": 0x53, "f": 0x46, "c": 0x43, "p": 0x50, "d": 0x44, "q": 0x51,
      " ": 0x20}

hwnd = u.FindWindowW(None, "Seek Nano Viewer")
if not hwnd:
    raise SystemExit("window not found")
u.ShowWindow(hwnd, 9)
u.SetForegroundWindow(hwnd)

keys = (sys.argv[1] if len(sys.argv) > 1 else "s").lower()
for ch in keys:
    vk = VK.get(ch)
    if vk is None:
        print("no VK for %r" % ch)
        continue
    scan = u.MapVirtualKeyW(vk, 0)
    down = 1 | (scan << 16)                     # key down, repeat count 1
    up = 1 | (scan << 16) | (1 << 30) | (1 << 31)
    a = u.PostMessageW(hwnd, WM_KEYDOWN, vk, down)
    time.sleep(0.06)
    b = u.PostMessageW(hwnd, WM_KEYUP, vk, up)
    print("posted %r vk=0x%02X down=%s up=%s" % (ch, vk, bool(a), bool(b)))
    time.sleep(0.5)
print("done")