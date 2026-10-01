"""Shared path helpers for the hardware-verification test_*.py suites.

The suites were written on the Windows dev box where the repo lived at
C:\\a\\src. They now resolve source/module paths from this file, so the
repo can live anywhere; paths that only exist on the camera box (the
libusb dll, the built exe, live capture files) come from env overrides:
  SN_CAM_BASE   camera-box root, default C:\\a   (hardware only)
  SN_OUT        scratch output dir,   default <repo>/out
"""
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = REPO / "app"
VIEWER = APP_DIR / "viewer.py"
PCDRIVER = REPO / "pcdriver"

CAM_BASE = pathlib.Path(os.environ.get("SN_CAM_BASE", r"C:\a"))
LIBUSB_DLL = CAM_BASE / "libusb-1.0.dll"
BUILT_EXE = CAM_BASE / "src" / "dist" / "SeekNano.exe"

OUT = pathlib.Path(os.environ.get("SN_OUT", REPO / "out"))
