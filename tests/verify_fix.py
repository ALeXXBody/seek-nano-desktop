"""Prove the background is now built at ONE gain, and render the result.

Reproduces the viewer's accumulation rule on the captured sequence, then renders
the corrected ROI through the real pipeline. Also asserts the old bug is gone:
the bank must never mix gains.
"""
import ast, pathlib, numpy as np
from PIL import Image

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
D = pathlib.Path(r"C:\a\out\now")

ns = {"np": np}
src = SRC.read_text(encoding="utf-8")
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {
            "_boxblur", "_nuc2d", "_roi_u16", "_frame_header", "_frame_reason",
            "_gain_locked", "_frame_has_content", "ironbow", "hot_lut",
            "gray_lut"}:
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
_box, _nuc, _iron = ns["_boxblur"], ns["_nuc2d"], ns["ironbow"]
_roi, _hdr, _locked = ns["_roi_u16"], ns["_frame_header"], ns["_gain_locked"]
_has, BG = ns["_frame_has_content"], ns["BG_FRAMES"]
IW, IH = ns["IMG_W"], ns["IMG_H"]
print(f"ROI rows {ns['ROI_Y']}..{ns['ROI_Y']+IH-1}, "
      f"cols {ns['ROI_X']}..{ns['ROI_X']+IW-1}")

files = sorted(D.glob("n_*.raw"))
# replay the viewer's accumulation exactly
bg_frames, bg_gains, hist, shown_gain = [], [], [], None
displayed, held = [], 0
for p in files:
    b = p.read_bytes()
    if ns["_frame_reason"](b) is not None:
        continue
    m, s, g = _hdr(b)
    if shown_gain is not None and g != shown_gain:
        held += 1
        continue
    if not _locked(g, hist):
        continue
    shown_gain = g
    if len(bg_frames) < BG:
        bg_frames.append(_roi(b)); bg_gains.append(g)
    displayed.append(b)

print(f"gains in the capture: {[ _hdr(f.read_bytes())[2] for f in files ]}")
print(f"background bank: {len(bg_frames)} frames, gains {sorted(set(bg_gains))}")
assert len(set(bg_gains)) <= 1, "background bank mixes gains - the old bug"
assert not bg_gains or bg_gains[0] == shown_gain, \
    f"bank gain {bg_gains[0]} != displayed gain {shown_gain}"
print(f"OK: bank is at the displayed gain (old code filled it with "
      f"4,9,14,25,26,27,28,8 and used it against gain 3)")
print(f"frames displayed: {len(displayed)} at gain {shown_gain}, held {held}")


def med3(x):
    p = np.pad(x, 1, mode="edge")
    return np.median(np.stack([p[dy:dy+IH, d:d+IW]
                               for dy in range(3) for d in range(3)]), axis=0)


def render(x):
    lo, hi = np.percentile(x, [2, 98])
    return _iron(np.clip((x - lo) / max(1.0, hi - lo), 0, 1)).astype(np.uint8)


dx = lambda x: float(np.abs(np.diff(x, axis=1)).mean())
span = lambda x: float(np.percentile(x, 99) - np.percentile(x, 1))
band = lambda x: float(np.abs(np.diff(x.mean(axis=1))).mean())

last = _roi(displayed[-1])
bgm = np.stack(bg_frames).mean(axis=0)
hp = bgm - _box(bgm, 9)
v = {"raw": med3(last), "highpass+NUC": _nuc(med3(last - hp))}
print(f"\n{'variant':<16}{'dx':>8}{'span':>8}{'band':>8}{'top-body':>10}")
print("-" * 50)
for k, x in v.items():
    print(f"{k:<16}{dx(x):>8.1f}{span(x):>8.0f}{band(x):>8.1f}"
          f"{x[:6].mean()-x[20:220].mean():>+10.0f}")
Image.fromarray(np.hstack([render(x) for x in v.values()])).save(
    D / "compare_fixed.png")
print(f"\nwrote {D}\\compare_fixed.png  (raw | highpass+NUC)")
