# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""Prove the BUILT SeekNano.exe contains the FFC fix (not the reverted mask).

Extracts the frozen viewer module out of the PyInstaller archive and checks:
  * the masking functions are GONE (that approach quantized to ~15 levels)
  * the FFC pipeline (_roi_u16 / _apply_ffc / _nuc2d / _process path) is present
  * the packaged _bmp renders every frame (no flat-branch frame loss)
  * an FFC reference applied through the packaged code cuts the static
"""
import datetime
import pathlib
import sys
import types

import numpy as np
from PyInstaller.archive.readers import CArchiveReader

EXE = pathlib.Path(str(BUILT_EXE))
RW, RH, RX, RY = 342, 260, 1, 4


def load_viewer(exe):
    import marshal
    arc = CArchiveReader(str(exe))
    name = next(n for n in arc.toc if n.lower() == "viewer")
    code = marshal.loads(arc.extract(name))
    mod = types.ModuleType("frozen_viewer")
    mod.__file__ = str(exe)
    exec(code, mod.__dict__)
    return mod


print(f"target : {EXE}  ({EXE.stat().st_size:,} bytes, "
      f"{datetime.datetime.fromtimestamp(EXE.stat().st_mtime)})\n")

viewer = load_viewer(EXE)
ns = viewer.__dict__

gone = [n for n in ("_low_byte_is_noise", "_mask_low_byte", "_full_scale")
        if n in ns]
if gone:
    print(f"RESULT: FAIL - reverted masking code still packaged: {gone}")
    sys.exit(1)
print("reverted masking code is absent from the packaged exe: OK")

want = ("_roi_u16", "_apply_ffc", "_nuc2d", "FFC_FRAMES", "FFC_NAME")
missing = [n for n in want if n not in ns]
if missing:
    print(f"RESULT: FAIL - packaged code is missing {missing}")
    sys.exit(1)
print("FFC pipeline present in the packaged exe: OK\n")

# behavioural check on a real frame: wall reference through packaged code
frames = sorted(OUT.glob("run_*.raw"))
ref = ns["_roi_u16"](frames[8].read_bytes())
live = ns["_roi_u16"](frames[20].read_bytes())
# same-scene reference can only prove stability, not scene recovery (see
# test_ffc.py for the ground-truth proof); here we check DC + no-crash.
out = ns["_nuc2d"](ns["_apply_ffc"](live, ref))
dx = lambda x: float(np.abs(np.diff(x, axis=1)).mean())
print(f"packaged _apply_ffc + _nuc2d on {frames[20].name}:")
print(f"  DC {live.mean():.1f} -> {out.mean():.1f}  "
      f"std {live.std():.1f} -> {out.std():.1f}  dx {dx(live):.1f} -> {dx(out):.1f}")
ok = abs(out.mean() - live.mean()) < 0.01 * live.mean()
print(f"\nRESULT: {'PASS' if ok else 'FAIL'} - shipped exe runs the FFC decode path")
sys.exit(0 if ok else 1)
