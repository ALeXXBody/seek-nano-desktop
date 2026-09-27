# SeekNano.exe - Windows viewer

## Install (fresh machine, no prior setup)

1. Plug the Seek Nano into a USB-C port.
2. Extract `SeekNano-driver.zip` and run **`SeekNanoDriverInstaller.exe`** (as admin):
   - answer "y" to trust the local project signer
   - it installs the WinUSB binding for `USB\VID_289D&PID_0011` via `pnputil`
   - if Windows policy refuses that INF on your build, use the *Have-Disk* fallback:
     *Device Manager → Other devices/Seek Thermal → Update driver → Browse my
     computer → Let me pick → Have Disk → `seeknano.inf`*
3. After a re-plug, run `SeekNano.exe`.
   Hotkeys: `s` start stream, `c` colormap, `p` PNG snapshot, `d` raw frame dump,
   space pause, `q` quit.

## Transports (in order of preference)

- `seeknanodirect.dll` — native libusb-1.0 transport (works with the WinUSB INF
  and with libusbK bindings alike)
- `seeknanousb.dll` — native WinUSB-API transport
- pyusb fallback

Protocol implemented: reverse-engineered from the Android app (see
`docs/protocol.md`). Built by the `build-release` workflow from source on every
release.
