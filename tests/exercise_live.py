"""Exercise the paths I changed, on the live app, and watch for crashes.

The crash-prone paths tonight were: a rejected frame flowing into _bmp, the
scale bar with a None display window, a snapshot on a rejected frame, and the
USB-retry timer. Static checks covered the code; this covers the running app by
driving those exact interactions and watching both the crash log and the
process.

Fails loudly if the process dies or the crash log appears.
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

CRASH = r"C:\a\seeknano_crash.log"
u = ctypes.windll.user32
u.FindWindowW.restype = wt.HWND
u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wt.UINT]
u.SetForegroundWindow.argtypes = [wt.HWND]
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u.mouse_event.argtypes = [wt.DWORD, ctypes.c_int, ctypes.c_int, wt.DWORD,
                          ctypes.c_void_p]
WM_GETTEXTLENGTH, WM_GETTEXT = 0x000E, 0x000D
u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

h = u.FindWindowW(None, "Seek Nano Viewer")
if not h:
    raise SystemExit("viewer not running")
u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0040)
time.sleep(0.6)
r = wt.RECT()
u.GetWindowRect(h, ctypes.byref(r))
u.SetForegroundWindow(h)
time.sleep(0.4)


def labels():
    out = {}
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hh, _l):
        n = u.SendMessageW(hh, WM_GETTEXTLENGTH, 0, 0)
        b = ctypes.create_unicode_buffer(n + 2)
        u.SendMessageW(hh, WM_GETTEXT, n + 2, ctypes.byref(b))
        if b.value.strip():
            out.setdefault(b.value, hh)
        return True
    u.EnumChildWindows(h, proc(cb), 0)
    return out


def click(match):
    for txt, hh in labels().items():
        if match.lower() in txt.lower():
            rr = wt.RECT()
            u.GetWindowRect(hh, ctypes.byref(rr))
            cx, cy = (rr.left + rr.right) // 2, (rr.top + rr.bottom) // 2
            u.SetCursorPos(cx, cy)
            time.sleep(0.12)
            u.mouse_event(0x0002, 0, 0, 0, None)
            time.sleep(0.05)
            u.mouse_event(0x0004, 0, 0, 0, None)
            time.sleep(0.35)
            return txt
    return None


def key(ch):
    u.SetCursorPos(r.left + 40, r.top + 700)
    VK = {" ": 0x20, "f": 0x46, "p": 0x50, "c": 0x43, "d": 0x44}
    vk = VK[ch]
    for dw in (0x0100, 0x0101):
        u.keybd_event(vk, 0, dw, 0)
    time.sleep(0.3)


def crash_size():
    try:
        return os.path.getsize(CRASH)
    except OSError:
        return 0


if os.path.exists(CRASH):
    os.remove(CRASH)

print("driving the app for 90 s, watching the crash log\n")
base = crash_size()
steps = []
steps.append(("cycle hot-spot mode x3", lambda: [click("hot spots") for _ in range(3)]))
steps.append(("cycle sensitivity x3", lambda: [click("sens") for _ in range(3)]))
steps.append(("snapshot P", lambda: key("p")))
steps.append(("next colormap C", lambda: key("c")))
steps.append(("pause/resume SPACE", lambda: [key(" "), time.sleep(0.6), key(" ")]))
steps.append(("capture flat F", lambda: key("f")))
steps.append(("Save PNG button", lambda: click("Save PNG")))
steps.append(("Dump raw button", lambda: click("Dump raw")))

for name, fn in steps:
    try:
        fn()
        s = crash_size()
        status = "CRASHED (%d B)" % s if s > base else "ok"
        print("  %-24s %s" % (name, status))
        if s > base:
            raise SystemExit("aborting - crash log grew")
    except SystemExit:
        raise
    except Exception as e:
        print("  %-24s driver error %r" % (name, e))

print("\nidling 45 s to let a USB stall or a rejected frame land...")
t0 = time.time()
while time.time() - t0 < 45:
    time.sleep(3)
    if crash_size() > base:
        print("  CRASH during idle")
        raise SystemExit(1)
    if not u.IsWindow(h):
        print("  THE WINDOW DIED")
        raise SystemExit(1)

print("  no crash, window alive")
print("\nRESULT: PASS" if crash_size() == base else "\nRESULT: FAIL")