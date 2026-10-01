"""Click a wx button by its label, on the real screen.

The hot-spot controls are buttons with no keyboard accelerator, and PrintWindow
is blind to wx windows, so the only way to drive them is a real mouse click at
the real screen position. Enumerating the child HWNDs finds the button and
gives its rectangle in screen coordinates.

Usage: click_button.py "<label substring>"
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

u = ctypes.windll.user32
u.FindWindowW.restype = wt.HWND
u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u.EnumChildWindows.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_void_p]
u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
u.SetForegroundWindow.argtypes = [wt.HWND]
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u.mouse_event.argtypes = [wt.DWORD, ctypes.c_int, ctypes.c_int,
                          wt.DWORD, ctypes.c_void_p]

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
LEFTDOWN, LEFTUP = 0x0002, 0x0004


def child_text(hwnd):
    n = u.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    buf = ctypes.create_unicode_buffer(n + 2)
    u.SendMessageW(hwnd, WM_GETTEXT, n + 2, ctypes.byref(buf))
    return buf.value


def children(hwnd):
    out = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _l):
        out.append(h)
        return True
    u.EnumChildWindows(hwnd, proc(cb), 0)
    return out


def main():
    want = sys.argv[1]
    main_hwnd = u.FindWindowW(None, "Seek Nano Viewer")
    if not main_hwnd:
        print("viewer window not found")
        return 2
    u.SetForegroundWindow(main_hwnd)
    time.sleep(0.4)
    for h in children(main_hwnd):
        txt = child_text(h)
        if want.lower() in txt.lower() and txt.strip():
            r = wt.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            print("clicking %r at (%d, %d)" % (txt, cx, cy))
            u.SetCursorPos(cx, cy)
            time.sleep(0.15)
            u.mouse_event(LEFTDOWN, 0, 0, 0, None)
            time.sleep(0.05)
            u.mouse_event(LEFTUP, 0, 0, 0, None)
            time.sleep(0.4)
            print("now reads %r" % child_text(h))
            return 0
    print("no button matching %r; visible labels were:" % want)
    for h in children(main_hwnd):
        t = child_text(h)
        if t.strip():
            print("   %r" % t)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())