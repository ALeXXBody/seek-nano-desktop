"""Validate the patched _nuc2d on SETTLED live frames.

diag_gain_cycle.py showed the camera's AGC converges to gain 3 after ~4 frames
and then holds mean brightness to +/-2.7 DL out of 6535 (0.04%). So use only
frames from the locked tail, which are the ones a viewer would actually show.
"""
import ast, pathlib, numpy as np
from PIL import Image

ROOT = pathlib.Path(r"C:\a\src")
OUT  = pathlib.Path(r"C:\a\out")
RAW_W, RAW_H, ROI_X, ROI_Y = 342, 260, 1, 4

src = (ROOT/"app"/"viewer.py").read_text(encoding="utf-8")
tree = ast.parse(src)
ns = {"np": np}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in {"_boxblur", "_nuc2d"}:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "viewer.py", "exec"), ns)
_nuc2d = ns["_nuc2d"]
print("extracted _nuc2d from app/viewer.py\n")

files = sorted(OUT.glob("run_*.raw"))
settled = files[6:]                       # skip the AGC transient
print(f"using {len(settled)} settled frames (skipping first 6 of {len(files)})\n")

def roi(p):
    a = np.frombuffer(p.read_bytes(), dtype="<u2").astype(np.float32)
    return a[:RAW_W*RAW_H].reshape(RAW_H, RAW_W)[ROI_Y:ROI_Y+240, ROI_X:ROI_X+320]
colstep = lambda im: float(np.abs(np.diff(im.mean(axis=0))).mean())
rowstep = lambda im: float(np.abs(np.diff(im.mean(axis=1))).mean())

imgs = [roi(p) for p in settled]
print(f"{'frame':<14}{'col before':>12}{'after':>9}{'x':>7}"
      f"{'row before':>12}{'after':>9}{'x':>7}")
print("-"*70)
outs = []
for p, im in zip(settled, imgs):
    o = _nuc2d(im); outs.append(o)
    print(f"{p.name:<14}{colstep(im):>12.1f}{colstep(o):>9.1f}{colstep(im)/max(1e-9,colstep(o)):>6.1f}x"
          f"{rowstep(im):>12.1f}{rowstep(o):>9.1f}{rowstep(im)/max(1e-9,rowstep(o)):>6.1f}x")

# temporal stability: this is what "stable" means for a live viewer
print("\nframe-to-frame difference (mean |delta| in DL):")
for tag, seq in (("raw", imgs), ("NUC", outs)):
    d = [float(np.abs(a-b).mean()) for a, b in zip(seq, seq[1:])]
    print(f"  {tag:<4} median {np.median(d):7.2f}  mean {np.mean(d):7.2f}  max {np.max(d):7.2f}")

# fixed-pattern noise: after NUC, is the column structure static across frames?
print("\ncolumn-offset stability (median column profile, frame-to-frame drift):")
cp = lambda x: np.median(x, axis=0)
d = [float(np.abs(cp(a)-cp(b)).mean()) for a, b in zip(outs, outs[1:])]
print(f"  NUC'd column profile drift: median {np.median(d):.3f} DL  max {np.max(d):.3f} DL")
d0 = [float(np.abs(cp(a)-cp(b)).mean()) for a, b in zip(imgs, imgs[1:])]
print(f"  raw  column profile drift: median {np.median(d0):.3f} DL  max {np.max(d0):.3f} DL")

d = OUT/"nuc"; d.mkdir(exist_ok=True)
def stretch(x):
    lo, hi = np.percentile(x, [1, 99])
    return (np.clip((x-lo)/max(1., hi-lo), 0, 1)*255).astype(np.uint8)
for i, (a, b) in enumerate(zip(imgs, outs)):
    Image.fromarray(np.hstack([stretch(a), stretch(b)]), "L").save(d/f"settled_{i:02d}_before_after.png")
print(f"\nwrote {d}/settled_*_before_after.png  (left raw, right NUC)")
print(f"a settled frame on disk: {settled[0]}  ({settled[0].stat().st_size} B)")
