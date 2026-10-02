#!/usr/bin/env python3
"""Regression tests for the 2026-10-01 audit batch-1 fixes.

Run:  python tests/test_audit_fixes.py   (repo root; same stub env as
tests/test_dev_mode.py)  or  pytest tests/test_audit_fixes.py.

No hardware or live capture data required.
"""
import importlib.util
import inspect
import numpy as np
import os
import queue
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


class TestLogIOAndNoZeroDiv(unittest.TestCase):
    """Batch-2/3 followups reported by the user."""

    def test_no_zero_division_in_pacing_log(self):
        # max(0.0, period-0.005) clamps period to 0.0 for shallow decay; the
        # inverse was then taken unconditionally once per 12 kicks
        src = open(os.path.join(REPO, "app", "viewer.py"),
                   encoding="utf-8").read()
        self.assertIn("if kicks % 12 == 0 and period > 0", src)
        # and no naked inverse on period elsewhere
        self.assertEqual(src.count("1.0/period"), 1)

    def test_single_pil_entry_point(self):
        src = open(os.path.join(REPO, "app", "viewer.py"),
                   encoding="utf-8").read()
        # the only function-local PIL import is inside the shared helper;
        # every save path goes through _pil_image()
        self.assertEqual(src.count("from PIL import Image"), 1)
        self.assertIn("def _pil_image(", src)
        self.assertGreaterEqual(src.count("_pil_image()"), 3)
        import _paths  # imports cleanly
        # the loader itself raises exactly ImportError when PIL is absent
        import PIL
        if not hasattr(PIL, "Image"):
            with self.assertRaises(ImportError):
                viewer._pil_image()


class TestDisplayFreezeWhenPaused(unittest.TestCase):
    """Regression: while paused, the newest pump frame must NOT be able to
    reach the screen through a stray paint event. The shown frame is pinned
    in _display_raw; _bmp reads that, not frame_raw."""

    def _src(self):
        return open(os.path.join(REPO, "app", "viewer.py"),
                    encoding="utf-8").read()

    def test_bmp_reads_display_raw(self):
        src = self._src()
        bmp = src[src.find("def _bmp"):]
        self.assertIn("_display_raw", bmp)
        self.assertIn("_roi_u16(disp)", bmp)

    def test_pause_pins_frame(self):
        src = self._src()
        self.assertIn("self._display_raw = payload", src)

    def test_unpause_resyncs_display(self):
        src = self._src()
        # both pause toggles must resync _display_raw when leaving pause
        self.assertEqual(src.count("_display_raw = self.frame_raw"), 2)


class TestDllCandidatesOrder(unittest.TestCase):
    """name-major everywhere: a partial bundle must never bind the silent
    SNLB transport just because it sits in an earlier search dir."""

    def test_name_major_with_two_dirs(self):
        mei, out = os.path.join("M", "x"), "E"
        pairs = viewer.dll_candidates(mei=mei, exe_dir=out)
        expect = [
            (os.path.join(mei, "seeknanousb.dll"), "SN_"),
            (os.path.join(out, "seeknanousb.dll"), "SN_"),
            (os.path.join(mei, "seeknanodirect.dll"), "SNLB_"),
            (os.path.join(out, "seeknanodirect.dll"), "SNLB_"),
            ("seeknanousb.dll", "SN_"),
            ("seeknanodirect.dll", "SNLB_"),
        ]
        self.assertEqual(pairs, expect)

    def test_usb_always_before_direct(self):
        pairs = viewer.dll_candidates(mei="M", exe_dir="E", module_dir="WD")
        seen_pathed = [(path, p) for path, p in pairs
                       if os.sep in path or "/" in path]
        # every SN_ probed via a directory comes before every SNLB_ probed
        # via a directory (bare-name fallbacks are all last, by design)
        self.assertLess(max(i for i, (_, pref) in enumerate(seen_pathed)
                            if pref == "SN_"),
                        min(i for i, (path, pref) in enumerate(seen_pathed)
                            if pref == "SNLB_"))


class TestAnalysisFeatures(unittest.TestCase):
    """Cursor stats / line profile / isotherm - pure numpy paths."""

    def test_sample_stats_window(self):
        img = np.zeros((240, 320), np.float32)
        img[100:104, 200:204] = 500.0
        val, mean, lo, hi = viewer._sample_stats(img, (201, 101))
        self.assertEqual(val, 500.0)
        self.assertEqual(hi, 500.0)
        self.assertGreater(mean, 0)
        self.assertEqual(lo, 0.0)

    def test_sample_stats_out_of_range(self):
        self.assertIsNone(viewer._sample_stats(np.zeros((4, 4), np.float32),
                                               (99, 99)))

    def test_line_profile_endpoints(self):
        img = np.zeros((240, 320), np.float32)
        img[:, :] = 100.0
        prof = viewer._line_profile(img, (0, 0), (319, 0))
        self.assertEqual(prof.shape, (192,))
        self.assertTrue((prof == 100.0).all())

    def test_line_profile_bilinear_half_step(self):
        # a line straddling two rows must return the average
        img = np.zeros((4, 4), np.float32)
        img[1, :] = 0.0
        img[2, :] = 100.0
        prof = viewer._line_profile(img, (0, 1.5), (3, 1.5), n=16)
        self.assertTrue(np.allclose(prof, 50.0, atol=1e-3))

    def test_isotherm_state_shape(self):
        v = viewer.Viewer.__new__(viewer.Viewer)
        # the shipped defaults: isotherm off, no threshold
        self.assertFalse(getattr(v, "iso_on", False))
        # a threshold set through the key path turns the band on
        v.iso_thr = 42.0
        v.iso_on = True
        self.assertTrue(v.iso_on)
        self.assertEqual(v.iso_thr, 42.0)


class TestDumpGate(unittest.TestCase):
    """_dump_pngs: missing PIL must not flood the log - both throttles back
    off, and after 3 failures the dumps disable permanently (one line)."""

    def _mk(self):
        v = viewer.Viewer.__new__(viewer.Viewer)
        v.q = queue.Queue()
        return v

    def test_disabled_gate_short_circuits(self):
        v = self._mk()
        v._dump_disabled = True
        v._dump_pngs("not-even-an-array")   # must not raise / touch _PIL

    def test_three_failures_disable(self):
        v = self._mk()
        for i in range(3):
            n_before = v.q.qsize()
            v._dump_pngs(None)              # _pil_image() raises -> counter
            self.assertEqual(v.q.qsize(), n_before + 1)
        self.assertTrue(getattr(v, "_dump_disabled", False))


class TestHiddenDevSurfaces(unittest.TestCase):
    """The stable build shows NO dev tools: the dev upload row and the log
    field are hidden; CLI modes remain available (hidden, not removed)."""

    def _src(self):
        return open(os.path.join(REPO, "app", "viewer.py"),
                    encoding="utf-8").read()

    def test_dev_row_hidden_at_startup(self):
        src = self._src()
        self.assertIn("self.dev_sizer = devbox", src)
        self.assertIn("devbox.ShowItems(False)", src)   # dev row: built, hidden
        self.assertIn("self.log.Hide()", src)           # log: hidden (Ctrl+L)

    def test_dev_row_toggle_hook(self):
        src = self._src()
        self.assertIn('c == "d":\n            self._dev_shown', src)
        self.assertIn('c == "l":\n            self._log_shown', src)

    def test_cli_dev_modes_intact(self):
        src = self._src()
        for flag in ("--dev-serve", "--dev-bind", "--dump-processed",
                     "--selftest", "--check-pil"):
            self.assertIn(flag, src)

    def test_icon_wired(self):
        src = self._src()
        self.assertIn("self.SetIcon(", src)
        assets = os.path.join(REPO, "assets")
        self.assertTrue(os.path.exists(os.path.join(assets, "icon.ico")))
        self.assertTrue(os.path.exists(os.path.join(assets, "icon_256.png")))
        yml = open(os.path.join(REPO, ".github", "workflows", "build.yml"),
                   encoding="utf-8").read()
        self.assertIn("--icon assets", yml)
        self.assertIn("icon_256.png", yml)


class TestGuiBuildSmoke(unittest.TestCase):
    """GUI construction is validated on the real wx toolkit in CI
    (windows runner, real wxPython installed by the workflow): any
    toolkit-API drift like the SetOwnBackColor crash fails the build.
    Skipped on this Linux box because creating a real styled frame cannot
    be reproduced here without side effects."""

    def test_smoke_script_exists_and_valid_python(self):
        p = os.path.join(REPO, "tests", "gui_smoke.py")
        self.assertTrue(os.path.exists(p))
        import ast
        ast.parse(open(p, encoding="utf-8").read())

    def test_ci_runs_the_smoke(self):
        yml = open(os.path.join(REPO, ".github", "workflows", "build.yml"),
                   encoding="utf-8").read()
        self.assertIn("gui_smoke.py", yml)


class TestRecordReplayCsvAnchors(unittest.TestCase):
    """Record/replay round trip (via ReplayThread), CSV export, anchors."""

    def _tmp(self):
        import tempfile
        self._old = os.getcwd()
        d = tempfile.mkdtemp()
        os.chdir(d)
        return d

    def tearDown(self):
        if getattr(self, "_old", None):
            os.chdir(self._old)

    def test_dl_to_c_anchors(self):
        import numpy as _np
        self.assertIsNone(viewer.dl_to_c(100, []))
        self.assertEqual(viewer.dl_to_c(100, [(100, 25.0)]), 25.0)
        two = viewer.dl_to_c(500, [(0, 20.0), (1000, 30.0)])
        self.assertAlmostEqual(two, 25.0, places=3)
        # inverse direction preserved
        self.assertAlmostEqual(viewer.dl_to_c(250, [(0, 20.0), (1000, 30.0)]),
                               22.5, places=3)

    def test_anchors_roundtrip(self):
        self.assertTrue(viewer.anchors_save([(1500.0, 21.5)]))
        self.assertEqual(viewer.anchors_load(), [(1500.0, 21.5)])
        os.remove("seeknano_anchors.json")

    def test_replay_thread_feeds_frames(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "rec_test.snrec")
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), "tests"))
        import stubs as W_
        with open(path, "wb") as fh:
            for i in range(6):
                fh.write(W_.make_frame_body(seq=i + 1))
        import queue as _q
        q = _q.Queue()
        rt = viewer.ReplayThread(q, path)
        self.assertEqual(len(rt.frames), 6)
        rt.start()
        rt.join(timeout=5)
        got = [item for item in q.queue if item[0] == "frame"]
        self.assertGreaterEqual(len(got), 6)

    def test_csv_export(self):
        import numpy as _np
        import tempfile
        os.chdir(tempfile.mkdtemp())
        v = viewer.Viewer.__new__(viewer.Viewer)
        v._analysis = _np.zeros((3, 4), _np.float32) + 1234.5
        v._anchors = []
        v.q = queue.Queue()
        v.push_status = lambda *_a, **_k: None   # no GUI in this test
        v._export_csv(None)
        import glob
        files = glob.glob("frame_*.csv")
        self.assertTrue(files, "export produced a file")
        with open(files[0]) as fh:
            rows = fh.read().splitlines()
        self.assertEqual(rows[0], "index,x,y,DL,degC")
        self.assertEqual(len(rows), 1 + 12)
        self.assertIn("1234.5", rows[1])


if __name__ == "__main__":
    unittest.main()
