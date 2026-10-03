"""What does holding gain 3 actually cost, versus letting the camera pick?

The app's HUD says "HOLDING GAIN 3 (CAMERA WANTS 20)". Gain 3 is the bottom of
the range, so it amplifies the least signal per frame - the worst case for
photon noise. The camera is asking for 20, which is 6.7x more amplification.

And the earlier decomposition says the wall-scene residual is photon noise, not
fixed pattern: averaging the background from 8 frames or from 200 changed
nothing (10.13% -> 9.80%), and no pixel exceeded 3x the median. No reference can
remove photon noise. Only the gain can.

So measure, per gain, on flat patches:
  temporal noise - the irreducible part, per-pixel std across frames
  what the display would show - noise as a fraction of the patch's own span

The background is built per gain from frames at that gain, exactly as the app
does, so this is the app's own arithmetic.
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
TOTAL = 177840
PATCH = 24
BG_FRAMES = 8

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

by_gain = defaultdict(list)
t_end = time.time() + 90
while time.time() < t_end and sum(len(v) for v in by_gain.values()) < 700:
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
    by_gain[int(h[2])].append(a.astype(np.float32))
usb.util.dispose_resources(d)


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


def run(frames):
    """The app's pipeline at one gain."""
    hp = np.stack(frames[:BG_FRAMES]).mean(axis=0)
    hp = hp - boxblur(hp, 9)
    out = np.stack([nuc2d(med3(f - hp)) for f in frames[BG_FRAMES:]])
    return out


print("frames per gain: %s\n"
      % {g: len(v) for g, v in sorted(by_gain.items())})

usable = {g: v for g, v in by_gain.items() if len(v) >= BG_FRAMES + 40}
if len(usable) < 2:
    print("only %d gain(s) gave enough frames to measure. The AGC settles"
          % len(usable))
    print("quickly when the camera is still; run the app so it holds a gain,")
    print("or point at a scene with more temperature range.")
    raise SystemExit(1)

print("gain  frames  flat-patch noise DL   patch span DL   noise as % of span")
print("-" * 68)
res = []
for g in sorted(usable):
    fr = usable[g]
    out = run(fr)
    mean_img = out.mean(axis=0)
    # flattest patches, which is what a wall gives
    cands = []
    for y in range(0, IH - PATCH, 6):
        for x in range(0, IW - PATCH, 6):
            p = mean_img[y:y + PATCH, x:x + PATCH]
            cands.append((float(np.percentile(p, 98) - np.percentile(p, 2)),
                          y, x))
    cands.sort()
    spans, pcts, noises = [], [], []
    for _, y, x in cands[:6]:
        st = np.stack([c[y:y + PATCH, x:x + PATCH] for c in out])
        n = float(np.mean([nse(s) for s in st]))
        span = float(np.percentile(st.mean(axis=0), 98)
                     - np.percentile(st.mean(axis=0), 2))
        spans.append(span)
        noises.append(n)
        pcts.append(100.0 * n / max(span, 1e-6))
    med_span = float(np.median(spans))
    med_noise = float(np.median(noises))
    med_pct = float(np.median(pcts))
    res.append((g, med_noise, med_span, med_pct))
    print("%4d  %6d  %17.2f  %14.1f  %18.2f%%"
          % (g, len(fr), med_noise, med_span, med_pct))

print()
if len(res) >= 2:
    lo = min(res, key=lambda r: r[3])
    hi = max(res, key=lambda r: r[3])
    print("cleanest gain %d at %.2f%% of span; worst gain %d at %.2f%%"
          % (lo[0], lo[3], hi[0], hi[3]))
    print("noise is %.2fx lower at gain %d than at gain %d"
          % (hi[1] / max(lo[1], 1e-6), lo[0], hi[0]))
    print()
    print("The app holds the LOWEST gain it sees. If the camera wants a higher")
    print("one, that is the camera asking for less noise, and holding gain 3")
    print("is holding the worst setting available.")
else:
    print("only one gain measurable this run")