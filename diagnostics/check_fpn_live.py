"""Is the per-pixel pattern FIXED across frames (FFC removes it) or varying?

Decisive test for the user's complaint. Take N back-to-back frames of a static
scene:
  * if frame N - median(frames) has small spatial structure, the offsets are
    fixed -> a wall reference removes them and the image is clean
  * if frame N differs from the median by the same magnitude as it differs from
    a neighbour, the noise is per-frame read noise -> no reference can help and
    the camera is genuinely not integrating
"""
import os, time, pathlib, numpy as np
DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core, usb.util

dev = usb.core.find(idVendor=0x289D, idProduct=0x0011)
assert dev is not None, "device not found"
dev.set_configuration()
claimed = None
for cfg in dev:
    for iface in cfg:
        ins = [e.bEndpointAddress for e in iface if (e.bmAttributes & 2) and (e.bEndpointAddress & 0x80)]
        if 0x81 in ins:
            try: usb.util.claim_interface(dev, iface); claimed = iface; break
            except Exception: pass
    if claimed is not None: break
def W(r, p): dev.ctrl_transfer(0x40, r, 0, 0, p, 1250)
def R(r, n): return bytes(dev.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00"); W(0x3c, b"\x00\x00"); R(0x3d, 2); W(0x3e, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00"); W(0x3c, b"\x01\x00")

OUT = pathlib.Path(r"C:\a\out\fpn"); OUT.mkdir(exist_ok=True)
frames, seqs, gains = [], [], []
for i in range(24):
    W(0x53, b"\x58\x5b\x01\x00")
    buf = bytearray()
    try:
        while len(buf) < 177840:
            buf += bytes(dev.read(0x81, min(6840, 177840 - len(buf)), timeout=1200))
    except Exception:
        pass
    if len(buf) == 177840:
        b = bytes(buf)
        u = np.frombuffer(b[:8], "<u2")
        if int(u[0]) == 0x0579 and 0 < int(u[2]) < 256:
            frames.append(np.frombuffer(b, dtype="<u2").reshape(260,342)[4:244,1:321].astype(np.float32))
            seqs.append(int(u[1])); gains.append(int(u[2]))
            (OUT/f"fpn_{len(frames):02d}.raw").write_bytes(b)
    time.sleep(0.02)
usb.util.release_interface(dev, claimed)
print(f"captured {len(frames)} valid frames, gains={sorted(set(gains))}")
if len(frames) < 3:
    print("not enough frames"); raise SystemExit(1)

S = np.stack(frames)
med = np.median(S, axis=0)
def dx(x): return float(np.abs(np.diff(x, axis=1)).mean())
print(f"\n{'#':>3}{'seq':>7}{'gain':>5}{'std':>8}{'dx':>8}{'|f-med|':>9}"
      f"{'dx(f-med)':>11}{'lagH':>8}")
lags = []
for i, f in enumerate(S):
    r = f - med
    pl, ql = f[:, :-1].ravel(), f[:, 1:].ravel()
    lag = float(((pl-pl.mean())*(ql-ql.mean())).mean()/(f.std()*ql.std()))
    lags.append(lag)
    print(f"{i:>3}{seqs[i]:>7}{gains[i]:>5}{f.std():>8.0f}{dx(f):>8.1f}"
          f"{np.abs(r).mean():>9.1f}{dx(r):>11.1f}{lag:>+8.3f}")
print(f"\nspatial dx of a frame           : {np.mean([dx(f) for f in S]):8.1f} DL")
print(f"temporal |frame - median|       : {np.mean([np.abs(f-med).mean() for f in S]):8.1f} DL")
print(f"spatial dx of (frame - median)  : {np.mean([dx(f-med) for f in S]):8.1f} DL")
print(f"mean lag-1                      : {np.mean(lags):+8.3f}")
if np.mean([dx(f-med) for f in S]) < 0.35 * np.mean([dx(f) for f in S]):
    print("\n=> offsets are FIXED: (frame-median) is smooth, so a wall reference")
    print("   removes the pattern. This is the FFC path working.")
else:
    print("\n=> offsets are NOT fixed between frames; FFC cannot help and the")
    print("   camera is not integrating - investigate gain/integration control.")
print("saved", OUT)
