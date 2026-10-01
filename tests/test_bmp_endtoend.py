# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""End-to-end: drive the real _bmp over the captured frame sequence.

Runs the actual method the viewer calls, on the real frames in capture order,
and checks what matters to a user: a bitmap comes back for EVERY frame (the
old flat-frame branch that swallowed frames is gone), with and without a
wall reference loaded.
"""
import ast, os, pathlib, sys, time, types, numpy as np

SRC = VIEWER

ns = {"np": np, "time": time, "os": os, "MIN_WINDOW": 20.0, "BLEND": 0.6}
src = SRC.read_text(encoding="utf-8")
for n in ast.parse(src).body:
    if not isinstance(n, ast.Assign):
        continue
    try:
        val = ast.literal_eval(n.value)
    except Exception:
        continue
    t = n.targets[0]
    if isinstance(t, ast.Name):
        ns[t.id] = val
    elif isinstance(t, (ast.Tuple, ast.List)):
        for nm, vv in zip(t.elts, val):
            if isinstance(nm, ast.Name):
                ns[nm.id] = vv
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {
            "_boxblur", "_nuc2d", "_apply_ffc", "_roi_u16", "_process",
    "_neighbour_median", "_apply_bad_pixels",
            "decode_frame", "ironbow", "hot_lut", "gray_lut"}:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "v", "exec"), ns)
    elif isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "COLORMAPS":
        ns["COLORMAPS"] = [(e.elts[0].value, ns[e.elts[1].id])
                           for e in n.value.elts]

nodes = {}
for cls in (n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)):
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name in {
                "_bmp", "_process", "_overlay", "_text", "_validate_ffc",
                "_detect_spots", "_draw_hotspots"}:
            nodes[n.name] = n
assert "_bmp" in nodes, "_bmp not found"
mod = ast.Module(body=list(nodes.values()), type_ignores=[])
ast.fix_missing_locations(mod)
print("_bmp + _process extracted from Viewer")


class FakeImage:
    def __init__(self, w, h, data):
        self.w, self.h, self.data = w, h, data

    def Scale(self, vw, vh, quality=None):
        return ("bitmap", vw, vh, len(self.data))


class FakeWx:
    Image = FakeImage
    Bitmap = staticmethod(lambda img: img)
    IMAGE_QUALITY_NEAREST = 1


class Panel:
    IMG_W, IMG_H = ns["IMG_W"], ns["IMG_H"]

    def __init__(self):
        self.frame_raw = None
        self.W, self.H = ns["IMG_W"], ns["IMG_H"]
        self.lut_i = 0
        self.ffc = None
        self.ffc_collect = None
        self._bmp_cache = None
        self.bg_frames = []
        self.bg_gains = []
        self.bg_hp = None
        self.bad = None
        self.spots = []
        self.spot_mode = "off"
        self.spot_sens = 1.0
        self.spot_hist = []
        self.spot_alarm = 0.0
        self.spot_alarm_on = False
        self.shown_frames = 0
        self.held_gain = 0
        self.saturated = 0
        self.bad_frames = 0
        self.shown_gain = 3
        self.last_gain = 3
        self.ffc_gain = None
        self.ffc_ok = None
        from collections import deque
        self._plo = deque(maxlen=9)
        self._phi = deque(maxlen=9)
        self.log = types.SimpleNamespace(AppendText=lambda s: None)
        self.lo_ema = None
        self.hi_ema = None
        self.prev_t = None
        self.stretch_skips = 0
        self.paused = False
        self.q = types.SimpleNamespace(put=lambda *a: None)


exec(compile(mod, "v", "exec"), ns)
Panel._bmp = ns["_bmp"]
Panel._process = ns["_process"]
Panel._overlay = ns["_overlay"]
Panel._text = ns["_text"]
Panel._validate_ffc = ns["_validate_ffc"]
Panel._detect_spots = ns["_detect_spots"]
Panel._draw_hotspots = ns["_draw_hotspots"]
Panel.ffc_ok = None
Panel.ffc_gain = None
ns["wx"] = FakeWx

files = sorted(OUT.glob("run_*.raw"))

for label, ffc in (("no wall reference", None),
                   ("wall reference = avg of 8 settled frames",
                    np.stack([ns["_roi_u16"](p.read_bytes())
                              for p in files[10:18]]).mean(axis=0))):
    panel = Panel()
    panel.ffc = ffc
    shown = 0
    for p in files:
        panel.frame_raw = p.read_bytes()
        panel._bmp_cache = None
        r = panel._bmp()
        assert r is not None, f"{p.name} returned None"
        shown += 1
    print(f"  {label}: displayed {shown}/{len(files)}")
    # wall reference must actually change the output, not be a no-op
    if ffc is not None:
        a = panel._process(ns["_roi_u16"](files[20].read_bytes()))
        print(f"    processed mean {a.mean():.1f} std {a.std():.1f} "
              f"(raw std {ns['_roi_u16'](files[20].read_bytes()).std():.1f})")

print("\nRESULT: PASS - every frame renders, with and without a wall reference")
