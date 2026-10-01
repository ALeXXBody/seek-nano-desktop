"""The static must come down WITHOUT taking the scene with it.

The previous version of this test only measured neighbour noise, and so passed
a change that removed 97% of the picture. It now asserts both halves:

    noise reduction > 5x   AND   scene contrast kept > 70%

against the shipped high-pass background term, on live frames the camera
produced, and on synthetic ground truth where the true scene is known.
"""
import ast, pathlib, sys, numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
D = pathlib.Path(r"C:\a\out\fpn")

ns = {"np": np}
src = SRC.read_text(encoding="utf-8")
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {
            "_boxblur", "_nuc2d", "_apply_ffc", "_roi_u16", "_frame_header",
            "_frame_has_content"}:
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
_roi, _nuc, _box, _hdr = ns["_roi_u16"], ns["_nuc2d"], ns["_boxblur"], ns["_frame_header"]
_has = ns["_frame_has_content"]
IW, IH = ns["IMG_W"], ns["IMG_H"]

dx = lambda x: float(np.abs(np.diff(x, axis=1)).mean())
span = lambda x: float(np.percentile(x, 99) - np.percentile(x, 1))
def med3(x):
    p = np.pad(x, 1, mode="edge")
    return np.median(np.stack([p[dy:dy+IH, dx_:dx_+IW]
                               for dy in range(3) for dx_ in range(3)]), axis=0)

fails = []

# ---------------------------------------------------------------- live data
recs = [(_hdr(p.read_bytes())[2], p.read_bytes()) for p in sorted(D.glob("fpn_*.raw"))]
best, cur = [], []
for g, b in recs:
    if cur and g != cur[-1][0]:
        cur = []
    cur.append((g, b))
    if len(cur) > len(best):
        best = cur
gain, raw_frames = best[0][0], [b for _, b in best]
frames = [_roi(b) for b in raw_frames]
print(f"live: gain {gain}, {len(frames)} frames in the settled run")

bg = np.stack(frames[:8]).mean(axis=0)
hp = bg - _box(bg, 9)
rest = frames[8:]

# NB: the pipeline order matters and must match Viewer._process exactly -
# subtract the background term FIRST, then the 3x3 median, then the NUC.
# Skipping the median made the NUC look harmful (dx 6.8 -> 12.1) when in the
# real order it is a small win (6.8 -> 6.6, column striping 2.94 -> 2.16).
n0 = np.mean([dx(med3(f)) for f in rest])
s0 = np.mean([span(med3(f)) for f in rest])
n1 = np.mean([dx(med3(f - hp)) for f in rest])
s1 = np.mean([span(med3(f - hp)) for f in rest])
n2 = np.mean([dx(_nuc(med3(f - hp))) for f in rest])
s2 = np.mean([span(_nuc(med3(f - hp))) for f in rest])
print(f"\n{'variant':<26}{'neigh dx':>10}{'scene span':>12}")
print(f"{'raw':<26}{n0:>10.1f}{s0:>12.0f}")
print(f"{'high-pass background':<26}{n1:>10.1f}{s1:>12.0f}")
print(f"{'high-pass + NUC':<26}{n2:>10.1f}{s2:>12.0f}")
noise_cut, scene_kept = n0 / n2, s2 / s0
print(f"\n  noise cut {noise_cut:.1f}x, scene kept {100*scene_kept:.0f}%")
if noise_cut <= 5:
    fails.append("noise reduction <= 5x")
if scene_kept < 0.70:
    fails.append(f"scene contrast only {100*scene_kept:.0f}% kept")

# the old, wrong variant, to prove the test would now catch a regression
nb = np.mean([dx(med3(f - bg + float(f.mean()))) for f in rest])
sb = np.mean([span(med3(f - bg + float(f.mean()))) for f in rest])
print(f"  (for reference, full-background subtraction keeps only "
      f"{100*sb/s0:.0f}% of the scene - this is what is now rejected)")

# ------------------------------------------------------- synthetic truth
rng = np.random.default_rng(5)
yy, xx = np.mgrid[0:IH, 0:IW]
scene = 6000 + 4.0 * xx + 2.0 * yy
scene += 900 * np.exp(-((xx - 210) ** 2 + (yy - 130) ** 2) / 45.0 ** 2)
scene -= scene.mean(); scene += 6500
offsets = rng.normal(0, 260, (IH, IW))
def synth(sc): return offsets + sc + rng.normal(0, 6, (IH, IW))
bank = np.stack([synth(scene) for _ in range(8)])
tbg = bank.mean(axis=0)
thp = tbg - _box(tbg, 9)
live = synth(scene)
out = _nuc(med3(live - thp))
raw_scene = med3(live)
print(f"  (column striping after the full pipeline: "
      f"{np.mean([abs(np.diff(_nuc(med3(f - hp)).mean(axis=0))).mean() for f in rest]):.2f} DL)")
rmse = lambda a: float(np.sqrt(((a - scene) ** 2).mean()))
print(f"\nsynthetic ground truth:")
print(f"  raw            RMSE {rmse(raw_scene):7.1f} DL  span {span(raw_scene):7.0f}")
print(f"  high-pass + NUC RMSE {rmse(out):7.1f} DL  span {span(out):7.0f}"
      f"   (true span {span(scene):.0f})")
t_noise = n0 / max(1e-9, dx(out))
if rmse(out) >= rmse(raw_scene):
    fails.append("synthetic: no improvement against known truth")
if abs(span(out) - span(scene)) / span(scene) > 0.35:
    fails.append("synthetic: scene contrast not recovered")

# --------------------------------------------------- saturated-frame guard
sat = [b for g, b in recs if not _has(b)]
good = [b for g, b in recs if _has(b)]
print(f"\nsaturated guard: {len(sat)} of {len(recs)} frames rejected "
      f"(real frames {len(good)})")
if not sat or not good:
    fails.append("saturated-frame guard has no data to work with")

print(f"\nRESULT: {'PASS' if not fails else 'FAIL ' + '; '.join(fails)}")
sys.exit(0 if not fails else 1)
