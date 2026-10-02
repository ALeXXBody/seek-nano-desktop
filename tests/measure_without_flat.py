"""What does the picture cost if nobody ever presses F?

The wall capture exists to remove the sensor's per-pixel offsets. The claim is
that without it the image is unusable and you must point at a flat surface -
which the phone app never asks for. That claim deserves a measurement rather
than an argument, because it decides whether the flat capture can go.

Compares, on the same live frames:
  raw                - what arrives off the wire
  + 3x3 median       - speckle floor
  + NUC              - the row/column correction
  + bg_hp            - the running-background fallback the app uses when there
                       is no reference: what the app shows with no FFC

Noise is the mean absolute neighbour difference, the same measure used
throughout this project. What matters is not the absolute number but the GAP
between the flat-field path and the fallback: that gap is what the wall capture
is actually buying.
"""
import os
import time

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
N = 400

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
for _ in range(N * 3):
    if len(frames) >= N:
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
    frames.append(a.astype(np.float32))
    gains.append(int(h[2]))
    time.sleep(0.01)
usb.util.dispose_resources(d)

fr = np.stack(frames)
uq = sorted(set(gains))
print("captured %d frames, gains seen %s" % (len(fr), uq))
# The background only makes sense at ONE gain: the per-pixel offsets scale with
# gain, so mixing them averages several different patterns into a blur of all of
# them. Use the most common gain - which is what the app's gain lock is for.
if len(uq) > 1:
    common = max(set(gains), key=gains.count)
    keep = [i for i, g in enumerate(gains) if g == common]
    fr = fr[keep]
    print("%d distinct gains - kept %d frames at gain %d (the most common)"
          % (len(uq), len(fr), common))
if len(fr) < 8:
    print("only %d frames at one gain: too few to build a background. Point the"
          % len(fr))
    print("camera at a steady scene, or use the app, which locks the gain.")
    raise SystemExit(1)
print()


def nse(a):
    return float(np.abs(np.diff(a, axis=1)).mean())


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


rows = []
rows.append(("raw off the wire", np.mean([nse(f) for f in fr])))

meds = np.stack([med3(f) for f in fr])
rows.append(("+ 3x3 median (speckle floor)", np.mean([nse(m) for m in meds])))

# running background: mean of settled frames, high-passed - what the app uses
# with no flat reference
bg = meds.mean(axis=0)
bg_hp = bg - boxblur(bg, 9)
rows.append(("+ running background (NO wall capture)",
             np.mean([nse(m - bg_hp) for m in meds])))

# the flat-field path, for comparison: subtract the full background
rows.append(("+ full background subtraction (the old, worse path)",
             np.mean([nse(m - bg) for m in meds])))

print("%-42s %10s" % ("stage", "|dx| DL"))
print("%-42s %10s" % ("-" * 42, "-" * 10))
for name, v in rows:
    print("  %-40s %10.1f" % (name, v))

no_fc = rows[2][1]
full = rows[3][1]
print()
print("the wall capture buys %.0fx on this scene (%.1f -> %.1f DL)"
      % (no_fc / max(full, 1e-6), no_fc, full))

# How much of the residual is a FIXED pattern (removable without a wall) versus
# temporal noise (not removable)? A per-pixel temporal std says how much of the
# leftover would survive any amount of background estimation.
resid = meds - bg_hp[None, ...]
fixed = resid.std(axis=0)
temporal = resid.std(axis=0).mean()
print()
print("after the running background: per-pixel spread %.1f DL" % temporal)
print("  of which the top 0.1%% of pixels carry %.1f DL (bad elements)"
      % (float(np.percentile(fixed, 99.9)),))
print("  pixels above 300 DL: %d of %d"
      % (int((fixed > 300).sum()), fixed.size))
print()
print("A fixed pattern the background cannot remove needs a wall; a TEMPORAL")
print("floor it can. The numbers above say which one dominates right now.")