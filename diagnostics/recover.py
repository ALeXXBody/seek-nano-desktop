import os, time
DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core, usb.util
d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
d.set_configuration(); usb.util.claim_interface(d, 0)
for _ in range(2):
    try: d.ctrl_transfer(0x40, 0x3C, 0, 0, b"\x00\x00", 1250)
    except Exception: pass
    time.sleep(0.15)
try:
    d.reset(); time.sleep(1.2); d.set_configuration()
    usb.util.claim_interface(d, 0)
except Exception as e: print("reset:", e)
time.sleep(0.5)
W = lambda r,p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r,n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54,b"\x00\x00"); W(0x3c,b"\x00\x00"); Rd(0x3d,2); W(0x3e,b"\x08\x00")
W(0x37,b"\xfc\x00\x04\x00"); W(0x3c,b"\x01\x00"); print("state:", Rd(0x3d,2).hex())
ok=0
for i in range(6):
    W(0x53,b"\x58\x5b\x01\x00"); b=bytearray()
    try:
        while len(b)<177840: b+=bytes(d.read(0x81,min(6840,177840-len(b)),timeout=700))
    except Exception: pass
    if len(b)==177840: ok+=1
    time.sleep(0.05)
print("primed frames:", ok, "/6")
usb.util.dispose_resources(d)
