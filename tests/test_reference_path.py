# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""Run the reference driver (pcdriver/seek_nano.py) but keep streaming and
report per-frame statistics, instead of its single-shot read_frame().

Two variables this isolates:
  A. full init (identity phase: chip_id / model / serial / fw) before streaming
  B. NO W 0x53 kick per frame - the reference run() never pumps, it just reads
     the bulk pipe. My earlier scripts kicked every frame, which is the most
     likely cause of the gain cycling (header u16[2] wandering 1..28).
"""
import os, sys, time, pathlib
import numpy as np

DLL = str(LIBUSB_DLL)
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
sys.path.insert(0, str(PCDRIVER))

import seek_nano as sn
import usb.core, usb.util

OUT.mkdir(exist_ok=True)
N = 20
RAW_W, RAW_H, ROI_X, ROI_Y = 342, 260, 1, 4

# seek_nano._claim() calls is_kernel_driver_active(), which libusbK does not
# implement (NotImplementedError). libusbK needs no kernel detach, so claim
# the interface directly.
def _claim(self):
    self.dev = usb.core.find(idVendor=sn.VID, idProduct=sn.PID)
    if self.dev is None:
        raise RuntimeError("Seek Nano not found")
    self.dev.set_configuration()
    for i in self.dev.get_active_configuration():
        if any(e.bEndpointAddress == 0x81 and (e.bmAttributes & 2) for e in i):
            usb.util.claim_interface(self.dev, i)
            print(f"[claim] interface {i.bInterfaceNumber} (has 0x81 IN)")
            return
    raise RuntimeError("no interface with bulk IN 0x81")

sn.SeekNano._claim = _claim

drv = sn.SeekNano(verbose=True)
drv.handshake()
print("[chip]  ", drv.chip_id())
# seek_nano.model_string() calls read_page(0x07, 1) but the signature is
# read_page(length=64) -> TypeError. Page addressing is set_page(mode, addr).
drv.set_page(0x20, 0x07 * 64)
print("[model] ", drv.read_page().rstrip(b"\x00"))
print("[serial]", drv.serial_number())
print("[fw]    ", drv.fw_info().hex())
drv.start_streaming()
print("[stream] read-back =", drv.control_in(0x3d, 2).hex(), "(expect 0100)")
print()

def roi(b):
    a = np.frombuffer(b, dtype="<u2").astype(np.float32)[:RAW_W*RAW_H].reshape(RAW_H, RAW_W)
    return a[ROI_Y:ROI_Y+240, ROI_X:ROI_X+320]
colstep = lambda im: float(np.abs(np.diff(im.mean(axis=0))).mean())
rowstep = lambda im: float(np.abs(np.diff(im.mean(axis=1))).mean())

print(f"{'#':>3} {'ms':>5} {'seq':>5} {'gain':>5} {'mean':>9} {'std':>8} "
      f"{'min':>7} {'max':>7} {'col':>7} {'row':>7}")
print("-" * 74)
rows = []
for i in range(N):
    t0 = time.time()
    try:
        b = drv.read_frame()
    except Exception as e:
        print(f"{i:>3} read failed: {type(e).__name__}")
        continue
    dt = (time.time() - t0) * 1000
    im = roi(b)
    u = np.frombuffer(b[:8], "<u2")
    seq, gain = int(u[1]), int(u[2])
    print(f"{i:>3} {dt:>5.0f} {seq:>5} {gain:>5} {im.mean():>9.0f} {im.std():>8.0f} "
          f"{im.min():>7.0f} {im.max():>7.0f} {colstep(im):>7.1f} {rowstep(im):>7.1f}")
    rows.append((b, im, seq, gain))
    (OUT / f"ref_{i:02d}.raw").write_bytes(b)

if rows:
    gains = [g for *_, g in rows]
    means = [im.mean() for _, im, *_ in rows]
    stds  = [im.std() for _, im, *_ in rows]
    print(f"\ngain word:   {gains}")
    print(f"  distinct: {sorted(set(gains))}  -> {'STABLE' if len(set(gains))==1 else 'CYCLING'}")
    print(f"mean spread: {max(means)-min(means):.0f} DL  "
          f"({100*(max(means)-min(means))/max(1,max(means)):.0f}% of peak)")
    print(f"std  spread: {max(stds)-min(stds):.0f} DL  "
          f"({100*(max(stds)-min(stds))/max(1,max(stds)):.0f}% of peak)")
    if len(rows) >= 2:
        d = [float(np.abs(a[1]-b[1]).mean()) for a, b in zip(rows, rows[1:])]
        print(f"frame-to-frame mean|delta|: median {np.median(d):.0f} DL  "
              f"min {min(d):.0f}  max {max(d):.0f}")
drv.close()
