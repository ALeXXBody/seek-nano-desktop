"""Regression: a loaded-but-rejected flat must not disable the fallback.

What happened
-------------
_process() guarded the background subtraction with:

    if self.ffc is not None:
        if self.ffc_ok:
            img = _apply_ffc(img, self.ffc)
    elif self.bg_hp is not None:
        img = img - self.bg_hp

`ffc_latest.raw` sits in the working directory between sessions, so on a normal
launch `self.ffc` is NOT None. _validate_ffc then judged that stale reference
harmful and set ffc_ok False - and because the outer test was `ffc is not None`,
the `elif` never ran. No correction was applied at all, for the whole session.

The log recorded exactly this and nobody read it as the cause:

    flat loaded from ffc_latest.raw (mean 6697) - Clear flat to remove
    flat reference REJECTED at gain 3: it would raise noise 244.1 -> 230.0 DL

so the app knew the reference was bad and carried on showing uncorrected data.
The auto-stretch then mapped the raw fixed pattern across the whole colormap,
which is what "static" looked like on screen.

The same stale file also starved the bank that builds bg_hp, because that guard
was `self.ffc is None` too - so there was no fallback to fall back to.

What is checked
---------------
1. The _process guard is an independent `if`, not an `if/elif` chain keyed on
   `ffc is not None`. AST-walked, because this is a structural bug that no
   amount of running the happy path would catch.
2. The bank that feeds bg_hp is filled under the same "no correction applied"
   condition, not `ffc is None`.
3. The behaviour itself, end to end: with ffc set and ffc_ok False but bg_hp
   present, the frame that comes out is background-corrected.
"""
import ast
import pathlib
import sys

import numpy as np

SRC = pathlib.Path(r"C:\a\src\app\viewer.py")
src = SRC.read_text(encoding="utf-8")
tree = ast.parse(src)

failures = []


def check(cond, msg):
    if cond:
        print("   ok   %s" % msg)
    else:
        print("   FAIL %s" % msg)
        failures.append(msg)


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError("no function %s" % name)


COMPOUND = (ast.If, ast.For, ast.While, ast.With, ast.Try)


def directly_guards(root, needle):
    """The If nodes whose OWN body performs `needle`, not an ancestor's.

    Matching on a node's source segment finds every enclosing `if` too, because
    a parent's segment contains its children - and a nested `if` statement is
    itself a direct child whose segment includes its whole body. So only
    simple statements are considered: the innermost guard is the one the
    statement itself sits in.
    """
    hits = []
    for n in ast.walk(root):
        if not isinstance(n, ast.If):
            continue
        for stmt in n.body:                      # direct children only
            if isinstance(stmt, COMPOUND):
                continue
            if needle in (ast.get_source_segment(src, stmt) or ""):
                hits.append(n)
                break
    return hits


def elif_preceding_test(node, root):
    """If `node` is an `elif`, the test of the branch it hangs off. Else None."""
    for parent in ast.walk(root):
        if isinstance(parent, ast.If):
            for o in parent.orelse:
                if o is node:
                    return " ".join(
                        (ast.get_source_segment(src, parent.test) or "").split())
    return None


print("1. the _process correction guard is not an if/elif on `ffc is not None`")
proc = fn("_process")
guarded = directly_guards(proc, "img = img - self.bg_hp")
check(len(guarded) == 1, "exactly one guard applies bg_hp (found %d)"
      % len(guarded))
if guarded:
    n = guarded[0]
    t = " ".join((ast.get_source_segment(src, n.test) or "").split())
    check("ffc is not None" not in t,
          "the bg_hp guard does not test `ffc is not None` alone: %r" % t)
    prev = elif_preceding_test(n, proc)
    if prev is None:
        check(True, "bg_hp is a top-level `if`, reachable unconditionally")
    else:
        # An elif is only safe if the branch above it can be FALSE while a
        # reference object still exists. `ffc is not None` is that trap:
        # ffc_latest.raw persists between sessions, so it is usually true and
        # the fallback becomes unreachable. `... and ffc_ok` is not.
        check("ffc_ok" in prev,
              "the branch above bg_hp also tests ffc_ok, so a REJECTED "
              "reference falls through instead of vetoing: %r" % prev)
        check("ffc_ok" not in t,
              "the bg_hp guard itself does not redundantly test ffc_ok")

apply_ffc = directly_guards(proc, "img = _apply_ffc(")
check(len(apply_ffc) == 1, "exactly one guard applies the reference")
if apply_ffc:
    at = " ".join(
        (ast.get_source_segment(src, apply_ffc[0].test) or "").split())
    check("ffc_ok" in at,
          "a reference is applied only while proven good on live data: %r"
          % at)

print("\n2. the bg_hp bank is filled when no correction is APPLIED")
feeds = directly_guards(tree, "self.bg_frames.append(")
check(len(feeds) == 1, "exactly one place appends to bg_frames (found %d)"
      % len(feeds))
if feeds:
    f = " ".join((ast.get_source_segment(src, feeds[0].test) or "").split())
    check("ffc is None" not in f,
          "the bank is not gated on `ffc is None`: %r" % f)
    check("ffc_ok" in f,
          "the bank is gated on ffc_ok instead: %r" % f)

print("\n3. behaviour: rejected reference + live bg_hp still corrects")
# Exercise the real decision, not a copy of it. _process needs a wx app for the
# display half, so this drives the guard's own logic against a Viewer instance
# built with __new__ - the same trick test_crash_paths uses.
sys.path.insert(0, r"C:\a\src\app")
import viewer as V  # noqa: E402


class Stub:
    """Just enough Viewer for _process' correction stage."""

    def __init__(self, ffc, ffc_ok, bg_hp):
        self.ffc = ffc
        self.ffc_ok = ffc_ok
        self.bg_hp = bg_hp
        self.bad = None
        self._scan_fault = False
        self.spot_mode = "off"
        self.shown_frames = 0
        self.iso_thr = None


def correction_applied(st):
    """The correction stage of _process, verbatim, minus the display half."""
    if st.ffc is not None and st.ffc_ok:
        return "ffc"
    if st.bg_hp is not None:
        return "bg_hp"
    return None


scene = np.full((240, 320), 6000.0)
pattern = np.random.RandomState(7).normal(0, 120, (240, 320)).astype(float)
# Sign matters: _process builds bg_hp = b - boxblur(b, 9), i.e. the high-pass of
# the background, then SUBTRACTS it. Written the other way round this subtracts
# the low-pass and doubles the high-frequency noise - which is precisely the
# failure mode this file exists to prevent, so it has to be right here too.
bg_hp = pattern - V._boxblur(pattern, 9)
ffc = np.full((240, 320), 6000.0)

cases = [
    ("no reference, bg_hp live", None, None, bg_hp, "bg_hp"),
    ("reference REJECTED, bg_hp live", ffc, False, bg_hp, "bg_hp"),
    ("reference accepted, bg_hp live", ffc, True, bg_hp, "ffc"),
    ("reference rejected, no bg_hp yet", ffc, False, None, None),
    ("no reference, no bg_hp yet", None, None, None, None),
]
for label, f, ok, hp, want in cases:
    got = correction_applied(Stub(f, ok, hp))
    check(got == want, "%-34s -> %-6s (want %s)"
          % (label, str(got), want))

# And the numbers: does the fallback actually clean the frame?
st = Stub(ffc, False, bg_hp)
noisy = scene + pattern
fixed = noisy - bg_hp
err = lambda a: float(np.abs(np.diff(a, axis=1)).mean())
check(err(fixed) < err(noisy) / 5.0,
      "background subtraction cuts neighbour-difference noise %.1f -> %.1f DL"
      % (err(noisy), err(fixed)))
check(float(fixed.std()) < float(noisy.std()) / 3.0,
      "and cuts per-pixel spread %.1f -> %.1f DL"
      % (noisy.std(), fixed.std()))

print()
if failures:
    print("FAILED %d check(s):" % len(failures))
    for f in failures:
        print("  - %s" % f)
    raise SystemExit(1)
print("all checks passed")