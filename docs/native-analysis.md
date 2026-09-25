# Native-library static analysis (radare2 pass 1)

Binary set from the app's `lib/armeabi-v7a/` — all clang-built Android NDK r26d
(llvm 17.0.2), stripped, Thumb-2, no C++ exceptions, no libusb (Android USB is
reached via JNI into the app's Java `NativeUsbDevice*` classes).

## Calling into USB from native

`libseekusb.so` never dials the kernel. `seekusb_*` functions hold a JavaVM handle
(sealed in `seekusb_load_jvm`), and the device struct (see below) carries JNI
object refs:

- offset `+0x0c` — `NativeUsbDevice` jobject (control ops in fcn.3ce0 call
  `ControlTransfer(I B I)I` through it: `bmRequestType` for read `0xC0`,
  write `0x40`, `wIndex` = bRequest, `wLength` = count; see JNI trace script
  for exact values as they hit the wire)
- offsets `+0x10`, `+0x1c` — cached endpoint objects (control / bulk-in)
- offset `+0x54` — `usbTimeout` value (mirror of Java field), default 1250 ms
- offset `+0x24` — double: rolling throughput metric updated with
  `len * 8 / 1000.0` after every successful bulk read
- offset `+0x48` — frame request pacing
- offset `+0x54` — device id / timeout shared slot
- offset `+0xf4` — last firmware error (host side)
- offset `+0xfa` — int16 opcode scratch for control replies (switch 0..9 maps to
  firmware error table at 0x29fc)
- offset `+0xf8` — payload area reused for control transfer send/recv scratch

## Device-manager bootstrap

`seekusb_device_manager_create(void* jvm_ctx)`:
1. calloc(1, 0x20) — manager
2. `fcn.37cc` / `fcn.38b4` build the Java-side bridge objects
3. zero-init device table (0xff fill via fcn.42da, then per-device zero via fcn.40f6)
4. store manager pointer in caller slot

## Device identity / authenticator

- `seekusb_device_get_device_id` returns `[dev+0x54]` (same slot as timeout — id and timeout are packed into the device context)
- `seekusb_authenticator_create` (424 B) allocates the authenticator, seeds it
  from the four GUID constants in .rodata (see README table) and the
  std::strcmp deny/allow list APIs (`_allow_guid`, `_deny_guid`,
  `_allow_core_part_number`, `_deny_core_part_number`, `_query_*`).
- This is the Nano "will not work with SDK" gate: part number/GUID filtering.
  The **unlock handshake** itself lives in `libseekcamera.so` (`seekcamera_nano_unlock`,
  log string "Failed to unlock nano device: %s"), and is invoked during
  `seekcamera_capture_session_start` when the attached core answers the
  `Microcore` identity probe.

## Frame loop

- `seekusb_device_run` spins a pthread ("USB Sink" thread name in .rodata),
  calling pacing from `get_frame_request_delay` (default from
  `seekusb_device_set_frame_request_delay`), issuing control-init reads
  (0x36 chip-id observed) and bulk reads into the 177,840-byte Java array,
  then invoking `frame_available` callbackregistered from
  `Java_..._initializeTransferArrays` path.
- Metrics (`uptime`, `frame_count`, `throughput`) update offsets +0x24/+0x48.
- Frame pacing default found at fcn.2924 → floor() call returns dt in ms;
  target ≈ `1000/9` ms (≈9 Hz nominal, raised by `set_frame_request_delay`).

## Strings of note (libseekcamera.so)

```
seekcamera_get_core_part_number
seekcamera_nano_unlock
Failed to unlock nano device: %s
Microcore
Failed to meet product firmware version requirement
```

Full symbol/string lists: `analysis-logs/symbols-libseek*.txt`, `analysis-logs/strings-libseek*.txt`.
