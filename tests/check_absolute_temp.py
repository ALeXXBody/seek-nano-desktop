"""Is there any way to get ABSOLUTE temperature from this camera?

No calibration has been found in the app, the docs, or the captured symbol and
string dumps. Three places could still hold it, and all three are cheap to
check with a frame in hand:

  1. the 8-byte frame header - we only decode magic, sequence and gain, so
     bytes 6-7 have never been looked at
  2. the unused border around the ROI: the transfer is 342x260 uint16 but the
     image is rows 12-251, cols 2-321, leaving 22 columns and 20 rows of
     padding that nothing in the pipeline reads. If the firmware puts anything
     there, that is where it would be.
  3. anything in the border that TRACKS the scene would be a temperature
     reading - so correlate every border element against the mean of the image
     across a range of scene temperatures. A real thermometer moves with the
     scene; a fixed constant is calibration metadata.
"""
import os
import time

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
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

raws = []
for _ in range(150):
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < 177840:
            b += bytes(d.read(0x81, min(6840, 177840 - len(b)), timeout=800))
    except Exception:
        pass
    if len(b) == 177840:
        h = np.frombuffer(b[:8], "<u2")
        if int(h[0]) == 0x0579:
            raws.append(bytes(b))
    time.sleep(0.02)
usb.util.dispose_resources(d)
print("captured %d frames\n" % len(raws))

print("1. THE 8-BYTE HEADER, decoded every way")
b0 = raws[0]
u16 = np.frombuffer(b0[:8], "<u2")
u8 = np.frombuffer(b0[:8], np.uint8)
print("   u16: %s" % " ".join("0x%04X" % v for v in u16))
print("   u8 : %s" % " ".join("%3d" % v for v in u8))
print("   we use [0]=0x0579 magic, [1]=seq, [2]=gain. [3] is never decoded.")

# does header byte 6/7 track the scene?
hdr = []
means = []
for r in raws:
    hu = np.frombuffer(r[:8], "<u2")
    a = np.frombuffer(r, dtype="<u2").reshape(RH, RW).astype(np.float32)
    hdr.append(hu)
    means.append(float(a[RY:RY + IH, RX:RX + IW].mean()))
hdr = np.array(hdr)
means = np.array(means)
print()
for i in range(4):
    col = hdr[:, i].astype(float)
    print("   header u16[%d]: min %6.0f max %6.0f   corr with scene mean %+.3f"
          % (i, col.min(), col.max(),
             np.corrcoef(col, means)[0, 1] if col.std() > 0 else float("nan")))

print()
print("2. THE UNUSED BORDER")
a = np.frombuffer(raws[0], dtype="<u2").reshape(RH, RW)
top = a[0:RY, :]
bot = a[RY + IH:, :]
left = a[RY:RY + IH, 0:RX]
right = a[RY:RY + IH, RX + IW:]
for nm, blk in (("top    %d rows" % top.shape[0], top),
                ("bottom %d rows" % bot.shape[0], bot),
                ("left   %d cols" % left.shape[1], left),
                ("right  %d cols" % right.shape[1], right)):
    u = np.unique(blk)
    print("   %-14s unique values %4d   min %6d max %6d   %s"
          % (nm, len(u), blk.min(), blk.max(),
             ("CONSTANT" if len(u) == 1 else
              "all-zero" if blk.max() == 0 else "varies")))

print()
print("3. DOES ANY BORDER ELEMENT TRACK THE SCENE?")
print("   (a real thermometer moves with the scene; constants are metadata)")
blocks = [("top", top), ("bottom", bot), ("left", left), ("right", right)]
for nm, blk in blocks:
    hs, ws = blk.shape
    best = (0.0, None)
    for y in range(hs):
        for x in range(ws):
            col = np.array([np.frombuffer(r, dtype="<u2").reshape(RH, RW)
                            [y if nm == "top" else (0 if nm == "left" else y),
                             x if nm in ("top", "left") else
                             (0 if nm == "left" else x)]
                            for r in raws[:40]], dtype=np.float32)
            if col.std() < 1e-6:
                continue
            c = abs(np.corrcoef(col, means[:40])[0, 1])
            if c > best[0]:
                best = (c, (y, x))
    print("   %-7s strongest correlation with scene mean: %.3f" % (nm, best[0]))

print()
print("4. SCENE SPREAD ACROSS THE CAPTURE")
print("   image mean ranged %.0f .. %.0f DL over %d frames"
      % (means.min(), means.max(), len(means)))
if means.max() - means.min() < 50:
    print("   the camera did not move, so a tracking element cannot be")
    print("   distinguished from a constant. Point it at something hot and")
    print("   cold and re-run to test properly.")