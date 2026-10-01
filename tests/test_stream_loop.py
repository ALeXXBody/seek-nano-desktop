# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""Reproduce the app's read loop EXACTLY, at its real kick pacing.

The earlier endpoint test read FRAME_BYTES+CHUNK and rescued the frame from the
timeout, so it passed while the app failed. This mirrors the shipped loop
byte-for-byte: kick, read in 6,840 B chunks to exactly FRAME_BYTES, 500 ms
per-chunk timeout, 40 ms target period, fails>60 gives up.
"""
import ast, os, time, pathlib
import numpy as np
DLL = str(LIBUSB_DLL)
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core, usb.util

SRC = VIEWER
ns = {"np": np}
src = SRC.read_text(encoding="utf-8")
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {
            "_frame_header", "_frame_reason", "_resync_frame", "_gain_locked",
            "_boxblur", "_nuc2d", "_roi_u16", "_apply_ffc"}:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "v", "exec"), ns)
    elif isinstance(n, ast.Assign):
        try: val = ast.literal_eval(n.value)
        except Exception: continue
        t = n.targets[0]
        if isinstance(t, ast.Name): ns[t.id] = val
        elif isinstance(t, (ast.Tuple, ast.List)):
            try:
                for nm, vv in zip(t.elts, val):
                    if isinstance(nm, ast.Name): ns[nm.id] = vv
            except Exception: pass
FRAME, CHUNK, EP = ns["FRAME_BYTES"], ns["CHUNK"], ns["BULK_EP"]
_reason, _resync, _hdr = ns["_frame_reason"], ns["_resync_frame"], ns["_frame_header"]
KICKS, PERIOD = 40, 0.040

dev = usb.core.find(idVendor=0x289D, idProduct=0x0011)
assert dev is not None, "device not found - close SeekNano first"
dev.set_configuration()
usb.util.claim_interface(dev, 0)
def W(r, p): dev.ctrl_transfer(0x40, r, 0, 0, p, 1250)
def R(r, n): return bytes(dev.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00"); W(0x3c, b"\x00\x00"); R(0x3d, 2); W(0x3e, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00"); W(0x3c, b"\x01\x00")
print("streaming enabled; mirroring the app loop\n")

fails = 0
frames, timeouts_on_last_chunk = [], 0
last_ok = time.time()
for kick in range(KICKS):
    wait = PERIOD - (time.time() - last_ok)
    if wait > 0:
        time.sleep(wait)
    t0 = time.time()
    W(0x53, b"\x58\x5b\x01\x00")
    buf = bytearray()
    read_err = None
    while len(buf) < FRAME and not False:
        try:
            buf.extend(dev.read(EP, min(CHUNK, FRAME - len(buf)), 500))
        except usb.core.USBError as ue:
            read_err = ue
            if len(buf) >= FRAME - CHUNK:
                timeouts_on_last_chunk += 1
            break
    last_ok = time.time()
    if len(buf) >= FRAME:
        fails = 0
        f = _resync(buf)
        if f is not None:
            frames.append(f)
            m, s, g = _hdr(f)
            print(f"{kick:>3} OK   {len(buf)} B  dt={1000*(last_ok-t0):5.0f} ms  "
                  f"seq={s:<5} gain={g}")
        else:
            print(f"{kick:>3} no valid frame in {len(buf)} B")
    else:
        fails += 1
        print(f"{kick:>3} FAIL {len(buf)}/{FRAME} B  {type(read_err).__name__}")
        if fails > 60:
            print("   -> app would have given up here"); break
usb.util.dispose_resources(dev)

print(f"\n{KICKS} kicks: {len(frames)} validated frames, {fails} consecutive "
      f"failures at the end")
print(f"timeouts landing on the final chunk: {timeouts_on_last_chunk}")
if frames:
    gains = [_hdr(f)[2] for f in frames]
    print(f"gains: {gains}")
    img = _roi(frames[-1]) if (_roi := ns['_roi_u16']) else None
    n = ns["_nuc2d"](img)
    print(f"last frame: std={img.std():.0f} dx={np.abs(np.diff(img,axis=1)).mean():.0f}"
          f"  after NUC dx={np.abs(np.diff(n,axis=1)).mean():.0f}")
ok = len(frames) >= KICKS * 0.8
print(f"\nRESULT: {'PASS' if ok else 'FAIL'} - the app loop must keep streaming")
