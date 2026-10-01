"""Does flicker actually separate the bad pixels on this hardware?

The bank-based detector assumes yes: that a defective element fluctuates far
more frame to frame than ordinary sensor noise, while the fixed pattern is
identical in every frame and cancels against the temporal median.

The app's last real wall capture returned 0 bad pixels with that test, while a
single-image test on the same scene returns 9. One of the two is wrong about
this hardware, so measure it instead of assuming: capture a bank of raw frames
and compare, at each of the six confirmed defect positions, how much the value
actually moves frame to frame against how much everything else moves.
"""
import os
import time

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
NFRAMES = 32

# Confirmed on 147 live frames earlier at these ROI coordinates.
SIX = [(189, 241), (189, 36), (161, 209), (155, 298), (137, 31), (130, 135)]

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
if d is None:
    raise SystemExit("camera not found")
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

bank = []
raw_all = []
for _ in range(NFRAMES * 3):
    if len(bank) >= NFRAMES:
        break
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < 177840:
            b += bytes(d.read(0x81, min(6840, 177840 - len(b)), timeout=800))
    except Exception:
        continue
    if len(b) != 177840:
        continue
    h = np.frombuffer(b[:8], "<u2")
    if int(h[0]) != 0x0579:
        continue
    a = np.frombuffer(b, dtype="<u2").reshape(RH, RW)[RY:RY + IH, RX:RX + IW]
    bank.append(a.astype(np.float32))
    raw_all.append(int(h[2]))
    time.sleep(0.01)
usb.util.dispose_resources(d)

bank = np.stack(bank)
np.save(r"C:\a\bank_probe.npy", bank)
print("captured %d frames, gains seen %s" % (len(bank), sorted(set(raw_all))))
print("saved C:\\a\\bank_probe.npy\n")

tmed = np.median(bank, axis=0)
resid = bank - tmed[None, ...]
rmad = float(np.median(np.abs(resid - np.median(resid))))
print("temporal residual spread (MAD-based sigma): %.2f DL" % rmad)
print("=> a 6-sigma flicker threshold is %.1f DL\n" % (6 * rmad))

print("per-pixel frame-to-frame range (max - min over the bank):")
rng_px = bank.max(axis=0) - bank.min(axis=0)
med_rng = float(np.median(rng_px))
mad_rng = float(np.median(np.abs(rng_px - med_rng)))
print("   whole image: median range %.1f DL, spread of range %.1f DL" % (med_rng, mad_rng))
print("   threshold max(6x, 300) = %.1f DL\n" % max(6 * mad_rng, 300.0))

print("the six confirmed defects:")
print("   %-12s %10s %10s %10s" % ("position", "range DL", "x spread", "offset DL"))
for (y, x) in SIX:
    r = rng_px[y, x]
    off = float(tmed[y, x] - np.median(
        np.delete(tmed[y].copy(), min(max(x - 2, 0), IW - 5))[:0] or [0]))
    print("   (%3d,%3d)    %10.1f %10.1f %10.1f"
          % (y, x, r, r / max(mad_rng, 1e-6), off))

print()
print("rank of each defect among all 76,800 pixels by flicker range (1 = most)")
order = np.argsort(rng_px.ravel())[::-1]
rankmap = np.empty(rng_px.size, np.int32)
rankmap[order] = np.arange(1, rng_px.size + 1)
for (y, x) in SIX:
    print("   (%3d,%3d)  rank %6d of %d" % (y, x, rankmap[y, x], rng_px.size))

print()
print("same, ranked by offset from the local median (what the old test used):")
dev = tmed - np.median(
    np.pad(tmed, 1, mode="edge")[1:-1, 1:-1])
dev = tmed - np.array([
    [np.median(tmed[max(0, y - 1):y + 2, max(0, x - 1):x + 2])
     for x in range(IW)] for y in range(IH)], np.float32)
ad = np.abs(dev)
order2 = np.argsort(ad.ravel())[::-1]
rank2 = np.empty(ad.size, np.int32)
rank2[order2] = np.arange(1, ad.size + 1)
for (y, x) in SIX:
    print("   (%3d,%3d)  rank %6d of %d   dev %+9.1f DL"
          % (y, x, rank2[y, x], ad.size, dev[y, x]))