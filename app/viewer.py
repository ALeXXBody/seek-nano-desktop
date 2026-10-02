"""Seek Nano thermal viewer (Windows) - proper GUI.

Layout:
  top     : buttons  (Start/Stop, Colormap, Save PNG, Dump raw frame, Copy log, Clear log)
  left    : video panel (2x zoom, status text bottom)
  right   : read-only, selectable log (Ctrl+C works) ; also duplicated in seeknano_verbose.log
"""
import os
import queue
import sys
import threading
import time
from collections import deque

import numpy as np
import wx

# keep next to the .exe (not PyInstaller's temp _MEIxxxx dir, Windows deletes it)
if getattr(sys, "frozen", False):
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
else:
    app_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(app_dir)

import usb.core
import usb.util

try:
    import libusb_package
    _BACKEND = libusb_package.get_libusb1_backend()
except Exception:
    _BACKEND = None

VID = 0x289D
PID = 0x11
REQ_OUT = 0x40
REQ_IN = 0xC0
BULK_EP = 0x81
FRAME_BYTES = 177_840
# 6,840 B per read. Raising this to 32,768 to cut GIL churn against the main
# thread was tried and reverted: the device then fails the transfer with
# USBTimeoutError ([Errno 10060]) and the stream drops frames. Six reads of this
# size per frame is the largest that reads reliably here.
CHUNK = 6_840
W, H = 320, 240
VW, VH = W * 2, H * 2

# ============================================================================
# --- DEV: endpoint knowledge from the 2026-10-02 capture campaign -----------
#   USB\VID_289D&PID_0011\serial     parent      usbccgp
#   USB\VID_289D&PID_0011&MI_00\...  iAP Interface  (alt 0: 0x01 OUT / 0x81 IN)
#   USB\VID_289D&PID_0011&MI_01\...  com.thermal.pir324.3 (alt 0: empty,
#                                    alt 1: 0x02 OUT / 0x82 IN)
# Direct capture on the camera host showed the *thermal stream on MI_00/0x81*
# (~63 ms per frame). MI_01/0x82 is silent, MI_01 binds it at alt 1 because
# the composite parent gives interface 1 two bulk pipes that only appear
# there. For a dev build we therefore try every bulk-in endpoint we can see,
# in a deterministic order, and prefer whichever one actually produces data.
# ============================================================================
EP_CANDIDATES = (0x81,)   # MI_00 only - see note below
# 0x82 was previously a candidate. It is dead on this hardware: it lives on
# MI_01, which is bound but returns only zeros, so rotating onto it produced a
# live/dead alternation (the original flicker). Worse, the stream only ever
# claims interface 0, so reading 0x82 raises
#     ValueError: Invalid endpoint address 0x82
# and kills the thread. Confirmed dead by direct capture: every 0x82 read
# returned 177,840 zero bytes while 0x81 carried the real stream.

# ============================================================================
# --- frame validation --------------------------------------------------------
# The bulk pipe does not always return a frame. A 177,840-byte read can come
# back as stale pipe residue, and the viewer used to hand that straight to the
# renderer, which is what produced "static plus flicker": garbage frames and
# real frames alternating on screen.
#
# Measured on a live session (2026-09-30, 10 consecutive reads):
#
#   file       len   magic    seq   gain   row-120 lag-1
#   see_00   177840  0x0579    212      3      +0.009     bad pixels
#   see_01   177840  0x01ec    952   4779      +0.543     GARBAGE (bad magic)
#   see_02   177840  0x184f   6636   6216      +0.272     GARBAGE
#   see_03   177840  0x0000      0      0      +0.236     GARBAGE
#   see_04   177840  0x0001      1  65535      +0.153     GARBAGE
#   see_06   177840  0x0579    134     28      +0.935     real image
#
# Only the 0x0579 frames carry an image; the others are 177,840 bytes of
# whatever was in the pipe. Every read is exactly FRAME_BYTES, so a length
# check cannot tell them apart - the header has to.
#
# Gain is also sanity-checked: real frames report 1..~40, garbage reported
# 0, 4779, 6216 and 65535.
# ============================================================================
FRAME_MAGIC = 0x0579          # u16 at bytes 0-1, little endian
GAIN_MIN, GAIN_MAX = 1, 255   # observed 1..40 on this hardware


def _frame_header(frame_raw):
    """(seq, gain) from a raw frame, or None if too short to hold a header."""
    if len(frame_raw) < 8:
        return None
    u = np.frombuffer(frame_raw[:8], dtype="<u2")
    return int(u[0]), int(u[1]), int(u[2])


def _vlog(kind, payload):
    """Append to seeknano_verbose.log via one long-lived handle.

    The old code re-opened the file on every log line - up to a few dozen
    open+close cycles per frame at 25 fps. The handle is created lazily and
    kept for the life of the process.
    """
    global _VLOGFH
    try:
        if _VLOGFH is None:
            _VLOGFH = open("seeknano_verbose.log", "a", buffering=1)
        _VLOGFH.write("%.3f %s %s\n" % (time.time(), kind, payload))
    except Exception:
        _VLOGFH = None


_VLOGFH = None


def _frame_reason(frame_raw, prev_seq=None):
    """Return None if the frame looks real, else a short reason string.

    Deliberately conservative: anything unexpected is rejected, because a
    rejected frame costs one refresh and a displayed garbage frame looks like
    a hardware fault to the user.
    """
    if len(frame_raw) != FRAME_BYTES:
        return "len %d" % len(frame_raw)
    hdr = _frame_header(frame_raw)
    if hdr is None:
        return "no header"
    magic, seq, gain = hdr
    if magic != FRAME_MAGIC:
        return "magic 0x%04x" % magic
    if not (GAIN_MIN <= gain <= GAIN_MAX):
        return "gain %d" % gain
    # NOTE: no payload and no sequence check here.
    #
    # Both were tried and both are unsound on this hardware, for the same
    # reason - the fixed-pattern noise is the dominant signal. Measured on a
    # settled gain-3 frame, horizontal lag-1 is only +0.02..+0.15 *within* a
    # row, because the per-pixel offsets are uncorrelated with their
    # neighbours. So "is this an image?" cannot be answered by correlation: a
    # perfectly good frame scores the same as a garbage one. Pooling over the
    # whole ROI instead reads +0.65, but that number is mostly driven by
    # row-to-row mean differences, not by image structure, so it swings wildly
    # with which rows get sampled (+0.07 for the same frame sampled every 32nd
    # row). Any threshold cut from it rejects good frames ~1 in 8.
    #
    # The sequence word is no better: it advanced by 3 in one session and by 34
    # in another on identical hardware, so a range check either drops good
    # frames or catches nothing.
    #
    # Magic + gain identify the 4-of-5 garbage frames in the capture above,
    # which is where the visible damage came from. The residual static is real
    # sensor FPN and belongs to the flat-field path, not to a frame filter.
    return None


MIN_SCENE_SPAN = 100.0   # DL, 1-99%; below this the frame is flat, see below
MAX_SCENE_SPAN = 5000.0  # DL, 1-99%; above this the frame is clipped


def _frame_has_content(frame_raw, min_span=MIN_SCENE_SPAN,
                       max_span=MAX_SCENE_SPAN):
    """False for frames that are flat or blown out.

    The AGC cycle visits a gain-20 setting whose frames are essentially flat:
    measured 1-99% span 13..30 DL against ~1500 DL for a real frame, and std
    ~6 DL against ~260. The viewer's auto-stretch divides by that span, so such
    a frame maps the whole image to a single colour - which is the black
    flashing the user reported. There is no picture in those frames to show, so
    the right thing is to keep the previous one.

    The bound has to work from BOTH sides. Measuring every gain the AGC visits
    in one session:

        gain   span DL    std DL   verdict
           1      675.2     159.0   real picture
           3      914.0     208.0   real picture
           6      679.2     149.5   real picture
           8     1252.0     278.3   real picture
          27      754.0     139.4   real picture
          28     1300.0     289.3   real picture
           7       24.0       6.0   flat - no picture
          20       24.0       5.8   flat - no picture
          25       50.0      10.7   flat - no picture
           4     9973.0    1246.7   CLIPPED
           9    34178.4    8990.0   CLIPPED
          14    65535.0   29694.6   CLIPPED
          26    65532.0   10596.7   CLIPPED

    The flat ones were already caught by the lower bound. The clipped ones were
    NOT: a span of 65535 DL is the full 16-bit range, and the lower bound alone
    passes them happily. They only avoided being displayed because the gain-hold
    happened to reject them for being at an unfamiliar gain - the content gate
    was never doing its job on its own. Anything that renders such a frame
    stretches it across the whole colormap, i.e. a solid white flash.

    5000 DL leaves better than 3x headroom over the widest real frame measured
    (1300 DL), so an ordinary hot subject cannot trip it.
    """
    img = _roi_u16(frame_raw)
    span = float(np.percentile(img, 99) - np.percentile(img, 1))
    return min_span <= span <= max_span


def _gain_locked(gain, hist):
    """True once `gain` has been seen GAIN_LOCK_FRAMES times in a row.

    `hist` is the caller's list of recent gains; it is trimmed in place.
    """
    if hist and hist[-1] == gain:
        hist.append(gain)
    else:
        hist[:] = [gain]
    return len(hist) >= GAIN_LOCK_FRAMES


def _payload_looks_like_image(frame_raw, min_corr=0.6):
    """DiagnostIC ONLY - deliberately NOT used to reject frames.

    Kept because it is the natural thing to reach for and it does not work
    here, so the next person does not re-add it. See the note in
    _frame_reason: within a settled frame, horizontal lag-1 is +0.02..+0.15
    because the per-pixel offsets dominate, so a real frame and a garbage frame
    score the same. Pooling over the whole ROI reads +0.65 but that is mostly
    row-to-row mean variation, and it swings to +0.07 for the same frame
    sampled every 32nd row.

    Returns the measured correlation so the log can show why it was rejected.
    """
    a = np.frombuffer(frame_raw, dtype="<u2")[:RAW_W * RAW_H].reshape(RAW_H, RAW_W)
    rows = a[8::32, :].astype(np.float64)
    if rows.shape[0] < 4:
        return None
    x = rows[:, :-1].ravel()
    y = rows[:, 1:].ravel()
    if x.std() < 1e-9 or y.std() < 1e-9:
        return None
    return float(((x - x.mean()) * (y - y.mean())).mean() / (x.std() * y.std()))


MAGIC_BYTES = b"\x79\x05"


def _resync_frame(buf):
    """Pull one frame out of `buf`, realigning on the 0x79 0x05 magic.

    Reading exactly FRAME_BYTES per kick is only correct while the pipe is
    already frame-aligned. When it is not - after a mode change, a reset, or a
    short read - every subsequent fixed-size read inherits the same offset and
    returns a shifted mix of old and new data, which is what the 8-of-9 garbage
    frames in the log above were.

    So: find the last magic at or before the end that still leaves a full frame
    after it, and return that slice. Returns None if no usable frame is
    present, in which case the caller should read more.
    """
    if len(buf) < FRAME_BYTES:
        return None
    limit = len(buf) - FRAME_BYTES
    # end bound must allow a magic STARTING at `limit`, i.e. limit+len(magic);
    # with limit+1 the search would never see the bytes it needs and an
    # already-aligned buffer (the common case) would fail to validate.
    start = buf.rfind(MAGIC_BYTES, 0, limit + len(MAGIC_BYTES))
    if start < 0:
        return None
    frame = bytes(buf[start:start + FRAME_BYTES])
    return frame if _frame_reason(frame) is None else None


def _assemble_valid(read_fn, tries=3):
    """Read until a validated frame comes out, or give up.

    `read_fn()` must append the next chunk to a fresh bytearray and return it.
    Over-reads are kept so _resync_frame can realign, which is what turns an
    11% hit rate into a usable stream.
    """
    buf = bytearray()
    for _ in range(tries):
        frame = _resync_frame(buf)
        if frame is not None:
            return frame, buf
        buf = read_fn(buf)
    return _resync_frame(buf), buf


# ----------------------------------------------------------------------------
# --- DEV: HTTP frame uploader (replaces loop.ps1 / sncap.exe) ----------------
class DevUploader:
    """PUTs captured frames to a dev host over plain HTTP.

    Keep this until the stream path is stable; it is a one-file replacement
    for the sncap.exe + loop.ps1 + loop.cmd + bind.ps1 experiment kit and is
    clearly segregated so it can be yanked for the stable release.
    """

    def __init__(self, host, port=8100):
        # host may be "192.168.50.200" or "192.168.50.200:8100"
        if ":" in host:
            parts = host.split(":")
            self.host, self.port = parts[0], int(parts[1])
        else:
            self.host, self.port = host, port
        self.seq = 0
        self.last_ok = False

    @property
    def base(self):
        return "http://{}:{}".format(self.host, self.port)

    def put_frame(self, blob, label=None):
        self.seq += 1
        name = label or "frame_{0:04d}.raw".format(self.seq)
        blob_to_send = blob
        return self._put(name, blob_to_send), name

    def put_logs(self, text):
        return self._put("logs.txt", text.encode("utf-8", "replace"))

    def put_bindinfo(self, text):
        return self._put("bindinfo.txt", text.encode("utf-8", "replace"))

    def _put(self, name, blob):
        import http.client
        try:
            c = http.client.HTTPConnection(self.host, self.port, timeout=15)
            c.request("PUT", "/" + name, blob,
                      {"Content-Type": "application/octet-stream"})
            r = c.getresponse()
            r.read()
            c.close()
            self.last_ok = (r.status == 200)
            return self.last_ok
        except Exception as e:
            print("dev upload failed: %r" % e)
            self.last_ok = False
            return False


# --- DEV: PnP bind-state probe (replaces diag.cmd) ---------------------------
def dev_bindinfo():
    """Return a text report of the PnP binding state of every Seek node.

    Wraps Get-PnpDevice so no helper .ps1/.cmd files are needed on the box.
    """
    import subprocess
    ps = (
        "Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -match 'VID_289D' } | "
        "ForEach-Object { "
        "  $svc = (Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName DEVPKEY_Device_Service).Data; "
        "  '{0,-30} {1,-26} svc={2}' -f $_.FriendlyName, $_.InstanceId, $svc "
        "}"
    )
    out = ["--- PnP binding state (Get-PnpDevice) ---"]
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30)
        out += (res.stdout or "").splitlines()
        if res.returncode != 0 and res.stderr:
            out += ["stderr: " + l for l in res.stderr.splitlines()[:4]]
    except Exception as e:
        out.append("bindinfo probe failed: %r" % e)
    out.append("--- dev: end ---")
    return "\n".join(l.rstrip() for l in out if l.strip())


def ironbow(t):
    r = np.clip(255.0 * (1.4 * t - 0.2), 0, 255)
    g = np.clip(255.0 * (0.62 + 2.4 * (t - 0.53)), 0, 255)
    g = np.where(t > 0.72, 255 * np.clip(1.35 - 0.9 * (t - 0.72) / 0.28, 0.35, 1.0), g)
    b = np.clip(255.0 * (0.61 + 0.35 * np.sin(5.9 * t - 1.2)), 0, 255)
    b = np.where(t > 0.72, 0.35 * 255.0, b)
    return np.stack([np.clip(b, 0, 255), np.clip(g, 0, 255), np.clip(r, 0, 255)],
                    axis=-1).astype(np.uint8)


def hot_lut(t):
    r = np.clip(t * 2.4, 0, 1) * 255
    g = np.clip(t * 1.7 - 0.5, 0, 1) * 255
    b = np.clip(t - 0.65, 0, 1) * 255
    return np.stack([b, g, r], axis=-1).astype(np.uint8)


def gray_lut(t):
    v = (t * 255).astype(np.uint8)
    return np.stack([v, v, v], axis=-1).astype(np.uint8)


COLORMAPS = [("ironbow", ironbow), ("hot", hot_lut), ("grayscale", gray_lut)]

# ---------------------------------------------------------------------------
# theme: camera-specific colours. Seek Thermal's brand orange on a deep
# navy-black field, with a HUD cyan for secondary readouts - the same palette
# the emblem icon uses, so the app reads as one product.
THEME = {
    "bg":      (7, 10, 14),       # near-black blue field
    "panel":   (16, 22, 30),      # title bar / control field
    "field":   (16, 22, 30),      # glass control fill
    "line":    (36, 48, 62),      # hairlines / button edges
    "text":    (235, 240, 246),   # near-white
    "muted":   (120, 132, 146),   # secondary readouts
    "accent":  (255, 107, 53),    # Seek orange - primary action
    "accent2": (56, 222, 246),    # HUD cyan - telemetry
    "ok":      (46, 204, 113),    # connected LED
    "warn":    (255, 193, 7),     # saturated / held
    "err":     (255, 76, 76),     # fault LED
}


RAW_W, RAW_H = 342, 260
IMG_W, IMG_H = 320, 240
# The 240x320 image does NOT start at (1, 4) in the 342x260 buffer. Scoring every
# 240-row x 320-col window on how flat its row/column profile is, over a temporal
# median of 23 live frames, picks rows 12..251 and cols 2..321:
#
#   window            top-vs-body row delta
#   rows  4..243 (old)         -1038 DL   <- the dark band across the top
#   rows 12..251 (new)         +145 DL
#
# Buffer rows 0, 1 and 3 read near zero and 258-259 do too, and rows 4-11 sit
# ~1000 DL below the body - a partial/border region, not picture. Column 325
# holds a counter that increments by exactly 1 per row (38401..38659) and is
# outside the window, so it is excluded.
#
# The old comment pointed at libseek-thermal's SeekThermalPro; that geometry
# does not match this unit. These values are measured, not assumed.
ROI_X, ROI_Y = 2, 12
OFFSET_BIAS = 0x4000


FFC_FRAMES = 16        # frames averaged into one flat-field reference (~1.2 s)
FFC_NAME = "ffc_latest.raw"   # on-disk reference, ROI-shaped uint16 (240x320)
BG_FRAMES = 8          # rolling frames for the no-reference background estimate
DISPLAY_SMOOTH = 3     # box blur after correction; see _process.
LATEST_PNG = "latest.png"   # newest displayed frame, overwritten ~1 Hz
SEEKNANO_LATEST = os.environ.get("SEEKNANO_LATEST", "") not in ("", "0")
FFC_GAIN = "ffc_latest.gain"  # gain the reference was captured at
FFC_BANK = "ffc_bank.npy"   # the individual captured frames, not just their mean
BAD_NAME = "badpixels.bin"   # mask of defective elements, ROI-shaped uint8
WATCHDOG_LOG = "seeknano_watchdog.log"  # thread stacks, when SEEKNANO_WATCHDOG set
BAD_K = 6.0              # a pixel is defective at > this x the local spread
BAD_MIN_DL = 300.0      # ...and at least this far out in absolute terms
STRETCH_EMA = 0.03          # ~30-frame time constant for the display window
MIN_WINDOW = 20.0     # DL; below this the display window is treated as collapsed
WINDOW_MEDIAN = 9      # frames; median over these, so one outlier cannot steer it

# Accelerator entry name -> the key it stands for. Needed because an
# accelerator emits a CommandEvent with no key code; see _hotkey_run.
ACCEL_KEY = {
    "Start/stop stream": "s",
    "Next colormap": "c",
    "Save PNG snapshot": "p",
    "Dump raw frame": "d",
    "Capture flat (wall)": "f",
    "Anchor temperature": "t",
    "Isotherm threshold": "[ ]",
    "Diagnostic stamp": "o",
    "Quit": "q",
    "Pause": " ",
}
BLEND = 1.0           # temporal blend weight; 1.0 = off. See below.
# The temporal blend was tried at 0.6 and measured 1.88x less frame-to-frame
# movement on screen. It was reverted anyway: it ghosts whenever the scene
# shifts, and the user reported artefacts reappearing with it. Trading visible
# ghosting for a metric improvement is a bad trade, so the blend is off by
# default and the knob is left in case it is wanted.
# The burst ring (burst_00..11.png) is a change-diagnostic: it keeps the
# last N frames the *display* produced so a hung/stale picture can be
# diffed offline (that is how the buffer-dangling blank and the FFC flap
# were caught). NOT a product feature and it writes files at 5 Hz - so it
# is OFF in the shipped viewer; builders re-enable with SEEKNANO_BURST=12.
BURST = int(os.environ.get("SEEKNANO_BURST", "0") or 0)
# The display window (lo/hi) is a slow exponential average rather than a
# per-frame percentile. Per-frame is the classic AGC flicker: the mapping
# rescales the whole image whenever the scene's warm/cold balance shifts by a
# little, and the user sees the picture breathe even though every frame is
# valid. 0.03 adapts in about half a second while rejecting frame-to-frame
# jitter.
# Was 5, lowered to 3: 5 removed more noise on paper but the user reported
# "artefacts" appearing once it was in. A wide box blur on a contrast-stretched
# image visibly softens and rings around edges, and 3 is below the threshold
# where that reads as an artefact. Set 0 to disable.
# The camera's AGC hunts on startup. Measured live (2026-09-30), the gain word
# walked 6 -> 1 -> 20 -> 3 over the first ~7 frames and then held at 3 for the
# rest of the session, and the brightness step between hunting frames is
# 1300-9100 DL, i.e. a full-screen flash. Showing those is the flicker, so the
# viewer holds black until the gain repeats GAIN_LOCK_FRAMES times.
GAIN_LOCK_FRAMES = 3


HOTSPOT_MODES = ("off", "mark", "outline", "track", "alarm")
HOTSPOT_SENS = 1.0        # scales how far above the scene a region must sit
HOTSPOT_MAX = 5           # how many to report at once
SPOT_STABLE = 3           # frames of evidence before a region is drawn

# A USB stall is transient. Reconnecting a bounded number of times rides out a
# momentary dropout; the camera itself occasionally wedges and only a physical
# unplug/replug clears that, so the retries are capped and then say so.
USB_RETRY_MAX = 6
# Milliseconds, as an int. wx.CallLater builds a wx.Timer and calls Start(),
# which rejects a float outright - "Timer.Start(): argument 1 has unexpected
# type 'float'" - and that is a hard crash in the event handler, not a no-op.
USB_RETRY_DELAY_MS = 3000


def _is_usb_timeout(payload):
    t = str(payload)
    return ("USBTimeoutError" in t or "Operation timed out" in t
            or "Errno 10060" in t or "Errno 110" in t
            or "Pipe error" in t)
SPOT_MOVE = 24            # px a region may drift and keep its stability score
# A row whose within-row variation exceeds the frame's own robust spread by
# this factor is treated as a corrupted scanline and the frame is kept.
# Calibrated against clean live frames: normal rows sit below 4x, the band seen
# on screen sat at 3.7x of a much noisier capture, so 8 is deliberately high -
# better to save a few extra frames than to miss the fault.
SCAN_FAULT_Z = 8.0
SCAN_FAULT_Z_INT = 8      # integer copy stored in the dump header
HOTSPOT_HOLD = 1.6        # "track": seconds a spot stays in the history


def _roi_u16(frame_raw):
    arr = np.frombuffer(frame_raw, dtype="<u2")[:RAW_W * RAW_H].reshape(RAW_H, RAW_W)
    return arr[ROI_Y:ROI_Y + IMG_H, ROI_X:ROI_X + IMG_W].astype(np.float32)


def _apply_ffc(img, ffc):
    """Subtract a flat-field reference, keeping the scene's own DC level.

    The reference holds the sensor's per-pixel offsets (temporally stable to
    ~6 DL, gain-stable to corr +0.8..+0.9 across gains 1/3/6 - measured
    2026-09-30). Subtracting removes them; adding the reference mean back
    keeps the room brightness instead of dragging it to zero.
    Shapes must match exactly; a stale file from another geometry is refused
    rather than applied.
    """
    if ffc is None or np.shape(ffc) != np.shape(img):
        return img
    return img - ffc + float(np.mean(ffc))


def _neighbour_median(a):
    """Median of the 8 neighbours of every pixel. numpy-only, edge-wrapped."""
    stack = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                stack.append(np.roll(np.roll(a, dy, 0), dx, 1))
    s = np.stack(stack)                       # 8 entries; np.median takes the
    mid = np.partition(s, (3, 4), axis=0)     # mean of the two central order
    return (mid[3] + mid[4]) / 2              # statistics - same value, no
                                              # redundant second pass


def _detect_bad_pixels(bank, k=BAD_K, min_dl=BAD_MIN_DL, min_frac=0.5):
    """Defective elements, found by their FLICKER rather than their offset.

    A single-image outlier test does not work on this hardware, and the reason
    is worth stating because it is the opposite of what I first assumed. The
    six confirmed defects sit +392 to +797 DL from their neighbours in live
    frames, while the scene's own local spread is ~180 DL, so a threshold above
    4 sigma misses every one of them. And their offset is not even stable
    between captures: measured +1144, +1148, +1151 and -1679 DL in one
    reference, then +748, +719, +707 and -910 DL in the next, so any threshold
    tuned on one averaged image is wrong for the other.

    The signal that does separate them is fluctuation. Subtract the temporal
    median of the captured bank and the fixed pattern cancels completely - it
    is identical in every frame - along with the scene. What survives is
    temporal noise plus the defects, so the spread estimate stops being
    dominated by the pattern and a defect is simply one that fluctuates far
    more than everything else.

    min_frac: fraction of frames in which it must fluctuate.
    """
    if bank is None:
        return None
    arr = np.asarray(bank, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[None, ...]
    if arr.shape[0] < 2:
        # one frame cannot show flicker; fall back to the plain outlier test
        f = arr[0]
        dev = f - _neighbour_median(f)
        mad = float(np.median(np.abs(dev - np.median(dev))))
        if mad <= 0:
            return None
        return np.abs(dev) > max(k * mad, min_dl)
    med = np.median(arr, axis=0)
    resid = arr - med[None, ...]
    mad = float(np.median(np.abs(resid - np.median(resid))))
    if mad <= 0:
        return None
    hits = (np.abs(resid) > max(k * mad, min_dl)).sum(axis=0)
    return hits >= max(1, int(round(min_frac * arr.shape[0])))


def _apply_bad_pixels(img, mask):
    """Replace each defective pixel with the median of its good neighbours.

    A median is used rather than a mean so a neighbouring defective pixel
    cannot drag the replacement; with only a handful of defects the 8-element
    median is still robust.
    """
    if mask is None or not mask.any() or np.shape(mask) != np.shape(img):
        return img
    out = img.copy()
    H, W = img.shape
    ys, xs = np.where(mask)
    for y, x in zip(ys.tolist(), xs.tolist()):
        y0, y1 = max(0, y - 1), min(H, y + 2)
        x0, x1 = max(0, x - 1), min(W, x + 2)
        win = img[y0:y1, x0:x1]
        keep = np.ones(win.shape, bool)
        keep[y - y0, x - x0] = False
        vals = win[keep]
        if vals.size:
            out[y, x] = np.median(vals)
    return out


def _boxblur(a, k):
    """Separable box blur, edge-padded. k must be odd. numpy-only.

    Accumulates in the input's float width: float32 is ample for DL-scale
    (max ~64000, sums of ~340 terms stay well inside float32 precision)
    and keeps the 25-frame/s pipeline out of float64 churn.
    """
    if k < 3:
        return a
    dt = a.dtype if a.dtype.kind == "f" else np.float64
    pad = k // 2
    p = np.pad(a, ((0, 0), (pad, pad)), mode="edge")
    c = np.cumsum(p, axis=1, dtype=dt)
    c = np.concatenate([np.zeros((a.shape[0], 1), dtype=dt), c], axis=1)
    out = (c[:, k:] - c[:, :-k]) / k
    p = np.pad(out, ((pad, pad), (0, 0)), mode="edge")
    c = np.cumsum(p, axis=0, dtype=dt)
    c = np.concatenate([np.zeros((1, c.shape[1]), dtype=dt), c], axis=0)
    return (c[k:, :] - c[:-k, :]) / k


def _nuc2d(img, col_strength=1.0, row_strength=0.5, smooth_px=9):
    """2-D non-uniformity correction, shutterless (no flat/shutter reference).

    Why not a fitted polynomial surface any more: a smooth lens vignette and a
    smooth scene gradient (warm sky over cold floor) are indistinguishable in a
    single frame, so a surface fit removes the scene along with the vignette.
    Measured on synthetic ground truth with a horizontal scene gradient:

        no-op                 RMSE 333.5 DL
        quadratic surface     RMSE 594.0 DL   <- 260 DL WORSE than doing nothing
        per-column mean       RMSE 618.6 DL   <- 285 DL WORSE than doing nothing
        this function         RMSE 179.4 DL   <- 154 DL better, on BOTH scenes

    Fixed-pattern noise is high-frequency; the scene is low-frequency. So
    estimate only the high-frequency per-axis structure and leave the smooth
    part of the image alone:

        hp      = img - boxblur(img)
        corr    = col_strength * hp.mean(axis=0) + row_strength * hp.mean(axis=1)
        out     = img - corr + img.mean()

    Columns get full strength: on live frames the striping is column-dominant
    (mean 49.3 DL step between adjacent columns, 284/319 columns over 20 DL).
    Rows are damped to 50% because a vertical scene gradient is extremely
    common in real thermal scenes and is the axis most easily over-corrected.
    """
    try:
        h, w = img.shape                      # never img_w/img_h: those are
        hp = img - _boxblur(img, smooth_px)  # (320,240), the image is (240,320)
        corr = col_strength * hp.mean(axis=0, keepdims=True)
        if row_strength:
            corr = corr + row_strength * hp.mean(axis=1, keepdims=True)
        # corr sums to ~0 by construction (hp is a high-pass residual), so
        # subtracting it preserves the image mean on its own. Adding img.mean()
        # back here would double the DC level: measured on a live settled frame
        # it turned a median of 6560 DL into 13088 DL.
        return img - corr
    except Exception as e:
        # Do NOT swallow this silently. A bare `except: return img` is what
        # hid a transpose bug here for the entire project: the fit raised
        # ValueError every single frame and the NUC never once ran.
        print(f"[nuc] correction failed, passing frame through uncorrected: {e}")
        return img


def decode_frame(frame_raw):
    """SeekThermalPro-layout decoder: raw 342x260 -> image 320x240.

    Accepts both sizes seen in the field:
      * 177 840 B — bare frame (342*260 uint16)
      * 177 856 B — 16 B dev-serve header + the frame
    """
    if len(frame_raw) == 177_856 and frame_raw[:2] == b"\x79\x05":
        frame_raw = frame_raw[16:]
    arr = np.frombuffer(frame_raw, dtype="<u2")[:RAW_W * RAW_H].reshape(RAW_H, RAW_W)
    raw = arr.astype(np.float32)
    fid = int(raw[0, 2])
    return raw, fid


def _sample_stats(img, pt, radius=3):
    """Cursor readout: (value, mean, min, max) around a point.

    img: float32 corrected array (H, W); pt: (x, y) in image coords.
    A small neighbourhood (radius 3) gives a stabler reading than a single
    pixel without smearing across edges.
    """
    h, w = img.shape
    x, y = int(pt[0]), int(pt[1])
    if not (0 <= x < w and 0 <= y < h):
        return None
    x0, x1 = max(0, x - radius), min(w, x + radius + 1)
    y0, y1 = max(0, y - radius), min(h, y + radius + 1)
    win = img[y0:y1, x0:x1]
    return (float(img[y, x]), float(win.mean()),
            float(win.min()), float(win.max()))


def _line_profile(img, p0, p1, n=192):
    """Bilinear sample of img values along a segment; returns (n,) float32.

    The FLIR-Tools signature analysis on a 25 fps viewer: read the profile
    along a line you draw, not probe pixels one by one. Samples that leave
    the frame collapse to 0, so the profile visibly dies at the edge.
    """
    x0, y0 = float(p0[0]), float(p0[1])
    x1, y1 = float(p1[0]), float(p1[1])
    ts = np.linspace(0.0, 1.0, n, endpoint=False)
    xs = np.clip(x0 + (x1 - x0) * ts, 0, img.shape[1] - 1.001)
    ys = np.clip(y0 + (y1 - y0) * ts, 0, img.shape[0] - 1.001)
    fx, fy = xs.astype(np.int32), ys.astype(np.int32)
    cx = np.clip(fx + 1, 0, img.shape[1] - 1)
    cy = np.clip(fy + 1, 0, img.shape[0] - 1)
    tx = xs - fx
    ty = ys - fy
    i00 = img[fy, fx]
    i10 = img[fy, cx]
    i01 = img[cy, fx]
    i11 = img[cy, cx]
    top = i00 * (1.0 - tx) + i10 * tx
    bot = i01 * (1.0 - tx) + i11 * tx
    return (top * (1.0 - ty) + bot * ty).astype(np.float32)


# ---------------------------------------------------------------------------
# Absolute temperature, two-point anchored (user-supplied exploit knowledge:
# the Nano series is factory-baselined to ±5 °C / 5 % over -20 °C...330 °C
# <35 mK NETD, but Seek publish no transfer function). We do NOT invent one:
# the user anchors the DL->°C line on a surface they know, and the readout
# is labelled accordingly.
ANCHOR_NAME = "seeknano_anchors.json"


def anchors_load():
    try:
        with open(ANCHOR_NAME, "r") as fh:
            import json
            return [(float(a["dl"]), float(a["c"])) for a in json.load(fh)]
    except Exception:
        return []


def anchors_save(anchors):
    import json
    try:
        with open(ANCHOR_NAME, "w") as fh:
            json.dump([{"dl": d, "c": c} for d, c in anchors], fh)
        return True
    except Exception:
        return False


def ffc_gain_path(gain):
    """One reference PER GAIN (see Viewer wiring: the phone app re-derives
    its flat per exposure via the shutter; holding a single gain-locked
    file made every launch re-capture)."""
    return "ffc_g%d.raw" % int(gain)


def ffc_bank_save(gain, ref):
    try:
        np.clip(np.rint(ref), 0, 65535).astype("<u2").tofile(ffc_gain_path(gain))
        return True
    except Exception:
        return False


def ffc_bank_load(gain):
    try:
        raw = np.fromfile(ffc_gain_path(gain), dtype="<u2")
    except OSError:
        return None
    if raw.size != IMG_W * IMG_H:
        return None
    return raw.reshape(IMG_H, IMG_W).astype(np.float32)


def ffc_bank_clear():
    removed = 0
    try:
        for fn in os.listdir("."):
            if fn.startswith("ffc_g") and fn.endswith(".raw"):
                os.remove(fn)
                removed += 1
    except OSError:
        pass
    return removed


def dl_to_c(dl, anchors):
    """Corrected DL -> °C through the user's anchor points.

    0 anchors -> None (DL only, the honest default);
    1 anchor   -> offset-only (level anchored, slope guessed from the
                  sensor's native scale - the safest single-point form);
    2 anchors  -> the linear map between them.

    Outside the anchor span this extrapolates - the display says so.
    """
    if not anchors:
        return None
    if len(anchors) == 1:
        d0, c0 = anchors[0]
        return dl + (c0 - d0)
    (d0, c0), (d1, c1) = anchors[0], anchors[1]
    if d1 == d0:
        return None
    return c0 + (dl - d0) * (c1 - c0) / (d1 - d0)


def _pil_image():
    """One PIL entry point used by every save path.

    PIL was imported function-locally at three call sites; a missing PIL in
    the bundled exe turned each into its own silent failure path with three
    different messages. Import once here (cached by Python) so callers only
    handle a single ImportError.
    """
    from PIL import Image
    return Image


img_w, img_h = IMG_W, IMG_H


# unified native transport loader (fresh-install friendly):
#   seeknanodirect.dll (libusb-1.0: works with libusbK AND WinUSB bindings)
#   seeknanousb.dll    (pure WinUSB API path)
import ctypes as _ct

def dll_candidates(mei=None, exe_dir=None, module_dir=None):
    """Return (path, prefix) pairs in probe order.

    seeknanousb.dll exports SN_*, seeknanodirect.dll exports SNLB_* —
    the same pairing every consumer uses. Probing order:
      1. sys._MEIPASS (frozen onefile build: dlls extracted there)
      2. the exe folder, then this module's folder — absolute paths only
      3. the bare name as a last resort
    Absolute paths first keep the load from being decided by a bare-name
    search over PATH/cwd (DLL side-load hardening).
    """
    NAMES = [("seeknanousb.dll", "SN_"), ("seeknanodirect.dll", "SNLB_")]
    pairs = []
    dirs = []
    if mei is not None:
        dirs.append(mei)
    for extra in (exe_dir, module_dir):
        if extra:
            dirs.append(extra)
    # name-major: for each dll, try every directory before its sibling. The
    # evidence says seeknanousb.dll is the transport that delivers frames and
    # seeknanodirect.dll lands on the silent MI_01 half - a dir-major order
    # could bind the silent one whenever the good dll happens to sit in a
    # later search dir (e.g. MEIPASS holds only SNLB).
    for n, p in NAMES:
        pairs.extend((os.path.join(dr, n), p) for dr in dirs)
    pairs.extend(NAMES)  # bare-name fallback last
    return pairs


class NativeTransport:
    def __init__(self):
        self.k = None
        self.name = None
        # candidates: also check PyInstaller's bundle dir; for a windowed
        # onefile build the dll is extracted to sys._MEIPASS, never the exe
        # folder, so plain ctypes.CDLL("name.dll") fails there.
        # Order matters: seeknanousb.dll (pure WinUSB API) is the transport
        # we verified delivering 177 840-byte frames in 47-218 ms on the
        # camera host; seeknanodirect.dll (libusb) enumerates the composite
        # and its handshake returned rc=-30 on that same hardware.
        mei = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
        exe_dir = (os.path.dirname(os.path.abspath(sys.executable))
                   if getattr(sys, "frozen", False) else None)
        mod_dir = os.path.dirname(os.path.abspath(__file__))
        for path, pfx in dll_candidates(mei, exe_dir, mod_dir):
            try:
                d = _ct.CDLL(path)
                getattr(d, pfx + "open").restype = _ct.c_int
                getattr(d, pfx + "stream_start").restype = _ct.c_int
                getattr(d, pfx + "get_frame").restype = _ct.c_int
                getattr(d, pfx + "get_frame").argtypes = [_ct.c_char_p]
                getattr(d, pfx + "stream_stop").restype = _ct.c_int
                getattr(d, pfx + "close").restype = None
                self.k = (name := os.path.basename(path), pfx, d)
                self.name = name
                break
            except Exception:
                continue
        if self.k is None:
            raise OSError("no native transport available")

_ctdll = None
try:
    _ctdll = NativeTransport()
except Exception:
    _ctdll = None


class NativeStream(threading.Thread):
    """driver-grade path: overlapped WinUSB reads in C, one call per frame."""
    def __init__(self, q):
        super().__init__(daemon=True)
        self.q = q
        self.stop_flag = threading.Event()
        self.buf = ctypes.create_string_buffer(FRAME_BYTES)
        self.dll = _ctdll
        self.dev = None

    def run(self):
        name, pfx, d = self.dll.k
        rc = getattr(d, pfx + "open")()
        if rc != 0:
            # This is EXPECTED on a libusbK-bound machine, not a fault. The
            # WinUSB DLL enumerates USB\VID_289D&PID_0011 by its WinUSB device
            # interface GUID; libusbK registers a different GUID, so SetupDi
            # finds nothing and SN_open returns -2. The pyusb/libusb-1.0 path
            # below talks to libusbK correctly and is the working transport
            # here, so say that rather than telling the user to go re-run a
            # driver installer that cannot help.
            # rc=3/4: enumeration failure; 5: open failed; 6: winusb; 7: pipe
            # policy; 8: already open (re-entry guard - a caller bug, do not
            # mask it as a driver problem; log the code before falling back)
            self.q.put(("log",
                        "native open rc=%d (-2: device bound to libusbK; "
                        "-8: already open)" % rc))
            self.q.put(("log",
                        "native WinUSB transport unavailable (device is bound to "
                        "libusbK, not WinUSB) - using the libusb-1.0 path, "
                        "which is the working transport on this machine"))
            self.q.put(("status", "switching to libusb-1.0 transport"))
            Stream(self.q).run()
            return
        self.dev = True
        self.q.put(("log", "native transport: " + name +
                        (" (libusb-1.0, libusbK/WinUSB compatible)" if pfx == "SNLB_" else " (WinUSB API)")))
        rc = getattr(d, pfx + "stream_start")()
        if rc != 0:
            self.q.put(("log", "native handshake rc=%d, using working USB path" % rc))
            getattr(d, pfx + "close")()
            Stream(self.q).run()
            return
        self.q.put(("status", "native streaming - up to 25 fps"))
        self.q.put(("log", "handshake ok, reading frames"))
        fails = 0
        get_frame = getattr(d, pfx + "get_frame")
        try:
            while not self.stop_flag.is_set():
                t0 = time.time()
                n = get_frame(self.buf)
                dt = time.time() - t0
                if n == FRAME_BYTES:
                    # The native pump returns exactly FRAME_BYTES even when the
                    # pipe handed back stale residue, so a length check passes
                    # garbage. Validate before it can reach the renderer.
                    frame = bytes(self.buf.raw)
                    bad = _frame_reason(frame)
                    if bad is not None:
                        self.q.put(("log", "native frame rejected: %s" % bad))
                        time.sleep(0.02)
                        continue
                    fails = 0
                    self.q.put(("frame", frame))
                    self.q.put(("info", "frame %.0f ms" % (dt * 1000)))
                    wait = 0.04 - dt
                    if wait > 0 and not self.stop_flag.is_set():
                        time.sleep(wait)
                else:
                    fails += 1
                    self.q.put(("log", "skip bad frame %d (rc %s)" % (fails, n)))
                    if fails > 12:
                        self.q.put(("error", "native pump stalled - stop/start"))
                        break
                    time.sleep(0.2)
        except Exception as e:
            self.q.put(("error", "native read: %s" % e))
        getattr(d, pfx + "stream_stop")()
        getattr(d, pfx + "close")()



class ReplayThread(threading.Thread):
    """Feeds recorded frames (rec_*.snrec) through the queue, no camera.

    Paced at the on-screen frame period; stop_flag drives it. --replay
    file: the viewer then becomes an analysis station for a capture:
    pause (space), frame dump/CSV, isotherm, profiles - all the analysis
    layer works exactly as live.
    """

    NAME = "replay"

    def __init__(self, q, path):
        super().__init__(daemon=True)
        self.q = q
        self.path = path
        self.stop_flag = threading.Event()
        # explode once up front so a corrupt file fails loudly in the log
        data = open(path, "rb").read()
        if not data:
            raise RuntimeError("replay file is empty: " + path)
        self.frames = [data[i:i + FRAME_BYTES]
                       for i in range(0, len(data) - FRAME_BYTES + 1,
                                      FRAME_BYTES)
                       if data[i:i + 2] == b"\x79\x05"]
        self.q.put(("log", "replay: %d frames from %s" % (
            len(self.frames), os.path.basename(path))))

    def run(self):
        for i, frame in enumerate(self.frames):
            if self.stop_flag.is_set():
                break
            t0 = time.time()
            self.q.put(("frame", frame))
            wait = 0.04 - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
        if not self.stop_flag.is_set():
            self.q.put(("status", "replay finished"))


class Stream(threading.Thread):
    def __init__(self, q, status_cb=None, upload_target=None, upload_port=None):
        super().__init__(daemon=True)
        self.q = q
        self.status_cb = status_cb
        self.upload_target = upload_target
        self.upload_port = upload_port
        self.stop_flag = threading.Event()
        try:
            self.logf = open("seeknano_verbose.log", "a", buffering=1)
        except Exception:
            self.logf = None
        # SEEKNANO_TRACE=1 re-enables the per-transfer handshake trace
        self.verbose = os.environ.get("SEEKNANO_TRACE", "") not in ("", "0")

    def trace(self, line, to_ui=False):
        if self.logf:
            self.logf.write("%.3f %s\n" % (time.time(), line))
        if to_ui:
            self.q.put(("log", line))

    def ctrl_out(self, request, payload):
        # per-kick traces ran twice per kick at 25 fps (~50 unbuffered
        # appends/s of handshake noise). Only trace them when explicitly
        # asked; failures still always log.
        verbose = getattr(self, "verbose", False)
        if verbose:
            self.trace("W req=0x%02x data=%s" % (request, payload.hex()))
        self.dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)
        if verbose:
            self.trace("W req=0x%02x OK" % request)

    def ctrl_in(self, request, length):
        r = bytes(self.dev.ctrl_transfer(REQ_IN, request, 0, 0, length, 1250))
        if getattr(self, "verbose", False):
            self.trace("R req=0x%02x len=%d -> %s" % (request, length, r.hex()))
        return r

    def put(self, item):
        self.q.put(item)

    def run(self):
        try:
            dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_BACKEND)
            if dev is None:
                raise RuntimeError(
                    "Seek Nano (vid 0x289d pid 0x11) not found.\n"
                    "If Windows Device Manager shows 'Seek Thermal' with an exclamation\n"
                    "mark, install the libusbK driver once with Zadig (see README).")
            dev.set_configuration()
            usb.util.claim_interface(dev, 0)
            self.claimed = {0}
            self.dev = dev
            eps = []
            try:
                for cfg in dev:
                    for i in cfg:
                        for ep in i:
                            self.trace("cfg=%d iface=%d ep=%#x attrs=0x%02x max=%d" % (
                                cfg.bConfigurationValue, i.bInterfaceNumber,
                                ep.bEndpointAddress, ep.bmAttributes,
                                ep.wMaxPacketSize))
                            if (ep.bEndpointAddress & 0x80) and (ep.bmAttributes & 3) == 2:
                                eps.append((i.bInterfaceNumber, ep.bEndpointAddress))
            except Exception as e:
                self.trace("iface walk skipped: %r" % e)
            self.put(("log", "bulk-in endpoints: " + str(eps)))
            self.q.put(("status", "device opened"))

            # Phase 1 - handshake (exact replay of phone session)
            self.ctrl_out(0x54, b"\x00\x00")
            self.ctrl_out(0x3c, b"\x00\x00")
            self.ctrl_in(0x3d, 2)
            self.ctrl_out(0x3e, b"\x08\x00")
            self.q.put(("status", "connected - protocol handshake ok"))

            # Phase 4 - enable streaming
            self.ctrl_out(0x37, b"\xfc\x00\x04\x00")
            self.ctrl_out(0x3c, b"\x01\x00")
            self.ctrl_in(0x3d, 2)
            self.q.put(("status", "streaming mode enabled - pumping frames"))

            if eps:
                # Only endpoints on an interface we actually claimed are
                # usable. 0x81 is on MI_00, which is the interface claimed
                # above; 0x82 is on MI_01, which is not, and reading it raises
                # "Invalid endpoint address". Keep the interface alongside the
                # endpoint so a future candidate cannot repeat that mistake.
                self.ep_iface = {ep: n for (n, ep) in eps}
                order = [ep for (_n, ep) in eps if ep in EP_CANDIDATES]
                if not order:
                    order = [ep for (n, ep) in eps if n == 0]
                if not order:
                    order = [BULK_EP]
                if 0x81 in order:                       # MI_00 = live stream
                    order = [0x81] + [e for e in order if e != 0x81]
                self.ep_order = order
                self.put(("log", "bulk-in candidates: " +
                          ", ".join("0x%02x (iface %d)" % (
                              e, self.ep_iface.get(e, 0)) for e in order)))
            else:
                self.ep_order = [BULK_EP]
                self.ep_iface = {BULK_EP: 0}
            self.ep_i = 0
            ep_addr = self.ep_order[0]
            self.put(("log", "starting with ep 0x%02x" % ep_addr))
            dev_uploader = None
            if getattr(self, "upload_target", None):
                host = self.upload_target
                port = int(self.upload_port) if self.upload_port else 8100
                dev_uploader = DevUploader(host, port)
                self.put(("log", "dev upload -> %s:%d" % (host, port)))
            kicks = 0
            fails = 0
            exchanged = 0
            # No artificial pacing. This used to sleep to a 40..115 ms period
            # and settle around 78 ms, which capped the app at 12.9 fps. That
            # pacing was a guard against over-reading, and the guard is not
            # needed: the bulk read BLOCKS until the camera has the frame, and
            # measured on this hardware it waits 39.6 ms on average - which is
            # precisely 1/25 fps. The read already paces the loop at the
            # camera's own rate.
            #
            # Measured with the pacing removed: 150 kicks in 6.07 s, 150
            # complete frames, 24.73 fps, not one short read. The kick payload
            # is byte-identical to the phone app's (W 0x53 "WFsBAA==" =
            # 58 5b 01 00), so this is the sensor's native rate, not something
            # we were asking for and failing to get.
            #
            # Failure back-off is kept: it now starts from a 20 ms floor rather
            # than from zero, so repeated failures still slow the loop down and
            # then decay away again.
            period = 0.0
            MIN_PERIOD, MAX_PERIOD = 0.0, 0.115
            last_ok = time.time()
            while not self.stop_flag.is_set():
                wait = period - (time.time() - last_ok)
                if wait > 0:
                    time.sleep(wait)
                t0 = time.time()
                self.ctrl_out(0x53, b"\x58\x5b\x01\x00")
                kicks += 1
                try:
                    # Exactly one frame per kick. The device queues 177,840 B
                    # and then stops, so asking for more only ends in a
                    # timeout. (An earlier revision over-read by one CHUNK for
                    # resync slack: every read then timed out, the timeout
                    # counted as a failure, and the thread bailed out with
                    # "no frames for 12 kicks - camera unresponsive".)
                    #
                    # A timeout on the final chunk is therefore NOT a failure -
                    # the bytes already read ARE the frame. Only fail if we
                    # genuinely came up short.
                    buf = bytearray()
                    read_err = None
                    while len(buf) < FRAME_BYTES and not self.stop_flag.is_set():
                        try:
                            buf.extend(dev.read(
                                ep_addr, min(CHUNK, FRAME_BYTES - len(buf)), 500))
                        except usb.core.USBError as ue:
                            read_err = ue
                            break
                    last_ok = time.time()
                    dt = last_ok - t0
                    if fails == 0 and period > MIN_PERIOD:
                        period = max(MIN_PERIOD, period - 0.005)
                        if kicks % 12 == 0 and period > 0:
                            # period can decay to exactly MIN_PERIOD (0.0)
                            # for low dt; only report the inverse when it is
                            # positive, else the pump thread dies with
                            # ZeroDivisionError and the picture freezes
                            self.put(("log", "pacing %.0f ms (dt %.0f ms) ~ %.1fkicks/s" % (period*1000, dt*1000, 1.0/period)))
                    if len(buf) >= FRAME_BYTES:
                        fails = 0
                        if dev_uploader:
                            try:
                                ok, name = dev_uploader.put_frame(bytes(buf))
                                exchanged += ok
                                self.put(("log", "dev upload %s -> %s" %
                                          (name, "ok" if ok else "FAILED")))
                            except Exception as ue:
                                self.put(("log", "dev upload error: %r" % ue))
                    else:
                        raise read_err if read_err is not None else \
                            usb.core.USBError("short read %d/%d" % (
                                len(buf), FRAME_BYTES))
                except usb.core.USBError as ue:
                    fails += 1
                    self.trace("BULK FAIL %s (fails=%d): %s" % (type(ue).__name__, fails, ue))
                    # Rotating endpoints is how the original flicker started:
                    # 0x82 is silent, so switching produced live/dead frames.
                    # It is no longer a candidate (see EP_CANDIDATES), so this
                    # only fires if a future build adds a genuinely live one -
                    # and then only if its interface is claimed first.
                    if fails == 8 and len(self.ep_order) > 1:
                        nxt = self.ep_order[(self.ep_i + 1) % len(self.ep_order)]
                        want_iface = self.ep_iface.get(nxt, 0)
                        if want_iface not in self.claimed:
                            try:
                                usb.util.claim_interface(dev, want_iface)
                                self.claimed.add(want_iface)
                            except Exception as ce:
                                self.put(("log", "cannot claim interface %d for "
                                                  "ep 0x%02x: %s" % (
                                                      want_iface, nxt, ce)))
                                self.ep_order = [e for e in self.ep_order
                                                 if self.ep_iface.get(e, 0)
                                                 in self.claimed]
                                self.ep_i = 0
                                ep_addr = self.ep_order[0]
                                fails = 0
                                continue
                        self.ep_i = (self.ep_i + 1) % len(self.ep_order)
                        ep_addr = self.ep_order[self.ep_i]
                        fails = 0
                        self.put(("log", "dev: switching to endpoint 0x%02x" % ep_addr))
                    if fails == 3:
                        self.put(("log", "stalled - resetting mode, pacing %.0f ms" % (period*1000)))
                        try:
                            self.ctrl_out(0x3c, b"\x00\x00")
                            self.ctrl_out(0x3e, b"\x08\x00")
                            time.sleep(0.05)
                            self.ctrl_out(0x37, b"\xfc\x00\x04\x00")
                            self.ctrl_out(0x3c, b"\x01\x00")
                            self.ctrl_in(0x3d, 2)
                            time.sleep(0.2)
                        except Exception:
                            pass
                    if fails > 60:
                        # Was 12, which is far too tight: one missed kick while
                        # the AGC changes exposure is normal, and the counter
                        # never reset because a short read also counted.
                        self.put(("error", "no frames for 60 kicks - camera "
                                           "unresponsive; press Stop then Start"))
                        return
                    if fails <= 3:
                        time.sleep(0.1)
                        period = min(MAX_PERIOD, max(0.020, period * 1.3))
                    continue
                if len(buf) >= FRAME_BYTES:
                    frame = _resync_frame(buf)
                    if frame is not None:
                        self.put(("frame", frame))
                        self.put(("info", f"frame {kicks} ok"))
                    else:
                        self.put(("info", f"no valid frame in {len(buf)} B "
                                           f"(offset/desync)"))
                else:
                    self.put(("info", f"short frame {len(buf)} B"))
        except Exception:
            import traceback
            tb = traceback.format_exc()
            self.trace(tb)
            self.q.put(("error", tb.splitlines()[-1] if tb else "unknown"))
        finally:
            # self.dev may never have been assigned if find/set_configuration
            # failed above; touching a missing attribute here would replace
            # the real traceback with an AttributeError.
            dev = getattr(self, "dev", None)
            if dev is not None and dev is not True:
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# HUD chrome — all painted, no stock wx chrome anywhere in the shell.
#   accent #FF6B35 primary action only
#   hud cyan #38DEF6 telemetry only
#   field #10161E controls, bg #070A0E, hairline #1E2A36

def _ui_font(size, bold=False, mono=False):
    """Segoe UI / Consolas with graceful fallback to the stock family."""
    try:
        if mono:
            for name in ("Consolas", "Lucida Console"):
                f = wx.Font(int(size), wx.FONTFAMILY_TELETYPE,
                            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL,
                            False, name)
                if f and f.IsOk():
                    return f
        else:
            f = wx.Font(int(size), wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL,
                        wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL,
                        False, "Segoe UI")
            if f and f.IsOk():
                return f
    except Exception:
        pass
    try:
        return wx.Font(int(size),
                       wx.FONTFAMILY_TELETYPE if mono else wx.FONTFAMILY_DEFAULT,
                       wx.FONTSTYLE_NORMAL,
                       wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL)
    except Exception:
        return None


def _icon_glyph(dc, key, cx, cy, s, col):
    """Draw a rail icon from primitives. No text, so no ClearType fringe.

    The rail buttons used to carry word labels, which on this dark panel picked
    up ClearType's per-subpixel colouring - every glyph fringed red/blue/cyan.
    Icons sidestep that entirely and take far less width, which is what lets the
    image run full-bleed.
    """
    pen = wx.Pen(wx.Colour(*col), 1)
    dc.SetPen(pen)
    dc.SetBrush(wx.TRANSPARENT_BRUSH)
    h = s // 2

    if key == "play":
        dc.SetBrush(wx.Brush(wx.Colour(*col)))
        dc.DrawPolygon([(cx - h // 2, cy - h // 2), (cx - h // 2, cy + h // 2),
                        (cx + h // 2, cy)])
    elif key == "stop":
        dc.SetBrush(wx.Brush(wx.Colour(*col)))
        dc.DrawRectangle(cx - h // 2, cy - h // 2, h, h)
    elif key == "cmap":
        for i, t in enumerate((0.25, 0.5, 0.75)):
            r = int(235 * t) + 20
            dc.SetPen(wx.Pen(wx.Colour(r, int(140 * (1 - t)), int(200 * t)), 2))
            y = cy - h // 2 + int(i * h / 2.0)
            dc.DrawLine(cx - h // 2, y, cx + h // 2, y)
    elif key == "flat":
        dc.DrawRectangle(cx - h // 2, cy - h // 2, h, h)
        dc.DrawLine(cx - h // 2, cy, cx + h // 2, cy)
    elif key == "clear":
        dc.DrawLine(cx - h // 2, cy - h // 2, cx + h // 2, cy + h // 2)
        dc.DrawLine(cx - h // 2, cy + h // 2, cx + h // 2, cy - h // 2)
    elif key == "png":
        dc.DrawRectangle(cx - h // 2, cy - h // 2, h, h)
        dc.DrawLine(cx - h // 2, cy + h // 4, cx + h // 2, cy + h // 4)
        dc.DrawLine(cx, cy + h // 4, cx, cy - h // 4)
    elif key == "raw":
        dc.DrawLine(cx, cy - h // 2, cx, cy + h // 4)
        dc.DrawLine(cx - h // 4, cy, cx, cy + h // 4)
        dc.DrawLine(cx + h // 4, cy, cx, cy + h // 4)
        dc.DrawLine(cx - h // 2, cy + h // 2 - 1, cx + h // 2, cy + h // 2 - 1)
    elif key == "spot":
        dc.DrawCircle(cx, cy, h // 3)
        dc.DrawLine(cx - h // 2, cy, cx - h // 3, cy)
        dc.DrawLine(cx + h // 3, cy, cx + h // 2, cy)
        dc.DrawLine(cx, cy - h // 2, cx, cy - h // 3)
        dc.DrawLine(cx, cy + h // 3, cx, cy + h // 2)
    elif key == "sens":
        dc.DrawLine(cx - h // 2, cy, cx + h // 2, cy)
        dc.DrawLine(cx, cy - h // 2, cx, cy + h // 2)
        dc.DrawCircle(cx, cy, 1)


class GlassButton(wx.Panel):
    """Flat control, painted. Hover raises fill + text; click fires.

    `icon` switches it to a square glyph button with no text at all - the rail
    form. State that used to be spelled out in the label is carried by the
    glyph instead (play/stop, filled/outline), and the tooltip carries the rest.
    """

    def __init__(self, parent, label="", accent=False, small=False,
                 on_click=None, tooltip=None, icon=None):
        wx.Panel.__init__(self, parent)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.label = label
        self.accent = accent
        self.small = small
        self.icon = icon
        self.glyph = icon           # may differ from `icon`: the start tile
        self._hover = False
        self._cb = on_click
        self._fit()
        if tooltip:
            self.SetToolTip(tooltip)
        try:
            self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        except Exception:
            pass
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._hover_in)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._hover_out)
        self.Bind(wx.EVT_LEFT_DOWN, self._press)

    # ---- metrics --------------------------------------------------------
    def _font(self):
        return _ui_font(10 if self.small else 12)

    def _fit(self):
        if self.icon:
            self.SetMinSize(wx.Size(40, 40))
            self.SetSize(self.GetMinSize())
            self.Refresh()
            return
        dc = wx.ClientDC(self)
        tw = th = 0
        f = self._font()
        if f is not None:
            dc.SetFont(f)
            tw, th = dc.GetTextExtent(self.label)
        pad_x, pad_y = (14, 6) if self.small else (20, 10)
        self.SetMinSize(wx.Size(tw + 2 * pad_x, th + 2 * pad_y))
        self.SetSize(self.GetMinSize())
        self.Refresh()

    # ---- API ------------------------------------------------------------
    def SetGlyph(self, key):
        """Swap the glyph on an icon button. No relayout - the tile is fixed."""
        self.glyph = key
        self.Refresh()

    def SetLabel(self, text):
        self.label = text
        if self.icon:
            # An icon button has no label to fit, and relaying out the rail on
            # every state change (the start button relabels each time the
            # stream toggles) would churn the layout for nothing. The glyph
            # already shows play vs stop.
            self.Refresh()
            return
        self._fit()
        # relayout: the label width may have changed -> re-fit the row
        win = self.GetParent()
        if win is not None:
            win.Layout()

    def GetLabel(self):
        return self.label

    def Bind(self, ev, fn):
        """EVT_BUTTON is stored and fired on click; other events pass through."""
        if ev is wx.EVT_BUTTON:
            self._cb = fn
        else:
            super().Bind(ev, fn)

    # ---- interaction ------------------------------------------------------
    def _hover_in(self, ev):
        self._hover = True
        self.Refresh()

    def _hover_out(self, ev):
        self._hover = False
        self.Refresh()

    def _press(self, ev):
        cb = self._cb
        self.Refresh()
        if cb:
            try:
                cb(None)          # every handler in this app accepts None
            except Exception:
                # handler failures must never take down the frame; the real
                # diagnostics land in the app log via tick
                pass

    # ---- paint ------------------------------------------------------------
    def on_paint(self, ev):
        w, h = self.GetClientSize()
        dc = wx.AutoBufferedPaintDC(self)
        if self.accent:
            fill = (255, 107, 53) if not self._hover else (255, 138, 90)
            text = (18, 24, 32)
        else:
            fill = (16, 22, 30) if not self._hover else (28, 39, 53)
            text = (222, 228, 235) if not self._hover else (140, 224, 246)
        if self.icon:
            # rail form: a tile with a glyph and no text
            #
            # ONE outline, recoloured on hover. This used to draw a second
            # rounded rect at (1, 1, w-3, h-3) radius 5 on top of the tile's
            # own at (0, 0, w, h) radius 6. The two arcs sit at different
            # radii, so on hover they interleaved into a jagged double corner -
            # a staircase of pure cyan against the pure slate border, which is
            # what read as "colours at the corners". Measured on the live
            # window: 29 chroma-carrying pixels in the corner boxes on hover
            # versus 10 idle, and the values were unmixed (56,222,246 next to
            # 36,48,62), so it was geometry, not ClearType or antialiasing.
            edge = wx.Colour(56, 222, 246) if self._hover \
                else wx.Colour(36, 48, 62)
            dc.SetPen(wx.Pen(edge))
            dc.SetBrush(wx.Brush(wx.Colour(*fill)))
            # Full w x h, deliberately. Shrinking to w-1, h-1 to "stay inside"
            # left the last row and column of the tile's paint buffer
            # untouched, and the uninitialised memory that showed through was
            # salmon - pure 255,107,53, the accent fill of the start tile,
            # left over from an earlier frame. It surfaced as L-shaped corner
            # brackets on the tiles whose buffers had held the orange tile.
            # 47 px, identical across 6 consecutive samples, so not flicker.
            dc.DrawRoundedRectangle(0, 0, w, h, 6)
            # `glyph`, not `icon`: the start tile shows play while stopped and
            # stop while running. Deriving it from `accent` instead - which is
            # what the first pass did - pinned it to stop permanently, because
            # the start tile is the only accent button and so was always
            # showing the opposite of its own state.
            # The glyph spans 55% of the tile: sizing it off min(w, h) - 18
            # left only ~22 px inside a 40 px tile, which clipped the colormap
            # bars and the crosshair.
            _icon_glyph(dc, self.glyph, w // 2, h // 2,
                        int(min(w, h) * 0.55), text)
            return
        dc.SetPen(wx.Pen(wx.Colour(30, 42, 54)))
        dc.SetBrush(wx.Brush(wx.Colour(*fill)))
        dc.DrawRoundedRectangle(0, 0, w, h, 6 if self.small else 9)
        f = self._font()
        if f is not None:
            dc.SetFont(f)
        dc.SetTextForeground(wx.Colour(*text))
        tw, th = dc.GetTextExtent(self.label)
        dc.DrawText(self.label, (w - tw) // 2, (h - th) // 2)


class TitleBar(wx.Panel):
    """The window's own title bar: emblem + title + LED + telemetry + buttons.

    Painted; dragging anywhere outside the two circles moves the frameless
    window. set_state() keeps the HeaderPanel API so push_status works.
    """

    def __init__(self, parent, theme):
        wx.Panel.__init__(self, parent)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)   # required AFTER init
        self.t = theme
        self.led_text = "no device"
        self.led_col = self.t["muted"]
        self.fps_text = ""
        self.info_text = ""
        self._emblem = None
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "assets", "icon_256.png")
            if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
                path = os.path.join(sys._MEIPASS, "assets", "icon_256.png")
            img = wx.Image(path, wx.BITMAP_TYPE_PNG)
            if img.IsOk():
                img.Rescale(22, 22, wx.IMAGE_QUALITY_BILINEAR)
                self._emblem = img.ConvertToBitmap()
        except Exception:
            self._emblem = None
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_down)
        self.Bind(wx.EVT_MOTION, self.on_move)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        _cap_lost = getattr(wx, "EVT_MOUSE_CAPTURE_LOST",
                            getattr(wx, "EVT_CAPTURE_LOST", None))
        if _cap_lost is not None:
            self.Bind(_cap_lost,
                      lambda ev: setattr(self, "_drag_from", None))

    # ---- geometry --------------------------------------------------------
    def _btn_rects(self):
        w, h = self.GetClientSize()
        y = h // 2 - 8
        return (wx.Rect(w - 74, y, 16, 16),   # close
                wx.Rect(w - 36, y, 16, 16))   # minimize

    # ---- drag ------------------------------------------------------------
    def on_down(self, ev):
        close, mini = self._btn_rects()
        p = ev.GetPosition()
        if close.Contains(p):
            self._act("close")
            return
        if mini.Contains(p):
            self._act("min")
            return
        self._drag_from = self.ClientToScreen(p)
        self._drag_win = self.GetParent().GetPosition()
        self.CaptureMouse()

    def on_move(self, ev):
        if getattr(self, "_drag_from", None) is None or not self.HasCapture():
            if getattr(self, "_drag_from", None) is None:
                # no capture in progress -> hover tracking for the buttons
                self._hover_pos = ev.GetPosition()
                self.Refresh()
            return
        p = self.ClientToScreen(ev.GetPosition())
        dx = p.x - self._drag_from.x
        dy = p.y - self._drag_from.y
        win = self.GetParent()
        win.Move(win.GetPosition().x + dx, win.GetPosition().y + dy)
        self._drag_from = p

    def on_up(self, ev):
        try:
            if self.HasCapture():
                self.ReleaseMouse()
        except Exception:
            pass
        self._drag_from = None

    def _act(self, what):
        win = wx.GetTopLevelParent(self)
        if what == "close":
            win.Close()
        elif what == "min":
            win.Iconize(True)

    # ---- state -----------------------------------------------------------
    def set_state(self, text, colour, fps="", info=""):
        self.led_text, self.led_col = text, colour
        self.fps_text, self.info_text = fps, info
        self.Refresh()

    # ---- paint -----------------------------------------------------------
    def on_paint(self, ev):
        w, h = self.GetClientSize()
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour(*self.t["panel"])))
        dc.Clear()
        x = 14
        if self._emblem is not None:
            dc.DrawBitmap(self._emblem, x, (h - 22) // 2, True)
            x += 30
        f = _ui_font(12, bold=True)
        if f is not None:
            dc.SetFont(f)
        dc.SetTextForeground(wx.Colour(*self.t["text"]))
        tw, th = dc.GetTextExtent("SEEK NANO")
        dc.DrawText("SEEK NANO", x, (h - th) // 2)
        x += tw + 24
        # LED + state, immediately after the title
        dc.SetBrush(wx.Brush(wx.Colour(*self.led_col)))
        dc.SetPen(wx.Pen(wx.Colour(*self.led_col)))
        dc.DrawCircle(x, h // 2, 5)
        f2 = _ui_font(10)
        if f2 is not None:
            dc.SetFont(f2)
        dc.SetTextForeground(wx.Colour(*self.t["muted"]))
        sw, sh = dc.GetTextExtent(self.led_text)
        dc.DrawText(self.led_text, x + 10, (h - sh) // 2)
        # telemetry (cyan), right-aligned with a margin for the window buttons
        if self.fps_text:
            f3 = _ui_font(10, mono=True)
            if f3 is not None:
                dc.SetFont(f3)
            dc.SetTextForeground(wx.Colour(*self.t["accent2"]))
            fw, fh = dc.GetTextExtent(self.fps_text)
            dc.DrawText(self.fps_text, w - fw - 96, (h - fh) // 2)
        # hairline border
        dc.SetPen(wx.Pen(wx.Colour(*self.t["line"])))
        dc.DrawLine(0, h - 1, w, h - 1)
        # window buttons: small circles with hover
        close, mini = self._btn_rects()
        hover_pos = getattr(self, "_hover_pos", None)
        for rect, glyph, col in ((close, "\u2715", self.t["err"]),
                                 (mini, "\u2014", self.t["muted"])):
            r = rect.x + 8
            hot = (hover_pos is not None and rect.Contains(hover_pos))
            dc.SetBrush(wx.Brush(wx.Colour(
                *(([60, 40, 44] if hot and glyph == "\u2715" else
                   (44, 58, 72) if hot else self.t["field"])))))
            dc.SetPen(wx.Pen(wx.Colour(*self.t["line"])))
            dc.DrawCircle(r, rect.y + 8, 8)
            f4 = _ui_font(9)
            if f4 is not None:
                dc.SetFont(f4)
            dc.SetTextForeground(wx.Colour(*col))
            gw, gh = dc.GetTextExtent(glyph)
            dc.DrawText(glyph, r - gw // 2, rect.y + 8 - gh // 2)

    def _track_hover(self, ev=None):
        """EVT_MOTION without capture: store the position so the window
        buttons can raise their hover state."""
        self._hover_pos = ev.GetPosition()
        self.Refresh()

    def __init_hooks(self):
        pass

class TelemetryBlock(wx.Panel):
    """Rail readout: status headline + mono stats + an ironbow identity bar.

    The gradient strip across the bottom is the camera's own ramp - the one
    identity colour that is obviously *this* instrument, not a generic accent.
    """

    def __init__(self, parent, theme):
        wx.Panel.__init__(self, parent, style=wx.BG_STYLE_PAINT)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.t = theme
        self.headline = "no device"
        self.head_col = theme["muted"]
        self.fps = ""
        self.gain = ""
        self.seq = ""
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def set(self, headline=None, col=None, fps=None, gain=None, seq=None):
        if headline is not None:
            self.headline = headline
        if col is not None:
            self.head_col = col
        if fps is not None:
            self.fps = fps
        if gain is not None:
            self.gain = gain
        if seq is not None:
            self.seq = seq
        try:
            top = wx.GetTopLevelParent(self)   # the Viewer frame holds _anchors
            self.state_anchored = bool(getattr(top, "_anchors", None))
        except Exception:
            self.state_anchored = False
        self.Refresh()

    def on_paint(self, ev):
        w, h = self.GetClientSize()
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour(*self.t["panel"])))
        dc.Clear()
        # headline, uppercase, letterspaced by drawing one char at a time
        f = _ui_font(15, bold=True)
        if f is not None:
            dc.SetFont(f)
        dc.SetTextForeground(wx.Colour(*self.head_col))
        text = self.headline.upper()
        # shrink to what the strip can hold beside the right-aligned readouts
        fits = max(10, int((w - 300) // 12))
        if len(text) > fits:
            text = text[:fits - 1] + "\u2026"
        cx = 16
        fsp = 1.5
        _th = dc.GetTextExtent(text[:1])[1]
        y = (h - _th) // 2 + 1
        for ch in text[:40]:
            dc.DrawText(ch, int(cx), y)
            cw = dc.GetTextExtent(ch)[0]
            cx += cw + fsp
        # One line, right-aligned: headline on the left, readouts on the right.
        # This block used to stack the headline at y=12, a mono line at y=52 and
        # the anchored-°C note at y=68 - about 80 px, which is what the 108 px
        # rail column needed. As a top HUD strip it has to be one line tall, and
        # the same readouts are already in the title bar, so they move to the
        # right of the same row rather than a second one.
        f2 = _ui_font(9, mono=True)
        if f2 is not None:
            dc.SetFont(f2)
        dc.SetTextForeground(wx.Colour(*self.t["muted"]))
        comp = []
        if self.fps:
            comp.append(self.fps.upper())
        if self.gain:
            comp.append("GAIN %s" % self.gain)
        if self.seq:
            comp.append("SEQ %s" % self.seq)
        line = "   ".join(comp)
        anchored = "anchored \u00b0C" if getattr(self, "state_anchored",
                                                 False) else "DL only"
        if line or anchored:
            tail = (anchored + "   " + line) if line else anchored
            dc.SetTextForeground(wx.Colour(*self.t["muted"]))
            tw, th = dc.GetTextExtent(tail)
            dc.DrawText(tail, max(cx + 18, w - tw - 16),
                         (h - th) // 2 + 2)
        # the anchored-°C state, so its absence is explicable on screen
        # the ironbow identity bar, hairline under the strip
        bar_w = max(1, w - 32)
        for i in range(bar_w):
            t = i / (bar_w - 1)
            r, g, b = ironbow(t)
            dc.SetPen(wx.Pen(wx.Colour(
                max(0, min(255, int(r * 255))),
                max(0, min(255, int(g * 255))),
                max(0, min(255, int(b * 255))))))
            dc.DrawLine(16 + i, h - 4, 16 + i, h - 2)


class Viewer(wx.Frame):
    def __init__(self):
        # no wx.CAPTION: the painted TitleBar is the frame; taskbar + resize kept
        wx.Frame.__init__(self, None, title="Seek Nano Viewer",
                          size=(1000, 660),
                          style=wx.MINIMIZE_BOX | wx.MAXIMIZE_BOX | 
                          wx.RESIZE_BORDER | wx.CLOSE_BOX | 
                          wx.CLIP_CHILDREN)
        self.q = queue.Queue()
        self.stream_thread = None
        self.frame_raw = None
        self.lut_i = 0
        self.paused = False
        self.W, self.H = IMG_W, IMG_H
        self.geom_done = True
        self.ffc = None            # flat-field reference (ROI float32) or None
        self.bad = None             # defective-element mask (ROI bool) or None
        # hot-spot detection
        self.spot_mode = "off"          # one of HOTSPOT_MODES
        self.spot_sens = HOTSPOT_SENS
        self.spots = []                 # this frame's regions
        self.spot_hist = []             # (t, y, x, peak) for "track"
        self.spot_alarm = 0.0           # timestamp of the last alarm
        self.spot_alarm_on = False
        self.spot_btn = None
        self.ffc_gain = None       # gain self.ffc was captured at
        self.ffc_ok = None         # None = not yet checked on live data
        self.ffc_collect = None     # list of ROI frames being averaged, or None
        self.last_fid = None        # last accepted frame sequence
        self.bad_frames = 0         # consecutive rejected frames
        self.gain_hist = []         # recent gain words, for the AGC-lock gate
        self.last_gain = None
        self.shown_gain = None      # exposure currently on screen
        self.shown_frames = 0       # frames actually put on screen
        self.held_gain = 0          # frames withheld because exposure changed
        self.saturated = 0          # frames withheld because the gain saturated
        self._info = ""
        # running background estimate, used only until a wall reference exists
        self.bg_frames = []
        self.bg_gains = []
        self.bg_hp = None          # high-pass of the background, computed once
        self.disp_prev = None      # temporal smoothing state
        self.nuc = None            # shutterless-NUC background reference
        self.lo_ema = None
        self.hi_ema = None
        self.prev_t = None        # previous displayed frame, for the blend
        self.stretch_skips = 0    # frames whose stretch window was rejected
        # median window buffers, available before Start: a frame shown without
        # clicking Start must not crash the paint with a missing attribute
        self._plo = deque(maxlen=WINDOW_MEDIAN)
        self._phi = deque(maxlen=WINDOW_MEDIAN)

        # ---- window chrome: frameless HUD shell ---------------------------
        self.SetBackgroundColour(wx.Colour(*THEME["bg"]))
        self.SetForegroundColour(wx.Colour(*THEME["text"]))

        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(*THEME["bg"]))
        panel.SetForegroundColour(wx.Colour(*THEME["text"]))

        # HUD title bar: emblem, letterspaced title, LED + state, telemetry
        self.header = TitleBar(panel, THEME)
        self.header.SetMinSize((-1, 46))

        # primary action is built on the rail itself (Reparent is fragile)

        # Tell wx "I paint every pixel of this myself". Without it, wxWindows
        # honours WM_ERASEBKGND and erases the panel to its background brush
        # before handing us the paint - and that background is black. So every
        # repaint had a window in which the panel was solid black, which is
        # exactly the flicker. (Details of that hunt are history; the setting
        # below keeps the panel from being erased underneath the paint.)
        self.video = wx.Panel(panel, style=wx.BG_STYLE_PAINT)
        self.video.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.video.SetBackgroundColour(wx.BLACK)
        self.video.Bind(wx.EVT_PAINT, self.on_paint)
        # analysis interactions: cursor readout, rubber-band region,
        # shift-drawn profile line (see _draw_pointer)
        self.mouse = None
        self.sel_box = None
        self.sel_line = None
        self._dragging = self._lining = False
        self.iso_on = False
        self.iso_thr = None
        self.video.Bind(wx.EVT_MOTION, self.on_motion_canvas)
        self.video.Bind(wx.EVT_LEFT_DOWN, self.on_down_canvas)
        self.video.Bind(wx.EVT_LEFT_UP, self.on_up_canvas)
        self.video.Bind(wx.EVT_LEAVE_WINDOW, self.on_leave_canvas)

        # ---- right rail: telemetry / command deck / (hidden diagnostics) ---
        rail = wx.Panel(panel, style=wx.BG_STYLE_PAINT)
        rail.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        rail.SetBackgroundColour(wx.Colour(*THEME["bg"]))
        self.rail = rail
        rail.Bind(wx.EVT_PAINT, self.on_rail_paint)

        self.telemetry = TelemetryBlock(panel, THEME)
        self.telemetry.SetMinSize((-1, 38))

        deck = wx.BoxSizer(wx.VERTICAL)
        deck.Add((1, 6), 0)
        # Icon rail: glyph + tooltip, no word labels. Every label used to be
        # text on a dark panel, and ClearType fringed all of them; a glyph has
        # no subpixels to fringe. It also costs 40 px a row instead of ~220,
        # which is what lets the image run full-bleed.
        self.start_btn = GlassButton(rail, icon="play", accent=True,
                                     on_click=self.on_toggle,
                                     tooltip="start / stop  (S)")
        deck.Add(self.start_btn, 0, wx.CENTRE|wx.ALL, 4)
        deck.Add((1, 6), 0)
        self.cmap_btn = GlassButton(rail, icon="cmap",
                                    on_click=self.on_cmap,
                                    tooltip="next colormap  (C)")
        self.flat_btn = GlassButton(rail, icon="flat",
                                    on_click=self.on_capture_flat,
                                    tooltip="capture flat field, point at a wall  (F)")
        flat_clear = GlassButton(rail, icon="clear",
                                 on_click=self.on_clear_flat,
                                 tooltip="clear the flat reference")
        snap_btn = GlassButton(rail, icon="png", on_click=self.on_snapshot,
                               tooltip="save PNG snapshot  (P)")
        raw_btn = GlassButton(rail, icon="raw", on_click=self.on_raw,
                              tooltip="dump raw frame  (D)")
        for b in (self.cmap_btn, self.flat_btn, flat_clear, snap_btn, raw_btn):
            deck.Add(b, 0, wx.CENTRE|wx.ALL, 4)
            deck.Add((1, 6), 0)
        self.spot_btn = GlassButton(rail, icon="spot",
                                    on_click=self.on_spot_mode,
                                    tooltip="hot-spot detection mode")
        self.spot_sens_btn = GlassButton(rail, icon="sens",
                                         on_click=self.on_spot_sens,
                                         tooltip="hot-spot sensitivity")
        deck.Add(self.spot_btn, 0, wx.CENTRE|wx.ALL, 4)
        deck.Add((1, 6), 0)
        deck.Add(self.spot_sens_btn, 0, wx.CENTRE|wx.ALL, 4)
        deck.Add((1, 10), 1, wx.EXPAND)           # spring
        # diagnostics field: hidden by default (Ctrl+L shows it inside the rail)
        self.log = wx.TextCtrl(rail, style=wx.TE_MULTILINE | wx.TE_READONLY |
                               wx.TE_DONTWRAP)
        self.log.SetBackgroundColour(wx.Colour(*THEME["panel"]))
        self.log.SetForegroundColour(wx.Colour(*THEME["muted"]))
        self.log.SetFont(_ui_font(9, mono=True))
        deck.Add(self.log, 0, wx.EXPAND|wx.ALL, 4)
        self.log.Hide()
        # dev upload row - built, wired, hidden (Ctrl+Alt+D)
        self.dev_host = wx.TextCtrl(rail, value="192.168.50.200:8100",
                                    style=wx.TE_PROCESS_ENTER)
        self.dev_upload = wx.CheckBox(rail, label="dev: upload frames")
        self.dev_upload.SetValue(False)
        self.dev_probe = GlassButton(rail, label="dev: retry endpoints",
                                     small=True, on_click=self.on_dev_probe)
        self.dev_bind = GlassButton(rail, label="dev: bind info",
                                    small=True, on_click=self.on_dev_bind)
        self.dev_host.SetForegroundColour(wx.Colour(*THEME["accent2"]))
        self.dev_upload.SetBackgroundColour(wx.Colour(*THEME["bg"]))
        self.dev_upload.SetForegroundColour(wx.Colour(*THEME["muted"]))
        devbox = wx.BoxSizer(wx.VERTICAL)
        devbox.Add((1, 6), 0)
        for b in (self.dev_host, self.dev_probe, self.dev_bind, self.dev_upload):
            devbox.Add(b, 0, wx.EXPAND|wx.ALL, 4)
            devbox.Add((1, 6), 0)
        deck.Add(devbox, 0, wx.EXPAND)
        devbox.ShowItems(False)                   # shipped app: invisible
        self.dev_sizer = devbox

        rail.SetMinSize(wx.Size(52, -1))
        railbox = wx.BoxSizer(wx.VERTICAL)
        railbox.Add(deck, 1, wx.EXPAND)
        rail.SetSizer(railbox)

        # video canvas: dominant, framed with its own inset margin
        main = wx.BoxSizer(wx.HORIZONTAL)
        # Full bleed: no inset margin around the canvas. The 12 px
        # frame used to border the picture; the HUD and rail sit
        # outside it now, so the image runs to the window edge.
        main.Add(self.video, 1, wx.EXPAND)
        main.Add(rail, 0, wx.EXPAND)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.header, 0, wx.EXPAND)
        sizer.Add(self.telemetry, 0, wx.EXPAND)
        sizer.Add(main, 1, wx.EXPAND)
        panel.SetSizer(sizer)

        # no status bar - the HUD title bar carries the state text

        # emblem icon in the window + taskbar (assets/ is wired into the exe)
        _icon = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "assets", "icon_256.png")
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            _icon = os.path.join(sys._MEIPASS, "assets", "icon_256.png")
        try:
            if os.path.exists(_icon):
                self.SetIcon(wx.Icon(_icon, wx.BITMAP_TYPE_PNG))
        except Exception:
            pass
        self.push_status("plug the Nano in, then press Start stream")
        # burst files from previous sessions are diagnostic leftovers from a
        # different run; the shipped viewer clears them once at startup
        try:
            for fn in os.listdir("."):
                if fn.startswith("burst_") and fn.endswith(".png"):
                    os.remove(fn)
        except OSError:
            pass

        self._load_flat()

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.tick, self.timer)
        # 33 ms = 30 Hz, not the 70 ms (14.3 Hz) this used to run at. The timer
        # drains the frame queue, so it was the display's ceiling: even with the
        # camera delivering 25 fps, a 70 ms tick could only ever show 14 of
        # them. 30 Hz leaves headroom over 25 without flooding the paint path -
        # a paint now costs ~1.5 ms (buffered, blit only).
        self.timer.Start(33)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        # The README documents s/c/p/d/q hotkeys but nothing ever bound them,
        # so pressing "p" did nothing at all. Bind them here, on the frame, and
        # give focus to the frame so they work without clicking first.
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.video.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        # Hotkeys must work whatever has focus. This used to bind EVT_CHAR_HOOK
        # to the log TextCtrl, which only fires when THAT BOX has keyboard
        # focus. Clicking "Start stream" moves focus to the button, so neither
        # the log handler nor the video handler saw the key and F did nothing -
        # the capture silently never happened, with no error to show for it.
        #
        # A frame accelerator table is dispatched before child controls see the
        # key, so it catches it regardless of focus. wx.AcceleratorEntry needs an
        # INTEGER id, not a bound method: passing a method raises "arguments
        # did not match any overloaded call" inside __init, so the window never
        # opens at all.
        names = (("S", "Start/stop stream"), ("C", "Next colormap"),
                 ("P", "Save PNG snapshot"), ("D", "Dump raw frame"),
                 ("F", "Capture flat (wall)"), ("Q", "Quit"))
        self._accel_names = {}
        table = []
        for ch, name in names:
            key = wx.NewIdRef() if hasattr(wx, "NewIdRef") else wx.NewId()
            self._accel_names[key] = name
            self.Bind(wx.EVT_MENU, self.on_hotkey, id=key)
            table.append(wx.AcceleratorEntry(wx.ACCEL_NORMAL, ord(ch), key))
        # space = pause, on the table too so it works like the rest
        key = wx.NewIdRef() if hasattr(wx, "NewIdRef") else wx.NewId()
        self._accel_names[key] = "Pause"
        self.Bind(wx.EVT_MENU, self.on_hotkey, id=key)
        table.append(wx.AcceleratorEntry(wx.ACCEL_NORMAL, wx.WXK_SPACE, key))
        self.SetAcceleratorTable(wx.AcceleratorTable(table))
        # Belt and braces: a frame-level CHAR_HOOK as well, since key events
        # that a child control does not handle propagate up to the frame. Both
        # routes go through the same _hotkey_run, so there is one implementation.
        self.Bind(wx.EVT_CHAR_HOOK, self._frame_char)
        self.log.SetFocus()
        self.SetFocus()

    def _frame_char(self, ev):
        """Frame-level key fallback. See the accelerator setup in __init__."""
        try:
            k = ev.GetKeyCode()
            ch = chr(k) if 0 < k < 256 else ""
            if ch.lower() in "scpdfq" or ch == " " or k in (wx.WXK_ESCAPE,
                                                           wx.WXK_SPACE):
                self._hotkey_run(ev, "key")
                return
        except Exception:
            self._show_fatal("frame key handler", sys.exc_info())
            return
        ev.Skip()

    def push_status(self, text):
        sb = self.GetStatusBar()
        if sb is not None:
            sb.SetStatusText(text)
        # HUD header carries the same state, colour-mapped
        low = text.lower()
        if "error" in low or "fault" in low or "unavail" in low:
            colour, label = THEME["err"], text
        elif "saturat" in low or "held" in low or "capturing" in low:
            colour, label = THEME["warn"], text
        elif "connected" in low or "streaming" in low or "running" in low:
            colour, label = THEME["ok"], text
        elif "stopped" in low or "no device" in low or low.startswith("plug"):
            colour, label = THEME["muted"], text
        else:
            colour, label = THEME["accent2"], text
        info = getattr(self._info, "strip", lambda: "")() if self._info else ""
        self.header.set_state(label, colour, info=info[:60])
        # rail telemetry block carries the same headline
        if getattr(self, "telemetry", None) is not None:
            self.telemetry.set(headline=label, col=colour)

    # ---- stream control ----
    def on_toggle(self, ev):
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.stop_flag.set()
            self.stream_thread = None
            self.start_btn.SetGlyph("play")
            self.start_btn.SetLabel("Start stream")
            self.push_status("stopped")
            return
        if getattr(self, "replay_path", None):
            # replay mode: play the recorded file instead of opening the camera
            try:
                thread = ReplayThread(self.q, self.replay_path)
            except Exception as e:
                self.push_status("replay failed: %r" % e)
                self.q.put(("log", "replay failed: %r" % e))
                return
            self.q.put(("log", "replay starting..."))
            self.start_btn.SetGlyph("stop")
            self.start_btn.SetLabel("Stop stream")
            self.stream_thread = thread
            self.stream_thread.start()
            return
        self.frame_raw = None
        self.last_fid = None      # new stream: accept any sequence again
        self.bad_frames = 0
        self.gain_hist = []       # re-arm the AGC-lock gate
        self.last_gain = None
        self.shown_gain = None    # let a new exposure through after a restart
        self.lo_ema = None        # latched display window; see _bmp
        self.hi_ema = None
        self.prev_t = None
        # median buffers for the contrast window: one bad frame cannot move a
        # median. Initialised here too (on_toggle re-creates them) so a frame
        # shown before Start cannot crash the paint.
        self._plo = deque(maxlen=WINDOW_MEDIAN)
        self._phi = deque(maxlen=WINDOW_MEDIAN)
        self.stretch_skips = 0
        self.paint_count = 0
        self._paint_seq = None
        self.paint_count = 0    # paints actually delivered to the screen
        self._paint_seq = None # frame seq the screen is displaying
        # median buffers for the contrast window: one bad frame cannot move a
        # median, which is what stops a transient from steering the mapping
        self._plo = deque(maxlen=WINDOW_MEDIAN)
        self._phi = deque(maxlen=WINDOW_MEDIAN)
        self.bg_frames = []       # rebuild the background for the new session
        self.bg_gains = []
        self.bg_hp = None
        self.frame_raw = None
        self.push_status("starting ...")
        upload_target = None
        upload_port = None
        if self.dev_upload.GetValue():
            raw = self.dev_host.GetValue().strip()
            if ":" in raw:
                upload_target, upload_port = raw.rsplit(":", 1)
            else:
                upload_target = raw
            self.q.put(("log", "dev: frames will POST to %s:%s" %
                        (upload_target, upload_port)))
        # DEV: when frame upload is on, prefer the pyusb stream so the
        # endpoint auto-probe and uploader are in the same loop, even if
        # the native dlls are present
        if self.dev_upload.GetValue() and upload_target:
            self.stream_thread = Stream(self.q, upload_target=upload_target,
                                        upload_port=upload_port)
        elif _ctdll is not None:
            self.stream_thread = NativeStream(self.q)
        else:
            self.stream_thread = Stream(self.q, upload_target=upload_target,
                                        upload_port=upload_port)
        self.stream_thread.start()
        self.start_btn.SetGlyph("stop")
        self.start_btn.SetLabel("Stop stream")

    def on_cmap(self, ev):
        self.lut_i = (self.lut_i + 1) % len(COLORMAPS)
        self.cmap_btn.SetLabel("colormap: " + COLORMAPS[self.lut_i][0])
        self.video.Refresh()

    # ---- flat-field reference: point at a uniform wall, hold still ~1 s ----
    def on_capture_flat(self, ev):
        if not self.frame_raw:
            self.push_status("start the stream first, then capture flat")
            return
        self.ffc_collect = []
        self.q.put(("log", "flat capture: hold the camera at a uniform wall "
                           "(or cover the lens) and keep still..."))
        self.push_status("capturing flat reference...")
        self.flat_btn.Disable()

    def on_clear_flat(self, ev):
        self.ffc = None
        self.bad = None
        self.ffc_gain = None
        self.ffc_ok = None
        self.ffc_collect = None
        self.bg_hp = None          # fall back to the background estimate
        self.flat_btn.Enable()
        try:
            os.remove(FFC_NAME)
            os.remove(FFC_GAIN)
        except OSError:
            pass
        # the per-gain bank must go too, or the cleared flat resurrects at
        # the next gain change
        removed = ffc_bank_clear()
        if removed:
            self.q.put(("log", "flat bank: %d per-gain reference(s) removed"
                               % removed))
        try:
            os.remove(BAD_NAME)
        except OSError:
            pass
        self.q.put(("log", "flat reference cleared (now showing uncorrected "
                           "sensor data + column NUC)"))
        self.push_status("flat cleared")
        self.video.Refresh()

    def _finish_flat(self):
        bank = np.stack(self.ffc_collect).astype(np.float32)
        self.ffc = bank.mean(axis=0)
        self.ffc_collect = None
        # Do NOT trust a fresh capture. This used to set ffc_ok = True, on the
        # reasonable-sounding assumption that a reference you just took must be
        # good. It is not: a capture taken while the camera was not actually on
        # a uniform surface bakes the scene into the reference, and subtracting
        # it then injects that scene as speckle everywhere. Measured on the
        # reference that produced the visible artefacts, over 75 consecutive
        # gain-3 frames:
        #
        #     neighbour |dx| RAW        240.4 DL
        #     neighbour |dx| AFTER FFC   334.0 DL
        #     frames where it helps      0 of 75
        #
        # Leaving ffc_ok as None lets _validate_ffc judge it on live data like
        # any other, which is what the gate is for. Setting it True disabled the
        # gate for precisely the case most likely to need it.
        self.ffc_ok = None
        self.ffc_gain = None
        self.flat_btn.Enable()
        try:
            np.clip(np.rint(self.ffc), 0, 65535).astype("<u2").tofile(FFC_NAME)
            # record the gain it was taken at, so it is never applied at another
            with open(FFC_GAIN, "w") as fh:
                fh.write(str(self.last_gain))
            # and into the bank: one reference per gain, restored automatically
            # whenever the camera shows that gain again
            if self.last_gain is not None:
                ffc_bank_save(self.last_gain, self.ffc)
            # Save the individual frames too, not just their average. The
            # average is all the display needs, but it discards the only
            # evidence that separates a flickering defective element from one
            # holding a fixed offset, and that question could not be settled from
            # the averaged reference alone - the same six positions deviated
            # +1144..+1679 DL in one capture and +707..+910 DL in the next.
            try:
                np.save(FFC_BANK, bank.astype(np.uint16))
            except Exception as e:
                self.q.put(("log", "flat: could not save the frame bank: %s"
                                   % e))
        except Exception as e:
            self.q.put(("log", "flat: could not save %s: %s" % (FFC_NAME, e)))
            return
        # The bank is a uniform wall, so anything that misbehave in most frames
        # is a defective element. Judged per-frame rather than from the average,
        # because these flicker and an average smooths them away.
        try:
            mask = _detect_bad_pixels(bank)
            self.bad = mask
            np.packbits(mask.astype(np.uint8)).tofile(BAD_NAME)
            self.q.put(("log", "bad pixels: %d found and mapped" % int(mask.sum())))
        except Exception as e:
            self.bad = None
            self.q.put(("log", "bad-pixel map failed: %r" % (e,)))
        self.q.put(("log", "flat captured: mean %.0f std %.1f DL over %d frames -> %s" % (
            self.ffc.mean(), self.ffc.std(), len(bank), FFC_NAME)))
        self.push_status("flat captured - static should be gone")
        self.video.Refresh()

    def _load_flat(self):
        try:
            raw = np.fromfile(FFC_NAME, dtype="<u2")
        except OSError:
            return
        if raw.size != IMG_W * IMG_H:
            self.q.put(("log", "flat: %s has %d samples, want %d - ignoring" % (
                FFC_NAME, raw.size, IMG_W * IMG_H)))
            return
        self.ffc = raw.reshape(IMG_H, IMG_W).astype(np.float32)
        try:
            with open(FFC_GAIN) as fh:
                self.ffc_gain = int(fh.read().strip())
        except (OSError, ValueError):
            self.ffc_gain = None
            self.q.put(("log", "flat: no gain recorded for %s - it will only be "
                               "used at the gain that captured it" % FFC_NAME))
        self.q.put(("log", "flat loaded from %s (mean %.0f) - Clear flat to remove" % (
            FFC_NAME, self.ffc.mean())))
        # The bad-pixel map cannot be re-derived from a single stored reference - it
        # needs the per-frame evidence the capture collected. So the mask is
        # stored alongside it and read back.
        try:
            raw_mask = np.fromfile(BAD_NAME, dtype=np.uint8)
            want = IMG_W * IMG_H
            if raw_mask.size * 8 < want:
                raise ValueError("mask has %d bits, want %d"
                                 % (raw_mask.size * 8, want))
            bits = np.unpackbits(raw_mask)[:want]
            self.bad = bits.reshape(IMG_H, IMG_W).astype(bool)
            self.q.put(("log", "bad pixels: %d loaded from %s"
                               % (int(self.bad.sum()), BAD_NAME)))
        except Exception as e:
            self.bad = None
            self.q.put(("log", "no usable bad-pixel map (%r) - press F on a flat "
                               "wall to build one" % (e,)))

    def on_snapshot(self, ev):
        if self.frame_raw:
            try:
                self._save_png("capture_%d.png")
            except Exception:
                self._show_fatal("save PNG", sys.exc_info())
        else:
            self.push_status("no frame yet - nothing captured yet")
            self.log.AppendText("PNG: no frame captured yet\n")

    def on_raw(self, ev):
        if self.frame_raw:
            name = "frame_%d.raw" % int(time.time())
            open(name, "wb").write(self.frame_raw)
            self.q.put(("log", "saved " + name))
        else:
            self.push_status("no frame yet")

    def on_copy(self, ev):
        val = self.log.GetValue()
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(val))
            wx.TheClipboard.Close()
            self.q.put(("log", "-> copied to clipboard"))
        else:
            self.q.put(("log", "-> clipboard unavaiable"))

    # ------------------------------------------------------------------
    # --- DEV handlers --------------------------------------------------
    def on_dev_probe(self, ev):
        """force the next Start to walk the endpoint candidates again"""
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.stop_flag.set()
            self.stream_thread = None
        self.start_btn.SetGlyph("play")
        self.start_btn.SetLabel("Start stream")
        self.push_status("dev: stopped, will auto-probe endpoints on Start")
        self.q.put(("log", "dev: restart the stream to re-probe endpoints"))

    def on_dev_bind(self, ev):
        text = dev_bindinfo()
        self.q.put(("log", text))
        name = "bindinfo.txt"
        open(name, "w", encoding="utf-8").write(text)
        self.q.put(("log", "saved " + name))

    # ------------------------------------------------------------------

    def on_clear(self, ev):
        self.log.Clear()

    def _save_png(self, fmt):
        # No 2-byte skip: the 177,840 B transfer is exactly 342*260 uint16, so
        # the leading 0x79 0x05 is row 0, not a header. Dropping two samples here
        # sheared every saved snapshot by one pixel and cut the last row.
        # Saved image == displayed image: same FFC + median + NUC pipeline.
        img_arr = self._process(_roi_u16(self.frame_raw))
        if img_arr is None:
            # _process returns None for a rejected frame (mid-frame gain
            # change). Calling .min() on None is how this path used to crash.
            self.push_status("snapshot skipped - frame rejected")
            return
        vmin, vrange = img_arr.min(), max(1, img_arr.max() - img_arr.min())
        t = (img_arr.astype(np.float32) - vmin) / vrange
        rgb = COLORMAPS[self.lut_i][1](t)
        if hasattr(rgb, "astype"):
            rgb = rgb.astype(np.uint8)
        try:
            Image = _pil_image()
            path = fmt % int(time.time())
            Image.fromarray(rgb).save(path)
            # written to the panel directly, not just queued: if the queue is
            # not being drained the user still sees the confirmation
            self.log.AppendText("saved %s\n" % path)
        except ImportError:
            self.log.AppendText("PIL missing, snapshot not saved\n")

    # ---- painting ----
    def on_paint(self, ev):
        # Counted so "is the screen keeping up with the data" is answerable
        # from a log file instead of by staring at it. Every measurement taken
        # from outside the app - processed dumps, burst ring, per-frame noise -
        # says the picture is stable, so if the screen pulses while the data
        # does not, the only remaining explanation is that paints are being
        # merged or dropped. paint_count vs shown_frames answers that directly.
        self.paint_count = getattr(self, "paint_count", 0) + 1
        if self.paint_count % 25 == 0:
            try:
                _vlog("PAINT",
                      "%d paints, %d frames shown, "
                      "last paint drew frame_seq %s"
                      " | raw p2 %.1f p98 %.1f"
                      " | window lo %.1f hi %.1f span %.1f"
                      " | ffc_ok %s | skips %d"
                      % (self.paint_count,
                                getattr(self, "shown_frames", 0),
                                getattr(self, "_paint_seq", None),
                                (self._raw_p[0] if getattr(self, "_raw_p", None)
                                 else float("nan")),
                                (self._raw_p[1] if getattr(self, "_raw_p", None)
                                 else float("nan")),
                                (self.lo_ema if self.lo_ema is not None
                                 else float("nan")),
                                (self.hi_ema if self.hi_ema is not None
                                 else float("nan")),
                                ((self.hi_ema - self.lo_ema)
                                 if (self.lo_ema is not None
                                     and self.hi_ema is not None)
                                 else float("nan")),
                                getattr(self, "ffc_ok", None),
                                getattr(self, "stretch_skips", 0)))
            except Exception:
                pass
        # Build the bitmap BEFORE clearing. Clearing first and only then
        # discovering there is nothing to draw leaves the window black, and it
        # stays black until a frame arrives - which on screen is indistinguishable
        # from the camera dropping out. Build first, clear only if we have
        # something to put there, so a gap between frames keeps showing the last
        # picture instead of flashing to black.
        try:
            rgb = self._bmp()
        except Exception:
            self._show_fatal("paint", sys.exc_info())
            return
        if rgb is None:
            self.blank_paints = getattr(self, "blank_paints", 0) + 1
            if self.blank_paints in (1, 50, 500):
                try:
                    _vlog("BLANK",
                          "paint %d: no frame to draw "
                          "(shown %d, held %d, rej %d)"
                          % (self.blank_paints,
                             getattr(self, "shown_frames", 0),
                             getattr(self, "held_gain", 0),
                             getattr(self, "bad_frames", 0)))
                except Exception:
                    pass
            # designed empty state: faint HUD frame + NO SIGNAL, not a void
            dc = wx.AutoBufferedPaintDC(self.video)
            cw, ch = self.video.GetClientSize()
            # explicit fill (SetBackground/Clear proved unreliable on wxGTK)
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(wx.Colour(7, 10, 14)))
            dc.DrawRectangle(0, 0, cw, ch)
            dc.SetPen(wx.Pen(wx.Colour(34, 44, 56)))
            dc.DrawRoundedRectangle(1, 1, cw - 2, ch - 2, 10)
            # 1-in-8 scanline grid texture, very faint
            dc.SetPen(wx.Pen(wx.Colour(14, 19, 25)))
            y = 8
            while y < ch - 8:
                dc.DrawLine(8, y, cw - 8, y)
                y += 28
            f = _ui_font(14, bold=True)
            if f is not None:
                dc.SetFont(f)
            dc.SetTextForeground(wx.Colour(64, 76, 90))
            label = "NO SIGNAL"
            lw, lh = dc.GetTextExtent(label)
            dc.DrawText(label, (cw - lw) // 2, (ch - lh) // 2 - 12)
            f2 = _ui_font(9)
            if f2 is not None:
                dc.SetFont(f2)
            sub = "plug the Nano in, then press Start stream"
            sw, sh = dc.GetTextExtent(sub)
            dc.DrawText(sub, (cw - sw) // 2, (ch - sh) // 2 + 18)
            return
        # The ENTIRE draw is inside its own try. It used to stop wrapping at
        # _bmp(), so a throw from wx.Image/Scale/Bitmap/DrawBitmap - the exact
        # calls that were failing - left the panel cleared to black with nothing
        # drawn and no error recorded anywhere, because in a windowed build an
        # exception in an event handler goes to a stderr nobody sees.
        try:
            bmp = self._to_bitmap(rgb)
            # wx.BufferedPaintDC, NOT wx.PaintDC. A plain PaintDC draws straight
            # onto the window, so the panel is visible mid-repaint and Windows
            # can erase it before the handler runs - the erased state is a black
            # panel. Measured from the real screen: the video panel went to mean
            # 0.47/255 with image-std 8.1 while normally sitting at mean 140 with
            # std 69, and the log panel beside it was rock steady (p-p 0.00), so
            # the flicker was entirely in the panel being drawn.
            #
            # Nothing was throwing: seeknano_crash.log stayed empty, and the
            # array handed to DrawBitmap was provably good on all 713 paints
            # (rgb std 69-78, never flat). So this was never bad data and never
            # a failed draw - it was an unbuffered one.
            # BufferedPaintDC draws off-screen and blits atomically.
            dc = wx.BufferedPaintDC(self.video)
            dc.SetBackground(wx.BLACK_BRUSH)
            dc.Clear()
            dc.DrawBitmap(bmp, 0, 0)
            # HUD frame overlay: hairline inset + corner ticks, drawn only
            # when a real frame is on screen (never over the blank startup)
            cw, ch = self.video.GetClientSize()
            # pen only: DrawRectangle fills with the DC brush (white on
            # wxGTK) unless the fill is transparent - that white vinyl
            # covering the thermal frame is exactly what the user saw
            dc.SetBrush(wx.TRANSPARENT_BRUSH)
            dc.SetPen(wx.Pen(wx.Colour(46, 62, 78)))
            dc.DrawRectangle(0, 0, cw, ch)
            oc = wx.Colour(*THEME["accent"])
            dc.SetPen(wx.Pen(oc))
            for (cx, cy, dx, dy) in ((0, 0, 18, 0), (0, 0, 0, 18),
                                     (cw - 1, 0, -18, 0), (cw - 1, 0, 0, 18),
                                     (0, ch - 1, 18, 0), (0, ch - 1, 0, -18),
                                     (cw - 1, ch - 1, -18, 0),
                                     (cw - 1, ch - 1, 0, -18)):
                dc.DrawLine(cx, cy, cx + dx, cy + dy)
            # ---- analysis overlays (all data-derived, off-screen accurate)
            ana = getattr(self, "_analysis", None)
            if ana is not None:
                self._draw_scale_bar(dc, cw, ch)
                self._draw_pointer(dc, ana, cw, ch)
            # iso tag lives outside the diluted helpers stack
            if getattr(self, "iso_on", False):
                self._draw_iso_tag(dc, ana, cw)
        except Exception:
            self._show_fatal("draw", sys.exc_info())

    # ---- analysis overlay helpers -------------------------------------------

    def _img_from_mouse(self, pt):
        """Mouse coords on the canvas -> corrected-frame coords."""
        img = getattr(self, "_analysis", None)
        if img is None:
            return None, None
        cw, ch = self.video.GetClientSize()
        if cw <= 0 or ch <= 0:
            return None, None
        ih, iw = img.shape
        return (pt[0] * iw / cw, pt[1] * ih / ch)

    def _draw_pointer(self, dc, img, cw, ch):
        """Cursor readout + rubber-band region stats + shift-drawn profile.

        Grown by measurement: the single most-used question a thermographer
        asks is 'what is this pixel / this patch / this line doing' - so the
        canvas answers all three without leaving the image.
        """
        mouse = getattr(self, "mouse", None)
        box = getattr(self, "sel_box", None)      # (p0, p1) rubber-band
        line = getattr(self, "sel_line", None)    # (p0, p1) shift-drawn
        dc.SetBrush(wx.TRANSPARENT_BRUSH)

        def chip(s, x, y, fg=THEME["text"], bg=(14, 20, 27)):
            f = _ui_font(9, mono=True)
            if f is not None:
                dc.SetFont(f)
            dc.SetTextForeground(wx.Colour(*fg))
            tw, th = dc.GetTextExtent(s)
            dc.SetBrush(wx.Brush(wx.Colour(*bg)))
            dc.SetPen(wx.Pen(wx.Colour(40, 54, 68)))
            dc.DrawRoundedRectangle(x, y, tw + 12, th + 6, 4)
            dc.DrawText(s, x + 6, y + 3)

        # profile: draw the line + the plot
        if line is not None:
            p0, p1 = self._img_from_mouse(line[0]), self._img_from_mouse(line[1])
            if p0[0] is not None and p1[0] is not None:
                dc.SetPen(wx.Pen(wx.Colour(*THEME["accent2"]), 1, wx.PENSTYLE_SHORT_DASH))
                dc.DrawLine(line[0][0], line[0][1], line[1][0], line[1][1])
                prof = _line_profile(img, p0, p1)
                self._draw_profile_plot(dc, prof, cw, ch)
        # rubber-band region stats
        if box is not None:
            x0, y0 = box[0]
            x1, y1 = box[1]
            dc.SetPen(wx.Pen(wx.Colour(*THEME["accent2"])))
            dc.DrawRectangle(min(x0, x1), min(y0, y1),
                             abs(x1 - x0), abs(y1 - y0))
            ip0, ip1 = self._img_from_mouse(box[0]), self._img_from_mouse(box[1])
            if ip0[0] is not None and ip1[0] is not None:
                ih, iw = img.shape
                rx0 = int(max(0, min(min(ip0[0], ip1[0]), iw)))
                rx1 = int(max(0, min(max(ip0[0], ip1[0]), iw)))
                ry0 = int(max(0, min(min(ip0[1], ip1[1]), ih)))
                ry1 = int(max(0, min(max(ip0[1], ip1[1]), ih)))
                if rx1 > rx0 and ry1 > ry0:
                    win = img[ry0:ry1, rx0:rx1]
                    chip("min %d  max %d  avg %.1f" % (win.min(), win.max(),
                                                       win.mean()),
                         12, 24, fg=THEME["accent2"])
        # cursor crosshair + value
        if mouse is not None:
            dc.SetPen(wx.Pen(wx.Colour(*THEME["accent2"])))
            mx, my = mouse
            dc.DrawLine(mx - 10, my, mx - 4, my)
            dc.DrawLine(mx + 4, my, mx + 10, my)
            dc.DrawLine(mx, my - 10, mx, my - 4)
            dc.DrawLine(mx, my + 4, mx, my + 10)
            pt = self._img_from_mouse(mouse)
            if pt[0] is not None:
                st = _sample_stats(img, pt)
                if st is not None:
                    c = dl_to_c(st[0], getattr(self, "_anchors", None))
                    ct = "" if c is None else "  %5.1f\u00b0C" % c
                    chip("%d%s" % (round(st[0]), ct), mx + 12, my - 22,
                         fg=THEME["accent2"])
                    cavg = dl_to_c(st[1], getattr(self, "_anchors", None))
                    avg_s = ("%.1f\u00b0C" % cavg) if cavg is not None \
                        else "%.1f" % st[1]
                    chip("\u03bc %s  min %d  max %d" % (avg_s, st[2], st[3]),
                         mx + 12, my + 6)

    def _draw_profile_plot(self, dc, prof, cw, ch):
        """The profile, drawn in the canvas' bottom-right corner."""
        pw, ph = min(420, cw // 2), 110
        x, y = cw - pw - 12, ch - ph - 12
        lo, hi = float(prof.min()), float(prof.max())
        span = max(1.0, hi - lo)
        dc.SetBrush(wx.Brush(wx.Colour(10, 15, 21)))
        dc.SetPen(wx.Pen(wx.Colour(40, 54, 68)))
        dc.DrawRoundedRectangle(x, y, pw, ph, 6)
        # the curve itself
        dc.SetPen(wx.Pen(wx.Colour(*THEME["accent2"])))
        for i in range(0, len(prof) - 1, 2):
            t0 = (float(prof[i]) - lo) / span
            t1 = (float(prof[i + 1]) - lo) / span
            px = x + 8 + int((pw - 16) * i / max(1, len(prof) - 1))
            py0 = (y + ph - 12) - t0 * (ph - 24)
            py1 = (y + ph - 12) - t1 * (ph - 24)
            dc.DrawLine(px, py0, px + int((pw - 16) / max(1, (len(prof) - 1) / 2.0)), py1)
        f = _ui_font(9, mono=True)
        if f is not None:
            dc.SetFont(f)
        dc.SetTextForeground(wx.Colour(*THEME["muted"]))
        dc.DrawText("profile %d \u2192 %d DL" % (lo, hi), x + 8, y + 4)

    def _draw_scale_bar(self, dc, cw, ch):
        """Ironbow identity bar + the current DL window, bottom-left."""
        bw, bh = 180, 10
        x, y = 14, ch - bh - 14
        for i in range(bw):
            t = i / max(1, bw - 1)
            r, g, b = ironbow(t)
            dc.SetPen(wx.Pen(wx.Colour(
                max(0, min(255, int(r * 255))),
                max(0, min(255, int(g * 255))),
                max(0, min(255, int(b * 255))))))
            dc.DrawLine(x + i, y, x + i, y + bh)
        f = _ui_font(9, mono=True)
        if f is not None:
            dc.SetFont(f)
        dc.SetTextForeground(wx.Colour(*THEME["muted"]))
        # `or 0`, not getattr(..., 0): the attribute EXISTS but is None until the
        # first frame completes its stretch, and getattr's default only applies
        # when the attribute is missing. A rejected frame returns before the
        # stretch runs, so this genuinely is None here, and "%d" % None raises.
        dc.DrawText("%d DL" % (self.lo_ema or 0), x, y - 16)
        hi = "%d DL" % (self.hi_ema or 0)
        hw, _hh = dc.GetTextExtent(hi)
        dc.DrawText(hi, x + bw - hw, y - 16)
        dc.DrawText("ironbow", x + bw + 10, y)

    def _draw_iso_tag(self, dc, img, cw):
        thr = getattr(self, "iso_thr", None)
        n = getattr(self, "_iso_count", None)
        f = _ui_font(9, mono=True)
        if f is not None:
            dc.SetFont(f)
        dc.SetTextForeground(wx.Colour(*THEME["accent2"]))
        dc.DrawText("ISO \u2265 %s  (%s px)" % (
            "%d" % thr if thr is not None else "-",
            n if n is not None else "-"), cw - 150, 12)

    def _scanline_health(self, img):
        """Watch for a corrupted scanline, and save the frame when one appears.

        There is a visible fault - a full-width horizontal band of broken colour
        confined to one or two rows - that has resisted every cheap explanation.
        Measured so far, by elimination rather than by argument:

          * it is not the hot-spot overlay: it is corrupted IMAGE DATA, and no
            drawing code can corrupt pixels
          * not the camera or the USB transfer: 60 frames pulled straight off the
            wire were clean
          * not _roi_u16, the 3x3 median, _nuc2d or _apply_ffc: a real frame
            walked through each stage is clean at all four
          * not the cursor / line-profile overlay: identical with the cursor on
            and off the canvas
          * not a mid-frame gain switch: a 240-frame burst with 26 gain
            transitions (gains 1..28) produced no seam and no dashes

        What is left is something that needs a frame WITH the fault in it, and
        it has not reproduced on demand. So the viewer now measures itself and
        keeps the evidence: a compact health line goes to the verbose log, and
        the moment a row scores far outside the frame's own spread, the raw
        frame is written next to the exe.

        Cheap by construction - one mean and one diff over the frame, every
        frame, no allocation beyond the row profile.
        """
        n = getattr(self, "_scan_n", 0) + 1
        self._scan_n = n
        prof = np.abs(np.diff(img, axis=1)).mean(axis=1)
        med = float(np.median(prof))
        mad = float(np.median(np.abs(prof - med)))
        scale = max(med + 3.0 * mad, 1e-3)
        z = prof / scale
        row = int(np.argmax(z))
        zmax = float(z[row])
        self._scan_z = zmax
        self._scan_row = row
        if n % 25 == 0:
            try:
                _vlog("SCAN", "row-profile max z %.1f at row %d (median %.1f DL)"
                           % (zmax, row, med))
            except Exception:
                pass
        # Rate-limited: only the first fault every 5 s is kept, so a persistent
        # fault cannot fill the disk.
        if zmax > SCAN_FAULT_Z:
            # REJECT the frame. Measured from the first captured fault: the
            # offending rows carried ~35,000 DL while rows 0-237 carried
            # ~6,500 - a ratio of 5.49, which is the AGC changing gain partway
            # down the frame. The bytes are valid and the header passes every
            # check, so nothing upstream can catch it, and the result on screen
            # is a full-width band of nonsense colour.
            #
            # Signalled to _process, which returns None, so _bmp falls through
            # to its cached bitmap and the previous good picture stays up. The
            # frame is dropped rather than repaired: the gain the bottom rows
            # were captured at is not known, so any correction would be a
            # guess.
            self._scan_fault = True
            self._scan_rejected = getattr(self, "_scan_rejected", 0) + 1
        else:
            self._scan_fault = False
        if zmax > SCAN_FAULT_Z and time.time() - getattr(
                self, "_scan_last_save", 0.0) > 5.0:
            self._scan_last_save = time.time()
            try:
                import struct
                # relative, like every other dump: the app chdirs to the exe
                # directory at startup, so this lands next to SeekNano.exe
                p = "scanline_fault_%d.raw" % n
                # self.frame_raw is the raw USB payload: bytes. np.clip on a
                # bytes object raises UFuncTypeError, so every earlier attempt
                # wrote a header-only 8-byte stub and the fault was never
                # actually captured - the log said "save failed" and moved on.
                # Dump the array that was ACTUALLY SCORED, not self.frame_raw.
                # self.frame_raw is rewritten by the reader thread for every
                # frame that arrives, while this runs on the GUI thread over an
                # older one - so saving frame_raw captured a different frame
                # than the faulted one, and the row that z=35 was reported on
                # came back perfectly normal (z 0.9) in the dump. Write the
                # scored image first, then the raw payload alongside so the two
                # can be compared.
                scored = np.ascontiguousarray(img, dtype=np.float32)
                raw = np.frombuffer(self.frame_raw, dtype=np.uint8)
                with open(p, "wb") as fh:
                    fh.write(struct.pack("<IHH", n, SCAN_FAULT_Z_INT, row))
                    fh.write(struct.pack("<I", scored.size))
                    fh.write(scored.tobytes())
                    fh.write(raw.tobytes())
                self.q.put(("log", "scanline fault: row %d at z %.1f -> %s"
                                   % (row, zmax, os.path.basename(p))))
                _vlog("SCANFAULT", "row=%d z=%.1f median=%.1f n=%d "
                                  "scored=%d raw=%d"
                      % (row, zmax, med, n, scored.size, raw.size))
            except Exception as e:
                self.q.put(("log", "scanline fault save failed: %r" % (e,)))

    def _detect_spots(self, img):
        """Run the detector on corrected device-unit values and update state."""
        if self.spot_mode == "off":
            self.spots = []
            return
        try:
            # hotspot.py lives next to viewer.py: make that importable on
            # every route (frozen onefile resolves via _MEIPASS when the
            # module is bundled; bare-run needs the app dir on sys.path).
            # The failure mode before this fix: the import raised inside a
            # windowed build's silent catch, spots stayed [] forever while
            # the button still cycled - "no data, artifacts".
            _here = os.path.dirname(os.path.abspath(__file__))
            if _here not in sys.path:
                sys.path.insert(0, _here)
            import hotspot as _hs
            mode = "top" if self.spot_mode == "alarm" else "auto"
            self.spots = _hs.detect(img, sensitivity=self.spot_sens,
                                    mode=mode, max_spots=HOTSPOT_MAX)
        except Exception:
            self._show_fatal("hotspot detect", sys.exc_info())
            self.spots = []
            return
        now = time.time()
        # temporal stability: a region must persist over consecutive frames
        # to be drawn. Detection jitter (a threshold that lands on noise)
        # made the boxes and the alarm border strobe - the "artifacts" seen
        # when hot-spots were enabled. A real hotspot is there for seconds.
        # Track regions by last known POSITION, not by grid cell. Keying on an
        # 8x8 cell was the real cause of the reported artifacts: a hotspot that
        # moves 1 px per frame crosses a cell boundary every 8 px, the new cell
        # starts its counter from zero, and the box blanks for 2 frames while it
        # climbs back to 3. Measured: 37 blackouts in 150 frames, one every
        # 8 px of travel. A still target hid it, which is why it looked fine
        # until something actually moved.
        #
        # So each region inherits the score of whichever previous region it is
        # closest to, within SPOT_MOVE px. Motion then carries the evidence
        # instead of resetting it.
        pts = [(g["y"], g["x"]) for g in self.spots]
        prev = list(getattr(self, "_spot_pts", []))
        prev_scores = list(getattr(self, "_spot_score", []))
        # Nearest-neighbour inheritance: each region takes the score of the
        # closest unmatched region from the previous frame, if it is within
        # SPOT_MOVE. One previous region can only be inherited once.
        scores = []
        used = set()
        for (cy, cx) in pts:
            best, bd, bi = 0.0, float(SPOT_MOVE) + 1.0, None
            for i, (py, px) in enumerate(prev):
                if i in used or i >= len(prev_scores):
                    continue
                d = ((cy - py) ** 2 + (cx - px) ** 2) ** 0.5
                if d <= SPOT_MOVE and d < bd:
                    bd, best, bi = d, float(prev_scores[i]), i
            if bi is not None:
                used.add(bi)
            scores.append(min(SPOT_STABLE, best + 1))
        stable = [g for g, s_ in zip(self.spots, scores) if s_ >= SPOT_STABLE]
        self._spot_score = scores
        self._spot_pts = list(pts)
        for g in stable:
            self.spot_hist.append((now, g["y"], g["x"], g["peak"]))
        # forget anything older than the hold time
        self.spot_hist = [h for h in self.spot_hist
                          if now - h[0] <= HOTSPOT_HOLD]
        if len(self.spot_hist) > 400:
            self.spot_hist = self.spot_hist[-400:]
        # the drawn boxes + the alarm both consume the STABLE set, so
        # frame-to-frame detector jitter cannot strobe the overlay
        self.spots = stable
        if self.spot_mode == "alarm" and self.spots:
            self.spot_alarm = now
            self.spot_alarm_on = True
        elif self.spot_alarm and now - self.spot_alarm > 0.4:
            self.spot_alarm_on = False

    def _draw_hotspots(self, rgb, t):
        """Overlay the detected regions on the colormapped image.

        Drawn after the colormap so the markers stay legible whatever palette
        is active, and scaled with the image so they read the same at any
        window size.
        """
        if self.spot_mode == "off" or not self.spots:
            return rgb
        H, W = t.shape
        out = rgb.copy()
        sx = out.shape[1] / float(W)
        sy = out.shape[0] / float(H)
        k = max(1, int(round(min(sx, sy))))

        def box(y0, y1, x0, x1, colour, thick=1):
            # Clamp into the image. Without this a region touching the bottom or
            # right edge asks for row 240 of a 240-row frame and the whole paint
            # is lost - which is exactly what happened, caught in
            # seeknano_crash.log: IndexError at this line.
            a = max(0, min(out.shape[0] - 1, int(y0 * sy)))
            b = max(0, min(out.shape[0] - 1, int(y1 * sy)))
            c = max(0, min(out.shape[1] - 1, int(x0 * sx)))
            d = max(0, min(out.shape[1] - 1, int(x1 * sx)))
            if b <= a or d <= c:
                return
            for o in range(thick):
                aa = min(out.shape[0] - 1, a + o)
                bb = max(0, b - o)
                cc = min(out.shape[1] - 1, c + o)
                dd = max(0, d - o)
                if bb < aa or dd < cc:
                    continue
                out[aa, cc:dd + 1] = colour
                out[bb, cc:dd + 1] = colour
                out[aa:bb + 1, cc] = colour
                out[aa:bb + 1, dd] = colour

        if self.spot_alarm_on:
            # flash the whole frame border while the alarm condition holds
            box(0, H - 1, 0, W - 1, (255, 40, 40), thick=max(2, 2 * k))

        now = time.time()
        for i, g in enumerate(self.spots):
            hot = (i == 0)
            colour = (255, 255, 0) if hot else (255, 170, 0)
            y0, y1 = g["y0"], g["y1"]
            x0, x1 = g["x0"], g["x1"]
            if self.spot_mode in ("mark", "track", "alarm"):
                box(y0 - 1, y1 + 1, x0 - 1, x1 + 1, colour, thick=k)
            elif self.spot_mode == "outline" and not hot:
                # strongest region drawn solid, the rest dotted by outline only
                box(y0 - 1, y1 + 1, x0 - 1, x1 + 1, colour, thick=k)
            # crosshair on the hottest
            cy, cx = int(g["y"] * sy), int(g["x"] * sx)
            for d in range(2 * k, 5 * k):
                for px, py in ((cx + d, cy), (cx - d, cy), (cx, cy + d),
                               (cx, cy - d)):
                    if 0 <= py < out.shape[0] and 0 <= px < out.shape[1]:
                        out[py, px] = colour

        if self.spot_mode == "track":
            for (ts, hy, hx, peak) in self.spot_hist:
                age = now - ts
                if age > HOTSPOT_HOLD:
                    continue
                f = 1.0 - age / HOTSPOT_HOLD
                c = (int(255 * f), int(255 * f), 0)
                px, py = int(hx * sx), int(hy * sy)
                if 0 <= py < out.shape[0] and 0 <= px < out.shape[1]:
                    y1 = min(out.shape[0] - 1, py + k)
                    y0 = max(0, py - k)
                    x1 = min(out.shape[1] - 1, px + k)
                    x0 = max(0, px - k)
                    out[y0:y1 + 1, x0:x1 + 1] = c
        return out

    def _usb_reconnect(self):
        """Restart the stream after a USB stall."""
        self._usb_retries = getattr(self, "_usb_retries", 0)
        if self.stream_thread is not None:
            return
        self.on_toggle(None)

    def on_spot_mode(self, ev=None):
        """Cycle off -> mark -> outline -> track -> alarm."""
        try:
            i = HOTSPOT_MODES.index(self.spot_mode)
        except ValueError:
            i = 0
        self.spot_mode = HOTSPOT_MODES[(i + 1) % len(HOTSPOT_MODES)]
        self.spot_hist = []
        self.spot_alarm_on = False
        if self.spot_btn is not None:
            self.spot_btn.SetLabel("hot spots: " + self.spot_mode)
        self.push_status("hot-spot mode: " + self.spot_mode)
        if self.spot_mode != "off":
            self.video.Refresh()

    def on_spot_sens(self, ev=None):
        """Cycle the detection sensitivity between three steps."""
        vals = (0.35, 1.0, 2.5)
        try:
            i = vals.index(self.spot_sens)
        except ValueError:
            i = 1
        self.spot_sens = vals[(i + 1) % len(vals)]
        self.push_status("hot-spot sensitivity: %.2f" % self.spot_sens)
        self.video.Refresh()

    def _overlay(self, t):
        """Burn the state into the picture.

        A PNG the user saves should answer every open question on its own -
        how many frames were shown, how many were rejected or held, which gain
        is on screen, and whether the background or a wall reference is active.
        Diagnosing this from a description of what the screen looks like has
        been the slowest part of the whole exercise.
        """
        # diagnostic stamp is OFF in the shipped viewer: it was baking the
        # whole session log into every picture. Toggle with "o" when
        # diagnosing; snapshots then ship as clean thermal images.
        if not getattr(self, "stamp", False):
            return t
        t = t.copy()
        # ffc state is shown as three distinct words, not a yes/no, because
        # "a reference exists" and "the reference is being applied" are
        # different things and the difference is exactly what the pulsing
        # turned on. Reading it off a burst PNG is the only way to tell from
        # outside whether the correction is switching.
        if self.ffc is None:
            ffc = "none"
        elif self.ffc_ok is None:
            ffc = "checking"
        elif self.ffc_ok:
            ffc = "ON"
        else:
            ffc = "OFF(stale)"
        lines = [
            "shown %d  rej %d  held %d  sat %d" % (
                self.shown_frames, self.bad_frames, self.held_gain,
                self.saturated),
            "gain %s  bg %d/%d  ffc %s" % (
                self.shown_gain, len(self.bg_frames), BG_FRAMES, ffc),
            "paint %d" % getattr(self, "paint_count", 0),
        ]
        if self.spot_mode != "off":
            top = self.spots[0] if self.spots else None
            if top is not None:
                lines.append("spot %d  hot %d DL @ %d,%d" % (
                    len(self.spots), top["peak"], int(top["x"]),
                    int(top["y"])))
            else:
                lines.append("spot 0  (none above threshold)")
        h = len(lines) * 12 + 6
        t[:h, :210] = 0.0
        for i, s in enumerate(lines):
            self._text(t, s, 4, 6 + i * 12)
        return t

    @staticmethod
    def _text(t, s, x, y, scale=2):
        """5x7 bitmap font, enough for digits and status text."""
        glyphs = {
            '0': ("11111", "10001", "10001", "10001", "10001", "10001", "11111"),
            '1': ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
            '2': ("11111", "00001", "00001", "11111", "10000", "10000", "11111"),
            '3': ("11111", "00001", "00001", "01111", "00001", "00001", "11111"),
            '4': ("10001", "10001", "10001", "11111", "00001", "00001", "00001"),
            '5': ("11111", "10000", "10000", "11111", "00001", "00001", "11111"),
            '6': ("11111", "10000", "10000", "11111", "10001", "10001", "11111"),
            '7': ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
            '8': ("11111", "10001", "10001", "11111", "10001", "10001", "11111"),
            '9': ("11111", "10001", "10001", "11111", "00001", "00001", "11111"),
            'a': ("00000", "00000", "11110", "00001", "11111", "10001", "01111"),
            'b': ("10000", "10000", "11110", "10001", "10001", "10001", "11110"),
            'c': ("00000", "00000", "01111", "10000", "10000", "10000", "01111"),
            'd': ("00001", "00001", "01111", "10001", "10001", "10001", "01111"),
            'e': ("00000", "00000", "11110", "10001", "11111", "10000", "11110"),
            'f': ("00000", "01110", "10000", "11110", "10000", "10000", "10000"),
            'g': ("00000", "01111", "10001", "10001", "01111", "00001", "11110"),
            'h': ("10000", "10000", "11110", "10001", "10001", "10001", "10001"),
            'i': ("00100", "00000", "01100", "00100", "00100", "00100", "01110"),
            'j': ("00010", "00000", "00110", "00010", "00010", "10010", "01100"),
            'k': ("10000", "10010", "10100", "11000", "10100", "10010", "10001"),
            'l': ("01100", "00100", "00100", "00100", "00100", "00100", "01110"),
            'm': ("00000", "00000", "11010", "10101", "10101", "10101", "10101"),
            'n': ("00000", "00000", "11110", "10001", "10001", "10001", "10001"),
            'o': ("00000", "00000", "01110", "10001", "10001", "10001", "01110"),
            'p': ("00000", "11110", "10001", "10001", "11110", "10000", "10000"),
            'q': ("00000", "01111", "10001", "10001", "01111", "00001", "00001"),
            'r': ("00000", "00000", "10110", "11001", "10000", "10000", "10000"),
            's': ("00000", "00000", "01111", "10000", "01110", "00001", "11110"),
            't': ("01000", "01000", "11110", "01000", "01000", "01001", "00110"),
            'u': ("00000", "00000", "10001", "10001", "10001", "10011", "01101"),
            'v': ("00000", "00000", "10001", "10001", "10001", "01010", "00100"),
            'w': ("00000", "00000", "10001", "10101", "10101", "10101", "01010"),
            'x': ("00000", "00000", "10001", "01010", "00100", "01010", "10001"),
            'y': ("00000", "10001", "10001", "10001", "01111", "00001", "11110"),
            'z': ("00000", "00000", "11111", "00010", "00100", "01000", "11111"),
            '/': ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
            '-': ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
            '.': ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
            ' ': ("00000",) * 7,
        }
        cx = x
        for ch in s.lower():
            g = glyphs.get(ch)
            if g is None:
                cx += 6 * scale
                continue
            for ry, rowstr in enumerate(g):
                for rx, bit in enumerate(rowstr):
                    if bit == "1":
                        ys, xs = y + ry * scale, cx + rx * scale
                        t[ys:ys + scale, xs:xs + scale] = 1.0
            cx += 6 * scale

    def _validate_ffc(self, img):
        """Keep the flat reference only while it actually helps.

        A wall reference goes stale: the sensor's per-pixel pattern drifts with
        temperature, and the AGC changes both the level and the pattern
        amplitude. Measured directly on this hardware, subtracting a reference
        captured four hours earlier took the noise from 223 DL to 318 DL - it
        was ADDING noise. So instead of trusting it, compare both ways on live
        data and keep whichever is cleaner.

        Keyed on shown_gain, NOT last_gain. last_gain is assigned for every
        frame the camera delivers (before the gain lock), so it cycles with the
        AGC - 3, 6, 1, 20, 3... - while the picture on screen is always shown_gain
        and the reference was captured from frames that had already passed the
        lock. Validating against last_gain re-ran this comparison on every AGC
        wobble, and a reference that is only marginally better flips the
        decision on noise alone. That switched the correction off and back on
        repeatedly, which is the brightness pulsing.
        """
        if self.ffc is None:
            self.ffc_ok = None
            return
        gain = getattr(self, "shown_gain", None)
        if gain is None:
            return
        if self.ffc_ok is not None and self.ffc_gain == gain:
            return
        raw_dx = float(np.abs(np.diff(img, axis=1)).mean())
        fixed = _apply_ffc(img, self.ffc)
        fix_dx = float(np.abs(np.diff(fixed, axis=1)).mean())
        # Require a clear margin, not merely "no worse". Judged on a single
        # frame, a reference that is roughly break-even can pass and fail on
        # successive frames, which switches the correction on and off and puts
        # the speckle back. A good reference is worth far more than this - a
        # fresh wall capture measures 240 DL down to a few tens - so demanding
        # 10% costs nothing when the reference is good and rejects the marginal
        # ones that cause artefacts.
        self.ffc_ok = fix_dx < raw_dx * 0.90
        if self.ffc_ok:
            self.ffc_gain = gain
            self.q.put(("log", "flat reference accepted at gain %s: noise "
                                "%.1f -> %.1f DL" % (gain, raw_dx, fix_dx)))
        else:
            self.q.put(("log", "flat reference REJECTED at gain %s: it would "
                                "raise noise %.1f -> %.1f DL - press F to "
                                "re-capture here" % (gain, raw_dx, fix_dx)))
            self.push_status("stale flat reference - press F to re-capture")

    def _process(self, img):
        """ROI float32 -> display-ready float32. One pipeline for screen + PNG.

        Order matters: the flat-field term removes the temporally-stable
        per-pixel offsets; the 3x3 median then only has whatever is left to
        chew on; the column/row NUC takes out the striping.

        With no wall reference the offsets are estimated from the last few
        settled frames. ONLY their high-frequency part may be subtracted:

            hp  = bg - boxblur(bg)     # the per-pixel offsets
            out = img - hp             # keep the frame's own low frequencies

        Subtracting the whole background (img - bg) looks much better on paper
        and is badly wrong. The background average contains the SCENE as well as
        the offsets, so it removes both. Measured on 9 held-out live frames at
        gain 3:

            variant                neighbour dx   1-99% scene span
            raw                             69.5            1661 DL
            img - bg   (was shipping)         1.9              42 DL  <- scene gone
            img - highpass(bg)               6.8            1486 DL

        So the "36x noise reduction" that measurement reported was 97% of the
        picture thrown away, and the viewer's auto-stretch then painted the
        residual noise across the full colour range - which is what the static
        and black frames were. The high-pass form still removes 90% of the noise
        and keeps 89% of the scene.
        """
        if self.ffc is not None and self.ffc_ok:
            # Only apply the reference while it demonstrably reduces noise on
            # live data, at the current gain. See _validate_ffc: a stale
            # reference measurably ADDS noise.
            img = _apply_ffc(img, self.ffc)
        elif self.bg_hp is not None:
            # The running background is built from frames at the locked gain, so
            # it is gain-correct by construction and stays valid.
            #
            # This used to be `elif self.ffc is not None:` guarding the whole
            # pair, so a flat that was LOADED but REJECTED by _validate_ffc
            # skipped the fallback entirely and left the picture uncorrected
            # for the rest of the session. ffc_latest.raw is on disk from an
            # earlier session, so this was the normal path, not an edge case:
            # the log read "flat reference REJECTED at gain 3: it would raise
            # noise 244.1 -> 230.0 DL" and then never corrected anything, and
            # the auto-stretch painted the raw pattern over the full colormap
            # as static. A rejected reference must fall back, not veto.
            img = img - self.bg_hp
        # Defective elements are corrected independently of the flat-field
        # reference. The reference fixes the broad per-pixel offsets; a handful
        # of elements stuck hundreds of DL hot survive that averaging entirely,
        # because five bad pixels out of 76,800 move the aggregate by nothing
        # while being unmissable on screen. Six were confirmed live at +392 to
        # +797 DL from their neighbours, in 100% of 147 frames.
        img = _apply_bad_pixels(img, self.bad)
        # Hot-spot detection runs on the corrected device-unit values, BEFORE
        # the contrast stretch. Stretching maps every frame to the full
        # colormap, so after it every frame has hot regions by construction and
        # the question is meaningless. Here "hot" means hot compared with this
        # scene, which is the only honest reading without absolute calibration.
        self._detect_spots(img)
        self._scanline_health(img)
        if getattr(self, "_scan_fault", False):
            # A mid-frame gain change. The previous good picture stays on screen
            # via the _bmp cache rather than showing the empty state.
            return None
        p1 = np.pad(img, 1, mode="edge")
        stack = np.stack([p1[dy:dy + IMG_H, dx:dx + IMG_W]
                          for dy in range(3) for dx in range(3)])
        # 3x3 median = the 5th smallest of 9 values, so partition at k=4 and
        # take that element. np.median(stack, axis=0) computes exactly the same
        # thing but measured 48.6 ms against 25.3 ms for this on one 240x320
        # float32 frame - it forms the mean of the two central elements as well,
        # which is wasted work when the count is odd, and copies first. At 25 fps
        # a 40 ms budget exists per frame, so those 23 ms are the difference
        # between painting and stalling. Verified bit-identical output.
        img = np.partition(stack, 4, axis=0)[4]
        # 2-D shutterless NUC (data-driven, fitted on a live frame)
        img = _nuc2d(img)
        if DISPLAY_SMOOTH >= 3:
            # The residue after correction is blotchy at a ~10-20 px scale, not
            # fine speckle, so a 9 px high-pass cannot reach it. Measured on
            # live frames with the camera close to the subject:
            #
            #   extra blur k    neighbour dx   scene span kept
            #   none                47.1            100%
            #   5                    7.6             81%
            #   7                    5.4             80%
            #   9                    4.1             79%
            #
            # k=5 is 6x cleaner for a 19% contrast cost. This is a display
            # choice, not a correction: it smooths what is left, it does not
            # claim to remove the pattern. A wall reference (Capture flat)
            # removes the pattern itself and is still the better path.
            img = _boxblur(img, DISPLAY_SMOOTH)
        return img

    def _bmp(self):
        # display frame, NOT the pump's newest frame: while paused the pump
        # keeps running but frame_raw changes underneath the cache key, and
        # any stray paint event would run the full pipeline on live data -
        # exactly the "paused but pulsing" bug. _display_raw is pinned by the
        # tick handler and only follows frame_raw while running/unpausing.
        disp = getattr(self, "_display_raw", self.frame_raw)
        cached = getattr(self, "_bmp_cache", None)
        if cached and cached[0] == disp:
            return cached[1]
        if disp is None:
            return None
        img = _roi_u16(disp)
        self._validate_ffc(img)
        img = self._process(img)
        if img is None:
            # Rejected frame (mid-frame gain change - see _scanline_health).
            # Everything below assumes a real array: the percentiles, the
            # stretch, the colormap. Without this the paint raised
            # TypeError inside np.percentile and the window stopped updating
            # entirely, which is worse than the artifact it fixed.
            return None
        self._analysis = img   # corrected device units: cursor stats/profile/iso read from here
        # Display stretch. Recomputing the 2/98 percentiles every frame was the
        # remaining source of flicker: the window is 869 frames shown, 0
        # rejected, only 18 held on gain - so nothing is being dropped, yet the
        # picture breathes, because any small change in how much of the frame is
        # warm rescales every pixel in it. Latch the window with a slow
        # exponential average instead, the way a real AGC does, so the mapping
        # changes imperceptibly rather than jumping.
        p_lo = float(np.percentile(img, 2))
        p_hi = float(np.percentile(img, 98))
        # kept so the paint log can separate "the contrast window moved" from
        # "the scene moved". If raw p2/p98 hold still while the displayed
        # brightness swings, the window is breathing; if they track it, the
        # scene really changed and there is nothing to fix.
        self._raw_p = (p_lo, p_hi)
        # The window must not be steerable by a single frame. Measured on this
        # hardware: raw p98 sat steady at 7114 DL while the window's upper bound
        # jumped to 7850 DL and took ~12 s to decay - a 2.8x rescale of the whole
        # picture, which is the brightness pulsing. An EMA with alpha 0.03
        # cannot move that far against a stable input, so the cause is a
        # transient frame (saturated, or a partially-read ROI) whose p98 was
        # enormous; the guard below only rejected spans BELOW MIN_WINDOW, so a
        # huge span passed straight through and the average absorbed 3% of it.
        # MIN_WINDOW catches collapsed windows, not explosive ones.
        self._plo.append(p_lo)
        self._phi.append(p_hi)
        m_lo = float(np.median(self._plo))
        m_hi = float(np.median(self._phi))
        span = m_hi - m_lo
        cur = (self.hi_ema - self.lo_ema) if (self.lo_ema is not None
                                              and self.hi_ema is not None) else 0.0
        # a median over 9 frames cannot be moved by one outlier at all; the
        # ratio guard additionally refuses any step that would more than
        # double or halve the contrast, which is always a transient and never a
        # real change of scene.
        if self.lo_ema is None or self.hi_ema is None:
            # First frame always seeds the window, guard or not - there is
            # nothing to protect yet, and refusing to seed leaves the window
            # undefined.
            self.lo_ema, self.hi_ema = m_lo, m_hi
            if self.hi_ema - self.lo_ema < MIN_WINDOW:
                self.lo_ema -= MIN_WINDOW / 2.0
                self.hi_ema += MIN_WINDOW / 2.0
        elif span < MIN_WINDOW or not (0.5 * cur <= span <= 2.0 * cur):
            self.stretch_skips += 1
        else:
            a = STRETCH_EMA
            self.lo_ema += a * (m_lo - self.lo_ema)
            self.hi_ema += a * (m_hi - self.hi_ema)
        lo, hi = self.lo_ema, self.hi_ema
        t = np.clip((img - lo) / max(1.0, hi - lo), 0, 1)
        if getattr(self, "iso_on", False) and getattr(self, "iso_thr", None) is not None:
            # density slicing: everything above the threshold (in corrected
            # device units, not stretched units) is burned to a HUD cyan
            # band, so the hot region reads as ONE instrument state instead
            # of a subtly-warmer patch of ironbow.
            hot = img >= self.iso_thr
            # mark it on the corrected-value timeline for analysis tests
            self._iso_count = int(hot.sum())
        # Temporal blend, off by default - see BLEND. It reduces screen
        # movement but ghosts on scene changes, which the user saw as
        # artefacts.
        if BLEND < 1.0 and self.prev_t is not None and not self.paused:
            t = BLEND * t + (1.0 - BLEND) * self.prev_t
        self.prev_t = t
        t = self._overlay(t)
        rgb = COLORMAPS[self.lut_i][1](t).astype(np.uint8)
        if getattr(self, "iso_on", False) and getattr(self, "_iso_count", 0) > 0:
            # OVER the colormap: the masked band renders in the HUD cyan so
            # it cuts across every palette identity
            hot = img >= self.iso_thr
            rgb[hot] = np.array((56, 222, 246), np.uint8)
        rgb = self._draw_hotspots(rgb, t)
        # Cache the NUMPY array, never a wx object. This used to build a wxImage,
        # scale it, wrap it in a wx.Bitmap and cache that bitmap across paints.
        # The scale makes a 2x-larger buffer that the Bitmap may reference
        # rather than copy, and the temporary wxImage then goes out of scope -
        # so the cached GDI object was left pointing at released memory. The
        # symptom was the screen alternating between a correct picture and a
        # blank panel once per paint, with no exception anywhere, because
        # nothing in the draw path was wrapped.
        #
        # A numpy array has no such lifetime: rebuilding the wx objects per
        # paint costs about 1.5 ms (measured: Scale 0.89 ms, Bitmap 0.66 ms),
        # which is cheap next to the 70 ms tick.
        self._bmp_cache = (self.frame_raw, rgb)
        return rgb

    def _dump_pngs(self, rgb):
        """Write the diagnostic thrill: burst ring / latest.png.

        BOTH default OFF in the shipped viewer. The user asked to have the
        diagnostics hidden, not removed. The last-frame remember-dump was
        overlooked when the burst ring was silenced - it wrote latest.png
        once a second next to the exe for no product purpose.
        Latest: SEEKNANO_LATEST=1; burst ring: SEEKNANO_BURST=12.
        """
        now = time.time()
        if getattr(self, "_dump_disabled", False):
            return
        try:
            im = _pil_image().fromarray(rgb)
            # Ring of recent displayed frames, overwritten in place. Everything
            # measurable from outside the app has come back "the data is
            # stable", so keep what the DISPLAY actually produced and let it be
            # diffed directly.
            #
            # Throttled to 5 Hz. A PNG encode measured 8.00 ms and this was
            # running once per accepted frame; at 26 fps that is 208 ms of
            # main-thread time per second, and it was throttling the DISPLAY:
            # the stream delivered 26.02 fps but paints only reached 11.33/s.
            # The ring is a diagnostic, not the product, and the screen itself
            # can now be captured directly (tests/screen_capture.py), so 5 Hz
            # is ample.
            if BURST and now - getattr(self, "_last_burst", 0.0) > 0.2:
                self._last_burst = now
                i = int(getattr(self, "_burst_i", 0)) % BURST
                self._burst_i = i + 1
                im.save("burst_%02d.png" % i)
            if SEEKNANO_LATEST and now - getattr(self, "_last_dump", 0) > 1.0:
                self._last_dump = now
                im.save(LATEST_PNG)
        except Exception as e:
            # BOTH throttles must back off. The handler used to advance only
            # _last_dump while the burst branch keyed off _last_burst, so a
            # missing/broken PIL retried every 0.2 s and flooded the log -
            # 5,517 errors in one recorded session.
            self._last_dump = now + 5.0
            self._last_burst = now + 1.0
            fails = getattr(self, "_dump_fails", 0) + 1
            self._dump_fails = fails
            if fails >= 3:
                self._dump_disabled = True
                self.q.put(("log", "frame dumps disabled after %d failures - "
                                   "they are diagnostics; the viewer runs "
                                   "fine without them (last: %r)" % (fails, e)))
            else:
                self.q.put(("log", "frame dump failed: %r (attempt %d)" % (
                    e, fails)))

    def _to_bitmap(self, rgb):
        """numpy RGB -> wx.Bitmap, built fresh every paint.

        Sized to the panel's ACTUAL client size. It used to be
        min(2*self.W, 820), which is 820 px wide regardless of how wide the
        panel really is, so the bitmap never matched the widget it was drawn
        into: it was clipped on one axis and left an undrawn black strip on the
        other. Any resize then moved that black strip, which reads as flicker.
        """
        try:
            w, h = self.video.GetClientSize()
        except Exception:
            w = h = 0
        if w <= 0 or h <= 0:
            w, h = IMG_W, IMG_H
        # Hold the buffer on the instance. wx.Image(w, h, bytes) is not
        # guaranteed to copy what it is handed, and rgb.tobytes() is a
        # temporary that is released the moment this function returns - leaving
        # the image, and anything scaled from it, pointing at freed memory.
        #
        # This is the flat blue frame on screen. Everything measurable said the
        # data was clean: 420 consecutive raw frames contained ZERO that were
        # near-constant after the reference was subtracted (minimum span 279 DL,
        # minimum std 83.5), and the burst ring - written from this very array -
        # showed a normal picture in 12 of 12 slots. So the corruption had to be
        # between rgb and the screen, and it was intermittent and silent, which
        # is the signature of a dangling buffer rather than bad data.
        buf = np.ascontiguousarray(rgb, dtype=np.uint8).tobytes()
        self._rgb_buf = buf
        i2 = wx.Image(IMG_W, IMG_H, buf)
        return wx.Bitmap(i2.Scale(int(w), int(h), wx.IMAGE_QUALITY_NEAREST))

    # _draw_bitmap used to live here. It built the bitmap AFTER the caller had
    # already cleared to black, so a frame that failed to build left the window
    # solid black. on_paint now builds first and only clears when it has
    # something to draw.

    # ---- tick ----
    def tick(self, ev):
        # A --windowed PyInstaller build has no console, so an exception in an
        # event handler is printed to a stderr nobody sees and the handler just
        # stops doing its job. That is how "no stream" can appear with a
        # perfectly healthy stream thread. Everything below is wrapped so a
        # failure lands in the log panel instead of vanishing.
        try:
            self._tick(ev)
        except Exception:
            self._show_fatal("tick", sys.exc_info())

    def _show_fatal(self, where, exc):
        import traceback
        tb = "".join(traceback.format_exception(*exc))
        try:
            self.log.AppendText("!!! %s failed:\n%s" % (where, tb))
            self.push_status("ERROR in %s - see log" % where)
        except Exception:
            pass
        try:
            open("seeknano_crash.log", "a").write(
                "\n=== %s @ %s ===\n%s" % (where, time.time(), tb))
        except Exception:
            pass

    def _tick(self, ev):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "info":
                # status bar only, but ALSO to the file - reading the log file
                # is the only way to see what the app is doing without the user
                # having to describe the screen
                self._info = payload
                if getattr(self, "header", None) is not None:
                    # HUD telemetry readout (e.g. "frame 39 ms")
                    self.header.set_state(self.header.led_text,
                                          self.header.led_col,
                                          fps=payload)
                _vlog("INFO", payload)
                continue
            if kind == "log":
                # Panel AND file. Panel-only is invisible from outside, which
                # is how the flat-reference accept/reject decisions went
                # unreadable for several builds - they are the single most
                # useful thing to know about, and they are exactly what
                # determines whether the picture pulses.
                try:
                    self.log.AppendText(payload if payload.endswith("\n")
                                        else payload + "\n")
                except Exception:
                    pass
                _vlog("LOG", payload)
                continue
            if kind == "frame":
                reason = _frame_reason(payload, self.last_fid)
                if reason is not None:
                    # never hand unvalidated bytes to the renderer
                    self.bad_frames += 1
                    if self.bad_frames in (1, 50, 500):
                        self.log.AppendText(
                            "dropped %d bad frame(s), last: %s\n" % (
                                self.bad_frames, reason))
                    continue
                hdr = _frame_header(payload)
                self.last_fid = hdr[1]
                self.bad_frames = 0
                self.last_gain = hdr[2]
                if getattr(self, "telemetry", None) is not None:
                    self.telemetry.set(fps=(self._info or ""),
                                       gain=str(hdr[2]), seq=str(hdr[1]))
                if self.shown_gain is not None and hdr[2] != self.shown_gain:
                    # The AGC never settles on this hardware. Measured over 20
                    # consecutive kicks the gain word walked
                    #   26 27 28 8 7 6 1 20 3 3 3 6 1 20 3 6 1 20 3 6
                    # and the brightness moved by up to 9000 DL between
                    # neighbours - that is the flicker. A different gain is
                    # also a different exposure and a different noise pattern,
                    # so the background estimate does not apply to it either.
                    #
                    # Keep showing the last frame instead: the picture then only
                    # updates on frames at one exposure, so it is stable and the
                    # background estimate stays valid.
                    self.held_gain += 1
                    self.push_status("holding gain %d (camera wants %d)" % (
                        self.shown_gain, hdr[2]))
                    continue
                if not _gain_locked(hdr[2], self.gain_hist):
                    self.push_status("AGC settling (gain %d)..." % hdr[2])
                    continue
                self.shown_gain = hdr[2]
                # gain changed (or a fresh session): restore the banked
                # reference for THIS gain if one exists - it is then judged
                # on live data by _validate_ffc like any other
                if self.ffc is None or self.ffc_gain != hdr[2]:
                    banked = ffc_bank_load(hdr[2])
                    if banked is not None:
                        self.ffc = banked
                        self.ffc_gain = hdr[2]
                        self.ffc_ok = None   # judged on the next live frame
                        self.flat_btn.Disable()
                        self.q.put(("log", "flat for gain %d restored from the "
                                           "bank" % hdr[2]))
                if not (self.ffc is not None and self.ffc_ok) \
                        and len(self.bg_frames) < BG_FRAMES:
                    # The bank is fed ONLY from frames that already passed the
                    # gain lock, so it can never contain a startup transient.
                    # Filling it before the lock is what broke the picture: it
                    # took gains 4, 9, 14, 25, 26, 27, 28, 8 and then
                    # subtracted a background at a different exposure from the
                    # gain-3 frames on screen. Because the bank fills one frame
                    # per tick, the correction also changed continuously - the
                    # flicker.
                    #
                    # The guard is "no reference is being APPLIED", not "a
                    # reference object exists". It used to be `ffc is None`,
                    # which meant a loaded-but-rejected flat both blocked the
                    # subtraction in _process AND starved this bank of frames,
                    # so there was no fallback to fall back to.
                    self.bg_frames.append(_roi_u16(payload))
                    self.bg_gains.append(hdr[2])
                if len(self.bg_frames) >= BG_FRAMES and self.bg_hp is None:
                    b = np.stack(self.bg_frames).mean(axis=0)
                    self.bg_hp = b - _boxblur(b, 9)   # precomputed once
                if self.frame_raw is not None and not _frame_has_content(payload):
                    # saturated frame: the auto-stretch would paint it solid.
                    # But FIRST: a *uniform burst* is what the shutter click
                    # looks like. The Nano's mechanical shutter (the audible
                    # click) drops a blackbody flag over the sensor for a
                    # second or so - the factory's own FFC moment. If we are
                    # not already capturing a reference, take it for free.
                    self.flat_run = getattr(self, "flat_run", 0) + 1
                    if self.flat_run == 3 and self.ffc_collect is None:
                        self.ffc_collect = []
                        self.q.put(("log", "shutter click (uniform burst) "
                                           "detected - FFC capture (auto)"))
                    if self.ffc_collect is not None:
                        self.ffc_collect.append(_roi_u16(payload))
                        if len(self.ffc_collect) >= FFC_FRAMES:
                            self._finish_flat()
                            self.q.put(("log", "shutter FFC done (auto)"))
                    self.saturated += 1
                    self.push_status("gain %d saturated - holding" % hdr[2])
                    continue
                self.flat_run = 0
                self.shown_frames += 1
                if self.shown_frames == 1:
                    self.push_status("streaming at gain %d" % hdr[2])
                    self.log.AppendText(
                        "streaming: first frame accepted (gain %d, seq %d)\n"
                        % (hdr[2], hdr[1]))
                if not (self.ffc is not None and self.ffc_ok) \
                        and self.shown_frames == 1:
                    # Say it ONCE, on the first frame, and only when no
                    # correction is actually being applied.
                    #
                    # This used to repeat every 15 frames for the whole session,
                    # telling the user to point at a wall. It was wrong on both
                    # counts. The phone app never asks for that, and the
                    # shutterless background measures well enough that asking is
                    # noise: 6.0 DL residual against a 572 DL scene span, 1.05%,
                    # with the scene preserved. And the honest trigger is "no
                    # correction applied", not "ffc is None" - with a stale
                    # ffc_latest.raw on disk the old test was false whenever it
                    # mattered.
                    self.push_status(
                        "streaming - shutterless correction active. "
                        "F on a uniform wall refines it further")
                if self.shown_frames % 50 == 0:
                    # ALSO to the file. Panel-only meant that when frames
                    # stopped being accepted, the counts that explain why
                    # (rejected / held / saturated / which gain) were
                    # unreachable from outside the app and the only visible
                    # symptom was a frozen picture.
                    self.q.put(("log", "%d shown, %d rejected, %d held, "
                                       "%d saturated, gain=%s last_seq=%s"
                                % (self.shown_frames, self.bad_frames,
                                   self.held_gain, self.saturated, hdr[2],
                                   hdr[1])))
                self.frame_raw = payload
                if getattr(self, "rec_f", None) is not None:
                    self.rec_f.write(payload)
                    self.rec_n += 1
                # which frame the screen will be showing after the next paint
                self._paint_seq = hdr[1]
                if self.ffc_collect is not None:
                    try:
                        self.ffc_collect.append(_roi_u16(payload))
                    except Exception:
                        pass
                    if len(self.ffc_collect) >= FFC_FRAMES:
                        self._finish_flat()
                    else:
                        self.push_status("capturing flat %d/%d - hold still..." % (
                            len(self.ffc_collect), FFC_FRAMES))
                if not self.paused:
                    # while paused the picture is frozen on purpose: skip
                    # the refresh and the display rebuild, the pump here
                    # then costs only validation, not the image pipeline.
                    # _display_raw stays pinned to the last pre-pause frame
                    self._display_raw = payload
                    self.video.Refresh()
                    # Build the display array HERE, on the timer, not in the
                    # paint handler. on_paint used to run the whole pipeline
                    # whenever the cache missed, so its cost alternated
                    # between ~5 ms (miss) and ~1.5 ms (hit) from one paint
                    # to the next; flicker tracked that alternation. Building
                    # here means the paint handler only ever converts and
                    # blits, at a constant cost.
                    try:
                        rgb = self._bmp()
                    except Exception:
                        self._show_fatal("bmp build", sys.exc_info())
                        rgb = None
                    if rgb is not None:
                        self._dump_pngs(rgb)
            elif kind == "status":
                self.push_status(payload)
            elif kind == "error":
                self.log.AppendText("ERROR: %s\n" % payload)
                # A USB timeout is transient, not fatal. Treating it as fatal
                # stops the stream and leaves the window showing a frozen last
                # frame under an ERROR banner, which reads as a crash and needs
                # the user to press Start again. Retry a bounded number of times
                # with a pause, so a momentary stall is ridden out instead of
                # ending the session.
                if _is_usb_timeout(payload):
                    n = getattr(self, "_usb_retries", 0) + 1
                    self._usb_retries = n
                    if n <= USB_RETRY_MAX:
                        self.push_status("USB timeout - reconnecting (%d/%d)"
                                         % (n, USB_RETRY_MAX))
                        _vlog("LOG", "USB timeout, reconnect %d/%d in %.1fs"
                              % (n, USB_RETRY_MAX,
                                 USB_RETRY_DELAY_MS / 1000.0))
                        self.stream_thread = None
                        self.start_btn.SetGlyph("clear")
                        self.start_btn.SetLabel("Reconnecting...")
                        # The static "start / stop (S)" tooltip would contradict
                        # an X glyph that is not clickable while the reconnect
                        # timer owns the camera.
                        self.start_btn.SetToolTip(
                            "reconnecting - waiting for the Nano")
                        wx.CallLater(int(USB_RETRY_DELAY_MS),
                                     self._usb_reconnect)
                        return
                    self.push_status(
                        "USB timed out %d times - unplug the Nano and replug it."
                        % USB_RETRY_MAX)
                self._usb_retries = 0
                self.push_status("ERROR: " + payload[:80])
                self.stream_thread = None
                self.start_btn.SetGlyph("play")
                self.start_btn.SetLabel("Start stream")
                self.start_btn.SetToolTip("start / stop  (S)")
            elif kind == "log":
                self.log.AppendText("%s\n" % payload)
                if self.log.GetLastPosition() > 4000:
                    self.log.Remove(0, 2000)
        if self.frame_raw and not self.paused:
            self.video.Refresh()

    # ---- recording / replay / CSV -------------------------------------------

    def on_record(self, ev):
        """R: start/stop raw recording into rec_<ts>.snrec.

        The record is the FRAMES AS DELIVERED (177 840 B each, concatenated)
        - replay can therefore be bit-exact, scrubbed and diffed offline
        without the camera attached.
        """
        if getattr(self, "rec_f", None) is not None:
            self.rec_f.close()
            self.rec_f = None
            self.q.put(("log", "recording stopped - %s (%d frames)"
                               % (self.rec_name, self.rec_n)))
            self.push_status("record saved: " + self.rec_name)
            return
        if not self.frame_raw:
            self.push_status("start the stream, then press R to record")
            return
        self.rec_name = "rec_%d.snrec" % int(time.time())
        self.rec_f = open(self.rec_name, "wb")
        self.rec_n = 0
        self.q.put(("log", "recording -> %s (press R again to stop)"
                           % self.rec_name))
        self.push_status("recording...")

    def _export_csv(self, ev):
        """X: corrected values of the frame on screen -> CSV.

        Machine-readable export of the analysis layer, one row per pixel
        (index, x, y, corrected DL, and the anchored °C when set)."""
        ana = getattr(self, "_analysis", None)
        if ana is None:
            self.push_status("nothing to export - no frame on screen")
            return
        name = "frame_%d.csv" % int(time.time())
        ih, iw = ana.shape
        ancf = getattr(self, "_anchors", None)
        with open(name, "w") as fh:
            fh.write("index,x,y,DL,degC\n")
            for y_ in range(ih):
                for x_ in range(iw):
                    dl = float(ana[y_, x_])
                    c = dl_to_c(dl, ancf)
                    fh.write("%d,%d,%d,%.1f,%s\n" % (
                        y_ * iw + x_, x_, y_, dl,
                        "" if c is None else "%.2f" % c))
        self.q.put(("log", "CSV written: %s (%d rows)" % (name, ih * iw)))
        self.push_status("exported " + name)


    def on_motion_canvas(self, ev):
        self.mouse = ev.GetPosition()
        if getattr(self, "_dragging", False):
            self.sel_box = (self.sel_box[0], self.mouse)
        elif getattr(self, "_lining", False) and ev.ShiftDown():
            self.sel_line = (self.sel_line[0], self.mouse)
        self.video.Refresh()

    def on_down_canvas(self, ev):
        pos = ev.GetPosition()
        if ev.ShiftDown():
            self._lining = True
            self.sel_line = (pos, pos)
        else:
            self._dragging = True
            self.sel_box = (pos, pos)
        self.video.CaptureMouse()

    def on_up_canvas(self, ev):
        try:
            if self.video.HasCapture():
                self.video.ReleaseMouse()
        except Exception:
            pass
        self._dragging = False
        self._lining = False
        self.video.Refresh()

    def on_leave_canvas(self, ev):
        self.mouse = None
        self.video.Refresh()

    # ---- rail field ------------------------------------------------------
    def on_rail_paint(self, ev):
        """Rail field: deep bg + 1px hairline separating it from the canvas."""
        w, h = self.rail.GetClientSize()
        dc = wx.AutoBufferedPaintDC(self.rail)
        dc.SetBackground(wx.Brush(wx.Colour(*THEME["bg"])))
        dc.Clear()
        dc.SetPen(wx.Pen(wx.Colour(*THEME["line"])))
        dc.DrawLine(0, 0, 0, h)

    def on_close(self, ev):
        if self.stream_thread:
            self.stream_thread.stop_flag.set()
        ev.Skip()

    def on_hotkey(self, ev):
        """Dispatch for the frame accelerator table set up in __init__.

        This used to be an empty stub, so even a correctly-fired accelerator
        would have done nothing.
        """
        name = getattr(self, "_accel_names", {}).get(ev.GetId())
        if name:
            # Wrapped for the same reason as _hotkey: in a --windowed build an
            # exception in an event handler goes to a stderr nobody sees, so
            # "p does nothing" was the only symptom.
            try:
                self._hotkey_run(ev, name)
            except Exception:
                self._show_fatal("hotkey %r" % name, sys.exc_info())
        ev.Skip()

    def _hotkey(self, ev, name):
        # Wrapped: an exception in a key handler in a --windowed build goes to
        # a stderr nobody sees, so "p does nothing" was the only symptom. This
        # is how on_snapshot's failure stayed invisible for three builds.
        try:
            self._hotkey_run(ev, name)
        except Exception:
            self._show_fatal("hotkey %r" % name, sys.exc_info())
        ev.Skip()

    def _hotkey_run(self, ev, name):
        """Dispatch one hotkey.

        Reached two ways, and they do NOT carry the same event:

          * the frame accelerator table emits a wx.CommandEvent, which has no
            GetKeyCode - the action is already known from the table entry
          * the CHAR_HOOK fallback emits a wx.KeyEvent, which does

        Calling GetKeyCode() unconditionally threw AttributeError on every
        accelerator press, i.e. every hotkey silently did nothing and wrote a
        traceback to seeknano_crash.log. So take the key from the event when it
        has one, and otherwise from the accelerator entry's name.
        """
        ch = ""
        escape = space = False
        if hasattr(ev, "GetKeyCode"):
            k = ev.GetKeyCode()
            ch = chr(k) if 0 < k < 256 else ""
            escape = k == wx.WXK_ESCAPE
            space = k == wx.WXK_SPACE
        else:
            ch = ACCEL_KEY.get(name, "")
        key = ch.lower() if ch else ""
        if key == "s":
            self.on_toggle(None)
        elif key == "r":
            self.on_record(None)
        elif key == "x":
            self._export_csv(None)
        elif key == "t":
            self.on_anchor(None)
        elif key == "c":
            self.on_cmap(None)
        elif key == "p":
            self.on_snapshot(None)
        elif key == "d":
            self.on_raw(None)
        elif key == "f":
            self.on_capture_flat(None)
        elif key == "q" or escape:
            self.Close()
        elif ch == " " or space or name == "Pause":
            self.paused = not self.paused
            if not self.paused:
                self._display_raw = self.frame_raw
            self.push_status("paused" if self.paused else "running")

    def on_anchor(self, ev):
        """t: two-point anchor - aim the cursor at a surface you know, type
        the \\u00b0C value (Seek publish no transfer function: the factory
        baseline is \\u00b15\\u00b0C / 5 % on their pipelines alone)."""
        if getattr(self, "mouse", None) is None:
            self.push_status("hover the canvas first, then press t")
            return
        dlg = wx.TextEntryDialog(
            self, "anchor temperature (\\u00b0C) at the cursor:",
            "Set anchor point")
        if dlg.ShowModal() == wx.ID_OK:
            try:
                self._anchors = list(getattr(self, "_anchors", []))
                st = _sample_stats(
                    getattr(self, "_analysis") or np.zeros((1, 1), np.float32),
                    self._img_from_mouse(self.mouse))
                if st is None:
                    raise ValueError("no frame under the cursor")
                self._anchors.append(
                    (float(st[0]), float(dlg.GetValue().replace(",", "."))))
                self._anchors = self._anchors[-2:]
                ok = anchors_save(self._anchors)
                self.push_status("anchor set: %d anchors%s" % (
                    len(self._anchors),
                    " (saved)" if ok else " (NOT saved - read-only dir)"))
                if getattr(self, "telemetry", None) is not None:
                    self.telemetry.state_anchored = True
            except Exception as e:
                self.push_status("anchor failed: %r" % e)
        dlg.Destroy()

    def on_key(self, ev):
        """Hotkeys documented in app/README.md: s c p d f q (and space).
        Hidden surfaces: Ctrl+Alt+D shows the dev upload row, Ctrl+L the log."""
        k = ev.GetKeyCode()
        try:
            c = chr(k).lower()
        except ValueError:
            c = ""
        ctrl = ev.ControlDown()
        alt = ev.AltDown()
        if ctrl and alt and c == "d":
            self._dev_shown = not getattr(self, "_dev_shown", False)
            self.dev_sizer.ShowItems(self._dev_shown)
            self.Layout()
            ev.Skip()
            return
        if ctrl and c == "l":
            self._log_shown = not getattr(self, "_log_shown", False)
            self.log.Show(self._log_shown)
            if self._log_shown:
                self.log.SetMinSize((-1, 170))
            else:
                self.log.SetMinSize((-1, 0))
            self.rail.Layout()
            ev.Skip()
            return
        if c in "[]":
            # isotherm threshold step (auto-seeds from the current frame p90)
            ana = getattr(self, "_analysis", None)
            base = float(np.percentile(ana, 90)) if ana is not None else 0.0
            step = max(2.0, 0.02 * (getattr(self, "hi_ema", 0.0) or base))
            self.iso_thr = (self.iso_thr if self.iso_thr is not None
                            else base) + (step if c == "]" else -step)
            self.iso_on = True
            self.push_status("isotherm on \u2265 %.0f" % self.iso_thr)
            self.video.Refresh()
            ev.Skip()
            return
        if c == "t":
            self.on_anchor(None)
            ev.Skip()
            return
        if c in "scpodf" or k in (wx.WXK_ESCAPE, wx.WXK_SPACE):
            if c == "s":
                self.on_toggle(None)
            elif c == "c":
                self.on_cmap(None)
            elif c == "p":
                self.on_snapshot(None)
            elif c == "d":
                self.on_raw(None)
            elif c == "f":
                self.on_capture_flat(None)
            elif c == "o":
                # diagnostic stamp: session state burned into the picture
                self.stamp = not getattr(self, "stamp", False)
                self.push_status("diagnostic stamp " +
                                 ("on" if self.stamp else "off"))
            elif c == "i":
                # isotherm: burn everything above the p90 of the current
                # frame in HUD cyan; [ / ] move the threshold
                if not self.iso_on:
                    ana = getattr(self, "_analysis", None)
                    self.iso_thr = (float(np.percentile(ana, 90))
                                    if ana is not None else None)
                    self.iso_on = True
                else:
                    self.iso_on = False
                self.push_status("isotherm " +
                                 ("on (thr %s)" % (
                                     "%d" % self.iso_thr if self.iso_thr
                                     is not None else "-")
                                  if self.iso_on else "off"))
                self.video.Refresh()
            elif c == "q" or k == wx.WXK_ESCAPE:
                self.Close()
            elif c == " ":
                self.paused = not self.paused
                if not self.paused:
                    self._display_raw = self.frame_raw
                self.push_status("paused" if self.paused else "running")
            ev.Skip()
            return
        ev.Skip()



def serve_mode(port=5005):
    """Headless camera ferry: streams every raw frame as UDP packets to the LAN.
    Usage:  SeekNano.exe --serve [port]
    Sends:  header: magic 'SNFR', frame seq (u32), offset (u32), total (u32), Frags (u16)
            payload: raw 177840 bytes, chunked into 1400-byte pieces.
    Also dumps every control transfer to seeknano_verbose.log."""
    import socket, struct
    import usb.core, usb.util

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)

    target = "255.255.255.255"  # broadcast on the LAN; server sniffs it

    # find the device the same way the viewer does (usb / libusb_package)
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except Exception:
        backend = None
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        print("camera not found")
        return
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    print("camera open, streaming to UDP broadcast")

    def ctrl_out(request, payload, retry=True):
        try:
            dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)
        except usb.core.USBError as e:
            # EPIPE (stalled control pipe) or busy device: reset once and retry
            if retry and (getattr(e, "errno", None) == 32 or "Pipe" in str(e)):
                print(f"pipe stall on ctrl req 0x{request:02x}: {e}; resetting camera")
                try:
                    dev.reset()
                    time.sleep(1)
                    dev.set_configuration()
                    usb.util.claim_interface(dev, 0)
                    dev.ctrl_transfer(REQ_OUT, request, 0, 0, payload, 1250)
                    return
                except Exception as e2:
                    print(f"reset failed: {e2}. unplug and replug the camera, then rerun.")
                    sys.exit(3)
            raise
    def ctrl_in(request, length):
        return bytes(dev.ctrl_transfer(REQ_IN, request, 0, 0, length, 1250))

    # exact 483-op replay of the phone session
    ctrl_out(0x54, b"\x00\x00")
    ctrl_out(0x3C, b"\x00\x00")
    ctrl_in(0x3D, 2)
    ctrl_out(0x3E, b"\x08\x00")
    # identity
    ctrl_out(0x56, bytes([0x08, 0x00, 0x02, 0x06, 0x00, 0x00]))
    ctrl_in(0x58, 16)
    ctrl_in(0x4E, 4)
    ctrl_out(0x55, b"\x17\x00")
    ctrl_in(0x4E, 64)
    ctrl_in(0x4E, 4)
    ctrl_in(0x36, 12)
    ctrl_out(0x55, b"\x15\x00")
    ctrl_in(0x4E, 64)
    ctrl_in(0x4E, 4)
    ctrl_in(0x36, 12)
    for page in range(11):
        addr = page * 64
        ctrl_out(0x56, bytes([0x20, 0x00, (addr >> 8) & 0xFF, addr & 0xFF, 0, 0]))
        ctrl_in(0x58, 64)
    ctrl_out(0x55, b"\x15\x00")
    ctrl_in(0x4E, 64)
    ctrl_in(0x35, 4)
    ctrl_out(0x37, b"\xfc\x00\x04\x00")
    ctrl_out(0x3C, b"\x01\x00")
    ctrl_in(0x3D, 2)
    print("handshake complete, streaming raw frames over UDP")

    seq = 1
    FrmPer = 1342  # 177840 / 1400 pieces
    while True:
        ctrl_out(0x53, b"\x58\x5b\x01\x00")
        buf = bytearray()
        while len(buf) < FRAME_BYTES:
            chunk = dev.read(BULK_EP, CHUNK, 400)
            buf.extend(chunk)
        if len(buf) != FRAME_BYTES:
            continue
        frame = bytes(buf)
        # 11 fragments of payload 1400 B each + last remainder
        offs = 0
        while offs < FRAME_BYTES:
            chunk = frame[offs:offs + 1400]
            pkt = b"SNFR" + struct.pack("<II I", seq, offs, FRAME_BYTES) + chunk
            sock.sendto(pkt, (target, port))
            offs += len(chunk)
        seq += 1
        print(f"frame {seq} sent", end="\r")

def _crashlog(fn, *a):
    try:
        fn(*a)
    except Exception:
        import traceback, sys as _s
        with open("crash.txt", "a") as f:
            traceback.print_exc(file=f)
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "crash.txt written next to the exe", "SeekNano", 0x10)


def parse_target(s, default_port=8100):
    "192.168.50.200[:8100] -> (ip, port)"
    if ":" in s:
        host, port = s.rsplit(":", 1)
        return host, int(port)
    return s, default_port


# ---------------------------------------------------------------------------
# --- DEV: headless frame collector, replaces loop.ps1 entirely --------------
def dev_serve(target_url="192.168.50.200:8100", frames_per_batch=3):
    """Dev headless capture: stream raw frames to a collector over HTTP PUT.

    Usage:  SeekNano.exe --dev-serve [host[:port]]

    Uses the native WinUSB transport dll so the run does not depend on libusb
    being able to open the composite *parent* node (pyusb fails with
    "[Errno 13] Access denied" there, because usbccgp owns the parent).
    Frames are PUT to http://<host>:<port>/frame_<seq>.raw.
    Log: devserve.log next to the exe (printf-safe, windowed build).
    """
    import ctypes
    _box = ctypes.windll.user32.MessageBoxW
    try:
        logf = open("devserve.log", "a", buffering=1)
    except Exception:
        # a read-only cwd must not kill startup in a windowed build
        # (the traceback would land on a stderr nobody sees)
        logf = open(os.devnull, "w")

    def say(msg, fatal=False):
        print(msg)
        # one logged line per message (the old say() wrote non-fatal messages
        # twice: once unconditionally, once more inside the try)
        try:
            logf.write("%.3f %s\n" % (time.time(), msg))
        except Exception:
            pass
        if fatal:
            _box(None, msg, "SeekNano dev", 0x10)

    host, port = parse_target(target_url)
    uploader = DevUploader(host, port)
    say("dev-serve target: %s:%d" % (host, port))

    # ---- native transport: WinUSB API on the MI_00 child (live stream) ----
    # prefer seeknanousb.dll (pure WinUSB API, GUID {1C2BD42A...} = MI_00,
    # bulk ep 0x81): that is the transport that captured 177840-byte frames
    # directly on this hardware.  seeknanodirect.dll (libusb) enumerates the
    # composite and lands on the *silent* MI_01 half (stream_start rc=-30).
    name = pfx = d = None
    mei = sys._MEIPASS if getattr(sys, "frozen", False) and \
        hasattr(sys, "_MEIPASS") else None
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if \
        getattr(sys, "frozen", False) else None
    for path, pfx_ in dll_candidates(mei, exe_dir,
                                     os.path.dirname(os.path.abspath(__file__))):
        try:
            d_ = _ct.CDLL(path)
            getattr(d_, pfx_ + "open").restype = _ct.c_int
            getattr(d_, pfx_ + "stream_start").restype = _ct.c_int
            getattr(d_, pfx_ + "get_frame").restype = _ct.c_int
            getattr(d_, pfx_ + "get_frame").argtypes = [_ct.c_char_p]
            getattr(d_, pfx_ + "stream_stop").restype = _ct.c_int
            getattr(d_, pfx_ + "close").restype = None
            name, pfx, d = os.path.basename(path), pfx_, d_
            break
        except Exception:
            continue
    if name is None:
        say("dev: no native transport dll found next to SeekNano.exe ({})\n"
            "Download SeekNano-devbundle.zip from the release and extract "
            "it next to the exe (seeknanousb.dll + seeknanodirect.dll)."
            .format(exe_dir or os.path.dirname(os.path.abspath(__file__))), True)
        return
    say("dev transport: " + name)

    if getattr(d, pfx + "open")() != 0:
        say("dev: native open failed.\n\n"
            "Make sure both child interfaces are bound to WinUSB "
            "(run bind.ps1 once, or SeekNanoDriverInstaller).\n\n"
            "Details: devserve.log, bindinfo.txt.", True)
        try:
            open("bindinfo.txt", "w", encoding="utf-8").write(dev_bindinfo())
        except Exception:
            pass
        return

    rc = getattr(d, pfx + "stream_start")()
    say("stream_start rc=%d" % rc)

    buf = ctypes.create_string_buffer(FRAME_BYTES)
    seq = 0
    batch = 0
    fails_total = 0
    last_ok = time.time()
    get_frame = getattr(d, pfx + "get_frame")
    stream_stop = getattr(d, pfx + "stream_stop")
    say("streaming; frames PUT to %s:%d" % (host, port))
    while True:
        n = get_frame(buf)
        if n != FRAME_BYTES:
            fails_total += 1
            say("frame attempt got %s bytes (fails=%d)" % (n, fails_total))
            if fails_total > 12:
                say("dev: native pump stalled - rerun, or unplug/replug.")
                stream_stop()
                return
            time.sleep(0.2)
            continue
        seq += 1
        batch += 1
        ok, _ = uploader.put_frame(bytes(buf.raw))
        dt = (time.time() - last_ok) * 1000.0
        say("frame %d ok  %4.0f ms  upload=%s" %
            (seq, dt, "ok" if ok else "FAILED"))
        fails_total = 0
        last_ok = time.time()
        if batch >= frames_per_batch:
            batch = 0
            time.sleep(0.5)


def dev_bind():
    """Print the PnP binding state (replaces diag.cmd binds)."""
    print(dev_bindinfo())
    open("bindinfo.txt", "w", encoding="utf-8").write(dev_bindinfo())
    print("saved bindinfo.txt")


def selftest():
    results = []
    for modstr in ("wx", "numpy", "queue", "PIL"):
        try:
            __import__(modstr)
            results.append((modstr, "ok"))
        except Exception as e:
            results.append((modstr, "FAIL " + repr(e)))
    for dname in ("seeknanodirect.dll", "seeknanousb.dll"):
        try:
            ctypes.CDLL(dname)
            results.append((dname, "ok"))
        except Exception as e:
            results.append((dname, "missing (install SeekNanoDriverInstaller first)"))
    try:
        import libusb_package
        libusb_package.get_libusb1_backend()
        results.append(("libusb-1.0 backend", "ok"))
    except Exception as e:
        results.append(("libusb-1.0 backend", "FAIL " + repr(e)))
    print("\n".join("%-22s %s" % (a, b) for a, b in results))
    # exit 1 if anything critical failed
    crit = [r for r in results if r[0] in ("wx", "numpy")]
    for r in crit:
        if "FAIL" in r[1]:
            sys.exit(1)
    sys.exit(0)

def _run_gui(autostart=False):    # Hang diagnostics. A GUI that stops painting looks identical to one that is
    # merely showing a static scene - the title bar says "Not Responding", the
    # image is frozen, and the reader thread keeps logging frames, so from the
    # outside there is nothing to tell a hang from a quiet camera. Dumping every
    # thread's stack on a timer answers it directly.
    #
    # Off unless SEEKNANO_WATCHDOG is set, so it costs nothing in normal use.
    _wd = os.environ.get("SEEKNANO_WATCHDOG", "")
    if _wd:
        import faulthandler
        _secs = float(_wd) if _wd.replace(".", "").isdigit() else 20.0
        _fh = open(WATCHDOG_LOG, "a", buffering=1)
        _fh.write("=== watchdog every %.0fs ===\n" % _secs)
        faulthandler.enable(file=_fh)
        faulthandler.dump_traceback_later(_secs, repeat=True, file=_fh)
    app = wx.App(False)
    v = Viewer()
    v.Show(True)
    if autostart:
        # Start pumping without a click. The device is ready by the time the
        # window maps, and this is what makes latest.png appear on its own.
        wx.CallAfter(v.on_toggle, None)
    app.MainLoop()


def dump_mode(outdir="dump", count=40):
    """Headless: stream and write what the viewer would DISPLAY, no GUI.

    The GUI hides the pipeline behind a window, so this runs the same
    _roi_u16 -> background -> median -> NUC -> stretch -> colormap steps and
    saves the result. This is how we check on the real camera whether the
    correction is engaging, which is not observable from a screenshot.
    """
    os.makedirs(outdir, exist_ok=True)
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_BACKEND)
    if dev is None:
        print("camera not found")
        return 1
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    ctrl = lambda r, p: dev.ctrl_transfer(REQ_OUT, r, 0, 0, p, 1250)
    rd = lambda r, n: bytes(dev.ctrl_transfer(REQ_IN, r, 0, 0, n, 1250))
    ctrl(0x54, b"\x00\x00"); ctrl(0x3c, b"\x00\x00"); rd(0x3d, 2)
    ctrl(0x3e, b"\x08\x00")
    ctrl(0x37, b"\xfc\x00\x04\x00"); ctrl(0x3c, b"\x01\x00")
    print("streaming state:", rd(0x3d, 2).hex())

    bg, gains, hist, shown = [], [], [], None
    shown_n, held, rejected, saturated = 0, 0, 0, 0
    lut = COLORMAPS[0][1]
    for _i in range(count):
        ctrl(0x53, b"\x58\x5b\x01\x00")
        buf = bytearray()
        try:
            while len(buf) < FRAME_BYTES:
                buf += bytes(dev.read(BULK_EP, min(CHUNK, FRAME_BYTES - len(buf)), 500))
        except Exception:
            pass
        f = _resync_frame(buf)
        if f is None or _frame_reason(f) is not None:
            rejected += 1
            why = ("no resync (%d B read)" % len(buf)) if f is None else _frame_reason(f)
            if rejected <= 5:
                hdr = np.frombuffer(buf[:8], "<u2") if len(buf) >= 8 else None
                print("  reject %d: %s  first8=%s" % (
                    rejected, why,
                    hdr.tolist() if hdr is not None else "n/a"))
            continue
        _m, _s, g = _frame_header(f)
        if shown is not None and g != shown:
            held += 1
            continue
        if not _gain_locked(g, hist):
            continue
        shown = g
        if len(bg) < BG_FRAMES:
            bg.append(_roi_u16(f))
            gains.append(g)
        if shown_n and not _frame_has_content(f):
            saturated += 1
            continue
        shown_n += 1
        img = _roi_u16(f)
        if bg and len(set(gains)) == 1:
            b = np.stack(bg).mean(axis=0)
            img = img - (b - _boxblur(b, 9))
        p1 = np.pad(img, 1, mode="edge")
        img = np.partition(
            np.stack([p1[dy:dy + IMG_H, dx:dx + IMG_W]
                      for dy in range(3) for dx in range(3)]), 4, axis=0)[4]
        img = _nuc2d(img)
        if DISPLAY_SMOOTH >= 3:
            img = _boxblur(img, DISPLAY_SMOOTH)
        lo = float(np.percentile(img, 2))
        hi = float(np.percentile(img, 98))
        t = np.clip((img - lo) / max(1.0, hi - lo), 0, 1)
        try:
            Image = _pil_image()
            Image.fromarray(lut(t).astype(np.uint8)).save(
                "%s/frame_%03d.png" % (outdir, shown_n))
        except Exception as e:
            print("save failed:", e)
        if shown_n <= 3 or shown_n % 10 == 0:
            print("  %3d gain=%-3d bg=%d span=%.0f dx=%.1f std=%.0f" % (
                shown_n, g, len(bg), hi - lo,
                float(np.abs(np.diff(img, axis=1)).mean()), float(img.std())))
    usb.util.dispose_resources(dev)
    print("shown %d, held %d, rejected %d, saturated %d, bg=%d gains=%s" % (
        shown_n, held, rejected, saturated, len(bg), sorted(set(gains))))
    print("wrote", outdir)
    return 0

def _run_replay(path):
    """Play a recorded capture through the full analysis layer, no camera."""
    app = wx.App(False)
    v = Viewer()
    v.replay_path = path
    v.Show(True)
    wx.CallAfter(v.on_toggle, None)
    app.MainLoop()


if __name__ == "__main__":
    if "--replay" in sys.argv:
        i = sys.argv.index("--replay")
        path = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        _crashlog(_run_replay, path)
    elif "--selftest" in sys.argv:
        selftest()
    elif "--check-pil" in sys.argv:
        # CI harness: prove the bundled PIL is real. Allocates an image,
        # writes a pixel, reads it back - a missing/broken bundle fails the
        # build here instead of reaching a user (who would only see
        # "saved nothing" and a log flood).
        try:
            Image = _pil_image()
            a = np.zeros((4, 4, 3), dtype=np.uint8)
            a[1, 1] = [1, 2, 3]
            back = np.array(Image.fromarray(a))
            if back[1, 1].tolist() != [1, 2, 3]:
                raise RuntimeError("pixel round-trip mismatch")
        except Exception as e:
            print("check-pil FAIL: %r" % e)
            sys.exit(1)
        print("check-pil ok")
    elif "--check-assets" in sys.argv:
        # assets/icon_256.png is read via sys._MEIPASS and hotspot.py is imported
        # from the bundle. Neither is reachable at module scope, so nothing else
        # proves they were packaged: a local build that omitted --add-data made
        # an exe that failed at runtime with wx logging
        #     Failed to load image from file ..._MEIxxxx\assets\icon_256.png
        # while the try/except around the load swallowed it and carried on with
        # no emblem. CI always passed --add-data; only a hand-rolled build missed
        # it. So assert the bundle, the same way check_pil asserts Pillow.
        import importlib.util
        want = ["assets/icon_256.png", "hotspot.py"]
        base = (sys._MEIPASS if getattr(sys, "frozen", False)
                and hasattr(sys, "_MEIPASS")
                else os.path.dirname(os.path.abspath(__file__)))
        bad = []
        for rel in want:
            p = os.path.join(base, *rel.split("/"))
            if not os.path.isfile(p):
                bad.append("%s (looked in %s)" % (rel, base))
        if getattr(sys, "frozen", False) \
                and importlib.util.find_spec("hotspot") is None:
            bad.append("hotspot is not importable from the bundle")
        if bad:
            print("check-assets FAIL: " + "; ".join(bad))
            sys.exit(1)
        print("check-assets ok: " + ", ".join(
            "%s (%d B)" % (r, os.path.getsize(
                os.path.join(base, *r.split("/")))) for r in want))
        sys.exit(0)
    elif "--dump-processed" in sys.argv:
        # headless: write what the window would draw, to confirm the pipeline
        i = sys.argv.index("--dump-processed") + 1
        a = sys.argv[i] if i < len(sys.argv) else "dump"
        n = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 40
        sys.exit(dump_mode(a, n))
    elif "--dev-serve" in sys.argv:
        # dev: python viewer.py --dev-serve 192.168.50.200:8100 [frames_per_batch]
        target = "192.168.50.200:8100"
        fps = 3
        argv = sys.argv[sys.argv.index("--dev-serve") + 1:]
        if argv:
            target = argv[0]
        if len(argv) > 1 and argv[1].isdigit():
            fps = int(argv[1])
        _crashlog(dev_serve, target, fps)
    elif "--dev-bind" in sys.argv:
        dev_bind()
    elif "--serve" in sys.argv:
        port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 5005
        _crashlog(serve_mode, port)
    else:
        # --stream starts streaming on launch. Worth having in its own right:
        # the hotkey path depends on key delivery, which has proved unreliable
        # to trigger from outside the app, and a viewer you have to coax into
        # starting is hard to measure against.
        _crashlog(_run_gui, "--stream" in sys.argv)

