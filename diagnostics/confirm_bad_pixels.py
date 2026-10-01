"""Confirm the bad pixels found in the flat reference against LIVE frames.

The reference is captured on a uniform wall, so scene content is averaged away
but defective elements remain as extreme outliers. Six pixels stand out by more
than 8x the local spread:

    (189,241) +1142    (189, 36) -6544    (161,209) +1145
    (155,298) +1147    (137, 31) -1678    (130,135) +1700

An independent live-frame scan separately flagged (137,31) as present in
102 of 105 frames, which is the same coordinate - a useful cross-check that
these are real defects and not an artefact of the reference capture.

This confirms each candidate against live frames: how often is it far from its
neighbours, compared with how often a randomly chosen good pixel is?
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

CANDS = [(189, 241), (189, 36), (161, 209), (155, 298), (137, 31), (130, 135)]
rng = np.random.default_rng(5)
CONTROL = [(int(rng.integers(4, IH - 4)), int(rng.integers(4, IW - 4)))
           for _ in range(6)]

hits = defaultdict(int)
devsum = defaultdict(float)
n = 0
for _ in range(150):
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
    n += 1
    for y, x in CANDS + CONTROL:
        win = r[y - 1:y + 2, x - 1:x + 2].copy()
        c = win[1, 1]
        nb = np.concatenate([win[0, :], win[2, :], win[1, :2], win[1, 2:]])
        dev = float(c - np.median(nb))
        devsum[(y, x)] += dev
        if abs(dev) > 60.0:
            hits[(y, x)] += 1
    time.sleep(0.012)
usb.util.dispose_resources(d)

print("live gain-3 frames: %d\n" % n)
print("candidates from the flat reference:")
print("  %-10s %-12s %-12s %s" % ("y,x", "frames>60DL", "mean dev", "verdict"))
bad = []
for y, x in CANDS:
    pct = 100.0 * hits[(y, x)] / max(1, n)
    verdict = "BAD PIXEL" if pct > 70 else ("suspect" if pct > 35 else "not confirmed")
    if pct > 70:
        bad.append((y, x))
    print("  %-10s %5.1f%%       %+8.1f DL  %s"
          % ("%d,%d" % (y, x), pct, devsum[(y, x)] / max(1, n), verdict))
print("\ncontrol pixels (random, should mostly NOT trigger):")
ctrl = 0
for y, x in CONTROL:
    pct = 100.0 * hits[(y, x)] / max(1, n)
    if pct > 70:
        ctrl += 1
    print("  %-10s %5.1f%%       %+8.1f DL"
          % ("%d,%d" % (y, x), pct, devsum[(y, x)] / max(1, n)))
print()
print("confirmed bad pixels: %d   (control false positives: %d of 6)"
      % (len(bad), ctrl))
if bad:
    print("confirmed: %s" % ", ".join("(%d,%d)" % p for p in bad))