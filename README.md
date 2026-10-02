# Seek Nano Desktop Viewer

A desktop viewer and analysis station for the **Seek Nano** thermal camera
(USB `289D:0011`). The camera ships with a phone-only Android/iOS app and Seek
publishes no desktop SDK, so this project reverse-engineered the USB protocol
from the app's bundled native libraries and reimplemented it. It streams a
stable 320×240 thermal image at **25 fps** on Windows.

**Latest release: [v1.0.3](releases/tag/v1.0.3)**

## Interface

The picture runs **full-bleed** to the window edge. A thin HUD strip across the
top carries the status headline plus `fps / gain / seq` and whether the °C scale
is anchored; a slim icon rail down the right holds the controls. Every rail
control carries a tooltip, and the same actions are on the keyboard.

Icons rather than word labels, deliberately: wxWindows subpixel-renders text
over a dark background, which fringed every label red/blue/cyan (measured: 88%
of glyph pixels carried colour spread). A glyph has no subpixels to fringe.

Diagnostics stay out of the way — `Ctrl+L` shows the log field, `Ctrl+Alt+D`
the dev row.

## Quick start

1. Download `SeekNano.exe` from the [releases page](releases).
2. Windows has no driver for this camera. Run `SeekNanoDriverInstaller.exe` from
   `SeekNano-driver.zip` once (Device Manager fallback in
   [app/README.md](app/README.md)).
3. Plug the Nano in and run `SeekNano.exe`.
4. Press **S** to stream. Point at a flat wall and press **F** for the best
   picture — see [Getting a clean image](#getting-a-clean-image).

## Controls

| Key | Action |
|---|---|
| **S** | Start / stop stream |
| **F** | Capture flat-field reference (point at a uniform wall) |
| **C** | Next colormap |
| **P** | Save PNG snapshot |
| **D** | Dump raw frame |
| **Space** | Pause |
| **T** | Anchor a temperature point (hover the canvas first) |
| **[** / **]** | Isotherm threshold down / up (seeds from the current frame) |
| **O** | Toggle the diagnostic stamp |
| **Q** | Quit |
| **Ctrl+L** | Show / hide the log field |
| **Ctrl+Alt+D** | Show / hide the dev row |

Move the mouse over the canvas for a cursor readout with rubber-band region
statistics and a line profile. The rail's crosshair button cycles hot-spot
detection through `off → mark → outline → track → alarm`; the one below it
cycles sensitivity through `0.35 → 1.00 → 2.50`.

## Temperature

The camera publishes no transfer function and no calibration is recoverable
from the device — Seek's own baseline is nominally ±5 °C / 5 % on their
pipelines alone. So the viewer starts in **device units (DL)** and says so.

To get degrees, **anchor the scale against something you know**: hover the
cursor over a surface of known temperature and press **T**, entering its value.
Anchors persist in `seeknano_anchors.json` next to the exe.

- 0 anchors → DL only (the honest default)
- 1 anchor → offset only, level anchored
- 2 anchors → the linear map between them

Outside the anchor span the value extrapolates, and the display says so rather
than presenting an invented number as a measurement.

## Hot-spot detection

Finds regions notably hotter than the scene and reports where they are. Four
modes over one detector: **mark** (box plus crosshair on the hottest),
**outline**, **track** (fading trail, to watch a fault develop), **alarm**
(flashes the frame border).

Detection runs on corrected device-unit values *before* the contrast stretch —
stretching maps every frame across the full colormap, which would make every
frame hot by construction and the question meaningless.

**Known limitation.** On a strongly textured scene the detector cannot be both
sensitive and free of false positives. Measured on a synthetic ±160 DL periodic
texture, a genuine +300 DL hotspot is only about 2× the scene's own variation,
and the texture alone produces 21 candidate regions. That is a property of the
scene, not a defect in the detector — which is why the mode and sensitivity are
user-selectable rather than tuned to a fixed answer.

## Getting a clean image

**Press F on a flat wall.** The sensor's per-pixel offsets drift as it warms, so
an old reference actively hurts: measured here, a reference captured four hours
earlier took the noise from 223 DL to 318 DL. References are now banked per gain,
so the right one is restored at the gain that needs it. On a good capture the
reference takes neighbour-difference noise from ~240 DL down to single digits.

Defective elements are found by their **flicker** rather than their offset,
judged across the captured frames against a temporal median so the fixed pattern
cancels. Judging one averaged image missed four of six confirmed defects: their
offset moved between captures (+1144…+1679 DL, then +707…+910 DL) while they
stayed present in 100 % of live frames.

> **Known uncertain:** a real wall capture found **0** bad pixels where the
> earlier single-image test found 9 on that same reference. Unexplained — the
> bad-pixel map should not be trusted until a capture on a uniform surface is
> checked.

## Analysis

- **Cursor readout** — point temperature and region statistics under the cursor
- **Line profile** — drag across the canvas for a shift-drawn profile, sampled to
  192 points. This is the FLIR-Tools signature: read the profile as you pan and
  the frame collapse to zero is visible as the line dying at the edge
- **Isotherms** — **[** and **]** step the threshold; it seeds from the current
  frame's 90th percentile
- **CSV export** — the corrected values of the frame on screen
- **Replay** — `SeekNano.exe --replay capture.raw` plays a recording through the
  entire analysis layer with no camera attached, so the same tools work offline

## Headless modes

| Flag | Purpose |
|---|---|
| `--serve [port]` | Stream every raw frame to the LAN as UDP packets |
| `--replay <file>` | Analysis station over a recorded capture, no camera |
| `--dump-processed <dir> [n]` | Write what the window would draw |
| `--selftest` / `--check-pil` | CI gates, run by the build |
| `--dev-serve`, `--dev-bind` | Development hooks |

## Performance

Measured on the real device, and on the **screen** rather than just the app's
output:

| Metric | Value |
|---|---|
| Stream rate | 24.96 fps (25 is the camera's native rate) |
| Paints reaching the screen | ~20/s |
| Black frames in 250 screen grabs | **0** |
| Bulk transfer failures | 0 |

The bugs that mattered most were all found by measuring rather than by reading
code:

- **`np.median` cost 49 ms/frame** on the 3×3 speckle median, because it also
  forms the mean of the two central elements — wasted work for an odd count.
  `np.partition(stack, 4, axis=0)[4]` is bit-identical at 25 ms. This was the
  stall: paints measured **0.00/s** while the reader thread logged frames.
- **A `ZeroDivisionError` froze the picture silently.** The pacing guard tested
  `period` before the decrement but divided after it, so stepping 0.005 → 0.0
  killed the reader thread and the GUI went on painting the last frame it had —
  paints past 16,000 against a frozen `frame_seq`.
- **PIL was never bundled**, so every released exe threw `ModuleNotFoundError`
  once per frame — 5,517 times in one session. The retry backoff advanced the
  wrong timestamp, so it never recovered. Now bundled, and CI asserts the frozen
  exe can import it, so a missing bundle fails the build instead of reaching you.

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

The buffer is exactly 342×260 `uint16`, not a header plus an image — confirmed
against a captured phone trace. Frame kickoff is `58 5b 01 00`, byte-identical
to the phone app's. Detail in [docs/protocol.md](docs/protocol.md).

Three earlier "fixes" were wrong and reverted: low-byte masking (destroyed real
data), full-background subtraction (removed scene content), and a temporal
blend — which measurably reduced frame-to-frame movement 1.88× and was reverted
anyway because it ghosts when the scene shifts. Trading visible ghosting for a
better metric is a bad trade.

## Repository layout

| Path | Contents |
|---|---|
| `app/viewer.py` | The viewer: pipeline, UI, USB transport, analysis |
| `app/hotspot.py` | Hot-spot detection (pure numpy, no scipy) |
| `docs/` | Protocol notes and analysis |
| `tests/` | CI-portable suites plus hardware-verification scripts |
| `diagnostics/` | Manual measurement and screen-capture drivers |
| `history/` | Reverse-engineering record (APK analysis, spy tooling) |
| `data/` | Recorded captures used by `--replay` |
| `driver/`, `transport/` | Windows driver installer and native transport |

`tests/gui_smoke.py` drives the real wx GUI. It earned its place by catching two
API mistakes that no unit test could see: `wx.EVT_CAPTURE_LOST` does not exist
(it is `EVT_MOUSE_CAPTURE_LOST`), and `GetContainingWindow` is not a wx method.

## Legal note

The APK is publicly distributed by Seek Thermal and was analysed here strictly
for interoperability with hardware its owner already possesses. No Seek code or
assets are redistributed — only our own analysis notes, scripts, and derived
protocol documentation.