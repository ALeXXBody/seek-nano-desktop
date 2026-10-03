"""Replay the vendor app's own init sequence and measure what it changes.

What the reverse engineering turned up
--------------------------------------
The Android SDK's control traffic (Frida trace, history/artifacts) is not just a
frame kick. It is a CONFIGURATION PHASE this project never performs:

    0x54, 0x3c=0000, 0x3d read, 0x3e=0800      <- the same init this app does
    0x56 / 0x58 x 80      set register address, read value back
                           -> "CQ-DBAX", "Jul 16 2...", "Compact", serial
    0x35 read x 44
    0x37 = fc 00 04 00     <- the start command, which this app never sends
    0x3c = 0100
    0x53 kick, then bulk 0x81

This app does the init, then goes straight to the 0x53 kick. It never sends 0x37
and never runs the 0x56/0x58 phase.

The symbol table explains why that matters: the SDK exposes a DSP image pipeline
this app knows nothing about -

    seekcamera_set_agc_mode / get_agc_mode
    seekcamera_set_linear_agc_lock_mode / _min / _max
    seekcamera_set_histeq_agc_gain_limit / _roi_* / _plateau / _trim_*
    seekcamera_set_pipeline_mode / get_pipeline_mode
    seekcamera_set_filter_state

A histogram-equalisation AGC with a gain lock and an ROI is the camera doing its
own normalisation, with no flat reference anywhere. If the DSP is left in a
default state because the config phase never runs, the raw fixed pattern would
be exactly what this app has been fighting with software filters - which is
consistent with the 65 DL of pattern that no spatial filter could remove.

What this measures
------------------
Frames captured the way the app does it now, then frames captured after replaying
the vendor's own sequence verbatim. Same scene, same stretch, same colormap, so
the two renders are directly comparable - and the fixed-pattern figure says
whether the camera's pipeline did the work.

The sequence replayed is the vendor's, unchanged. That matters: an earlier probe
that invented payloads for unknown requests wedged the camera and needed a
physical replug. Replaying a known-good sequence is a different risk class.
"""
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, r"C:\a\src\app")
os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

import viewer as V

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
TOTAL = 177840


def open_cam():
    d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
    if d is None:
        raise SystemExit("camera not found - close SeekNano first")
    d.set_configuration()
    usb.util.claim_interface(d, 0)
    return d


def wr(d, req, payload, t=800):
    return d.ctrl_transfer(0x40, req, 0, 0, payload, t)


def rd(d, req, n, t=800):
    return bytes(d.ctrl_transfer(0xC0, req, 0, 0, n, t))


def basic_init(d):
    """What the app already does."""
    wr(d, 0x54, b"\x00\x00")
    wr(d, 0x3C, b"\x00\x00")
    rd(d, 0x3D, 2)
    wr(d, 0x3E, b"\x08\x00")
    wr(d, 0x3C, b"\x01\x00")
    rd(d, 0x3D, 2)


def vendor_phase(d):
    """The vendor's configuration phase, verbatim from the trace."""
    print("  replaying the vendor config phase (0x56/0x58 x80, 0x35 x44, 0x37)")
    reads = []
    for hi in (0x0000, 0x2000, 0x4000, 0x6000, 0x8000, 0xA000, 0xC000, 0xE000):
        for lo in range(10):
            payload = struct_pack(hi, lo)
            try:
                wr(d, 0x56, payload, 400)
                v = rd(d, 0x58, 24, 400)
            except Exception:
                continue
            if any(v):
                reads.append((hi, lo, v))
    print("    %d of 80 registers returned non-zero" % len(reads))
    for hi, lo, v in reads[:12]:
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in v)
        print("      %04x/%02x  %s" % (hi, lo, txt[:24]))
    n35 = 0
    for _ in range(44):
        try:
            rd(d, 0x35, 4, 300)
            n35 += 1
        except Exception:
            break
    print("    0x35 reads: %d" % n35)
    try:
        wr(d, 0x55, b"\x15\x00", 400)
        wr(d, 0x55, b"\x17\x00", 400)
    except Exception as e:
        print("    0x55 writes failed:", e)
    try:
        wr(d, 0x37, b"\xfc\x00\x04\x00", 800)
        print("    sent 0x37 = fc 00 04 00  <- the command this app never sends")
    except Exception as e:
        print("    0x37 failed:", e)
    try:
        wr(d, 0x3C, b"\x01\x00", 400)
    except Exception:
        pass


def struct_pack(hi, lo):
    return b"\x20\x00" + hi.to_bytes(2, "little") + lo.to_bytes(2, "little")


def grab(d, n):
    out = []
    for _ in range(n):
        wr(d, 0x53, b"\x58\x5b\x01\x00")
        b = bytearray()
        try:
            while len(b) < TOTAL:
                b += bytes(d.read(0x81, min(6840, TOTAL - len(b)), timeout=900))
        except Exception:
            continue
        if len(b) != TOTAL:
            continue
        h = np.frombuffer(b[:8], "<u2")
        if int(h[0]) != 0x0579:
            continue
        a = np.frombuffer(b, dtype="<u2").reshape(RH, RW)[RY:RY + IH, RX:RX + IW]
        out.append((a.astype(np.float32), int(h[2])))
    return out


def med3(a):
    p1 = np.pad(a, 1, mode="edge")
    st = np.stack([p1[dy:dy + IH, dx:dx + IW]
                   for dy in range(3) for dx in range(3)])
    return np.partition(st, 4, axis=0)[4]


def fixed_pattern(frames):
    """Spatial std of the time-averaged frame: the blotch amplitude."""
    st = np.stack([med3(f) for f in frames])
    return float(st.mean(axis=0).std())


def render(frames, path, label):
    st = np.stack([med3(f) for f in frames])
    m = st.mean(axis=0)
    lo = float(np.percentile(m, 2))
    hi = float(np.percentile(m, 98))
    if hi - lo < 1.0:
        lo, hi = float(m.min()), float(m.max())
    n = np.clip((m - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    lut = np.array([V.ironbow(t) for t in np.linspace(0, 1, 256)], np.float32)
    from PIL import Image
    Image.fromarray(
        (lut[(n * 255).astype(int)] * 255).astype(np.uint8)
    ).resize((IW * 3, IH * 3), Image.NEAREST).save(path)
    print("  saved %s  (%s)" % (path, label))


print("=== PASS 1: exactly what the app does today ===")
d = open_cam()
basic_init(d)
before = grab(d, 60)
usb.util.dispose_resources(d)
g_before = Counter(g for _, g in before).most_common(1)
if not before:
    raise SystemExit("no frames - unplug and replug the Nano")
fr_before = [f for f, g in before if g == g_before[0][0]]
fb = fixed_pattern(fr_before[8:])
print("  %d frames, dominant gain %d" % (len(fr_before), g_before[0][0]))
print("  fixed pattern: %.1f DL" % fb)
render(fr_before[8:], r"C:\a\_repro_before.png", "app's current path")

print()
print("=== PASS 2: after the vendor's own configuration phase ===")
d = open_cam()
basic_init(d)
vendor_phase(d)
after = grab(d, 60)
usb.util.dispose_resources(d)
if not after:
    print("  NO FRAMES after the vendor phase - that sequence is not safe to")
    print("  replay blindly, and nothing has been changed in the app.")
    raise SystemExit(2)
g_after = Counter(g for _, g in after).most_common(1)
fr_after = [f for f, g in after if g == g_after[0][0]]
fa = fixed_pattern(fr_after[8:])
print("  %d frames, dominant gain %d" % (len(fr_after), g_after[0][0]))
print("  fixed pattern: %.1f DL" % fa)
render(fr_after[8:], r"C:\a\_repro_after.png", "after vendor config")

print()
print("=== RESULT ===")
print("  app as-is        %6.1f DL" % fb)
print("  after vendor cfg %6.1f DL   (%.2fx)" % (fa, fb / max(fa, 1e-6)))
print()
if fa < fb * 0.5:
    print("The vendor configuration phase is doing real work. The camera has a")
    print("DSP pipeline this app never switches on, which is why software")
    print("correction could not remove the pattern.")
else:
    print("No material change. The config phase is SDK bookkeeping, not the")
    print("image pipeline - so the AGC symbols must be driven by other means.")
print()
print("Look at both PNGs before believing either number.")