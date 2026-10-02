# SeekNano.exe - Windows viewer

## Install (fresh machine, no prior setup)

1. Plug the Seek Nano into a USB-C port.
2. Extract `SeekNano-driver.zip` and run **`SeekNanoDriverInstaller.exe`** (as admin):
   - answer "y" to trust the local project signer
   - it installs the WinUSB binding for `USB\VID_289D&PID_0011` via `pnputil`
   - if Windows policy refuses that INF on your build, use the *Have-Disk* fallback:
     *Device Manager -> Other devices/Seek Thermal -> Update driver -> Browse my
     computer -> Let me pick -> Have Disk -> `seeknano.inf`*
3. After a re-plug, run `SeekNano.exe`.

## Hotkeys

| Key | Action |
|---|---|
| `s` | Start / stop stream |
| `f` | Capture flat-field reference (point at a uniform wall) |
| `c` | Next colormap |
| `p` | Save PNG snapshot |
| `d` | Dump raw frame |
| `t` | Anchor a temperature point (hover the canvas first) |
| `[` / `]` | Isotherm threshold down / up |
| `o` | Toggle the diagnostic stamp |
| space | Pause |
| `q` | Quit |
| Ctrl+L | Show / hide the log field |
| Ctrl+Alt+D | Show / hide the dev row |

## Headless modes

- `SeekNano.exe --serve [port]` - stream raw frames to the LAN as UDP
- `SeekNano.exe --replay <file>` - analysis station over a recording, no camera
- `SeekNano.exe --dump-processed <dir> [n]` - write what the window would draw
- `SeekNano.exe --selftest`, `--check-pil` - CI gates

## Transports (in order of preference)

- `seeknanodirect.dll` - native libusb-1.0 transport (works with the WinUSB INF
  and with libusbK bindings alike)
- `seeknanousb.dll` - native WinUSB-API transport
- pyusb fallback

Protocol implemented: reverse-engineered from the Android app (see
`docs/protocol.md`). Built by the `build-release` workflow from source on every
release.