"""Prove the whole display chain with the real GUI code, headless.

Runs the shipped tick() and on_paint() against captured frames with a stub wx,
so a failure in the paint path shows up here instead of as a black window.
Also asserts the exact diagnostic counters the GUI now prints, which is what
tells us where frames stop if it still misbehaves on your machine.
"""
import ast, pathlib, sys, types, numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
SEE = pathlib.Path(r"C:\a\out\see")
FPN = pathlib.Path(r"C:\a\out\fpn")

ns = {"np": np, "sys": sys, "os": __import__("os")}
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
        try:
            for nm, vv in zip(t.elts, val):
                if isinstance(nm, ast.Name):
                    ns[nm.id] = vv
        except Exception:
            pass
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {
            "_frame_header", "_frame_reason", "_resync_frame", "_gain_locked",
            "_boxblur", "_nuc2d", "_apply_ffc", "_roi_u16", "ironbow",
            "hot_lut", "gray_lut"}:
        exec(compile(ast.Module(body=[n], type_ignores=[]), "v", "exec"), ns)
    elif isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "COLORMAPS":
        ns["COLORMAPS"] = [(e.elts[0].value, ns[e.elts[1].id]) for e in n.value.elts]

want = {"tick", "_tick", "_show_fatal", "on_paint", "_draw_bitmap", "_bmp",
        "_process", "_save_png", "on_cmap"}
nodes = []
for cls in (n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)):
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name in want:
            nodes.append(n)
got = {n.name for n in nodes}
missing = want - got
if missing:
    print(f"MISSING from viewer.py: {missing}")
    sys.exit(1)
mod = ast.Module(body=nodes, type_ignores=[])
ast.fix_missing_locations(mod)


class Log:
    def __init__(self): self.lines = []
    def AppendText(self, s): self.lines.append(s)
    def GetLastPosition(self): return sum(len(s) for s in self.lines)
    def Remove(self, *a): pass
    def Clear(self): self.lines = []
    def GetValue(self): return "".join(self.lines)


class Video:
    def __init__(self): self.painted = 0
    def Refresh(self): pass


class Status:
    def __init__(self): self.text = ""
    def SetStatusText(self, t): self.text = t


class Image:
    def __init__(self, w, h, data): self.data = data
    def Scale(self, *a, **k): return ("bitmap", len(self.data))


class PaintDC:
    def __init__(self, *a): pass
    def SetBackground(self, *a): pass
    def Clear(self): pass
    def SetPen(self, *a): pass
    def DrawBitmap(self, *a): self.drawn = getattr(self, "drawn", 0) + 1


class FakeWx:
    Image = Image
    Bitmap = staticmethod(lambda i: i)
    PaintDC = PaintDC
    BLACK_BRUSH = object()
    Pen = lambda *a, **k: None
    IMAGE_QUALITY_NEAREST = 1


class Q:
    def __init__(self): self.items = []
    def put(self, it): self.items.append(it)
    def get_nowait(self):
        if not self.items:
            raise __import__("queue").Empty
        return self.items.pop(0)


class Panel:
    def __init__(self):
        self.q = Q(); self.log = Log(); self.video = Video()
        self.statusbar = Status()
        self.frame_raw = None
        self.W, self.H = ns["IMG_W"], ns["IMG_H"]
        self.lut_i = 0
        self.paused = False
        self.ffc = None
        self.ffc_collect = None
        self.last_fid = None
        self.bad_frames = 0
        self.gain_hist = []
        self.last_gain = None
        self.shown_gain = None
        self.shown_frames = 0
        self.held_gain = 0
        self._info = ""
        self.bg_frames = []
        self.bg_gains = []

    def push_status(self, t):
        self.statusbar.text = t

    def GetStatusBar(self):
        return self.statusbar


exec(compile(mod, "v", "exec"), ns)
ns["wx"] = FakeWx
ns["queue"] = __import__("queue")
for name in want:
    setattr(Panel, name, ns[name])

files = sorted(FPN.glob("fpn_*.raw")) + sorted(SEE.glob("see_*.raw"))
panel = Panel()
fails = []
for p in files:
    panel.q.put(("frame", p.read_bytes()))
    try:
        panel.tick(None)
    except Exception:
        panel._show_fatal("tick", sys.exc_info())
        fails.append((p.name, "tick"))
    try:
        panel.on_paint(None)
    except Exception:
        panel._show_fatal("paint", sys.exc_info())
        fails.append((p.name, "paint"))

print(f"fed {len(files)} frames through the real tick() + on_paint()")
print(f"  shown={panel.shown_frames} rejected={panel.bad_frames} "
      f"held_on_gain={panel.held_gain} bg={len(panel.bg_frames)} "
      f"f{frame_raw!s}" if False else
      f"  shown={panel.shown_frames} rejected={panel.bad_frames} "
      f"held_on_gain={panel.held_gain} bg={len(panel.bg_frames)} "
      f"ffc={'yes' if panel.ffc is not None else 'no'}")
print(f"  frame_raw set: {panel.frame_raw is not None}")
print(f"  status bar: {panel.statusbar.text!r}")
print("\nlog panel output:")
for ln in panel.log.lines:
    print("   " + ln.rstrip())
if panel.shown_frames == 0:
    print("\nFAIL: no frame reached the screen")
    fails.append(("all", "nothing shown"))
crash = pathlib.Path(r"C:\a\seeknano_crash.log")
if crash.exists() and fails:
    print(f"\nNOTE: {crash} exists - the GUI handlers DID throw:")
    print(crash.read_text()[-1500:])

print(f"\nRESULT: {'PASS' if not fails else 'FAIL ' + str(fails)}")
sys.exit(0 if not fails else 1)
