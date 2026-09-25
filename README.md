# Seek Nano Desktop Project

Project goal: reverse-engineer the **Seek Nano** (USB-C thermal camera, phone-only by
design) and build a **desktop PC viewer/driver** so the camera can be used directly on a
computer (Linux/Windows).

Status: **in progress** — APK intelligence gathering complete, dynamic trace pending.

---

## 1. Why the camera does not work on a PC (public state of the art)

- Seek's FAQ states the Nano works only with the *Seek Nano* Android/iOS app and
  "will not work with Seek's SDK".
- [OpenThermal/libseek-thermal](https://github.com/OpenThermal/libseek-thermal)
  (the main open-source Linux driver) supports the Compact/CompactXR/CompactPRO line
  only and **explicitly does not support the Nano 200/300** (open issue, no work done).
- The Nano support request (Sep 2025) has had no reverse-engineering progress.

## 2. What this project found

The Seek Nano Android app (`com.thermal.seeknano`, version **1.5.0**, versionCode 27)
was pulled from APKPure as an XAPK (35 MB) and dissected. It is a **Flutter** app that
ships Seek's **full native thermal SDK** as Android `.so` libraries — this is the
proprietary protocol itself, bundled inside a freely downloadable app.

### 2.1 Native library inventory (`lib/armeabi-v7a/`, from `config.armeabi_v7a.apk`)

| Library | Size | Role |
|---|---|---|
| `libseekcamera.so` | 368 KB | Camera SDK core: capture sessions, AGC, color palettes, thermography, **`seekcamera_nano_unlock`** |
| `libseekusb.so` | 22 KB | USB abstraction: device manager, authenticator, control/bulk transfer glue to Android Java USB API |
| `libseekframe.so` | 9 KB | Frame container API |
| `libseekspi.so` / `libseeki2c.so` / `libseekgpio.so` / `libseekfs.so` / `libseekgdf.so` | 5–104 KB | Sensor bus + firmware FS layers (OEM core shared code) |
| `libseekip.so` | 224 KB | IP-mode streaming (network core support) |
| `libseekutils.so` | 20 KB | Shared utils |
| `libapp.so` + `libflutter.so` | — | Flutter UI (Dart AOT compiled) |

Also bundled: FFmpeg libs (`libav*`, `libsw*`) used for video encoding/recording.

### 2.2 Exported symbols of interest

`libseekusb.so` (all 50 exports listed in [docs/symbols-libseekusb.md](docs/symbols-libseekusb.md)):
- `seekusb_device_manager_create/destroy`
- `seekusb_device_run` / `register_frame_available_callback`
- `seekusb_device_get/set_opmode`, `get/set_timeout`, `get/set_frame_request_delay`
- `seekusb_device_get_chipid`, `get_firmware_info`, `get_serial_number`,
  `get_core_part_number`, `get_factory_settings`, `get_manufacture_date`
- `seekusb_device_read/write/copy_memory_region`
- `seekusb_device_peripheral_control`, `sleep`, `reboot`, `bandwidth_test`
- **`seekusb_authenticator_create`**, `authenticator_allow/deny_guid`,
  `authenticator_allow/deny/query_core_part_number` ← the vendor/device lock logic

`libseekcamera.so` (160 exports):
- `seekcamera_manager_create`, `seekcamera_capture_session_start/stop`,
  `seekcamera_register_frame_available_callback`
- `seekcamera_frame_get_frame_by_format` (multiple pixel formats)
- **`seekcamera_nano_unlock`** ← present and confirmed by string
  `"Failed to unlock nano device: %s"`
- String `Microcore` confirms the Nano is treated as Seek's OEM "Micro Core" line.

### 2.3 USB facts recovered from the Java/dex layer (decompiled with androguard)

From `com.thermal.seekcamera.NativeUsbDeviceManager` / `NativeUsbDevice`:

| Constant | Value | Meaning |
|---|---|---|
| **Vendor ID** | `0x289D` (10397) | Seek Thermal — matches **any product ID** from this vendor |
| Interface | 0 | single interface |
| Control endpoint | EP0 | `bmRequestType` = `0xC0` (IN) / `0x40` (OUT), built in `ControlTransfer()` |
| **Bulk IN endpoint** | interface 0, endpoint index 1 | frame data |
| Bulk chunk cap | 16,224 B | per-transfer cap (`Math.min(16224, remaining)`) |
| USB timeout | 1250 ms | default `usbTimeout` |
| Frame buffer | **177,840 B** | pre-allocated bulk receive array |
| Control request `0x36` | 12-byte return | chip ID / firmware info read (seen in `GetChipID()` debug call) |

Frame-buffer math: 177,840 B ≈ Nano-300 raw frame (320×240 px @ 16-bit = 153,600 B)
plus per-frame header/padding. The bulk receive loop calls back into native code
(`libseekusb.so` JNI) which consumes frames and tracks throughput metrics.

### 2.4 The lock ("authenticator") & unlock

- `libseekusb.so` strings reveal four GUIDs used by the authenticator:
  ```
  F4BAF4F4-177D-4165-8A6B-90C3306F9CA3
  36607C12-F598-4044-AD5C-A8F98DDA496A
  53225FC0-DE69-4DCB-AD8F-3CB36214ABE1
  9DAD69CE-287C-4C5C-9A2A-EE310EE8A9B3
  ```
- `libseekcamera.so` contains **`seekcamera_nano_unlock`** and log strings
  `"Failed to unlock nano device: %s"`, `"Microcore"`,
  `"Failed to meet product firmware version requirement"`.
- Conclusion: the Nano implements a firmware-level authenticity check that the
  phone app satisfies during init — this sequence is what we must capture and replay
  in the PC driver.

### 2.5 What we do *not* have yet

- The actual init handshake byte sequence (control transfer values, magic writes,
  authenticator ticket exchange) — it lives inside stripped ARM Thumb code in
  `libseekcamera.so` / `libseekusb.so`.
- Frame payload layout (header fields, temperature encoding, calibration data).
- Both are being obtained via **dynamic instrumentation** (see roadmap).

## 3. The spy app (built, awaiting deployment)

To capture the protocol without any physical connection to the server:

1. The Play Store XAPK was merged into a universal APK (`APKEditor`).
2. A **Frida gadget 17.19.0** (arm) was injected as `lib/armeabi-v7a/libseekgadget.so`;
   `MainActivity.<clinit>` was smali-patched to `System.loadLibrary("seekgadget")`
   *before* loading `seekcamera`.
3. Gadget config asset (`assets/libseekgadget_config.so`, actually JSON) loads
   `spytrace.js` from `/data/local/tmp/` and hot-reloads on change.
4. Package renamed `com.thermal.seeknano` → **`com.thermal.seeknanospy`**
   (manifest package, custom permission, androidx-startup / share-provider
   authorities, arsc package name) so it **installs alongside** the original app
   without conflict; original app remains untouched.
5. Re-signed with a local key; manifest renames verified with androguard.

Artifacts: `seekspy-installer.apk` (51 MB) — see `artifacts/` (attached to repo
releases rather than git, too big for git).

## 4. Roadmap

- [x] Pull APK, extract native SDK + dex
- [x] Static analysis: VID/PID policy, endpoints, timeouts, buffer sizes,
      `nano_unlock` symbol presence, authenticator GUIDs
- [x] Build + sign spy APK (Frida gadget, renamed package)
- [ ] Install on phone, pair adb over **wireless debugging** (no cable)
- [ ] Trace one full live session: connect → unlock → stream → disconnect
- [ ] Reconstruct handshake in `pcdriver/` (Python + libusb/usb.core)
- [ ] Validate frame format against native frame metrics
- [ ] Build desktop viewer: colormaps, spot/temp, snapshots, recording,
      optional v4l2loopback output
- [ ] Document full protocol in `docs/protocol.md`

## 5. Tooling used (all scripted, nothing manual)

| Tool | Purpose |
|---|---|
| androguard 4.1.4 | dex/axml/arsc parsing, manifest verification |
| APKEditor 1.4.9 | split-APK merge, XML/arsc decode + rebuild |
| apktool 3.0.3 (available) | fallback smali roundtrip |
| radare2 6.2.2 | ARM/Thumb disassembly of native libs |
| frida / frida-tools 17.19 | dynamic instrumentation (gadget injected in APK) |
| apksigner + keytool | repack signing |
| adb | wireless-debugging install/trace transport |

## 6. Legal note

The APK is publicly distributed by Seek Thermal and analyzed here strictly for
interoperability with hardware its owner already possesses. No Seek code or assets
are redistributed in this repository — only our own analysis notes, scripts, and
derived protocol documentation.
