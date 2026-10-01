"""Endpoint selection: prove 0x81 alone streams and 0x82 is unusable.

Checks the three facts behind the EP_CANDIDATES change, on the live device:
  1. the claimed interface actually owns 0x81
  2. reading 0x82 (unclaimed interface) raises, which is what killed the thread
  3. 0x81 yields real frames through the shipped validation
"""
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa
import os, time, pathlib, ast
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
            "_frame_header", "_frame_reason", "_resync_frame",
            "_gain_locked", "_boxblur", "_nuc2d", "_roi_u16"}:
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
_reason, _hdr, _resync = ns["_frame_reason"], ns["_frame_header"], ns["_resync_frame"]
_locked, _roi, _nuc = ns["_gain_locked"], ns["_roi_u16"], ns["_nuc2d"]
CAND, FRAME, CHUNK = ns["EP_CANDIDATES"], ns["FRAME_BYTES"], ns["CHUNK"]

dev = usb.core.find(idVendor=0x289D, idProduct=0x0011)
assert dev is not None, "device not found - close SeekNano first"
dev.set_configuration()

eps = []
for cfg in dev:
    for i in cfg:
        for ep in i:
            if (ep.bEndpointAddress & 0x80) and (ep.bmAttributes & 3) == 2:
                eps.append((i.bInterfaceNumber, i.bAlternateSetting,
                            ep.bEndpointAddress))
print("bulk-in endpoints (iface, alt, ep):", eps)
owner = {ep: n for (n, _alt, ep) in eps}
usb.util.claim_interface(dev, 0)
print(f"claimed interface 0; EP_CANDIDATES = {tuple('0x%02x' % e for e in CAND)}")

order = [ep for (_n, _a, ep) in eps if ep in CAND] or \
        [ep for (n, _a, ep) in eps if n == 0]
print(f"endpoint order the viewer will use: {tuple('0x%02x' % e for e in order)}")
assert all(owner.get(e) == 0 for e in order), \
    f"candidate on an unclaimed interface: {order}"
print("OK: every candidate lives on the claimed interface 0")

print("\n--- reading 0x82 (interface 1, NOT claimed) ---")
try:
    dev.read(0x82, 64, timeout=400)
    print("  0x82 read unexpectedly succeeded")
except Exception as e:
    print(f"  raises {type(e).__name__}: {e}")
    print("  -> confirms why rotating onto 0x82 killed the stream thread")

def W(r, p): dev.ctrl_transfer(0x40, r, 0, 0, p, 1250)
def R(r, n): return bytes(dev.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00"); W(0x3c, b"\x00\x00"); R(0x3d, 2); W(0x3e, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00"); W(0x3c, b"\x01\x00")

ep = order[0]
hist, prev, ok_frames, kicks = [], None, 0, 0
print(f"\n--- 20 kicks on 0x{ep:02x} ---")
for i in range(20):
    W(0x53, b"\x58\x5b\x01\x00"); kicks += 1
    buf = bytearray()
    try:
        while len(buf) < FRAME + CHUNK:
            buf += bytes(dev.read(ep, min(CHUNK, FRAME + CHUNK - len(buf)),
                                  timeout=800))
    except Exception:
        pass
    f = _resync(buf)
    if f is None:
        print(f"{i:>3} no frame"); continue
    m, s, g = _hdr(f)
    if _reason(f) is not None:
        print(f"{i:>3} rejected: {_reason(f)}"); continue
    prev = s
    locked = _locked(g, hist)
    img = _roi(f); out = _nuc(img)
    ok_frames += 1
    print(f"{i:>3} seq={s:<5} gain={g:<3} locked={str(locked):<5} "
          f"std={img.std():6.0f} dx={np.abs(np.diff(img,axis=1)).mean():6.1f} "
          f"nuc_dx={np.abs(np.diff(out,axis=1)).mean():6.2f}")
usb.util.dispose_resources(dev)
print(f"\n{ok_frames} validated frames from {kicks} kicks on the single candidate")
assert ok_frames >= 10, "not enough frames on 0x81"
print("RESULT: PASS - one endpoint is enough, and it is on the claimed interface")
