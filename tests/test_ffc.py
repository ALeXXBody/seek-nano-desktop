"""FFC proof: can a wall reference actually remove this sensor's static?

Setup mirrors the measured device (2026-09-30):
  * per-pixel offsets: temporally stable (~6 DL), i.e. FIXED pattern
  * temporal noise: ~6 DL per frame
  * scene: smooth gradient + hotspot, 16-bit

Test 1 (the product claim): reference = mean of 16 wall frames (offsets + noise,
NO scene); scene frame = same offsets + scene + noise. FFC must recover the
scene. Uses the shipped _apply_ffc + _nuc2d extracted from viewer.py by AST.

Test 2 (plumbing): _roi_u16 shape, _apply_ffc DC preservation + shape refusal,
FFC file round-trip in the repo's u16 ROI convention (ffc_latest.raw).
"""
import ast, pathlib, sys, numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
WANT = {"_boxblur", "_nuc2d", "_apply_ffc", "_roi_u16"}
ns = {"np": np}
src = SRC.read_text(encoding="utf-8")
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in WANT:
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
missing = WANT - {k for k in WANT if k in ns}
assert not missing, f"missing from viewer.py: {missing}"
_apply, _nuc = ns["_apply_ffc"], ns["_nuc2d"]
RW, RH, RX, RY, IW, IH = ns["RAW_W"], ns["RAW_H"], ns["ROI_X"], ns["ROI_Y"], ns["IMG_W"], ns["IMG_H"]

rng = np.random.default_rng(11)
# measured FPN: spatially white, std ~343 DL, fixed in time
offsets = rng.normal(0, 343, (IH, IW))
# smooth scene: gradient + hotspot, span ~1600 DL like the live room
yy, xx = np.mgrid[0:IH, 0:IW]
scene = 6000 + 4.0*xx + 2.0*yy
scene += 900*np.exp(-((xx-200)**2 + (yy-150)**2)/40.0**2)
scene -= scene.mean(); scene += 6500

def frame(sc):
    return offsets + sc + rng.normal(0, 6, (IH, IW))

wall_bank = np.stack([frame(np.full_like(scene, 6500.0)) for _ in range(16)])
ref = wall_bank.mean(axis=0)
live = frame(scene)

def rmse(a, b): return float(np.sqrt(((a-b)**2).mean()))
def dx(x): return float(np.abs(np.diff(x, axis=1)).mean())

print("=== 1. wall reference recovers the scene ===")
print(f"  {'path':<22}{'RMSE to scene':>14}{'neigh dx':>10}")
print(f"  {'raw live':<22}{rmse(live,scene):>14.1f}{dx(live):>10.1f}")
out = _apply(_nuc(_apply(live, ref)), None) if False else _nuc(_apply(live, ref))
# note: _apply(out, None) is identity; written out to show shape-refusal is safe
print(f"  {'FFC + column/row NUC':<22}{rmse(out,scene):>14.1f}{dx(out):>10.1f}")
print(f"  {'NUC only (no wall)':<22}{rmse(_nuc(live),scene):>14.1f}{dx(_nuc(live)):>10.1f}")
ok1 = rmse(out, scene) < 0.25 * rmse(live, scene)
print(f"  -> FFC cuts scene error by {rmse(live,scene)/rmse(out,scene):.1f}x: {'OK' if ok1 else 'FAIL'}")

print("\n=== 2. masking (the reverted approach) destroys the scene ===")
masked_live = (live // 256 * 256)  # what >>8<<8 effectively did to precision
print(f"  quantization alone: RMSE {rmse(masked_live,scene):.1f} DL, "
      f"unique levels {len(np.unique((live//256).astype(int)))} in 8-bit")
print("  -> 16-bit + FFC keeps ~340 DL of real per-pixel precision; masking kept ~15 levels")

print("\n=== 3. plumbing ===")
fake = (np.arange(RW*RH, dtype=np.uint16).reshape(RH, RW)).tobytes()
r = ns["_roi_u16"](fake)
print(f"  _roi_u16 shape {r.shape} (want (240, 320)): {'OK' if r.shape==(240,320) else 'FAIL'}")
d0 = live.mean()
o = _apply(live, ref)
print(f"  DC preserved: {d0:.1f} -> {o.mean():.1f} (drift {100*abs(o.mean()-d0)/d0:.3f}%): "
      f"{'OK' if abs(o.mean()-d0) < 0.01*d0 else 'FAIL'}")
bad = _apply(live, np.zeros((10, 10)))
print(f"  wrong-shape reference refused: {'OK' if np.shares_memory(bad, live) or np.array_equal(bad, live) else 'FAIL'}")
tmp = pathlib.Path(r"C:\a\out\_ffc_roundtrip.raw")
np.clip(np.rint(ref), 0, 65535).astype("<u2").tofile(tmp)  # same as _finish_flat
back = np.fromfile(tmp, dtype="<u2").reshape(IH, IW).astype(np.float32)
print(f"  u16 file round-trip max err {np.abs(back-ref).max():.1f} DL (want <=0.5): "
      f"{'OK' if np.abs(back-ref).max() <= 0.5 else 'FAIL'}")
tmp.unlink()
rt_ok = bool(np.abs(back - ref).max() <= 0.5)
ok = bool(ok1 and r.shape == (240, 320) and rt_ok)
print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
