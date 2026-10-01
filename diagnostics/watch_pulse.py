"""Watch the live display output for a minute and look for SLOW pulsing.

Every previous measurement sampled only the 12-frame burst ring, which spans
about 1.2 seconds. The contrast window is an EMA with alpha 0.03, a ~2.4 s time
constant, so a slow "breathing" of the whole picture has a period longer than
the window I was looking through. Measuring 1.2 s and declaring the image
stable cannot rule that out.

This polls the files the app already writes, so it needs no access to the USB
device and does not disturb the running app:
  - burst_00..11.png  is a ring, overwritten in sequence; polling it gives a
    time series across a long window rather than a single 1.2 s snapshot
  - latest.png        is rewritten ~1 Hz, an independent slow sample

Reports the mean brightness series, any dominant period, and whether the swing
is large enough to see.
"""
import glob
import os
import time

import numpy as np
from PIL import Image

C = r"C:\a"
DUR = 70.0
W = np.array([0.299, 0.587, 0.114], np.float32)


def body_mean(path):
    try:
        a = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    except Exception:
        return None
    if a.shape[0] < 60:
        return None
    # skip the overlay strip at the top
    lum = (a * W).sum(axis=2)
    b = lum[46:, :]
    return float(b.mean()), float(b.std())


burst = {}
for f in glob.glob(os.path.join(C, "burst_*.png")):
    burst[os.path.basename(f)] = 0.0
print("polling %d burst slots + latest.png for %.0f s" % (len(burst), DUR))

series = []          # (t, mean, std, tag)
slow = []
t0 = time.perf_counter()
next_slow = 0.0
while time.perf_counter() - t0 < DUR:
    now = time.perf_counter() - t0
    for name in list(burst):
        p = os.path.join(C, name)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt <= burst[name]:
            continue                      # not rewritten since we last read it
        r = body_mean(p)
        burst[name] = mt
        if r is None:
            continue
        series.append((now, r[0], r[1], name[-6:-4]))
    if now >= next_slow:
        r = body_mean(os.path.join(C, "latest.png"))
        if r:
            slow.append((now, r[0], r[1]))
        next_slow = now + 0.9
    time.sleep(0.04)

series.sort()
slow.sort()
print("\n%d burst samples over %.0f s (%.1f Hz)"
      % (len(series), DUR, len(series) / DUR))
if len(series) < 30:
    raise SystemExit("not enough samples")

t = np.array([s[0] for s in series])
m = np.array([s[1] for s in series])
s = np.array([s[2] for s in series])
print(f"body mean: {m.mean():.2f}  min {m.min():.2f}  max {m.max():.2f}  "
      f"p-p {m.max() - m.min():.2f}/255 ({100 * (m.max() - m.min()) / 255:.1f}%)")
print(f"body std : {s.mean():.2f}  min {s.min():.2f}  max {s.max():.2f}  "
      f"p-p {s.max() - s.min():.2f}")

print("\nseries (every 8th sample):")
for i in range(0, len(m), 8):
    bar = "#" * int(max(0, (m[i] - m.min()) / max(1e-6, m.max() - m.min()) * 50))
    print("  %5.1fs  %7.2f  %s" % (t[i], m[i], bar))

# look for a dominant period
d = m - m.mean()
f = np.fft.rfftfreq(len(d), d=float(np.median(np.diff(t))) or 1.0)
sp = np.abs(np.fft.rfft(d))
top = [i for i in np.argsort(sp)[1:10][::-1] if f[i] > 0][:5]
if top:
    print("\ndominant periods: "
          + ", ".join("%.2f s" % (1.0 / f[i]) for i in top))
    print("(a period near 2-3 s would be the contrast window breathing)")

# Update cadence: how often the screen actually gets new content, and whether
# that is steady. A steady 14 Hz looks fine; 14 Hz delivered in irregular
# 2-then-0-then-1 bursts looks like a stutter, and the eye reads that as
# flicker even when the picture itself is unchanged.
gaps = np.diff(t)
print(f"\nupdate cadence: median gap {np.median(gaps) * 1000:.0f} ms  "
      f"mean {gaps.mean() * 1000:.0f} ms  min {gaps.min() * 1000:.0f}  "
      f"max {gaps.max() * 1000:.0f}")
print(f"  gap std {gaps.std() * 1000:.0f} ms "
      f"({'IRREGULAR - the screen updates in bursts' if gaps.std() > 0.4 * gaps.mean() else 'fairly steady'})")
h = np.histogram(gaps * 1000, bins=[0, 40, 80, 120, 200, 400, 800, 5000])
for lo_, c in zip(h[1][:-1], h[0]):
    if c:
        print(f"    {lo_:6.0f} ms+ : {c:4d}  {'#' * min(60, c)}")

if len(slow) > 20:
    sm = np.array([x[1] for x in slow])
    print("\nlatest.png slow series: %d samples, mean %.2f, p-p %.2f/255"
          % (len(sm), sm.mean(), sm.max() - sm.min()))

pp = m.max() - m.min()
print("\np-p %.2f/255 = %.1f%% of full scale - %s"
      % (pp, 100 * pp / 255,
         "VISIBLE pulse" if pp > 5 else "below the visible threshold"))