# Seek Nano Desktop Viewer

A desktop viewer for the **Seek Nano** thermal camera (USB `289D:0011`). The camera
ships with a phone-only Android/iOS app and Seek does not publish a desktop SDK, so
this project reverse-engineered the USB protocol from the app's bundled native
libraries and reimplemented it. It now streams a stable 320×240 thermal image at
**25 fps** on Windows.

**Latest release: [v0.5.0-hotspot](releases/tag/v0.5.0-hotspot)** — hot-spot detection
and a fix for a stall that left the window showing a static picture.

## UI

The viewer runs in a dark, HUD-style theme (Seek orange on deep navy, cyan
readouts) matching the emblem icon (`assets/make_icon.py` builds it;
PyInstaller embeds it into the exe). Diagnostics stay out of sight:

- the log field is hidden — `Ctrl+L` shows/hides it
- the dev upload row (frame upload to a dev host, endpoint retries, bind
  probes) is hidden — `Ctrl+Alt+D` shows/hides it
- the CLI dev modes still work as before: `--dev-serve`, `--dev-bind`,
  `--dump-processed`, `--serve`, `--selftest`, `--check-pil`

## Quick start

1. Download `SeekNano.exe` from the [releases page](releases).
2. Windows will not have a driver for this camera. Run `SeekNanoDriverInstaller.exe`
   from `SeekNano-driver.zip` once (Device Manager fallback in
   [app/README.md](app/README.md)).
3. Plug the Nano in and run `SeekNano.exe`.
4. Press **Start stream**, then **F** while pointing at a flat wall. That builds the
   flat-field reference and the bad-pixel map, and it is worth doing every session —
   see [Getting a clean image](#getting-a-clean-image).

## Controls

| Key | Action |
|---|---|
| **S** | Start / stop stream |
| **F** | Capture flat-field reference (point at a uniform wall) |
| **C** | Next colormap |
| **P** | Save PNG snapshot |
| **D** | Dump raw frame |
| **Space** | Pause |
| **Q** | Quit |

Two more buttons cycle hot-spot detection: **hot spots** steps through
`off → mark → outline → track → alarm`, and **sens** steps the detection sensitivity
through `0.35 → 1.00 → 2.50`.

## Hot-spot detection

Finds regions that are notably hotter than the scene and reports where they are.
Four modes, all sharing one detector:

- **mark** — box each region with a crosshair on the hottest
- **outline** — box them without the crosshair
- **track** — leave a fading trail so you can see a fault developing
- **alarm** — flash the frame border while any region is detected

**Values are in device units (DL), not degrees, and are labelled that way.** No
absolute temperature is available from this camera: there is no calibration in the
app, in this codebase, or in the captured symbol and string dumps. Reporting degrees
would mean inventing a conversion, so the viewer does not pretend to. It labels each
region relative to the current scene instead, which is the honest reading.

Detection runs on corrected device-unit values *before* the contrast stretch.
Stretching maps every frame across the full colormap, which would make every frame
hot by construction and the question meaningless.

**Known limitation.** On a strongly textured scene the detector cannot be both
sensitive and free of false positives. Measured on a synthetic ±160 DL periodic
texture, a genuine +300 DL hotspot is only about 2× the scene's own variation, and
the texture itself produces 21 candidate regions. That is a property of the scene
rather than a defect in the detector, which is why the mode and sensitivity are
user-selectable rather than tuned to a fixed answer.

## Getting a clean image

**Press F on a flat wall each session.** The sensor's per-pixel offsets drift as it
warms, so yesterday's reference actively hurts: measured on this hardware, a
reference captured four hours earlier took the noise from 223 DL to 318 DL. The
viewer judges the reference against live data and rejects it when it stops helping,
but a fresh capture is always better. On a good capture the reference takes
neighbour-difference noise from ~240 DL down to single digits.

Defective elements are mapped separately. They are found by their *flicker* rather
than their offset, judged across the captured frames against a temporal median so
the fixed pattern cancels out. Judging a single averaged image missed four of six
confirmed defects, because their offset moves between captures while they stay
present in every frame.

> **Known uncertain:** the most recent real wall capture found **0** bad pixels,
> where the earlier single-image test found 9 on that same reference. This is
> unexplained and the bad-pixel map should not be trusted until a capture on a
> uniform surface is checked.

## Performance

Measured on the real device and measured on the **screen**, not just the app's
output:

| Metric | Value |
|---|---|
| Stream rate | 24.96 fps (25 is the camera's native rate) |
| Paints reaching the screen | ~20/s |
| Black frames in 250 screen grabs | **0** |
| Bulk transfer failures | 0 |

Two fixes mattered a lot here, both found by measuring rather than reading the code:

- The 3×3 median cost **49 ms/frame** because `np.median` also computes the mean of
  the two central elements — wasted work when the count is odd. `np.partition` at
  k=4 is bit-identical and takes 25 ms. This was the stall: paints measured
  **0.00/s**, so the window sat static while the reader thread logged frames.
- Peak-finding for the hottest-region mode cost **50–79 ms/call** because it cut at
  the 90th percentile, masked ~10% of the image, then built a full-frame boolean
  mask per component. Replaced with local-maxima peak finding at ~10 ms, whose cost
  no longer scales with how busy the scene is.

## What the project found

The Android app (`com.thermal.seeknano` 1.5.0) ships Seek's full proprietary
thermal SDK as native `.so` libraries — the protocol itself, inside a freely
downloadable app.

| Fact | Value |
|---|---|
| Vendor ID | `0x289D` (Seek Thermal) |
| Product ID | `0x0011` |
| Bulk IN endpoint | `0x81` on interface 0 |
| Control transfers | `0xC0` in / `0x40` out |
| Frame buffer | **177,840 B** = 342 × 260 × 2 bytes |
| Usable image region | rows 12–251, cols 2–321 = **320 × 240** |
| Frame rate | 25 fps, native |

The 177,840-byte buffer is exactly 342×260 `uint16`, not a header plus image — that
was confirmed against a captured phone trace. Frame kickoff is `58 5b 01 00`, which
is byte-identical to the phone app's. Detail is in [docs/protocol.md](docs/protocol.md)
and [docs/native-analysis.md](docs/native-analysis.md).

### Three earlier "fixes" that were wrong, and were reverted

Recorded because the measurements are the interesting part:

- **Low-byte masking** — masked the low byte of each pixel on the theory it was
  noise. It was real data; masking it destroyed the image.
- **Full-background subtraction** — subtracting the whole averaged background
  removed scene content along with the pattern.
- **Temporal blend** — measurably reduced frame-to-frame movement (1.88×) and was
  reverted anyway, because it ghosts whenever the scene shifts. Trading visible
  ghosting for a better metric is a bad trade.

A stutter was chased that did not exist; it turned out to be aliasing in my own
sampling.

## Repository layout

| Path | Contents |
|---|---|
| `app/viewer.py` | The viewer. Pipeline, wx UI, USB transport |
| `app/hotspot.py` | Hot-spot detection (pure numpy, no scipy) |
| `docs/` | Protocol notes, native analysis, symbols, [security posture](docs/security.md) |
| `tests/` | portable suites run in CI (test_dev_mode.py, test_audit_fixes.py) plus test_*.py hardware-verification suites written for the camera box (C:a dev layout); manual measurement scripts live in diagnostics/ |
| `artifacts/` | Captured phone trace and analysis logs |

Development hooks (`--dev-serve`, `--dev-bind`, the GUI dev row) are documented in
the README history and remain for diagnostics.

## Legal note

The APK is publicly distributed by Seek Thermal and was analysed here strictly for
interoperability with hardware its owner already possesses. No Seek code or assets
are redistributed in this repository — only our own analysis notes, scripts, and
derived protocol documentation.
