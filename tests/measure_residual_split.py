"""Is the residual on a wall removable WITHOUT a reference, or is it photon noise?

The user is right that the wall-free picture is not good enough: on the flattest
patches - what a wall looks like - the noise fills 9-10% of the colour ramp,
which reads as static. My earlier "1.05%" was the whole frame with a hand and a
laptop in it, a 782 DL span; a wall is ~50 DL, so the same noise fills ten times
more of the ramp.

So: decompose the residual. The two parts need opposite answers.

  FIXED PATTERN  - the same offset every frame. A wall reference removes it,
                    and so does a better background estimate. If this dominates,
                    the wall-free path can be fixed properly.
  TEMPORAL NOISE  - different every frame, photon noise at the current gain.
                    No reference can remove it. Only more gain or more
                    integration can.

The test is per-pixel standard deviation ACROSS frames: a fixed pattern shows up
as a large spatial variance of that per-pixel figure (some pixels are
consistently noisy, others are not), while pure photon noise gives a flat
per-pixel distribution.

Also compares the two available estimators at several frame counts, because if
8 frames is simply too few to see past the photon noise, averaging longer fixes
it with no wall at all.
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
t_end = time.time() + 60
while time.time() < t_end and len(frames) < 420:
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
if len(fr) < 60:
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


def pipeline(test, bank):
    """Exactly the app's order: subtract high-passed raw background, median, NUC."""
    hp = bank.mean(axis=0)
    hp = hp - boxblur(hp, 9)
    out = []
    for f in test:
        r = med3(f - hp)
        out.append(nuc2d(r))
    return np.stack(out)


# Find the flattest patches once, on a long run, and reuse them for every variant
# so the comparison is like-for-like.
long_run = pipeline(fr[8:8 + 60], np.stack(fr[:8]))
mean_img = long_run.mean(axis=0)
cands = []
for y in range(0, IH - PATCH, 6):
    for x in range(0, IW - PATCH, 6):
        p = mean_img[y:y + PATCH, x:x + PATCH]
        cands.append((float(np.percentile(p, 98) - np.percentile(p, 2)), y, x))
cands.sort()
flat = [(y, x) for _, y, x in cands[:4]]
ref_span = float(np.percentile(mean_img[36:36 + PATCH, 36:36 + PATCH], 98)
                 - np.percentile(mean_img[36:36 + PATCH, 36:36 + PATCH], 2))
print("flat patches (y,x): %s   typical span %.1f DL\n"
      % (flat, ref_span))


def wall_score(out, y, x):
    """Noise as a fraction of the patch's OWN span: what the eye judges."""
    st = np.stack([c[y:y + PATCH, x:x + PATCH] for c in out])
    n = float(np.mean([nse(s) for s in st]))
    span = float(np.percentile(st.mean(axis=0), 98)
                 - np.percentile(st.mean(axis=0), 2))
    return n, span, 100.0 * n / max(span, 1e-6)


print("=== how many frames does the background need, on a WALL scene? ===")
print("  bank   noise DL   span DL   noise as % of span")
print("  " + "-" * 48)
test = fr[8:8 + 30]
y, x = flat[0]
best = None
for n in (8, 16, 32, 64, 128, 200):
    if n + 30 > len(fr):
        break
    out = pipeline(test, np.stack(fr[:n]))
    nn, span, pct = wall_score(out, y, x)
    if best is None or pct < best[1]:
        best = (n, pct)
    print("  %4d   %7.2f  %7.1f   %8.2f%%" % (n, nn, span, pct))
print("\n  best so far: %d frames at %.2f%%" % best)

# Now the decomposition, on the best available estimate.
nb = max((n for n in (8, 16, 32, 64, 128, 200) if n + 60 <= len(fr)), default=8)
out = pipeline(fr[nb:nb + 60], np.stack(fr[:nb]))
per_pixel = out.std(axis=0)          # temporal spread, per pixel
print("\n=== is the residual fixed pattern or photon noise? ===")
print("  (bank of %d frames, %d test frames)" % (nb, len(out)))
y, x = flat[0]
patch = per_pixel[y:y + PATCH, x:x + PATCH]
print("  per-pixel temporal spread in a flat patch:")
print("    mean %.2f DL   p10 %.2f   p90 %.2f   ratio p90/p10 %.2f"
      % (patch.mean(), np.percentile(patch, 10), np.percentile(patch, 90),
         np.percentile(patch, 90) / max(np.percentile(patch, 10), 1e-6)))
print()
print("  A FIXED PATTERN gives a few pixels with a large, stable offset.")
print("  Pure PHOTON NOISE gives a flat per-pixel distribution - every pixel")
print("  wobbles by about the same amount, so the ratio is near 1.")
print()
sp = np.sort(patch.ravel())
print("  top 0.5%% of pixels in the patch carry %.2f DL vs the median %.2f DL"
      % (sp[int(sp.size * 0.995)], np.median(sp)))
print("  pixels above 3x the median: %d of %d"
      % (int((patch > 3 * np.median(sp)).sum()), patch.size))
print()
n, span, pct = wall_score(out, y, x)
print("  wall-patch score at %d frames: %.2f%% of span" % (nb, pct))
print()
print("If a longer bank does not help, the residual is photon noise and NO")
print("reference - wall or otherwise - will remove it. That is the honest")
print("answer to the user's report: they would be right that a wall is")
print("needed, and right that the phone app must be doing something else.")