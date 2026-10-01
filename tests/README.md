# tests/

- `test_dev_mode.py`, `test_audit_fixes.py`, `stubs.py`, `_paths.py` are
  portable and run in CI (`.github/workflows/build.yml`) on a stub env —
  no camera required. Run them directly:
  `python tests/test_dev_mode.py` / `python tests/test_audit_fixes.py`.
- the other `test_*.py` suites are hardware-verification **scripts** written
  for the Windows camera box. They now resolve source paths via `_paths.py`,
  so the repo can live anywhere; paths that only exist on the hardware box
  (`libusb-1.0.dll`, built exe, live captures) default to `C:\a` and are
  overridable with `SN_CAM_BASE` and `SN_OUT`. They are not pytest-collectable
  (they execute at import and `sys.exit`) — run them directly on the box:
  `python tests/test_display_path.py`.
- manual measurement / screen-capture drivers live in `../diagnostics/`.
