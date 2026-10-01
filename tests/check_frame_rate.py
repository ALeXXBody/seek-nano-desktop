"""Is 12.9 fps the camera, or is it the USB transport?

The kick payload is byte-identical to the phone app's (W 0x53 "WFsBAA==" =
58 5b 01 00), so the app is not asking for a different rate there. That leaves
two candidates:

  a) the sensor produces a frame every ~74 ms no matter what we ask
  b) the link cannot deliver 177840 bytes any faster

(b) is testable. Sustaining 12.9 fps needs 177840 / 0.074 = 2.4 MB/s. Measure
the actual bulk throughput: if it is far above that, the transport has headroom
and the camera is the limit.

Also kicks back-to-back without waiting, to see whether the camera has frames
queued up or produces strictly on its own clock.
"""
import os
import time
from collections import defaultdict

import numpy as np

DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core
import usb.util

FRAME_BYTES = 177840
RW, RH = 342, 260

d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
print("device:", d)
for attr in ("bcdUSB", "bNumConfigurations"):
    try:
        print("  %s = %s" % (attr, getattr(d, attr)))
    except Exception:
        pass
try:
    cfg = d.get_active_configuration()
    print("  active config bNumInterfaces:", cfg.bNumInterfaces)
except Exception as e:
    print("  config:", e)

d.set_configuration()
usb.util.claim_interface(d, 0)
for _ in range(2):
    try:
        d.ctrl_transfer(0x40, 0x3C, 0, 0, b"\x00\x00", 1250)
    except Exception:
        pass
    time.sleep(0.15)
try:
    d.reset()
    time.sleep(1.2)
    d.set_configuration()
    usb.util.claim_interface(d, 0)
except Exception as e:
    print("reset:", e)
time.sleep(0.4)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00")
W(0x3C, b"\x00\x00")
Rd(0x3D, 2)
W(0x3E, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00")
W(0x3C, b"\x01\x00")
print("stream state:", Rd(0x3D, 2).hex())

# warm up
for _ in range(4):
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < FRAME_BYTES:
            b += bytes(d.read(0x81, min(6840, FRAME_BYTES - len(b)), timeout=700))
    except Exception:
        pass
    time.sleep(0.02)

# ---- A: per-frame timing and throughput -------------------------------
print("\nA. normal paced loop (what the app does)")
kick, first, done, bytes_all = [], [], [], []
N = 60
t0 = time.perf_counter()
prev = t0
for _ in range(N):
    ta = time.perf_counter()
    W(0x53, b"\x58\x5b\x01\x00")
    tb = time.perf_counter()
    b = bytearray()
    try:
        while len(b) < FRAME_BYTES:
            b += bytes(d.read(0x81, min(6840, FRAME_BYTES - len(b)), timeout=900))
    except Exception:
        pass
    tc = time.perf_counter()
    kick.append(tb - ta)
    done.append(tc - tb)
    bytes_all.append(len(b))
    if len(b) >= FRAME_BYTES:
        first.append(tb)
    prev = tc
t1 = time.perf_counter()
n = len(bytes_all)
total = sum(bytes_all)
print("  %d frames in %.2f s -> %.2f fps" % (n, t1 - t0, n / (t1 - t0)))
print("  kick cost   : mean %.2f ms" % (np.mean(kick) * 1000))
print("  read wait   : mean %.1f ms  min %.1f  max %.1f"
      % (np.mean(done) * 1000, np.min(done) * 1000, np.max(done) * 1000))
print("  throughput  : %.0f bytes/s = %.2f MB/s"
      % (total / (t1 - t0), total / (t1 - t0) / 1e6))
need = FRAME_BYTES * 12.9
print("  to sustain 12.9 fps we need %.0f B/s = %.2f MB/s" % (need, need / 1e6))
print("  headroom    : %.1fx" % (total / (t1 - t0) / need))

# ---- B: raw bulk throughput, no per-frame framing ---------------------
print("\nB. raw bulk read throughput (does the link go faster?)")
t0 = time.perf_counter()
got = 0
try:
    while time.perf_counter() - t0 < 3.0:
        got += len(d.read(0x81, 65536, timeout=200))
except Exception:
    pass
dt = time.perf_counter() - t0
print("  %d bytes in %.2f s = %.2f MB/s" % (got, dt, got / dt / 1e6))
print("  that is %.1fx the 2.4 MB/s needed for 12.9 fps" % (got / dt / need))

# ---- C: kick as fast as possible, do not wait --------------------------
print("\nC. kick back-to-back with no pacing - does the camera queue frames?")
t0 = time.perf_counter()
comps = 0
attempts = 0
while time.perf_counter() - t0 < 6.0:
    attempts += 1
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < FRAME_BYTES:
            b += bytes(d.read(0x81, min(6840, FRAME_BYTES - len(b)), timeout=250))
    except Exception:
        pass
    if len(b) >= FRAME_BYTES:
        comps += 1
    if attempts > 400:
        break
dt = time.perf_counter() - t0
print("  %d kicks in %.2f s, %d complete frames -> %.2f fps"
      % (attempts, dt, comps, comps / dt))
print("  if this is still ~12.9 the camera produces on its own clock and the")
print("  74 ms period is the sensor's, not something we control by pacing")

usb.util.dispose_resources(d)