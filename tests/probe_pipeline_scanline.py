"""Where does the scanline artifact come from?

Established so far, by measurement:
  - it is not the overlay. The artifact is a corrupted row of IMAGE DATA, so no
    drawing code can produce it, hot spots included
  - it is not the camera or the USB transfer: 60 frames pulled straight off the
    wire with no viewer running contain no such row

So it is added somewhere in the processing pipeline. This walks one real frame
through each stage of the actual _process and scores the scanline signature
after every stage, so the stage that introduces it is named rather than guessed.
"""
import ast
import pathlib
import re
import sys

import numpy as np

sys.path.insert(0, r"C:\a\src\app")

SRC = pathlib.Path(r"C:\a\src\app\viewer.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
NS = {"np": np, "sys": sys, "os": __import__("os")}
# Module constants, including tuple unpacking like `RAW_W, RAW_H = 342, 260`,
# which a line regex misses and which the extracted functions reference.
for node in TREE.body:
    if not isinstance(node, ast.Assign):
        continue
    try:
        val = eval(compile(ast.Expression(node.value), "<c>", "eval"),
                   dict(NS))
    except Exception:
        continue
    for tgt in node.targets:
        if isinstance(tgt, ast.Name):
            NS[tgt.id] = val
        elif isinstance(tgt, ast.Tuple) and isinstance(val, (tuple, list)):
            # `RAW_W, RAW_H = 342, 260`
            for sub, v in zip(tgt.elts, val):
                if isinstance(sub, ast.Name):
                    NS[sub.id] = v

WANT = {"_roi_u16", "_nuc2d", "_boxblur", "_apply_ffc", "_apply_bad_pixels",
        "_neighbour_median", "_detect_bad_pixels", "_line_profile"}
for node in [n for n in TREE.body if isinstance(n, ast.FunctionDef)]:
    if node.name in WANT:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "v", "exec"), NS)

raw = np.fromfile(r"C:\a\src\data\frame_1790470060.raw", dtype="<u2")
RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
full = raw.reshape(RH, RW)

img = NS["_roi_u16"](raw.tobytes())
print("start: %s" % (img.shape,))


def score(f, label):
    """Scanline signature: rows alternating far more than their neighbours."""
    step = np.abs(np.diff(f.astype(np.float32), axis=1)).mean(axis=1)
    med = float(np.median(step))
    mad = float(np.median(np.abs(step - med)))
    scale = max(med + 3 * mad, 1.0)
    bad = [(i, float(step[i])) for i in range(len(step)) if step[i] > 6 * scale]
    print("  %-34s median step %8.2f   flagged rows: %s"
          % (label, med, bad[:6] if bad else "none"))
    return len(bad)


score(img, "after _roi_u16 (raw ROI)")

# 3x3 median, exactly as the pipeline does it
p1 = np.pad(img, 1, mode="edge")
stack = np.stack([p1[dy:dy + IH, dx:dx + IW] for dy in range(3) for dx in range(3)])
med = np.partition(stack, 4, axis=0)[4]
score(med, "after 3x3 median")

nuc = NS["_nuc2d"](med.copy())
score(nuc, "after _nuc2d")

# flat-field subtraction on the median image
ffc = np.fromfile(r"C:\a\src\data\ffc_latest.raw",
                  dtype="<u2").reshape(IH, IW).astype(np.float32)
ffcd = NS["_apply_ffc"](med.copy(), ffc)
score(ffcd, "after _apply_ffc")

print()
print("If every stage reports 'none', the artifact does not appear in this")
print("recorded frame and only shows live - which would point at the gain")
print("saturation the HUD was reporting (GAIN 3 SATURATED) rather than the")
print("spatial pipeline.")
print()
print("live gain/saturation state from the last verbose log:")
try:
    for line in open(r"C:\a\seeknano_verbose.log", errors="ignore"):
        if "saturated" in line and "shown" in line:
            print("   " + line.strip()[-90:])
            break
except Exception as e:
    print("   (no log: %r)" % (e,))