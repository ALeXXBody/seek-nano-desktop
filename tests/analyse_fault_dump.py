"""Find the stage that breaks the row, using a dump that contains the fault.

scanline_fault_<n>.raw layout:
    0..3    frame number           u32
    4..5    threshold              u16
    6..7    offending row          u16   (ROI row, as scored)
    8..11   scored pixel count     u32
    12..    scored image           float32, ROI-shaped (IH x IW)
    then    raw USB payload        uint16, 342 x 260

The scored image is the one the watchdog actually measured, so the fault is
definitely present in it. The raw payload alongside is the frame that was being
read at dump time - a different frame, since the reader thread runs ahead - so it
is a reference for what the wire looked like around the same moment, NOT the
same frame.

What this prints: the row profile of the scored image, which rows are anomalous,
and what that row actually contains.
"""
import struct
import sys

import numpy as np

PATH = sys.argv[1] if len(sys.argv) > 1 else r"C:\a\scanline_fault_2085.raw"
IW, IH = 320, 240

blob = open(PATH, "rb").read()
frame_no, thr, row = struct.unpack("<IHH", blob[:8])
npx = struct.unpack("<I", blob[8:12])[0]
print("frame %d  threshold %d  offending row %d  scored %d px"
      % (frame_no, thr, row, npx))

scored = np.frombuffer(blob[12:12 + npx * 4], dtype=np.float32).reshape(IH, IW)
raw = blob[12 + npx * 4:]
print("raw payload alongside: %d bytes" % len(raw))
print()


def profile(f):
    return np.abs(np.diff(f, axis=1)).mean(axis=1)


def zscores(f):
    p = profile(f)
    med = float(np.median(p))
    mad = float(np.median(np.abs(p - med)))
    return p / max(med + 3.0 * mad, 1e-3), med


z, med = zscores(scored)
print("row profile of the SCORED image: median |dx| %.2f DL" % med)
bad = np.where(z > 8)[0]
print("rows above z=8: %s" % (bad.tolist() if bad.size else "none"))
print("header row %d scores z %.1f" % (row, z[row]))
print()
print("worst 8 rows by z:")
for i in np.argsort(z)[::-1][:8]:
    mark = "  <-- header row" if i == row else ""
    print("   row %3d  z %8.1f%s" % (i, z[i], mark))

if bad.size:
    print()
    print("the anomalous row(s) in detail:")
    for i in bad[:4]:
        r = scored[i]
        nb = np.concatenate([scored[max(0, i - 1)], scored[min(IH - 1, i + 1)]])
        d, dnb = float(np.abs(np.diff(r)).mean()), float(np.abs(np.diff(nb)).mean())
        print("   row %3d: mean %8.1f  min %7.1f  max %7.1f | row mean - "
              "neighbour mean = %+8.1f DL" % (i, r.mean(), r.min(), r.max(),
                                             r.mean() - nb.mean()))
        print("            mean |dx| row %.1f vs neighbours %.1f  (%.1fx)"
              % (d, dnb, d / max(dnb, 1e-6)))
        print("            first 16 values: %s"
              % np.round(r[:16], 0).astype(int).tolist())

print()
# Compare against the raw reference frame at the same moment.
if len(raw) >= 342 * 260 * 2:
    full = np.frombuffer(raw, dtype="<u2").reshape(260, 342)
    roi = full[12:12 + IH, 2:2 + IW].astype(np.float32)
    zr, medr = zscores(roi)
    print("the RAW payload from the same moment (a different frame - the reader")
    print("runs ahead of the GUI thread):")
    print("   median |dx| %.2f DL, max z %.1f at row %d, rows>8: %s"
          % (medr, zr.max(), int(np.argmax(zr)),
             np.where(zr > 8)[0].tolist() or "none"))
    print("   scored median %.2f DL vs raw median %.2f DL"
          % (med, medr))
    if med > medr * 1.3:
        print()
        print("   NOTE: the scored image is %.1fx noisier row-to-row than the raw"
              % (med / max(medr, 1e-6)))
        print("   frame beside it. That difference is produced by the pipeline")
        print("   between the wire and the watchdog, and is where to look next.")