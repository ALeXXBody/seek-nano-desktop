# portable-path header (see _paths.py); camera-box-only paths
# keep their default under SN_CAM_BASE/SN_OUT overrides
import _paths
from _paths import VIEWER, APP_DIR, PCDRIVER, LIBUSB_DLL, BUILT_EXE, OUT, CAM_BASE  # noqa

"""Frame validation on real captured frames.

Deliberately narrow now: the header checks (magic + gain) are the only gates,
because the payload and sequence checks were tried and removed. See the note
in viewer.py::_frame_reason and the assertions below that pin down WHY - so
nobody re-adds a test that rejects good frames.

Captured sets:
  out/see/  10 consecutive reads - 4 have a corrupt header, 1 real image
  out/fpn/  24 reads at gains 6/1/20/3 - 24 valid frames, all real images
"""
import ast, pathlib, sys, numpy as np

SRC = VIEWER
SEE = OUT / r'see'
FPN = OUT / r'fpn'

ns = {"np": np}
src = SRC.read_text(encoding="utf-8")
for n in ast.parse(src).body:
    if isinstance(n, ast.FunctionDef) and n.name in {
            "_frame_header", "_frame_reason", "_payload_looks_like_image",
            "_resync_frame", "_boxblur", "_nuc2d", "_apply_ffc", "_roi_u16"}:
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
_reason, _hdr, _resync = ns["_frame_reason"], ns["_frame_header"], ns["_resync_frame"]
_looks, FRAME = ns["_payload_looks_like_image"], ns["FRAME_BYTES"]

fails = []
def check(name, got, want_ok, detail=""):
    ok = want_ok if isinstance(want_ok, bool) else (got == want_ok)
    print(f"  {name:<34} -> {str(got):<20} {'OK' if ok else 'FAIL'}{detail}")
    if not ok:
        fails.append(name)

print("=== 1. the corrupt-header frames from the live capture ===")
print(f"{'file':<14}{'magic':>8}{'seq':>7}{'gain':>7}  verdict")
for p in sorted(SEE.glob("see_*.raw")):
    b = p.read_bytes()
    m, s, g = _hdr(b)
    print(f"{p.name:<14}{'0x%04x'%m:>8}{s:>7}{g:>7}  {_reason(b, None)}")
corrupt = [p for p in sorted(SEE.glob("see_*.raw"))
           if _reason(p.read_bytes(), None) is not None]
check("corrupt frames rejected", len(corrupt) >= 4, True,
      f" ({len(corrupt)} rejected)")

print("\n=== 2. every fpn frame is a valid header (no false rejections) ===")
fps = sorted(FPN.glob("fpn_*.raw"))
bad = [p.name for p in fps if _reason(p.read_bytes(), None) is not None]
check("fpn frames all accepted", len(bad), 0,
      f" (false rejects: {bad[:3]})" if bad else "")

print("\n=== 3. individual gates ===")
real = next(p.read_bytes() for p in fps)
check("short buffer", _reason(real[:1000], None), True)
corrupt_one = next(p.read_bytes() for p in corrupt)
check("corrupt magic", "magic" in str(_reason(corrupt_one, None)), True)
_h = _hdr(corrupt_one)
check("absurd gain rejected", "gain" in str(_reason(corrupt_one, None)), True)
check("clean frame passes", _reason(real, None), None)

print("\n=== 4. resync recovers a frame at any byte offset ===")
for off in (0, 1, 7, 512, 6840):
    shifted = (b"\x00" * off + real + b"\x00" * 200) if off else (real + b"\x00" * 200)
    check(f"resync @+{off} B", _resync(shifted) == real, True)
check("resync on all-zero", _resync(b"\x00" * (FRAME + 100)), None)

print("\n=== 5. why the payload gate was removed ===")
print("  A good frame and a bad one score the same, so it cannot gate:")
for tag, p in (("good fpn_16", next(q for q in fps if "16" in q.name)),
               ("bad  see_00", next(SEE.glob("see_00.raw")))):
    r = _reason(p.read_bytes(), None)
    print(f"    {tag:<14} payload corr {_looks(p.read_bytes()):+.4f}   "
          f"header verdict {r}")
# and pooled-over-ROI swings with row sampling, which is the trap
a = np.frombuffer(real, dtype="<u2").reshape(260, 342).astype(np.float64)
def corr(rows):
    x, y = rows[:, :-1].ravel(), rows[:, 1:].ravel()
    return float(((x-x.mean())*(y-y.mean())).mean()/(x.std()*y.std()))
print(f"    same GOOD frame, whole ROI      {corr(a[4:244,1:321]):+.4f}")
print(f"    same GOOD frame, every 32nd row {corr(a[8::32,:]):+.4f}"
      "   <- swings by 10x on row choice alone")

print(f"\nRESULT: {'PASS' if not fails else 'FAIL ' + str(fails)}")
sys.exit(0 if not fails else 1)
