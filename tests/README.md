# tests/

- `test_dev_mode.py`, `test_audit_fixes.py`, `stubs.py` are portable and run in
  CI (see `.github/workflows/build.yml`) on a stub env — no camera required.
- the other `test_*.py` suites are hardware-verification tests written for the
  Windows dev box (`C:\a\src\...` paths + live capture files). They cannot run
  on Linux or in CI yet; port them by replacing the hardcoded paths with
  repo-relative ones based on `__file__`.
- manual measurement / screen-capture drivers live in `../diagnostics/`.
