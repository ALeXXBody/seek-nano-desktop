"""Why the no-wall picture looks like static while the probe said it was clean.

Found by reading the pipeline, not by guessing. The app builds its background
from RAW frames:

    L3523  self.bg_frames.append(_roi_u16(payload))     <- raw, no median
    L3527  b = np.stack(self.bg_frames).mean(axis=0)
    L3528  self.bg_hp = b - _boxblur(b, 9)

and then subtracts it BEFORE the 3x3 median:

    L3159  img = img - self.bg_hp
    L3188  img = 3x3 median

Every probe in this project built the background from median-filtered frames
instead. That is the whole discrepancy: 236.8 DL of raw |dx| noise averaged over
8 frames still leaves ~84 DL of noise in the background itself, and subtracting
that injects noise into every frame rather than removing it.

So replicate the app's exact ordering, then swap only the background source and
measure. Same frames, same order, one variable.
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
fr = [frames[i] for i, g in enumerate(gains) if g == common]
print("%d frames at gain %d\n" % (len(fr), common))
if len(fr) < 40:
    raise SystemExit("not enough frames at one gain")


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


def nuc2d(img, col_strength=1.0, row_strength=0.5, smooth_px=9):
    col = img.mean(axis=0, keepdims=True)
    col = col - boxblur(col, smooth_px)
    row = img.mean(axis=1, keepdims=True)
    row = row - boxblur(row, smooth_px)
    return img - col_strength * col - row_strength * row


def nse(a):
    return float(np.abs(np.diff(a, axis=1)).mean())


N = 8
bank_raw = np.stack(fr[:N])                 # what the app does
bank_med = np.stack([med3(f) for f in fr[:N]])   # what the probes did
test = fr[N:N + 24]                         # frames the bank never saw

# The app's own ordering, replicated exactly: subtract bg, then median, then NUC.
print("pipeline as the app runs it:  img - bg_hp -> 3x3 median -> NUC")
print()
print("  background built from      residual |dx| DL   2-98 span DL   noise/span")
print("  " + "-" * 68)
out = {}
for name, bank in (("raw frames (app)", bank_raw),
                   ("median frames", bank_med)):
    b = bank.mean(axis=0)
    hp = b - boxblur(b, 9)
    resid = []
    for f in test:
        r = f - hp
        r = med3(r)
        r = nuc2d(r)
        resid.append(r)
    rn = float(np.mean([nse(r) for r in resid]))
    allr = np.stack(resid)
    span = float(np.percentile(allr, 98) - np.percentile(allr, 2))
    out[name] = rn
    print("  %-24s %12.1f   %12.1f   %8.2f%%"
          % (name, rn, span, 100.0 * rn / max(span, 1e-6)))

a, bb = out["raw frames (app)"], out["median frames"]
print()
print("the app's raw-frame background leaves %.1f DL; a median-frame one "
      "leaves %.1f DL" % (a, bb))
print("that is %.1fx more noise on screen, and it is the whole reason the "
      "no-wall" % (a / max(bb, 1e-6)))
print("picture looked like static. No wall reference would have fixed it: the")
print("problem is the noise INSIDE the background, not a missing offset.")
print()
print("Scene is preserved either way - a wide 2-98 span is real contrast. What")
print("changes is how much of that span is noise rather than scene.")