"""Live capture from the Seek Nano over libusb-1.0 -> libusbK.

MI_00/MI_01 are now bound to libusbK (oem225.inf / oem390.inf), GUIDs
{D60ACF02-...} / {30F3476C-...}. sncap.exe only knows the WinUSB GUIDs, so it
cannot see these - this talks the documented protocol from docs/protocol.md
instead: handshake, enable streaming, then bulk-read 177840 B off 0x81.
"""
import os, sys, time, ctypes, pathlib

DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]

import usb.core, usb.util

OUT = pathlib.Path(r"C:\a\out")
OUT.mkdir(exist_ok=True)
FRAME = 177_840
CHUNK = 6_840

def hexs(b): return " ".join(f"{x:02x}" for x in b)

dev = usb.core.find(idVendor=0x289D, idProduct=0x0011)
if dev is None:
    print("device not found by VID/PID")
    sys.exit(1)

print(f"found: {dev}")
try:    print("  manufacturer:", usb.util.get_string(dev, dev.iManufacturer))
except Exception: pass
try:    print("  product     :", usb.util.get_string(dev, dev.iProduct))
except Exception: pass
try:    print("  serial      :", usb.util.get_string(dev, dev.iSerialNumber))
except Exception: pass

def eps(iface):
    """pyusb exposes endpoints as a flat list; bulk IN = attr&2 and addr&0x80."""
    ins, outs = [], []
    for e in iface:
        addr = e.bEndpointAddress
        if not (e.bmAttributes & 2):
            continue
        (ins if addr & 0x80 else outs).append(addr)
    return sorted(ins), sorted(outs)

for cfg in dev:
    for iface in cfg:
        i, o = eps(iface)
        print(f"  iface {iface.bInterfaceNumber} alt {iface.bAlternateSetting} "
              f"class {iface.bInterfaceClass}.{iface.bInterfaceSubClass} "
              f"bulk IN={i} OUT={o}")

def claim():
    cfg = dev.get_active_configuration()
    for iface in cfg:
        ins, _ = eps(iface)
        if 0x81 in ins:
            try:
                usb.util.claim_interface(dev, iface)
                print(f"  claimed iface {iface.bInterfaceNumber} (has 0x81 IN)")
                return iface
            except Exception as e:
                print(f"  claim iface {iface.bInterfaceNumber} failed: {e}")
    return None

def W(req, payload=b"\x00\x00", t=1250):
    d = dev.ctrl_transfer(0x40, req, 0, 0, payload, timeout=t)
    print(f"  W 0x{req:02x} {hexs(payload)} -> {d}")
    return d
def R(req, n, t=1250):
    d = dev.ctrl_transfer(0xC0, req, 0, 0, n, timeout=t)
    print(f"  R 0x{req:02x} [{n}] -> {hexs(d)}")
    return d

iface = claim()
if iface is None:
    print("no claimable interface with 0x81 IN"); sys.exit(1)

print("\n--- phase 1: handshake ---")
W(0x54); W(0x3c); R(0x3d, 2); W(0x3e, b"\x08\x00")
print("--- phase 4: enable streaming ---")
W(0x37, b"\xfc\x00\x04\x00"); W(0x3c, b"\x01\x00"); R(0x3d, 2)
print("--- kick + read ---")

got = bytearray()
frames = []
for f in range(3):
    W(0x53, b"\x58\x5b\x01\x00")
    R(0x35, 4)
    t0 = time.time()
    got = bytearray()
    while len(got) < FRAME:
        want = min(CHUNK, FRAME - len(got))
        try:
            part = dev.read(0x81, want, timeout=3000)
        except Exception as e:
            print(f"  frame {f}: bulk read failed at {len(got)}: {e}")
            break
        got += bytes(part)
    dt = (time.time() - t0) * 1000
    if len(got) == FRAME:
        print(f"  frame {f}: {len(got)} B in {dt:.0f} ms  head: {hexs(got[:16])}")
        frames.append(bytes(got))
    else:
        print(f"  frame {f}: short {len(got)}/{FRAME}")
    if f == 0 and len(got) >= 16:
        import struct
        print(f"    magic={hexs(got[0:2])} w1={struct.unpack_from('<H',got,2)[0]} "
              f"w2={struct.unpack_from('<I',got,4)[0]} w3={struct.unpack_from('<H',got,8)[0]} "
              f"w4={struct.unpack_from('<I',got,10)[0]} w5={struct.unpack_from('<H',got,14)[0]}")
    if not frames and len(got) == 0:
        time.sleep(0.2)

if frames:
    n = len(frames)
    p = OUT / "frame.raw"
    p.write_bytes(frames[-1])
    print(f"\nWROTE {p}  ({p.stat().st_size} B) from {n} good frame(s)")
    for i, fr in enumerate(frames):
        (OUT / f"live_frame_{i}.raw").write_bytes(fr)
        print(f"   also saved live_frame_{i}.raw")
else:
    print("\nno frames captured")
usb.util.release_interface(dev, iface)
