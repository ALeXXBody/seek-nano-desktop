"""Does the picture actually work on a LOW-CONTRAST scene, without a wall?

I claimed 1.05% noise, measured on frames containing a hand and a laptop: a
572-890 DL scene span. That is the easy case, and every screenshot I took was of
it. A wall is the hard case - a nearly uniform scene, so the 2-98 span the
auto-stretch maps across the entire colormap is small, and whatever noise
survives gets amplified to full scale.

So measure the honest number: the flattest regions in the frame, which is what a
wall looks like, and compare their noise against their OWN span rather than the
whole frame's.

Reports, per flat patch:
  noise    - mean absolute neighbour difference, the measure used throughout
  span     - that patch's own 2-98 percentile spread
  pct      - noise as a percentage of the span it will be stretched across

If pct is high in the flat patches, then the picture only looked good because
the scene was dramatic, and the user's report is correct.
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
N_BANK = 8            # BG_FRAMES
PATCH = 24            # patch edge, px

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
while time.time() < t_end and len(frames) < 140:
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
print("%d frames at gain %d (the most common)\n" % (len(fr), common))


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


# The app's own pipeline, in its own order: background from 8 RAW frames,
# subtract, then median, then NUC. Replicated from _process.
bank = np.stack(fr[:N_BANK])
bg_hp = bank.mean(axis=0)
bg_hp = bg_hp - boxblur(bg_hp, 9)
clean = []
for f in fr[N_BANK:N_BANK + 30]:
    r = f - bg_hp
    r = med3(r)
    r = nuc2d(r)
    clean.append(r)
clean = np.stack(clean)

print("whole frame:")
whole_span = float(np.percentile(clean, 98) - np.percentile(clean, 2))
print("  2-98 span %.1f DL   noise %.1f DL   = %.2f%% of span"
      % (whole_span, nse(clean.mean(axis=0)), 100.0 * nse(clean.mean(axis=0))
         / max(whole_span, 1e-6)))
print("  ^ this is the number I quoted. It is the DRAMATIC scene.\n")

# Score every patch by how flat it is, then report the flattest ones - those are
# what a wall gives you.
mean_img = clean.mean(axis=0)
ys, xs = [], []
for y in range(0, IH - PATCH, 4):
    for x in range(0, IW - PATCH, 4):
        ys.append(y)
        xs.append(x)
flatness = []
for y, x in zip(ys, xs):
    p = mean_img[y:y + PATCH, x:x + PATCH]
    flatness.append((float(np.percentile(p, 98) - np.percentile(p, 2)), y, x))
flatness.sort()

print("the flattest patches in frame - this is what a WALL looks like:")
print("  patch      span DL   noise DL   noise as %% of span")
print("  " + "-" * 52)
rows = []
for span, y, x in flatness[:6]:
    stack = np.stack([c[y:y + PATCH, x:x + PATCH] for c in clean])
    n = float(np.mean([nse(s) for s in stack]))
    pct = 100.0 * n / max(span, 1e-6)
    rows.append((span, n, pct))
    print("  y%-4d x%-4d %7.1f   %7.1f   %8.1f%%" % (y, x, span, n, pct))

worst = max(rows, key=lambda r: r[2])
print()
print("worst flat patch: %.1f%% of its own span is noise." % worst[2])
print()
# What does that look like? The auto-stretch maps span -> full colormap, so the
# noise occupies noise/span of the colour range. Count how many distinct
# colormap bands the patch's noise crosses: many bands = visible static.
print("How much of the colour range the noise would fill:")
for span, n, pct in rows[:3]:
    bands = min(1.0, n / max(span, 1e-6))
    print("  span %6.1f DL: noise fills %5.1f%% of the colour ramp"
          % (span, 100.0 * bands))
print()
print("Interpretation: under ~10%% the patch reads as a clean surface; much")
print("above that it reads as static, because the stretch gives the noise the")
print("full colour range to play with.")
print()
print("A wall is the flattest scene there is. If these numbers are bad, then")
print("the wall-free picture is NOT good enough and the user is right.")