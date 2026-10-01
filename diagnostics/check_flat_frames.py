"""Are there flat frames in the RAW stream, or is the flat frame made on screen?

The screen alternates between the scene and a completely uniform blue field.
The burst ring - written from the same rgb the screen is drawn from - never
contains one (12/12 normal, mean ~47, std ~50). So either the data contains
flat frames that the gate lets through, or the flat frame is manufactured
between rgb and the screen.

Check the data: stream raw frames, and for each report the raw span, the span
after subtracting the on-disk reference, and the std after it. A frame that
becomes constant after subtraction would render uniform.
"""
import os
import sys
import time
from collections import Counter

import numpy as np

DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

sys.path.insert(0, r"C:\a\src\app")

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
REF = r"C:\a\ffc_latest.raw"

ffc = None
if os.path.exists(REF):
    ffc = np.fromfile(REF, dtype="<u2").reshape(IH, IW).astype(np.float32)
    print("reference: mean %.0f  std %.1f" % (ffc.mean(), ffc.std()))

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
d.set_configuration()
usb.util.claim_interface(d, 0)
for _ in range(2):
    try:
        d.ctrl_transfer(0x40, 0x3C, 0, 0, b"\x00\x00", 1250)
    except Exception:
        pass
    time.sleep(0.15)
try:
    d.reset()
    time.sleep(1.2)
    d.set_configuration()
    usb.util.claim_interface(d, 0)
except Exception as e:
    print("reset:", e)
time.sleep(0.4)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00")
W(0x3C, b"\x00\x00")
Rd(0x3D, 2)
W(0x3E, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00")
W(0x3C, b"\x01\x00")
Rd(0x3D, 2)

rows = []
t0 = time.perf_counter()
N = 420
for _ in range(N):
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < 177840:
            b += bytes(d.read(0x81, min(6840, 177840 - len(b)), timeout=700))
    except Exception:
        pass
    if len(b) == 177840:
        h = np.frombuffer(b[:8], "<u2")
        if int(h[0]) == 0x0579 and 0 < int(h[2]) < 256:
            a = np.frombuffer(b, dtype="<u2").reshape(RH, RW)
            im = a[RY:RY + IH, RX:RX + IW].astype(np.float32)
            raw_span = float(np.percentile(im, 99) - np.percentile(im, 1))
            if ffc is not None:
                sub = im - ffc + float(ffc.mean())
                sub_span = float(np.percentile(sub, 99) - np.percentile(sub, 1))
                sub_std = float(sub.std())
                raw_std = float(im.std())
            else:
                sub_span = sub_std = raw_std = float("nan")
            rows.append((int(h[2]), raw_span, raw_std, sub_span, sub_std,
                         time.perf_counter() - t0))
    time.sleep(0.005)
usb.util.dispose_resources(d)

per = np.diff([r[5] for r in rows])
print("captured %d frames in %.1f s -> %.1f fps (median period %.0f ms)"
      % (len(rows), rows[-1][5] if rows else 0,
         len(rows) / max(1e-9, rows[-1][5]),
         np.median(per) * 1000 if len(per) else 0))

gains = Counter(r[0] for r in rows)
print("gains:", dict(sorted(gains.items())))

rs = np.array([r[1] for r in rows])
rstd = np.array([r[2] for r in rows])
ss = np.array([r[3] for r in rows])
sstd = np.array([r[4] for r in rows])
print()
print("RAW   span: min %.0f  p5 %.0f  median %.0f  max %.0f"
      % (rs.min(), np.percentile(rs, 5), np.median(rs), rs.max()))
print("RAW   std : min %.1f  median %.1f  max %.1f"
      % (rstd.min(), np.median(rstd), rstd.max()))
print("AFTER FFC span: min %.0f  p5 %.0f  median %.0f  max %.0f"
      % (ss.min(), np.percentile(ss, 5), np.median(ss), ss.max()))
print("AFTER FFC std : min %.1f  median %.1f  max %.1f"
      % (sstd.min(), np.median(sstd), sstd.max()))
print()
flat_raw = int((rs < 100).sum())
flat_sub = int((ss < 20).sum())
print("frames failing MIN_SCENE_SPAN on raw data     : %d" % flat_raw)
print("frames near-constant AFTER reference subtract : %d" % flat_sub)
print()
print("the flat blue frame on screen would need a near-constant AFTER-FFC span")
print("while still passing the raw-span gate. count of those:")
bad = [(r[0], r[1], r[3], r[4]) for r in rows
       if r[1] >= 100 and r[3] < 20]
print("  %d frames" % len(bad))
for b in bad[:10]:
    print("    gain %-3d raw span %8.0f  after-ffc span %7.1f  std %6.2f" % b)