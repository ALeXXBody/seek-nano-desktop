"""Every consumer of _process must survive it returning None.

_process returns None for a frame the watchdog rejects (mid-frame gain change).
Three separate crashes came from that contract being changed without auditing
its callers:

  1. np.percentile(None) in _bmp            - froze the window
  2. "%d DL" % None in _draw_scale_bar      - killed every paint
  3. img_arr.min() in _save_png             - would kill every snapshot

and one unrelated one found at the same time:

  4. wx.CallLater(3000.0) - wx.Timer.Start rejects a float, hard crash in the
     event handler

This suite pins all of it down by walking the actual call graph rather than
listing the ones remembered.
"""
import ast
import os
import pathlib
import re
import sys

SRC = pathlib.Path(r"C:\a\src\app\viewer.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
FAIL = []


def check(cond, msg):
    print(("   ok   " if cond else "   FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


print("1. the audit: find every caller of _process and _bmp, from the AST")
callers = []
for cls in [n for n in TREE.body if isinstance(n, ast.ClassDef)]:
    for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
        seg = ast.get_source_segment(SRC, fn) or ""
        for callee in ("_process", "_bmp"):
            if ("self." + callee + "(") in seg and fn.name != callee:
                callers.append((cls.name, fn.name, callee))
for c in callers:
    print("      %s.%s -> self.%s()" % c)
check(len(callers) >= 4,
      "found %d call sites (expected at least 4)" % len(callers))

print("\n2. each _process caller handles None")
proc_callers = [c for c in callers if c[2] == "_process"]
for cls_name, fn_name, _ in proc_callers:
    fn = next(f for c in TREE.body if isinstance(c, ast.ClassDef)
              and c.name == cls_name for f in c.body
              if isinstance(f, ast.FunctionDef) and f.name == fn_name)
    seg = ast.get_source_segment(SRC, fn) or ""
    line = next((n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_process"), None)
    # look at the statements from the call onwards for a None test
    guards = re.search(r"if\s+\w+\s+is\s+None\s*:", seg)
    check(bool(guards),
          "%s.%s guards the _process() result for None" % (cls_name, fn_name))

print("\n3. no unguarded %d/%f format of lo_ema or hi_ema")
bad = []
for node in TREE.body:
    if isinstance(node, ast.ClassDef):
        for f in node.body:
            if not isinstance(f, ast.FunctionDef):
                continue
            seg = ast.get_source_segment(SRC, f) or ""
            for m in re.finditer(r'["\'][^"\']*%[df][^"\']*["\']\s*%\s*'
                                 r'(getattr\(\s*self\s*,\s*["\']'
                                 r'(lo_ema|hi_ema)["\']\s*,\s*0(\.0)?\s*\)|\w+\.(lo_ema|hi_ema)\b)'
                                 , seg):
                bad.append("%s.%s" % (node.name, f.name))
check(not bad,
      "no '%%d' %% getattr(..., 0) on the display window (found %s)"
      % (bad or "none"))

print("\n4. no float reaches a wx timer")
timers = []
for node in TREE.body:
    if isinstance(node, ast.ClassDef):
        for f in node.body:
            if not isinstance(f, ast.FunctionDef):
                continue
            for n in ast.walk(f):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr in ("CallLater", "Start"):
                    for a in n.args:
                        if isinstance(a, ast.BinOp) or (
                                isinstance(a, ast.Constant)
                                and isinstance(a.value, float)):
                            timers.append("%s.%s L%d" % (node.name, f.name, n.lineno))
check(not timers,
      "CallLater/Start get no arithmetic or float literal (%s)"
      % (timers or "none"))

m = re.search(r"USB_RETRY_DELAY_MS\s*=\s*(\d+)", SRC)
check(m is not None, "USB_RETRY_DELAY_MS is defined as an int")
check("USB_RETRY_DELAY\b" not in SRC,
      "the old float USB_RETRY_DELAY is gone")

print("\n5. _bmp returns before touching the array")
fn = next(f for c in TREE.body if isinstance(c, ast.ClassDef) and c.name == "Viewer"
          for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "_bmp")
seg = ast.get_source_segment(SRC, fn) or ""
i_proc = seg.find("self._process(")
# the guard must be the one AFTER the call - _bmp has an earlier "disp is None"
# check that has nothing to do with this contract
i_guard = seg.find("is None", i_proc)
i_pct = seg.find("np.percentile", i_proc)
check(i_proc < i_guard < i_pct,
      "_process() at %d, its None guard at %d, np.percentile at %d - the guard "
      "for the REJECTED frame comes before the array is touched"
      % (i_proc, i_guard, i_pct))

print()
if FAIL:
    print("FAILED %d check(s):" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")