"""Is the scanline artifact a mid-frame GAIN SWITCH?

Everything cheaper has been eliminated by measurement:
  - not the hot-spot overlay (the artifact is corrupted image data; drawing
    cannot produce it)
  - not the camera transfer: 60 raw frames were clean
    - though that capture was primed, so gain never varied - see below
  - not _roi_u16 / 3x3 median / _nuc2d / _apply_ffc
  - not the cursor / line-profile overlay

What is left is the condition the HUD was reporting when the band was on screen:
GAIN 3 SATURATED, gain held at 3 while the camera wanted 20. If the camera
switches gain partway through a frame, one row of that frame carries values
belonging to the other gain - a horizontal seam that comes and goes. The earlier
clean burst is consistent with this: priming pins the gain, so the seam never
forms.

So: capture a long burst with the camera free (no priming), record the gain
byte per frame, and score each frame for
  (a) within-row alternation - the dashes actually seen on screen
  (b) a row whose mean disagrees with its neighbours - the seam itself
then correlate both against gain transitions.
"""
import os
import time

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
WANT = 240

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
if d is None:
    raise SystemExit("camera not found")
d.set_configuration()
usb.util.claim_interface(d, 0)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
# deliberately NOT priming: let the AGC run so gain actually varies
W(0x54, b"\x00\x00")
W(0x3C, b"\x00\x00")
Rd(0x3D, 2)
W(0x3E, b"\x08\x00")
W(0x3C, b"\x01\x00")
Rd(0x3D, 2)

frames, gains, seqs = [], [], []
for _ in range(WANT * 4):
    if len(frames) >= WANT:
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
    seqs.append(int(h[1]))
    time.sleep(0.008)
usb.util.dispose_resources(d)

g = np.array(gains)
seq = np.array(seqs)
fr = np.stack(frames)
print("captured %d frames" % len(fr))
uq = sorted(set(gains))
print("gain values seen: %s" % uq)
trans = np.where(g[1:] != g[:-1])[0]
print("gain transitions: %d  (at frames %s)"
      % (len(trans), trans[:12].tolist()))
print()


def alternation(f):
    """Within-row violent alternation - the dashes."""
    step = np.abs(np.diff(f, axis=1)).mean(axis=1)
    med = float(np.median(step))
    mad = float(np.median(np.abs(step - med)))
    return med, (step > max(med + 3 * mad, 1.0) * 4).sum()


def seam(f):
    """A row whose value disagrees with the rows around it."""
    prof = f.mean(axis=1)
    d2 = np.abs(prof - np.median(prof))
    med = float(np.median(d2))
    mad = float(np.median(np.abs(d2 - med)))
    return float(d2.max()), (d2 > max(med + 3 * mad, 1.0) * 6).sum()


alt_hits, seam_hits = [], []
for i in range(len(fr)):
    _, a = alternation(fr[i])
    _, s = seam(fr[i])
    alt_hits.append(a)
    seam_hits.append(s)
alt_hits = np.array(alt_hits)
seam_hits = np.array(seam_hits)

print("frames with within-row alternation (dashes): %d of %d"
      % (int((alt_hits > 0).sum()), len(fr)))
print("frames with a disagreeing row (seam):        %d of %d"
      % (int((seam_hits > 0).sum()), len(fr)))

if len(trans):
    at = set((trans + 1).tolist())
    near = set()
    for t in trans:
        for k in (-2, -1, 0, 1, 2):
            j = t + k
            if 0 <= j < len(fr):
                near.add(j)
    print()
    print("frames within 2 of a gain transition: %d" % len(near))
    print("  of those, alternation rows: %d   seam rows: %d"
          % (int((alt_hits[sorted(near)] > 0).sum()),
             int((seam_hits[sorted(near)] > 0).sum())))
    far = [i for i in range(len(fr)) if i not in near]
    if far:
        print("frames far from any transition: %d" % len(far))
        print("  of those, alternation rows: %d   seam rows: %d"
              % (int((alt_hits[far] > 0).sum()),
                 int((seam_hits[far] > 0).sum())))
else:
    print()
    print("NO gain transitions in this burst - the camera held one gain, so this")
    print("test cannot fire. Point it at a scene whose brightness changes, or")
    print("let the AGC work by pointing away from a uniform wall.")

# which row is flagged, when anything is
bad = [i for i in range(len(fr)) if seam_hits[i] > 0 or alt_hits[i] > 0]
if bad:
    print()
    print("flagged frames and their gain:")
    for i in bad[:15]:
        print("   frame %3d seq %5d gain %2d  alt %2d seam %2d"
              % (i, seq[i], g[i], alt_hits[i], seam_hits[i]))
else:
    print()
    print("nothing flagged. The artifact did not occur in this burst.")