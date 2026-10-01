# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""Regression guard: the DC level must be preserved by the NUC.

Bug found on live hardware 2026-09-29: _nuc2d ended with
    return img - corr + img.mean()
corr is a high-pass residual with ~zero mean, so that final `+ img.mean()`
added a constant equal to the image mean and DOUBLED the brightness
(live settled frame: median 6560 DL -> 13088 DL). Fix is `return img - corr`.
This test fails if the additive term ever comes back.
"""
import ast, pathlib, numpy as np

src = (VIEWER).read_text(encoding="utf-8")
ns = {"np": np}
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {"_boxblur", "_nuc2d"}:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "v", "exec"), ns)
_nuc2d = ns["_nuc2d"]

rng = np.random.default_rng(7)
fails = 0
for name, img in [
    ("uniform",  np.full((240, 320), 5000.0)),
    ("gradient", np.tile(np.linspace(4000, 6000, 320), (240, 1))),
    ("hotspot",  5000 + 800*np.exp(-(((np.arange(240)[:,None]-85)**2 +
                                      (np.arange(320)[None,:]-91)**2)/600.0))),
    ("noisy",    5000 + rng.normal(0, 200, (240, 320))),
]:
    out = _nuc2d(img)
    raw_m, out_m = float(img.mean()), float(out.mean())
    rel = abs(out_m - raw_m) / max(1.0, abs(raw_m))
    ok = rel < 0.01
    fails += not ok
    print(f"  {name:<10} mean {raw_m:8.1f} -> {out_m:8.1f}   "
          f"drift {rel*100:6.3f}%   {'OK' if ok else 'FAIL (DC not preserved)'}")

# a constant image must come back constant - no banding, no halos
flat = np.full((240, 320), 5000.0)
o = _nuc2d(flat)
print(f"\n  flat input -> output spread {o.max()-o.min():.4f} DL "
      f"(must be ~0, i.e. no invented structure)")
if o.max() - o.min() > 0.5: fails += 1

print("\nRESULT:", "FAIL" if fails else "PASS")
raise SystemExit(1 if fails else 0)
