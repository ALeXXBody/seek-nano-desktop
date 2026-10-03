"""What high-pass scale actually removes the blotches?

Established on this wall:
  raw fixed pattern          105.9 DL
  high-pass at 9 px (ships)   83.7 DL   <- barely helps
  full background subtraction 48.1 DL  <- works, but risks ghosting

The 9 px cutoff is the problem. The blotches are roughly 10-30 px across, so a
9 px box high-pass cannot remove structure at that scale - it passes straight
through. The old code comment even noted the pattern "extends past 25 px".

So sweep the cutoff. A high-pass at scale S removes everything finer than S and
keeps everything coarser. If the blotches live at 10-30 px, a cutoff well above
that removes them, and what survives is the scene's own large-scale structure -
the wall's thermal gradient - which is exactly what should survive.

This needs no wall reference and no user action, which is the whole point.
Measured both ways: blotches left, and scene kept.
"""
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, r"C:\a\src\app")
os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

import viewer as V

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
t_end = time.time() + 55
while time.time() < t_end and len(frames) < 340:
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
fr = [frames[i] for i, g in enumerate(gains) if g == common]
N = 40
bank = np.stack(fr[:N])
test = fr[N:N + 30]
print("%d frames at gain %d, bank of %d\n" % (len(fr), common, N))


def med3(a):
    p1 = np.pad(a, 1, mode="edge")
    st = np.stack([p1[dy:dy + IH, dx:dx + IW]
                   for dy in range(3) for dx in range(3)])
    return np.partition(st, 4, axis=0)[4]


def finish(r):
    return V._boxblur(V._nuc2d(med3(r)), V.DISPLAY_SMOOTH)


def score(out):
    return (float(out.mean(axis=0).std()),
            float(np.percentile(out, 98) - np.percentile(out, 2)))


b_mean = bank.mean(axis=0)
raw_fx, raw_sp = score(np.stack([finish(f) for f in test]))
print("reference points on this wall:")
print("  no correction            blotch %6.1f DL   span %6.1f DL"
      % (raw_fx, raw_sp))
full = np.stack([finish(f - b_mean) for f in test])
ffx, fsp = score(full)
print("  full subtraction         blotch %6.1f DL   span %6.1f DL  (ghost risk)"
      % (ffx, fsp))
print()
print("  high-pass cutoff sweep:")
print("  cutoff   blotch left   span left   span kept vs raw")
print("  " + "-" * 58)
best = None
imgs = {}
for S in (9, 15, 21, 27, 35, 45, 61):
    hp = b_mean - V._boxblur(b_mean, S)
    out = np.stack([finish(f - hp) for f in test])
    fx, sp = score(out)
    imgs[S] = out
    kept = 100.0 * sp / max(raw_sp, 1e-6)
    print("  %5d px %11.1f DL %10.1f DL %13.0f%%" % (S, fx, sp, kept))
    if fx <= ffx and kept >= 60.0 and (best is None or fx < best[1]):
        best = (S, fx, kept)

print()
if best:
    print("best cutoff that beats full subtraction while keeping >=60%% of the")
    print("scene: %d px  (blotch %.1f DL vs full's %.1f DL, %.0f%% of span kept)"
          % (best[0], best[1], ffx, best[2]))
else:
    print("no cutoff beat full subtraction while keeping the scene")
    print("(blotch <= %.1f DL and >= 60%% span)" % ffx)

# Render the best, plus 9 px and full, so the three can be looked at.
lut = np.array([V.ironbow(t) for t in np.linspace(0, 1, 256)], np.float32)


def save(out, name):
    m = out.mean(axis=0)
    lo = float(np.percentile(m, 2))
    hi = float(np.percentile(m, 98))
    if hi - lo < 1.0:
        lo, hi = float(m.min()), float(m.max())
    n = np.clip((m - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    from PIL import Image
    Image.fromarray((lut[(n * 255).astype(int)] * 255).astype(np.uint8)).resize(
        (IW * 3, IH * 3), Image.NEAREST).save(name)
    print("saved %s" % name)


S = best[0] if best else 35
save(imgs[S], r"C:\a\_offline_cut_best.png")
save(full, r"C:\a\_offline_cut_full.png")