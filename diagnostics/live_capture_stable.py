"""Robust settled capture.

Findings this encodes:
  * the first bulk read after W 0x3c/01 00 ALWAYS times out - the camera needs
    one primed frame before it will deliver. Skip until a good frame arrives.
  * R 0x35 returns 00 00 00 00 ("nothing new") on this hardware even when a
    frame is queued, so it is useless as a ready-poll; rely on the bulk read.
  * back-to-back grabs are not guaranteed fresh, so allow a gap and record the
    frame sequence word (raw[0,2]) so we can tell real progress from a stall.
"""
import os, time, pathlib, numpy as np
DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core, usb.util

OUT = pathlib.Path(r"C:\a\out"); OUT.mkdir(exist_ok=True)
FRAME, CHUNK, N, GAP, SETTLE = 177_840, 6_840, 15, 0.30, 2.0
RAW_W, RAW_H, ROI_X, ROI_Y = 342, 260, 1, 4

dev = usb.core.find(idVendor=0x289D, idProduct=0x0011)
if dev is None: raise SystemExit("device not found")
ifc = next(i for i in dev.get_active_configuration()
           if any(e.bEndpointAddress == 0x81 and (e.bmAttributes & 2) for e in i))
usb.util.claim_interface(dev, ifc)
W = lambda r, p=b"\x00\x00": dev.ctrl_transfer(0x40, r, 0, 0, p, timeout=1250)
R = lambda r, n:    dev.ctrl_transfer(0xC0, r, 0, 0, n, timeout=1250)

W(0x54); W(0x3c); R(0x3d, 2); W(0x3e, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00"); W(0x3c, b"\x01\x00")
print("streaming:", bytes(R(0x3d, 2)).hex(), f"- settle {SETTLE}s")
time.sleep(SETTLE)

def read_frame():
    buf = bytearray()
    while len(buf) < FRAME:
        buf += bytes(dev.read(0x81, min(CHUNK, FRAME - len(buf)), timeout=2000))
    return bytes(buf)

def grab():
    W(0x53, b"\x58\x5b\x01\x00")
    R(0x35, 4)
    return read_frame()

def roi(b):
    a = np.frombuffer(b, dtype="<u2").astype(np.float32)[:RAW_W*RAW_H].reshape(RAW_H, RAW_W)
    return a[ROI_Y:ROI_Y+240, ROI_X:ROI_X+320]
colstep = lambda im: float(np.abs(np.diff(im.mean(axis=0))).mean())
rowstep = lambda im: float(np.abs(np.diff(im.mean(axis=1))).mean())

print("priming (skipping cold frames)...")
primed = 0
for _ in range(12):
    try:
        grab(); primed += 1
        if primed >= 3: break
    except Exception:
        pass
print(f"  primed with {primed} frame(s)")

print(f"\n{'#':>3} {'ms':>5} {'seq':>5} {'mean':>9} {'std':>9} {'min':>7} {'max':>7} "
      f"{'colstep':>8} {'rowstep':>8}  verdict")
print("-"*80)
rows = []
for i in range(N):
    t0 = time.time()
    try:
        b = grab()
    except Exception as e:
        print(f"{i:>3} {'--':>5}  bulk read failed: {type(e).__name__}")
        time.sleep(GAP); continue
    dt = (time.time()-t0)*1000
    im = roi(b); seq = int(np.frombuffer(b[:4], "<u2")[1])
    flat = im.std() < 40
    sat   = im.max() >= 65500 or im.min() == 0
    v = "FLAT" if flat else ("SATURATED" if sat else "ok")
    print(f"{i:>3} {dt:>5.0f} {seq:>5} {im.mean():>9.0f} {im.std():>9.0f} "
          f"{im.min():>7.0f} {im.max():>7.0f} {colstep(im):>8.1f} {rowstep(im):>8.1f}  {v}")
    if v == "ok":
        rows.append((b, im))
        (OUT/f"settled_{i:02d}.raw").write_bytes(b)
    time.sleep(GAP)

if rows:
    stds = [im.std() for _, im in rows]
    print(f"\ngood frames: {len(rows)}/{N}   std spread "
          f"{100*(max(stds)-min(stds))/max(1,max(stds)):.0f}%")
    if len(rows) >= 2:
        for (a, ia), (b, ib) in zip(rows, rows[1:]):
            print(f"  mean|delta| between good frames: "
                  f"{np.abs(ia-ib).mean():.0f} DL  (scene motion vs fixed-pattern noise)")
    print(f"\nsettled frames in {OUT}")
usb.util.release_interface(dev, ifc)
