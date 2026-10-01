"""Numeric description of what the camera is actually looking at.

I cannot view the PNGs, so describe the settled scene instead: where the heat is,
whether the structure looks like a real room, and whether NUC preserves it.
Also measures contrast in DL so the user can compare against a thermometer.
"""
import ast, pathlib, numpy as np

ROOT = pathlib.Path(r"C:\a\src"); OUT = pathlib.Path(r"C:\a\out")
RAW_W, RAW_H, ROI_X, ROI_Y = 342, 260, 1, 4
src = (ROOT/"app"/"viewer.py").read_text(encoding="utf-8")
ns = {"np": np}
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {"_boxblur", "_nuc2d"}:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "v", "exec"), ns)
_nuc2d = ns["_nuc2d"]

def roi(p):
    a = np.frombuffer(p.read_bytes(), dtype="<u2").astype(np.float32)
    return a[:RAW_W*RAW_H].reshape(RAW_H, RAW_W)[ROI_Y:ROI_Y+240, ROI_X:ROI_X+320]

f = sorted(OUT.glob("run_*.raw"))[20]
im = roi(f); out = _nuc2d(im)
print(f"frame {f.name}  (gain 3, AGC locked)\n")

for tag, x in (("raw", im), ("NUC", out)):
    lo, hi = np.percentile(x, [1, 99])
    p1, p50, p99 = np.percentile(x, [1, 50, 99])
    print(f"{tag}: min {x.min():.0f}  p1 {p1:.0f}  median {p50:.0f}  p99 {p99:.0f}  max {x.max():.0f}")
    print(f"     1-99% span {p99-p1:.0f} DL   std {x.std():.0f} DL")

print("\n--- where is the heat? (5x5 block means, DL, NUC'd) ---")
h, w = out.shape
bh, bw = h//5, w//5
for r in range(5):
    cells = []
    for c in range(5):
        blk = out[r*bh:(r+1)*bh, c*bw:(c+1)*bw]
        cells.append(f"{blk.mean():7.0f}")
    print("   " + " ".join(cells))

print("\n--- hottest / coldest 3% of the frame ---")
flat = out.ravel(); idx = np.argsort(flat)
n = int(0.03*flat.size)
cold = flat[idx[:n]]; hot = flat[idx[-n:]]
print(f"   coldest 3%: mean {cold.mean():.0f} DL   hottest 3%: mean {hot.mean():.0f} DL"
      f"   delta {hot.mean()-cold.mean():.0f} DL")
ys, xs = np.unravel_index(np.argsort(flat)[-n:], out.shape)
print(f"   hottest region centre: row {ys.mean():.0f} col {xs.mean():.0f} of ({h},{w})")

print("\n--- does NUC invent structure? gradient energy inside flat areas ---")
lap = np.abs(np.diff(out, 2, axis=1))
print(f"   raw  mean|d2/dx2| {np.abs(np.diff(im,2,axis=1)).mean():.2f}")
print(f"   NUC  mean|d2/dx2| {lap.mean():.2f}   (haloing would raise this a lot)")

print("\n--- temporal noise on a static scene (pixel-wise, NUC'd) ---")
seq = [_nuc2d(roi(p)) for p in sorted(OUT.glob("run_*.raw"))[10:30]]
st = np.stack(seq)
print(f"   per-pixel std across 20 frames: median {np.median(st.std(axis=0)):.2f} DL"
      f"  max {st.std(axis=0).max():.2f} DL")
print(f"   -> this is the residual noise floor after NUC")
