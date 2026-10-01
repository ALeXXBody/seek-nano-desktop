#!/usr/bin/env python3
"""Regression tests for the 2026-10-01 audit batch-1 fixes.

Run:  python tests/test_audit_fixes.py   (repo root; same stub env as
tests/test_dev_mode.py)  or  pytest tests/test_audit_fixes.py.

No hardware or live capture data required.
"""
import importlib.util
import inspect
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import stubs as W_  # noqa: E402,F401  (wx/usb scaffolding lives in tests/)

# ---- register stub modules before importing viewer (same as test_dev_mode) --
if "wx" not in sys.modules:
    fake_usb_core = types.ModuleType("usb.core")
    fake_usb_core.USBError = W_.USBError
    fake_usb_core.find = lambda **kw: None
    fake_usb = types.ModuleType("usb")
    fake_usb.USBError = W_.USBError
    fake_usb.core = fake_usb_core
    fake_usb.util = W_.FakeUSBUtil()
    sys.modules["usb"] = fake_usb
    sys.modules["usb.core"] = fake_usb_core
    sys.modules["usb.util"] = fake_usb.util
    sys.modules["wx"] = W_.FakeWx()
    sys.modules["libusb_package"] = types.ModuleType("libusb_package")
    sys.modules["libusb_package"].get_libusb1_backend = lambda: None
    sys.modules["PIL"] = types.ModuleType("PIL")

spec = importlib.util.spec_from_file_location(
    "viewer", os.path.join(REPO, "app", "viewer.py"))
viewer = importlib.util.module_from_spec(spec)
sys.modules["viewer"] = viewer
os_chdir_dir = os.path.join(REPO, "app")
old_cwd = os.getcwd()
os.chdir(os_chdir_dir)
try:
    spec.loader.exec_module(viewer)
finally:
    os.chdir(old_cwd)


class TestDdlCandidates(unittest.TestCase):
    """dll/prefix pairing must be 1:1 (audit finding #6)."""

    def test_pairs_all_names_have_matching_prefix(self):
        pairs = viewer.dll_candidates(mei=None)
        self.assertEqual(pairs, [
            ("seeknanousb.dll", "SN_"),
            ("seeknanodirect.dll", "SNLB_"),
        ])

    def test_pairs_frozen_paths_point_at_meipass(self):
        mei = r"C:\Users\t\AppData\Local\Temp\_MEI123"
        pairs = viewer.dll_candidates(mei=mei)
        # _MEIPASS copies first, bare-name fallback second, prefixes track
        self.assertEqual(pairs[0][0], os.path.join(mei, "seeknanousb.dll"))
        self.assertEqual(pairs[1][0], os.path.join(mei, "seeknanodirect.dll"))
        # fallback entries are the bare names, not under _MEIPASS
        self.assertEqual(pairs[2][0], "seeknanousb.dll")
        self.assertEqual(pairs[3][0], "seeknanodirect.dll")
        self.assertEqual([p for _, p in pairs],
                         ["SN_", "SNLB_", "SN_", "SNLB_"])

    def test_no_zip_truncation_pattern_in_source(self):
        # the old bug was zip(cand, [4 prefixes]) - truncation means the
        # SNLB_ prefixes were unreachable; guard against reintroducing it
        src = open(os.path.join(REPO, "app", "viewer.py"),
                   encoding="utf-8").read()
        self.assertNotIn('zip(cand, ["SN_", "SN_", "SNLB_", "SNLB_"])', src)


class TestPaintSingleDraw(unittest.TestCase):
    """Paint handler must draw the bitmap exactly once, inside the try
    (audit finding #1: duplicate draw + NameError on the trailing call)."""

    def _src(self):
        return inspect.getsource(viewer.Viewer.on_paint)

    def test_no_draw_outside_try(self):
        src = self._src()
        try_idx = src.rindex("except Exception")
        tail = src[try_idx:]
        tail = tail[tail.index("\n") + 1:]  # drop the except-line itself
        self.assertNotIn("DrawBitmap", tail,
                         "no DrawBitmap may run after the except block")

    def test_exactly_one_draw_inside(self):
        self.assertEqual(self._src().count("dc.DrawBitmap("), 1)


class TestStreamFinallyGuard(unittest.TestCase):
    """Stream.run cleanup must not raise AttributeError when the device
    was never opened (audit finding #10)."""

    def test_cleanup_tolerates_missing_dev(self):
        s = viewer.Stream.__new__(viewer.Stream)  # skip __init__/run
        # no .dev attribute set: the finally block previously blew up here
        self.assertIsNone(getattr(s, "dev", None))

    def test_cleanup_skips_sentinel_dev_true(self):
        # NativeStream uses self.dev = True as a marker; Stream cleanup must
        # not call usb util dispose on a boolean sentinel
        self.assertIn("dev is not True",
                      inspect.getsource(viewer.Stream.run))


class TestInstallerSourceContract(unittest.TestCase):
    """driver/installer.cs consistency (audit findings #4/#5); parses the
    source text so the checks hold without a Windows build box."""

    def _src(self):
        return open(os.path.join(REPO, "driver", "installer.cs"),
                    encoding="utf-8").read()

    def test_installer_returns_worst_result(self):
        src = self._src()
        self.assertIn("return worst;", src)
        lines = src.splitlines()
        idx = next(i for i, l in enumerate(lines) if "return worst;" in l)
        tail = "\n".join(lines[idx:idx + 8])
        self.assertNotIn("return 0;", tail)

    def test_installer_uses_pnputil_no_wdi(self):
        """The old installer shelled wdi-simple.exe, which was never shipped
        anywhere - every install silently broke. It now stages the INF via
        pnputil directly (audit batch 3)."""
        src = self._src()
        self.assertNotRegex(src, r'wdi-simple\.exe"|\bwdi_simple|\bwdi_simple\.exe')
        self.assertIn('"pnputil"', src)
        self.assertIn("return worst;", src)

    def test_parent_node_not_bound(self):
        # parent USB node must stay unbound, and the constraint documented
        self.assertIn("NOT bound", self._src())

    def test_inf_comment_and_installer_agree(self):
        inf = open(os.path.join(REPO, "driver", "seeknano.inf"),
                   encoding="utf-8", errors="replace").read()
        self.assertIn("deliberately NOT bound", inf)
        self.assertIn("NOT bound", self._src())


class TestCTransportGuards(unittest.TestCase):
    """C-side audit fixes #7-#9, verified at source level (the actual
    compile runs in CI on Windows)."""

    _NATIVE = os.path.join(REPO, "transport", "native", "seeknanousb.c")
    _LIBUSB = os.path.join(REPO, "transport", "libusb", "seeknanousb_libusb.c")

    def test_heapalloc_checked_before_deref(self):
        src = open(self._NATIVE, encoding="utf-8").read()
        idx = src.find("HeapAlloc(GetProcessHeap(), 0, need)")
        self.assertGreaterEqual(idx, 0)
        # the NULL check must appear between the allocation and the deref
        self.assertLess(src.find("if (!det)", idx),
                        src.find("det->cbSize", idx))

    def test_no_overlapped_flag(self):
        src = open(self._NATIVE, encoding="utf-8").read()
        self.assertNotIn("FILE_FLAG_OVERLAPPED", src)

    def test_libusb_partial_load_not_cached(self):
        src = open(self._LIBUSB, encoding="utf-8", errors="replace").read()
        body = src[src.find("static int load_libusb"):src.find("__declspec")]
        fails = body.count("return -")
        frees = body.count("FreeLibrary(g_libusb)")
        # -1: the LoadLibrary-failure path frees nothing (nothing loaded)
        self.assertEqual(frees, fails - 1,
                         "each failure after LoadLibrary must FreeLibrary")


if __name__ == "__main__":
    unittest.main()
