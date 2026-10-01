# Security posture of the dev tools

## `--dev-serve` / the `dev: upload frames` checkbox

What it does: streams every accepted camera frame (~177 KB, ~12-25 fps) as a
plain `HTTP PUT /frame_NNNN.raw` to a host you configure, **unauthenticated
and unencrypted**.

Risk assessment - this is deliberate, not an oversight:

- The viewer fully works **without** the dev upload. It is opt-in hardware:
  an unchecked checkbox in the GUI row, and an explicitly-invoked CLI mode.
- When enabled, anyone who can reach `<host>:<port>` can *receive* the camera
  frames, and anyone who can reply to it can *inject* frames into the
  receiving pipeline. On a home LAN behind your router this is low risk; do
  not enable it on a hostile or shared network.
- `dev_serve` (CLI) additionally uploads `bindinfo.txt` (USB PnP binding
  state) and `logs.txt` to the same host. Machine-specific but not secret.
- The GUI upload target defaults to `192.168.50.200:8100` (the dev box).
  Change it in the GUI text field before streaming if you want it elsewhere.

If you want a hard boundary instead of documentation: leave the checkbox
off, or firewall `:8100` to the single destination host.

## Driver package (SeekNano-driver.zip)

- The INF binds **only** `USB\VID_289D&PID_0011&MI_00` / `&MI_01` to WinUSB,
  exact hardware IDs, no wildcards. The parent composite node is deliberately
  not bound - it may be owned by another stack (see seeknano.inf header).
- The catalog is a CI-generated **self-signed** certificate (10-year
  validity, regenerated per run). It has no trust value by itself; installing
  `SeekNanoSigner.cer` into "Trusted Root / Trusted Publisher" is exactly the
  user-bypass flow we would rather not normalise. The honest default is:
  `pnputil` will refuse the unsigned package, and the fallback (printed by
  the installer) is Device Manager / Have Disk for **this device only** -
  never "always disable driver signature enforcement".
- No Authenticode signing on the exe or DLLs; SHA256 of the release assets
  should be verified from the release page before running.
- The transport DLLs (`seeknanousb.dll`, `seeknanodirect.dll`,
  `libusb-1.0.dll`) load via fully-qualified paths inside the app directory
  first. libusb-1.0.dll additionally loads from the transport dll's own
  directory, never a bare PATH search.
- `SeekNanoDriverInstaller.exe` stages the INF with `pnputil /add-driver
  /install` and reports real exit codes (it no longer depends on a
  wdi-simple.exe binary that was never shipped).

## Secrets / logging

- No embedded tokens in the repo (verified).
- `seeknano_verbose.log`, `devserve.log`, `crash.txt` are runtime artifacts,
  gitignored, and stay on the local machine.
