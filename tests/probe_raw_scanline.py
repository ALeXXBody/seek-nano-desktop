"""Is the scanline artifact in the RAW transfer, or added by the pipeline?

Everything so far has been scraped off the screen, which is confounded: the
scene changes between samples, and the window moves. This bypasses all of it.

Pull raw frames straight from the camera over USB, with no viewer running, and
look for the same signature - one or two rows that alternate violently in
colour while their neighbours are smooth. If it is here, the cause is the
camera or the transfer, and no amount of image processing is involved. If it is
absent here, it is added somewhere between the wire and the screen.
"""
import os
import time

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
NFRAMES = 60

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
if d is None:
    raise SystemExit("camera not found - is SeekNano.exe still holding it?")
d.set_configuration()
usb.util.claim_interface(d, 0)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00")
W(0x3C, b"\x00\x00")
Rd(0x3D, 2)
W(0x3E, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00")
W(0x3C, b"\x01\x00")
Rd(0x3D, 2)

frames = []
for _ in range(NFRAMES * 3):
    if len(frames) >= NFRAMES:
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
usb.util.dispose_resources(d)

frames = np.stack(frames)
print("captured %d raw frames, ROI %dx%d" % (frames.shape[0], IW, IH))


def scanline_score(f):
    """Per-row violent within-row alternation, in DEVICE units (no colour)."""
    step = np.abs(np.diff(f, axis=1)).mean(axis=1)
    return step


z = 4.0
hit_rows = {}
for i, f in enumerate(frames):
    p = scanline_score(f)
    med = float(np.median(p))
    mad = float(np.median(np.abs(p - med)))
    scale = max(med + 3 * mad, 1.0)
    bad = [r for r in range(len(p)) if p[r] > z * scale]
    for r in bad:
        hit_rows[r] = hit_rows.get(r, 0) + 1

print()
print("rows flagged as a corrupted scanline (threshold %.1fx the frame's own"
      % z)
print("row spread):")
if not hit_rows:
    print("   none - the raw stream has NO scanline artifact")
else:
    for r, n in sorted(hit_rows.items(), key=lambda kv: -kv[1])[:12]:
        print("   row %3d  seen in %2d of %d frames" % (r, n, len(frames)))

print()
# Also look for a row that is wildly out of family with its neighbours across
# the whole bank - a row that decodes to a different kind of data entirely.
med_frame = np.median(frames, axis=0)
dev = np.abs(frames - med_frame[None, ...])
row_dev = dev.mean(axis=(0, 2))
rmed = float(np.median(row_dev))
rmad = float(np.median(np.abs(row_dev - rmed)))
print("per-row deviation from the temporal median:")
print("   median %.2f DL, rows beyond 6x: %s"
      % (rmed, [i for i in range(len(row_dev))
                if row_dev[i] > rmed + 6 * max(rmad, 1e-6)][:10] or "none"))
top = np.argsort(row_dev)[::-1][:5]
print("   noisiest rows: " + ", ".join("%d(%.1f DL)" % (i, row_dev[i])
                                      for i in sorted(top)))