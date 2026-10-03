"""Can the gain be commanded? If so, the fix is to stop holding gain 3.

The AGC will not move on its own while the camera is still - it sat at gain 3
for 673 of 700 frames - so a passive comparison is impossible. But the protocol
almost certainly exposes a gain write: the HUD reads "HOLDING GAIN 3 (CAMERA
WANTS 20)", so the app can both read a wanted gain and set a current one.

This probes the vendor requests around the ones already known (0x53 kick, 0x54,
0x55, 0x56, 0x58, 0x50, 0x51) looking for a setter, by writing a candidate and
seeing whether the gain in the frame header changes. Read-only requests are
identified by the fact that nothing changes.

It is a search, so it reports what it tried and what happened rather than
assuming success. A found setter means the wall-free path can be fixed by
letting the camera choose its gain instead of pinning the lowest.
"""
import os
import time

import numpy as np

os.add_dll_directory(r"C:\a")
os.environ["PATH"] = r"C:\a" + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
TOTAL = 177840


def open_cam():
    d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
    if d is None:
        raise SystemExit("camera not found - close SeekNano first")
    d.set_configuration()
    usb.util.claim_interface(d, 0)
    return d


def write(d, req, payload, timeout=600):
    return d.ctrl_transfer(0x40, req, 0, 0, payload, timeout)


def read(d, req, n, timeout=600):
    return bytes(d.ctrl_transfer(0xC0, req, 0, 0, n, timeout))


def init(d):
    write(d, 0x54, b"\x00\x00")
    write(d, 0x3C, b"\x00\x00")
    read(d, 0x3D, 2)
    write(d, 0x3E, b"\x08\x00")
    write(d, 0x3C, b"\x01\x00")
    read(d, 0x3D, 2)


def grab(d):
    """One frame's gain from the header, or None."""
    write(d, 0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < TOTAL:
            b += bytes(d.read(0x81, min(6840, TOTAL - len(b)), timeout=800))
    except Exception:
        return None
    if len(b) != TOTAL:
        return None
    h = np.frombuffer(b[:8], "<u2")
    if int(h[0]) != 0x0579:
        return None
    return int(h[2])


d = open_cam()
init(d)

print("baseline gain over 8 frames:")
base = [grab(d) for _ in range(8)]
print("   %s" % base)
live = [g for g in base if g is not None]
if not live:
    usb.util.dispose_resources(d)
    raise SystemExit("no frames - camera wedged? unplug and replug")
cur = live[0]

# Candidate setters: a vendor request with a plausible little-endian payload.
# Range guesses cover the observed gain values (1..28) and beyond.
CANDS = []
for req in (0x50, 0x51, 0x55, 0x56, 0x58, 0x59, 0x5A, 0x5C, 0x5D, 0x5E,
            0x5F, 0x60, 0x61, 0x62):
    CANDS.append((req, 20, b"\x14\x00"))     # gain 20, LE u16
    CANDS.append((req, 20, b"\x14"))         # gain 20, single byte
print()
print("trying %d candidate writes, looking for the header gain to change..."
      % len(CANDS))
found = []
for req, val, payload in CANDS:
    before = grab(d)
    if before is None:
        continue
    try:
        rc = write(d, req, payload, 400)
    except Exception:
        continue
    time.sleep(0.05)
    after = grab(d)
    if after is not None and after != before:
        found.append((req, payload.hex(), before, after, rc))
        print("   req 0x%02X payload %-8s gain %s -> %s   (rc=%s)"
              % (req, payload.hex(), before, after, rc))
    else:
        # put it back if it moved
        if after is not None and after != before:
            try:
                write(d, req, bytes([before & 0xFF, (before >> 8) & 0xFF]), 400)
            except Exception:
                pass

usb.util.dispose_resources(d)
print()
if found:
    print("FOUND a gain setter. First hit: req 0x%02X, payload %s"
          % (found[0][0], found[0][1]))
    print("Gain can be commanded, so the app can stop holding the lowest gain")
    print("and instead let the camera pick - which is what removes the noise a")
    print("wall reference cannot.")
else:
    print("No setter found among those candidates.")
    print("That does NOT mean gain is not settable - only that none of these")
    print("request/payload combinations moved it. The vendor surface is")
    print("undocumented and the search space is large.")