"""Are the bright blobs fixed-position bad pixels, or transient noise?

The screen shows 3-5 small, distinctly hot, blocky blobs. Two very different
causes need different fixes:

  a) bad pixels - the same sensor elements read high every frame. The fix is a
     bad-pixel map that replaces each one with its neighbours' median. Every
     thermal camera does this, and it is persistent and countable.
  b) transient spikes - noise, which move around and average away.

So: find isolated outliers (pixels far hotter than the median of their eight
neighbours, after the flat-field reference is removed) and check whether the
same coordinates recur frame to frame.
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

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
d.set_configuration()
usb.util.claim_interface(d, 0)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00")
W(0x3C, b"\x00\x00")
Rd(0x3D, 2)
W(0x3E, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00")
W(0x3C, b"\x01\x00")
Rd(0x3D, 2)

ref = np.fromfile(r"C:\a\ffc_latest.raw", dtype="<u2").reshape(IH, IW).astype(np.float32)
print("reference: mean %.0f std %.1f\n" % (ref.mean(), ref.std()))


def neighbours(a):
    s = np.zeros_like(a)
    c = 0
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                s += np.roll(np.roll(a, dy, 0), dx, 1)
                c += 1
    return s / c


hits = defaultdict(int)
per_frame = []
devs = []
for n in range(120):
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < 177840:
            b += bytes(d.read(0x81, min(6840, 177840 - len(b)), timeout=800))
    except Exception:
        pass
    if len(b) != 177840:
        continue
    h = np.frombuffer(b[:8], "<u2")
    if int(h[0]) != 0x0579 or int(h[2]) != 3:
        continue
    a = np.frombuffer(b, dtype="<u2").reshape(RH, RW).astype(np.float32)
    a = a[RY:RY + IH, RX:RX + IW]
    r = a - ref + float(ref.mean())
    dev = r - neighbours(r)
    mad = float(np.median(np.abs(dev - np.median(dev))))
    thr = max(8.0 * mad, 200.0)
    hot = dev > thr
    per_frame.append(int(hot.sum()))
    for y, x in zip(*np.where(hot)):
        hits[(int(y), int(x))] += 1
        devs.append(float(dev[y, x]))
    time.sleep(0.015)
usb.util.dispose_resources(d)

print("frames analysed: %d" % len(per_frame))
if not per_frame:
    raise SystemExit("no frames")
print("hot outliers per frame: mean %.1f  min %d  max %d"
      % (np.mean(per_frame), min(per_frame), max(per_frame)))
print("peak deviation above neighbours: median %.0f  max %.0f DL"
      % (np.median(devs), max(devs)))
print()
total = len(per_frame)
recurring = {k: v for k, v in hits.items() if v >= max(3, total * 0.5)}
print("distinct outlier pixels ever seen : %d" % len(hits))
print("seen in >=50%% of frames (bad px) : %d" % len(recurring))
if recurring:
    top = sorted(recurring.items(), key=lambda kv: -kv[1])[:12]
    print()
    print("  y,x  frames-hit")
    for (y, x), c in top:
        print("   %3d,%-3d  %d/%d" % (y, x, c, total))
print()
if len(recurring) >= 2:
    print("VERDICT: these are FIXED sensor bad pixels.")
    print("A bad-pixel map (replace each with the median of its neighbours)")
    print("would remove them permanently, with no ghosting and no lag.")
else:
    print("VERDICT: not fixed - transient spikes, so they cannot be mapped.")
    print("They will average away; a temporal median would cost ghosting.")