"""How many frames does the shutterless background actually need?

The app builds bg_hp from BG_FRAMES = 8 frames and then freezes it (`and
self.bg_hp is None`). The previous probe subtracted a background built from
245 frames and got 10.3 DL of residual noise against a 475 DL scene span -
2%, which would look clean. If 8 frames leaves far more noise than that, the
missing wall capture is a red herring: the background is simply built from too
little data, and no amount of prompting the user for a wall fixes that.

Sweeps the frame count and reports the residual, so the number BG_FRAMES needs
is measured rather than guessed.
"""
import os
import time

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
TOTAL = 177840

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

frames, gains = [], []
t_end = time.time() + 45
while time.time() < t_end and len(frames) < 320:
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
    frames.append(a.astype(np.float32))
    gains.append(int(h[2]))
usb.util.dispose_resources(d)

common = max(set(gains), key=gains.count)
keep = [i for i, g in enumerate(gains) if g == common]
fr = [frames[i] for i in keep]
print("%d frames captured, %d at gain %d (the most common)"
      % (len(frames), len(fr), common))
if len(fr) < 40:
    raise SystemExit("not enough frames at one gain")
print()


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


meds = np.stack([med3(f) for f in fr])

# Residuals are scored on frames AFTER the ones the background was built from,
# so the background is never scored against the exact data that produced it.
print("  n   build(stat)      residual |dx| DL   noise as % of scene span")
print("  " + "-" * 60)
scene = float(np.percentile(meds, 98) - np.percentile(meds, 2))
print("  scene 2-98 span = %.1f DL\n" % scene)
rows = []
for n in (4, 8, 12, 16, 24, 32, 48, 64, 96, 128):
    if n + 20 > len(meds):
        break
    for stat in ("mean", "median"):
        ref = (meds[:n].mean(axis=0) if stat == "mean"
               else np.median(meds[:n], axis=0))
        hp = ref - boxblur(ref, 9)
        test = meds[n:n + 20]
        resid = test - hp[None, ...]
        r = float(np.mean([nse(x) for x in resid]))
        pct = 100.0 * r / max(scene, 1e-6)
        rows.append((n, stat, r, pct))
        print("  %3d  %-8s      %10.1f        %6.2f%%" % (n, stat, r, pct))

print()
best8 = [r for r in rows if r[0] == 8]
if best8:
    n8 = min(best8, key=lambda r: r[2])[2]
    n64 = min([r for r in rows if r[0] >= 64], key=lambda r: r[2],
              default=None)
    if n64:
        print("at 8 frames the residual is %.1f DL; at %d frames it is %.1f DL "
              "- %.1fx better"
              % (n8, n64[0], n64[2], n8 / max(n64[2], 1e-6)))
print()
print("BG_FRAMES is 8 today. The number above is what it should be.")