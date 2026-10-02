"""Does the gain lock explain the unusable picture?

The app holds gain 3 ("HOLDING GAIN 3 (CAMERA WANTS 20)"). Gain 3 is the
bottom of the range and therefore the noisiest: the sensor integrates less
signal per frame. The auto-stretch then maps the 2-98 percentile span across
the whole ironbow ramp, so whatever noise exists is amplified to full scale.
A wall reference cannot fix that - it removes a fixed per-pixel offset, not
the photon noise that a low gain produces.

So measure the same pipeline at each gain and see where the noise actually is.
If gain 3 is much noisier than gain 20, then holding the gain is the defect,
not the missing flat capture.
"""
import os
import time
from collections import defaultdict

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
TOTAL = 177840
WANT = 260

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
if d is None:
    raise SystemExit("camera not found - close SeekNano first")
d.set_configuration()
usb.util.claim_interface(d, 0)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00")
W(0x3C, b"\x00\x00")
Rd(0x3D, 2)
W(0x3E, b"\x08\x00")
W(0x3C, b"\x01\x00")
Rd(0x3D, 2)

by_gain = defaultdict(list)
t_end = time.time() + 40
while time.time() < t_end and sum(len(v) for v in by_gain.values()) < WANT:
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < TOTAL:
            b += bytes(d.read(0x81, min(6840, TOTAL - len(b)), timeout=800))
    except Exception:
        continue
    if len(b) != TOTAL:
        continue
    h = np.frombuffer(b[:8], "<u2")
    if int(h[0]) != 0x0579:
        continue
    a = np.frombuffer(b, dtype="<u2").reshape(RH, RW)[RY:RY + IH, RX:RX + IW]
    by_gain[int(h[2])].append(a.astype(np.float32))
usb.util.dispose_resources(d)


def med3(a):
    p = np.pad(a, 1, mode="edge")
    return np.partition(
        np.stack([p[dy:dy + IH, dx:dx + IW]
                  for dy in range(3) for dx in range(3)]), 4, axis=0)[4]


def boxblur(a, k):
    p = np.pad(a, k // 2, mode="edge")
    c = np.cumsum(np.cumsum(p, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / float(k * k)


def nse(a):
    return float(np.abs(np.diff(a, axis=1)).mean())


print("gain  frames   raw    +median  +bg_hp   2-98 span after bg_hp")
print("-" * 68)
rows = []
for g in sorted(by_gain):
    fr = by_gain[g]
    if len(fr) < 4:
        continue
    meds = np.stack([med3(f) for f in fr])
    bg = meds.mean(axis=0)
    hp = bg - boxblur(bg, 9)
    fixed = meds - hp[None, ...]
    raw_n = float(np.mean([nse(f) for f in fr]))
    med_n = float(np.mean([nse(m) for m in meds]))
    bg_n = float(np.mean([nse(f) for f in fixed]))
    span = float(np.percentile(fixed, 98) - np.percentile(fixed, 2))
    rows.append((g, len(fr), raw_n, med_n, bg_n, span))
    print("%4d  %6d  %6.1f  %7.1f  %6.1f  %10.1f"
          % (g, len(fr), raw_n, med_n, bg_n, span))

if len(rows) < 2:
    raise SystemExit("only one gain appeared - point the camera somewhere with "
                     "more contrast, or run the app so its gain lock settles")

lo = min(rows, key=lambda r: r[2])
hi = max(rows, key=lambda r: r[2])
print()
print("gain %d is the noisiest at %.1f DL, gain %d the cleanest at %.1f DL "
      "- %.1fx" % (lo[0], lo[2], hi[0], hi[2], lo[2] / max(hi[2], 1e-6)))
print("the app holds gain %d" % rows[0][0])
print()
print("The 2-98 span is what the auto-stretch maps across the whole colormap.")
print("A wide span is a GOOD sign (real scene contrast); a narrow span means")
print("noise is being stretched to full scale, which is what makes the display")
print("look like static.")