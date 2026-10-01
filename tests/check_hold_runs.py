"""How long does the AGC dwell off the shown gain? That is the freeze.

The stutter is the display freezing while _tick holds the previous frame. The
freeze length is the run of consecutive frames at some other gain, times the
frame period. So measure the run lengths directly rather than inferring a
bound.

Also records the frame period, so run lengths can be converted to wall-clock
freeze, which is what the eye actually sees.
"""
import os
import time
from collections import defaultdict

import numpy as np

DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH = 342, 260
d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
d.set_configuration()
usb.util.claim_interface(d, 0)
for _ in range(2):
    try:
        d.ctrl_transfer(0x40, 0x3C, 0, 0, b"\x00\x00", 1250)
    except Exception:
        pass
    time.sleep(0.15)
try:
    d.reset()
    time.sleep(1.2)
    d.set_configuration()
    usb.util.claim_interface(d, 0)
except Exception as e:
    print("reset:", e)
time.sleep(0.4)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00")
W(0x3C, b"\x00\x00")
Rd(0x3D, 2)
W(0x3E, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00")
W(0x3C, b"\x01\x00")
Rd(0x3D, 2)

seq = []
t0 = time.perf_counter()
for _ in range(600):
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < 177840:
            b += bytes(d.read(0x81, min(6840, 177840 - len(b)), timeout=700))
    except Exception:
        pass
    t = time.perf_counter() - t0
    if len(b) == 177840:
        h = np.frombuffer(b[:8], "<u2")
        if int(h[0]) == 0x0579 and 0 < int(h[2]) < 256:
            seq.append((t, int(h[2])))
    time.sleep(0.005)
usb.util.dispose_resources(d)
print("captured %d frames over %.1f s" % (len(seq), seq[-1][0] if seq else 0))

if len(seq) < 30:
    raise SystemExit("too few frames")
per = np.diff([s[0] for s in seq])
print("frame period: median %.0f ms  mean %.0f ms  (%.1f fps)"
      % (np.median(per) * 1000, per.mean() * 1000, 1.0 / per.mean()))

# run-length encode the gain sequence
runs = []
cur, n = seq[0][1], 0
for _, g in seq:
    if g == cur:
        n += 1
    else:
        runs.append((cur, n))
        cur, n = g, 1
runs.append((cur, n))

byg = defaultdict(list)
for g, n in runs:
    byg[g].append(n)

print()
print(f"{'gain':>5}{'runs':>6}{'frames':>8}{'max run':>9}{'freeze of that run':>20}")
for g in sorted(byg, key=lambda x: -max(byg[x])):
    mx = max(byg[g])
    print(f"{g:>5}{len(byg[g]):>6}{sum(byg[g]):>8}{mx:>9}"
          f"{mx * np.median(per) * 1000:>17.0f} ms")

# what actually matters: runs of a gain OTHER than the shown one
SHOWN = 3
off = [n for g, n in runs if g != SHOWN]
if off:
    off_ms = np.array(off) * np.median(per) * 1000
    print()
    print("freezes if we hold everything except gain %d:" % SHOWN)
    print("  %d episodes, %d frames total" % (len(off), sum(off)))
    print("  run length frames: max %d  mean %.1f" % (max(off),
                                                       float(np.mean(off))))
    print("  freeze ms: max %.0f  mean %.0f  p90 %.0f"
          % (off_ms.max(), off_ms.mean(), np.percentile(off_ms, 90)))
    print("  fraction of time frozen: %.1f%%"
          % (100.0 * sum(off) / len(seq)))
    print()
    for cap in (2, 3, 4, 6, 8):
        kept = sum(min(n, cap) for n in off)
        ms = np.mean([min(n, cap) for n in off]) * np.median(per) * 1000
        print("  cap hold at %d frames: max freeze %.0f ms, mean %.0f ms, "
              "frozen %.1f%% of the time"
              % (cap, cap * np.median(per) * 1000, ms,
                 100.0 * kept / len(seq)))