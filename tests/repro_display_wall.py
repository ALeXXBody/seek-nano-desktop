"""Reproduce EXACTLY what the viewer puts on screen, on this wall, and decompose it.

The live app shows static on a wall. The log says the raw frame's 2-98 span is
~776 DL, which on its own should look fine against a ~5 DL noise floor. So one
of those two numbers is not describing what is displayed.

This captures frames on whatever the camera is pointed at, runs the app's real
pipeline (background -> median -> NUC -> EMA stretch -> ironbow -> PNG), saves
the resulting image, and separately reports:

  fixed pattern  - per-pixel temporal MEAN. What a wall reference removes.
  photon noise   - per-pixel temporal STD.  What no reference removes.
  the true span  - 2-98 percentiles of the corrected frame.

The saved PNG is the point: it is the same arithmetic the viewer does, so if the
PNG is static then the pipeline is producing static, and the numbers say which
stage is responsible.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"C:\a\src\app")
os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

import viewer as V  # the real module, not a copy

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
TOTAL = 177840
BG_FRAMES = V.BG_FRAMES
PATCH = 24

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
t_end = time.time() + 50
while time.time() < t_end and len(frames) < 300:
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

from collections import Counter
print("gains seen: %s" % dict(Counter(gains)))
common = max(set(gains), key=gains.count)
fr = [frames[i] for i, g in enumerate(gains) if g == common]
print("%d frames at gain %d\n" % (len(fr), common))


def nse(a):
    return float(np.abs(np.diff(a, axis=1)).mean())


# --- the app's own stages, in its own order -------------------------------
bank = np.stack(fr[:BG_FRAMES])
bg_hp = bank.mean(axis=0)
bg_hp = bg_hp - V._boxblur(bg_hp, 9)

def med3(a):
    """The 3x3 median the viewer applies, verbatim from _process."""
    p1 = np.pad(a, 1, mode="edge")
    st = np.stack([p1[dy:dy + IH, dx:dx + IW]
                   for dy in range(3) for dx in range(3)])
    return np.partition(st, 4, axis=0)[4]


corrected = []
for f in fr[BG_FRAMES:BG_FRAMES + 40]:
    r = f - bg_hp
    r = med3(r)
    r = V._nuc2d(r)
    r = V._boxblur(r, V.DISPLAY_SMOOTH)
    corrected.append(r)
corrected = np.stack(corrected)

print("=== decomposition of the CORRECTED frame (what the stretch sees) ===")
p_lo = float(np.percentile(corrected, 2))
p_hi = float(np.percentile(corrected, 98))
span = p_hi - p_lo
print("  2-98 span            %8.1f DL" % span)

fixed = corrected.mean(axis=0)            # per-pixel temporal mean
temporal = corrected.std(axis=0)          # per-pixel temporal std
print("  fixed pattern (mean) %8.1f DL  <- a wall reference removes this"
      % float(fixed.std()))
print("  photon noise  (std)  %8.1f DL  <- NO reference removes this"
      % float(temporal.mean()))
print()
print("  so noise as %% of the span the stretch uses: %.2f%%"
      % (100.0 * float(temporal.mean()) / max(span, 1e-6)))
print()

# Spatial frequency of the corrected frame: is the mottle fine or coarse?
# A wall's texture is coarse; noise is per-pixel.
sd = np.abs(np.diff(corrected.mean(axis=0), axis=1)).mean()
print("  neighbour-difference of the time-averaged frame: %.1f DL" % sd)
print("  if this is close to the fixed-pattern figure, the mottle is a")
print("  STATIC pattern in space (visible even in a time average).")
print()

# --- render exactly as the viewer does -------------------------------------
mean_frame = corrected.mean(axis=0)
lo = p_lo
hi = p_hi
if hi - lo < V.MIN_WINDOW:
    lo -= V.MIN_WINDOW / 2.0
    hi += V.MIN_WINDOW / 2.0
norm = np.clip((mean_frame - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
rgb = np.zeros((IH, IW, 3), np.float32)
for ch in range(3):
    rgb[:, :, ch] = np.array([V.ironbow(t)[ch] for t in
                              np.linspace(0, 1, 256)], np.float32)[
        (norm[:, :] * 255).astype(int)]
img = (rgb * 255).astype(np.uint8)
try:
    from PIL import Image
    Image.fromarray(img).resize((IW * 3, IH * 3), Image.NEAREST).save(
        r"C:\a\_offline_wall.png")
    print("saved C:\\a\\_offline_wall.png  (the time-averaged frame, same "
          "stretch + colormap)")
except Exception as e:
    print("save failed:", e)

# And a single frame, not averaged, for comparison
one = corrected[0]
norm1 = np.clip((one - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
rgb1 = np.zeros((IH, IW, 3), np.float32)
for ch in range(3):
    rgb1[:, :, ch] = np.array([V.ironbow(t)[ch] for t in
                               np.linspace(0, 1, 256)], np.float32)[
        (norm1[:, :] * 255).astype(int)]
try:
    from PIL import Image
    Image.fromarray((rgb1 * 255).astype(np.uint8)).resize(
        (IW * 3, IH * 3), Image.NEAREST).save(r"C:\a\_offline_wall_1f.png")
    print("saved C:\\a\\_offline_wall_1f.png  (one frame, same stretch)")
except Exception:
    pass