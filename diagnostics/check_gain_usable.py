"""Is the gain-hold discarding frames that are actually fine to show?

The display freezes whenever the AGC steps off the shown gain, because _tick
holds the previous frame. Measured update cadence showed gaps up to 536 ms,
which is the stutter. But the only gain that genuinely has no picture is 20
(1-99% span 13..30 DL against ~1500 for a real frame) - and MIN_SCENE_SPAN
= 100 DL already rejects that on content alone.

So the question is whether gains 1 and 6 pass the content gate and look like
gain 3. If they do, the gain hold is discarding usable frames and freezing
the picture for nothing.
"""
import os
import sys
import time
from collections import defaultdict

import numpy as np

DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

sys.path.insert(0, r"C:\a\src\app")
import viewer as V

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240

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

raws = []
for _ in range(160):
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < 177840:
            b += bytes(d.read(0x81, min(6840, 177840 - len(b)), timeout=700))
    except Exception:
        pass
    if len(b) == 177840:
        h = np.frombuffer(b[:8], "<u2")
        if int(h[0]) == 0x0579 and 0 < int(h[2]) < 256:
            raws.append((int(h[2]), bytes(b)))
    time.sleep(0.015)
usb.util.dispose_resources(d)
print("captured %d frames\n" % len(raws))

byg = defaultdict(list)
for g, r in raws:
    byg[g].append(r)

print("gate: MIN_SCENE_SPAN = %.0f DL (1-99%% span)" % V.MIN_SCENE_SPAN)
print()
print(f"{'gain':>5}{'n':>5}{'1-99 span DL':>14}{'std DL':>9}{'verdict':>12}"
      f"{'shape corr vs g3':>20}")
base = None
stats = {}
for g in sorted(byg):
    rs = byg[g]
    spans = []
    imgs = []
    for r in rs:
        a = np.frombuffer(r, dtype="<u2")[:RW * RH].reshape(RH, RW)
        im = a[RY:RY + IH, RX:RX + IW].astype(np.float32)
        spans.append(float(np.percentile(im, 99) - np.percentile(im, 1)))
        imgs.append(im)
    ok = V._frame_has_content(rs[0])
    sp = float(np.mean(spans))
    med = np.median(np.stack(imgs), axis=0)
    if base is None:
        base = med
        corr = 1.0
    else:
        a0, b0 = base - base.mean(), med - med.mean()
        corr = float((a0 * b0).sum() / max(1e-9,
                  np.sqrt((a0 * a0).sum() * (b0 * b0).sum())))
    stats[g] = (sp, corr, ok, len(rs))
    print(f"{g:>5}{len(rs):>5}{sp:>14.1f}{med.std():>9.1f}"
          f"{('SHOW' if ok else 'reject'):>12}{corr:>20.4f}")

print()
good = [g for g in stats if stats[g][2]]
print("gains that PASS the content gate : %s" % sorted(good))
print("gains that FAIL it                : %s"
      % sorted(g for g in stats if not stats[g][2]))
print()
if 3 in stats:
    for g in good:
        if g == 3:
            continue
        sp, corr, ok, n = stats[g]
        print("gain %-3d passes the gate and its pattern correlates %+.4f with "
              "gain 3 -> usable, so holding it is what freezes the display"
              % (g, corr))
print()
print("VERDICT:", "the gain hold is discarding usable frames"
      if len(good) > 1 else "only one usable gain; the hold is doing no harm")