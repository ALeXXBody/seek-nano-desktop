"""On a wall, is full background subtraction safe and does it work?

The corrected frame still carries 64.7 DL of fixed pattern - blotches at roughly
10-30 px - and 40 frames of averaging do not remove them, so they are spatial,
not temporal. A 9 px box high-pass cannot remove structure at that scale by
construction, which is why they survive.

A wall reference subtracts the FULL background and would remove them. The
objection has always been that full subtraction also removes the scene - the
old docstring in _process records "img - bg (was shipping) 1.9 DL noise, 42 DL
span <- scene gone". But that measurement was made on a scene with a hand and a
laptop in it. On a wall there is no scene to lose: the frame IS background.

So compare, on this wall, three estimators:
  high-pass  - what ships today; keeps the scene, leaves the blotches
  full       - subtract the whole background; removes the pattern, and on a
               uniform scene there is nothing of the scene to lose
  none       - no correction, as the reference point

And measure both halves that matter: the fixed pattern that REMAINS, and how
much scene SURVIVES. A fix that improves the first by destroying the second is
not a fix, and that is exactly the trap the old note warned about.
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
    """fixed pattern left, and scene span left."""
    f = float(out.mean(axis=0).std())
    s = float(np.percentile(out, 98) - np.percentile(out, 2))
    return f, s


b_mean = bank.mean(axis=0)
hp = b_mean - V._boxblur(b_mean, 9)

variants = {
    "no correction": lambda f: f,
    "high-pass (ships today)": lambda f: f - hp,
    "full subtraction": lambda f: f - b_mean,
}

print("%-26s %12s %12s %10s" % ("estimator", "fixed left", "span left",
                                "span kept"))
print("-" * 64)
base = None
for name, fn in variants.items():
    out = np.stack([finish(fn(f)) for f in test])
    fx, sp = score(out)
    if base is None:
        base = sp
    print("%-26s %9.1f DL %9.1f DL %9.0f%%"
          % (name, fx, sp, 100.0 * sp / max(base, 1e-6)))

print()
print("Read it as: 'fixed left' is the blotch amplitude that survives - lower")
print("is better. 'span kept' is the scene contrast that survives - if it")
print("collapses to near zero the estimator has thrown the picture away,")
print("which is what full subtraction did on the hand-and-laptop scene.")
print()

# Render the full-subtraction result so it can be looked at, not just counted.
out = np.stack([finish(f - b_mean) for f in test])
mean_frame = out.mean(axis=0)
lo = float(np.percentile(mean_frame, 2))
hi = float(np.percentile(mean_frame, 98))
if hi - lo < 1.0:
    lo, hi = mean_frame.min(), mean_frame.max()
norm = np.clip((mean_frame - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
lut = np.array([V.ironbow(t) for t in np.linspace(0, 1, 256)], np.float32)
rgb = lut[(norm * 255).astype(int)]
try:
    from PIL import Image
    Image.fromarray((rgb * 255).astype(np.uint8)).resize(
        (IW * 3, IH * 3), Image.NEAREST).save(r"C:\a\_offline_full.png")
    print("saved C:\\a\\_offline_full.png  (full subtraction, this wall)")
except Exception as e:
    print("save failed:", e)

# And the high-pass for side-by-side.
out2 = np.stack([finish(f - hp) for f in test])
m2 = out2.mean(axis=0)
lo2 = float(np.percentile(m2, 2))
hi2 = float(np.percentile(m2, 98))
n2 = np.clip((m2 - lo2) / max(hi2 - lo2, 1e-6), 0.0, 1.0)
Image.fromarray((lut[(n2 * 255).astype(int)] * 255).astype(np.uint8)).resize(
    (IW * 3, IH * 3), Image.NEAREST).save(r"C:\a\_offline_hp.png")
print("saved C:\\a\\_offline_hp.png     (high-pass, this wall)")