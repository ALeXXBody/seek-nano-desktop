"""One long session, known-good kick, to characterise the gain cycling.

Everything so far points at the device cycling its AGC gain rather than at a
payload problem:
  * u16[2] (row 0, col 2 - inside the buffer but cropped by ROI_Y=4) fully
    predicts brightness: gain 27 -> mean 11959 twice, gain 28 -> 53491 twice,
    and the scene is static, so the same gain reproduces the same mean.
  * gains seen across frames: 1, 6, 7, 8, 14, 20, 25, 26, 27, 28, 28, 27

This logs 40 frames in a single session: seq, the gain word, R 0x35 status, and
ROI brightness - to show whether the gain has a period, drifts, or converges.
"""
import os, sys, time, pathlib
import numpy as np

DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
sys.path.insert(0, r"C:\a\src\pcdriver")
import seek_nano as sn
import usb.core, usb.util

FRAME, CHUNK, RAW_W, RAW_H, N = 177_840, 6_840, 342, 260, 40
OUT = pathlib.Path(r"C:\a\out"); OUT.mkdir(exist_ok=True)
KICK = bytes([0x58, 0x5b, 0x01, 0x00])

def _claim(self):
    self.dev = usb.core.find(idVendor=sn.VID, idProduct=sn.PID)
    self.dev.set_configuration()
    for i in self.dev.get_active_configuration():
        if any(e.bEndpointAddress == 0x81 and (e.bmAttributes & 2) for e in i):
            usb.util.claim_interface(self.dev, i); return
sn.SeekNano._claim = _claim

drv = sn.SeekNano(verbose=False)
drv.handshake()
drv.control_out(0x37, b"\xfc\x00\x04\x00"); drv.control_out(0x3c, b"\x01\x00")
print("stream:", bytes(drv.control_in(0x3d, 2)).hex())
time.sleep(2.0)

def roi(b):
    a = np.frombuffer(b, dtype="<u2").astype(np.float32)[:RAW_W*RAW_H].reshape(RAW_H, RAW_W)
    return a[4:244, 1:321]

recs, frames = [], []
print(f"\n{'#':>3} {'ms':>5} {'seq':>6} {'R35':>10} {'gain':>5} {'mean':>9} "
      f"{'std':>8} {'p05':>7} {'p95':>7}")
print("-" * 68)
for i in range(N):
    t0 = time.time()
    drv.control_out(0x53, KICK)
    r35 = bytes(drv.control_in(0x35, 4))
    buf = bytearray()
    try:
        while len(buf) < FRAME:
            buf += bytes(drv.dev.read(0x81, min(CHUNK, FRAME-len(buf)), timeout=1500))
    except Exception as e:
        print(f"{i:>3} read failed: {type(e).__name__}"); break
    dt = (time.time()-t0)*1000
    b = bytes(buf)
    u = np.frombuffer(b[:8], "<u2")
    im = roi(b)
    gain = int(u[2])
    p05, p95 = np.percentile(im, [5, 95])
    recs.append((int(u[1]), gain, r35.hex(), float(im.mean()), float(im.std())))
    frames.append(b)
    print(f"{i:>3} {dt:>5.0f} {int(u[1]):>6} {r35.hex():>10} {gain:>5} "
          f"{im.mean():>9.0f} {im.std():>8.0f} {p05:>7.0f} {p95:>7.0f}")

gains = [r[1] for r in recs]
means = [r[3] for r in recs]
print(f"\ngain sequence : {gains}")
print(f"distinct gains: {sorted(set(gains))}")
# is brightness a pure function of the gain word?
from collections import defaultdict
byg = defaultdict(list)
for g, m in zip(gains, means):
    byg[g].append(m)
print("\ngain -> mean brightness (a tight cluster means gain is the only driver):")
for g in sorted(byg):
    v = byg[g]
    print(f"   gain {g:>3}  n={len(v)}  mean {np.mean(v):8.0f} +/- {np.std(v):6.1f}")

print("\nR 0x35 values seen:", sorted({r[2] for r in recs}))
d = [gains[i+1]-gains[i] for i in range(len(gains)-1)]
print("gain deltas:", d)
# periodicity check
for p in range(2, 13):
    if len(gains) > 2*p and all(gains[i] == gains[i+p] for i in range(len(gains)-p)):
        print(f"  -> gain sequence is periodic with period {p}")
        break
else:
    print("  -> no exact period up to 12; gain is walking, not looping")

for i, f in enumerate(frames):
    (OUT/f"run_{i:02d}.raw").write_bytes(f)
print(f"\nsaved {len(frames)} frames to {OUT}/run_*.raw")
drv.close()
