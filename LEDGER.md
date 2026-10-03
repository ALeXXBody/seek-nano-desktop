# SeekNano LEDGER

Everything established by measurement, so it can be searched instead of
re-derived. Newest findings at the top. Every claim here has a command or a
file behind it; nothing is inferred.

Search tags: `[PROTO] [AGC] [NUC] [TEMP] [GAIN] [FRAME] [GUI] [BUILD] [ANDROID]`

---

## 1. The wall capture is required, and the reason is spatial  `[NUC]`

Measured on a real wall, offline through the real pipeline, rendered to PNG:

| correction | fixed pattern left | span kept |
|---|---|---|
| none | 90.6 DL | 100% |
| high-pass 9 px (**ships today**) | 64.7 DL | 67% |
| high-pass 61 px | 59.0 DL | 58% |
| full background subtraction | 0.7 DL | 8.9 DL — **scene gone** |

Three conclusions, all measured:

- The residual **survives averaging 40 frames**, so it is spatial, not photon
  noise. (I asserted the opposite earlier, on a laptop-and-hand scene. Wrong.)
- It **survives a high-pass sweep from 9 px to 61 px**, moving only 5 DL. So it
  sits at the *same spatial scale as the scene* and no spatial filter can
  separate them. On a uniform surface, scene and pattern are mixed.
- Full subtraction removes it but **collapses the scene span from 369 to
  8.9 DL**. The resulting image looks plausible — smooth thermal gradients — and
  is meaningless. I was briefly impressed by exactly that render before counting
  it. That is the trap the old `_process` docstring warned about.

**Corollary:** the whole-frame figure quoted earlier (1.05% noise) was measured
against a **782 DL** span with a hand and a laptop in frame. On a wall the span
is ~50 DL, so the same noise fills **ten times more** of the colour ramp. Always
measure the flattest patches, never the whole frame.

Evidence: `tests/measure_wall_scene.py`, `measure_residual_split.py`,
`measure_full_vs_hp.py`, `measure_hp_scale.py`, `repro_display_wall.py`.

---

## 2. The phone app exposes NO NUC or AGC controls  `[AGC] [ANDROID]`

Opened the installed app's Settings screen on the device. It contains exactly:

- Temperature Units (°F/°C)
- Seek Thermal Watermark (on)
- Color Bar (on)
- About / Quick Start Guide / Thermal.com / Customer Support

**No AGC. No shutter mode. No pipeline mode. No flat-field option. Anywhere.**

The SDK exports `set_agc_mode`, `set_shutter_mode`, `set_histeq_agc_gain_limit`,
`set_histeq_agc_roi_*`, `set_histeq_agc_plateau`, `set_histeq_agc_trim_*`,
`set_linear_agc_lock_mode/_min/_max`, `set_filter_state`. **This app calls none
of them.** It streams on camera defaults and the DSP does the work.

**This kills the entire AGC-protocol line of investigation.** The phone app does
not achieve a clean image by configuring a shutterless NUC, so there is nothing
to reverse-engineer on that path. The earlier plan to isolate an "AGC request
code" was aimed at the wrong mechanism.

---

## 3. The phone app shows ABSOLUTE temperature  `[TEMP] [ANDROID]`

Live capture from the device read **13.9 °C – 19.1 °C** with a colour bar. The
desktop viewer reports "DL only" and has no absolute scale at all.

So real calibration exists and the SDK loads it —
`seekcamera_store_calibration_data`, `seekcamera_load_app_resources`,
`FlatField.bin`, and `seekcamera_set_thermography_offset` /
`set_thermography_window` / `set_scene_emissivity`.

**This is an achievable win independent of everything else** — absolute
temperature would remove the awkward "hover + press T to anchor °C" step. Not
started.

---

## 4. USB protocol: what is confirmed and what is not  `[PROTO]`

**Confirmed working on real hardware** by this project — used to calibrate every
static sweep:

```
0x36  0x37  0x3C  0x3D  0x3E  0x53  0x54  0x55  0x56  0x58
```

VID:PID `289D:0011`. Frame buffer exactly 342×260 uint16 = 177,840 B.
ROI rows 12–251, cols 2–321 = 320×240. EP `0x81`. Kick `58 5b 01 00`.

- `0x56`/`0x58` = set register address / read it back. The vendor app
  enumerates **80 config registers** this way; almost all return zero. The
  populated ones are the serial, a firmware date (`"Jul 16 2"`), the model
  (`"CQ-DBAX"`) and the pipeline-mode string (`"Compact"`).
- **Replaying the vendor's whole config phase changed nothing:**
  123.6 DL → 123.8 DL (1.00×). It is device identification, not image
  conditioning. `0x37 fc000400` is just the start command.
- `0x4e` reads return a 64-byte ASCII blob — plausibly calibration/identity
  resource loading. **Untested. Not yet replayed against a live camera.**
- `0x35` read 44× at startup. Purpose unknown.

Evidence: `tests/repro_vendor_init.py`, `tests/measure_gain_noise.py`.

---

## 5. The real open question  `[FRAME]`

The same camera, on defaults, gives the phone a clean absolute-temperature
image. The desktop viewer, also on defaults, gets **65 DL of fixed pattern**.

**The difference is not NUC configuration — finding 2 rules that out.** It is
something about init or frame handling. Candidates:

- the `0x4e` resource reads the viewer never performs
- `0x37 fc000400`, the start command the viewer never sends
- frame format / ROI extraction differences

None tested end-to-end. This is where the work should go.

---

## 6. Vendor SDK internals, established by disassembly  `[PROTO] [AGC]`

`libseekcamera.so` is available in two builds:

- armeabi-v7a, 367,744 B — from `seekspy.apk` in the repo's own
  `v0.1-spyapk` release (SHA256 matches the recorded value). APKPure and
  APKMirror both 403/reset from here.
- **arm64-v8a, 485,040 B — pulled off the phone.** Prefer this: fixed 4-byte
  AArch64, no Thumb interworking, and it is the build actually running.

The library is stripped of `.symtab` but keeps `.dynsym`. It links **libyaml** —
config is a YAML document with dotted keys (`power_ctrl`, `device_ctrl`,
`camera_sync`, `settling_time_ms`, `max_transfer_size`).

Key strings in `.rodata`:

```
Cannot set agc mode while in auto state
Failed to set AGC mode
Cannot set linear agc lock mode while in auto state
Cannot set histeq agc gain limit while in auto state
Failed to trigger shutter: invalid mode
FlatField.bin
Failed to generate flat scene correction
```

`seekcamera_set_agc_mode`: `cmp r1,#1 ; bhi` → **agc_mode is a 2-value enum**
(0/1). Stored in a software struct at `camera+0x14d4` with a dirty flag at +4,
pushed toward hardware only if `*(camera+0x14d4+0x108)` is set, via a generic
`(device_handle, key)` writer.

**Unmet precondition, unexplained:** the device ships in AUTO and the SDK
refuses parameter writes while AUTO is active. Nothing found yet shows how to
leave AUTO. This is the most likely reason an earlier blind probe did nothing.

`libseekusb.so` is a **pure JNI pass-through** — `ControlTransfer(int, byte[],
int)`, `BulkTransfer(int, byte[], int)` — and holds no protocol. Request codes
are chosen by the *caller*, in `libseekcamera.so`.

**Tooling gotchas, both of which produce plausible-looking garbage:**
- Thumb-ness is **bit 0 of the symbol value**, not the ELF symbol type. Every
  export has an odd address (`seekcamera_get_agc_mode` = `0xb259`). Decoding
  those as ARM yields nonsense.
- `.ARM.exidx` compact-model entries can resolve **mid-function**, so "extent up
  to the next boundary" can be 4 bytes and silently drop every `bl` in the body.

**Validated sweep:** all 10 confirmed codes recovered from both builds
(10/10). Request codes are **rare constants** — `0x53` occurs once, `0x55` twice —
while common values like `0x30` (342 hits) are ordinary immediates. Frequency is
the discriminator.

**Where static analysis is stuck:** reachability is over-broad — `set_agc_mode`
"reaches" 491 of 640 functions, so the intersection that should isolate AGC
codes returns everything. Exact function extents from `.ARM.extab` are needed.
Given finding 2, this line is now low priority.

---

## 7. Instrumented arm64 app on the phone  `[ANDROID] [BUILD]`

Status: **built, installed, gadget loads and accepts one client.** No USB trace
captured yet.

- Device: `SM-S938B`, Android 16, wireless ADB.
- Toolchain on the PC: Temurin JDK 17, apktool 2.9.3, build-tools r34.
- arm64 Frida gadget 17.2.17 (24,872,296 B, `e_machine=183`). The original
  `seekspy.apk` is armeabi-v7a only and **cannot install** on this phone.
- Rebuilt from the phone's own APKs: `base.apk` (3,457,057 B, 4,965 smali files)
  + `split_config.arm64_v8a.apk` (33,343,349 B).
- Injection point: `com/thermal/seeknano/MainActivity.smali` carries a
  `System.loadLibrary` even though the app is Flutter (native loading otherwise
  goes through `FlutterJNI`). `seekgadget` loads immediately before it.
- Signed with a debug key, installed as `com.thermal.seeknano`, 13,844,880 B.
- Gadget config as a string resource `frida_gadget_config`
  (`interaction.type=listen`, 127.0.0.1:27042) — without this the attach raised
  `TransportError: connection closed`.

Three build failures, all fixed and worth not repeating:

1. **Smali patch syntax.** Extracting the register by splitting the line gave
   `const-string const-string v0, "seekgadget"`, which does not assemble. The
   register comes from the const-string *group*.
2. **`res=-110 Failed to extract native libraries`.** The manifest sets
   `android:extractNativeLibs="false"`, requiring uncompressed, page-aligned
   libs. Setting it to `true` is correct for an instrumented debug build.
3. **Attach target name.** An embedded gadget registers as a process literally
   named `Gadget`, not under the host app's name.

**The remaining blocker:** the gadget serves **one client per app launch**, and
the USB session opens during app start — before the hook is attached. So the
capture sees zero transfers. Needs either a much faster attach, or a way to make
the app re-open the camera while hooked (background/foreground, or unplug and
replug).

---

## 8. Desktop viewer state  `[GUI] [FRAME]`

- Full-bleed canvas, 38 px HUD strip, 52 px icon rail. Verified on screen.
- **Scanline fault fixed** — mid-frame gain change rejected; the user confirmed
  "band is gone". Rows 238–239 held ~35,836 DL vs ~6,530, ratio 5.49×.
- **Stale-flat veto fixed** (`29df07a`) — a loaded-but-rejected `ffc_latest.raw`
  blocked the shutterless fallback permanently via an `if/elif`, so the app
  corrected nothing all session. Guard now tests whether a correction is being
  *applied*, not whether a reference *exists*.
- Rail hover corners fixed — a second rounded rect at radius 5 over the tile's
  own radius 6 interleaved into a jagged double corner. One outline now
  recolours.
- Four crash sources fixed. 15 test suites pass twice; `gui_smoke` passes;
  `--check-pil`/`--check-assets`/`--selftest` exit 0.

**Known open, do not trust:** the bad-pixel map returns **0** on a real wall
capture where the older single-image test found 9. Gain lock holds gain 3 while
the camera wants 20, so saturated frames climb. Both pre-existing.

---

## 10. HANDOFF — where to start, and what not to redo  `[META]`

Everything below is on disk. Paths are absolute.

**Do not rebuild these:**

| What | Where |
|---|---|
| Real arm64 `libseekcamera.so` (485 KB, off the phone) | `C:\a\_apk\arm64\x\` |
| `libapp.so` — Dart AOT snapshot, 5.38 MB | `C:\a\_apk\arm64\libapp.so` |
| `base.apk` + `split_arm64.apk` | `C:\a\_apk\arm64\` |
| Signed installable instrumented APK | `C:\a\_apk\arm64\signed.apk` |
| Frida gadget arm64 17.2.17 | `C:\a\_gadget\libseekgadget.so` |
| JDK / apktool / build-tools | `C:\a\_tools\` |

**Re-run these to reproduce any claim above:** `tests/measure_wall_scene.py`,
`measure_residual_split.py`, `measure_full_vs_hp.py`, `measure_hp_scale.py`,
`repro_display_wall.py`, `dis_arm64.py`, `dis_callgraph.py`.

**The two open leads, in priority order:**

1. **Dart pipeline** (`libapp.so`). My claim that the radiometric conversion is
   "in there" is **UNVERIFIED** — I listed the file and never established
   anything inside it. The isolate snapshot data is 2.16 MB at offset `0x40c0`,
   instructions 3.29 MB at `0x236940`. Only 5 dynsym entries survive
   (`_kDartIsolateSnapshotData` etc.), names are obfuscated, and keyword
   grepping yields only UI strings. **A Dart snapshot parser is required** to
   rebuild the object pool and recover real class/method names. That is a
   substantial piece of work — I mis-scoped it as an afternoon and should not
   have implied otherwise.

2. **USB init delta** (`0x4e` resource reads, `0x37 fc000400`). `0x37` was
   already replayed with the config phase and changed nothing
   (123.6 → 123.8 DL). **`0x4e` has never been tested.** Falsifiable in one
   run, and a null result is worth as much as a positive one.

**Phone state:** `SM-S938B`, Android 16, wireless ADB. The instrumented build is
currently installed as `com.thermal.seeknano`. The user was told to reinstall
from the Play Store. Gadget serves **one client per app launch** and the USB
session opens during app start, before a hook can attach — that is why
`tests/capture_usb.py` captured zero transfers. Fix that first if runtime
capture is wanted.

**Three traps that produce plausible-looking garbage rather than errors:**

- Dart AOT keyword grepping. `inflate`, `FlattenParser`, `postureFlat` all match
  NUC-shaped regexes.
- ARM Thumb detection: bit 0 of the symbol value, not the ELF symbol type.
- `.ARM.exidx` compact entries can resolve mid-function, truncating decodes.

**Do not repeat the failure mode.** Three times this session I announced a
conclusion ahead of the evidence — "no wall capture needed" (shipped as two
releases), "the residual is photon noise", and "the conversion is in
`libapp.so`". Each was a lead stated as a finding. Verify before claiming, and
say "not established" when that is the truth.

---

## 11. Corrections to my own earlier claims  `[META]`

Recorded because I got these wrong and the ledger should not repeat them.

| Claim | Correction |
|---|---|
| "No wall capture needed, 1.05% noise" | Measured on a 782 DL scene. On a wall it is ~10% of the span. **Wrong.** |
| "The residual is photon noise" | It survives 40 frames of averaging. It is **spatial**. **Wrong.** |
| "No gain setter found" | Overstated. The device refuses writes while in AUTO, which makes any naive probe uninformative. |
| "Build the background from median frames" | The app's **raw**-frame background is 8× cleaner (6.0 vs 48.8 DL). Backwards. |
| "The gain lock is the problem" | Not tested properly; the AGC hypothesis was never actually exercised. |
| "Timeline: ten minutes" | Said twice, overshot both times. |

**Method rules that held up:** calibrate every static sweep against codes
confirmed on real hardware, or the numbers are noise. Measure the hardest case,
not the flattering one. Look at the screen. When a hypothesis fails, say so
before reaching for the next one.