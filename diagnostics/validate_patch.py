"""Validate the REAL _nuc2d from app/viewer.py.

viewer.py imports wx (and ctypes transports) at module scope, so we can't just
import it. Parse the file with ast, pull out the _boxblur and _nuc2d defs, and
exec only those - so we are testing the shipped code, not a transcription.
"""
import ast, pathlib, numpy as np
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
src = (ROOT/"app"/"viewer.py").read_text(encoding="utf-8")
tree = ast.parse(src)
want = {"_boxblur", "_nuc2d"}
ns = {"np": np}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in want:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "viewer.py", "exec"), ns)
missing = want - set(ns)
assert not missing, f"could not extract {missing}"
_nuc2d = ns["_nuc2d"]
print("extracted from app/viewer.py:", ", ".join(sorted(want)))

# ---- real frames ---------------------------------------------------------
RAW_W, RAW_H, ROI_X, ROI_Y = 342, 260, 1, 4
def load(n):
    raw = np.frombuffer((ROOT/n).read_bytes(), dtype="<u2").astype(np.float32)
    raw = raw[:RAW_W*RAW_H].reshape(RAW_H, RAW_W)
    return raw[ROI_Y:ROI_Y+240, ROI_X:ROI_X+320]

def col_step(im):
    return float(np.abs(np.diff(im.mean(axis=0))).mean())
def row_step(im):
    return float(np.abs(np.diff(im.mean(axis=1))).mean())

print("\n--- REAL FRAMES (stripe metric, lower = cleaner) ---")
print(f"{'frame':<26}{'col step':>10}{'row step':>10}{'std':>9}")
print("-"*55)
for n in ("frame_1790470060.raw", "frame_1790470565.raw"):
    im = load(n)
    out = _nuc2d(im)
    print(f"{n:<26}{col_step(im):>10.1f}{row_step(im):>10.1f}{im.std():>9.1f}   before")
    print(f"{'':<26}{col_step(out):>10.1f}{row_step(out):>10.1f}{out.std():>9.1f}   AFTER")

# ---- synthetic ground truth ---------------------------------------------
h, w = 240, 320
yy, xx = np.mgrid[0:h, 0:w]
rng = np.random.default_rng(11)
col = rng.normal(0,50,(1,w)).astype(np.float32)
row = rng.normal(0,30,(h,1)).astype(np.float32)
r = np.sqrt(((xx-w/2)/(w/2))**2 + ((yy-h/2)/(h/2))**2)
vig = (-420*(r**2)).astype(np.float32)
gain = 1.0 + rng.normal(0,0.008,(h,w)).astype(np.float32)
FPN = ((col+row+vig)*gain).astype(np.float32)

def sc_vert():
    s = (300+900*(yy/h)).astype(np.float32)
    s += 2600*np.exp(-(((xx-215)/26.)**2+((yy-150)/26.)**2)); return s
def sc_horiz():
    s = (300+1800*(xx/w)).astype(np.float32)
    s += 2600*np.exp(-(((xx-215)/26.)**2+((yy-150)/26.)**2))
    s -= 1500*np.exp(-(((xx-70)/20.)**2+((yy-90)/20.)**2)); return s

print("\n--- SYNTHETIC GROUND TRUTH (RMSE to true scene, lower = better) ---")
print(f"{'scene':<34}{'no-op':>9}{'patched':>10}{'change':>10}")
print("-"*63)
for nm, fn in (("vertical gradient", sc_vert), ("horizontal gradient", sc_horiz)):
    SC = fn()
    OBS = SC*gain + FPN + rng.normal(0,6,(h,w)).astype(np.float32)
    o = _nuc2d(OBS); o = o - o.mean() + SC.mean()
    b = float(np.sqrt(((OBS-SC)**2).mean()))
    a = float(np.sqrt(((o-SC)**2).mean()))
    print(f"{nm:<34}{b:>9.1f}{a:>10.1f}{a-b:>+10.1f}")

outdir = ROOT/"nuc_out"; outdir.mkdir(exist_ok=True)
def stretch(x):
    lo,hi = np.percentile(x,2),np.percentile(x,98)
    return (np.clip((x-lo)/max(1.,hi-lo),0,1)*255).astype(np.uint8)
im = load("frame_1790470060.raw")
Image.fromarray(np.hstack([stretch(im), stretch(_nuc2d(im))]),"L").save(outdir/"PATCHED_real_before_after.png")
print("\nwrote nuc_out/PATCHED_real_before_after.png  (before | after) - please eyeball this")
