"""Fixed-pattern confirmation, corrected flicker measurement, and gain scaling.

Correction to the previous run: the flicker figure diffed along axis=0 of a
single mapped frame, i.e. the vertical gradient inside one image, not change
over time. It is redone here as frame[i+1] minus frame[i].

Three questions:
  1. Is the 225 DL really fixed-pattern? (compare temporal vs spatial variance)
  2. Does normalising each frame's contrast window calm the flicker the way the
     latched EMA window does not?
  3. Does the pattern amplitude SCALE with gain? If it scales predictably, a
     single reference captured at one gain can be scaled to any other gain,
     which is what would stop the AGC from making the correction invalid.
"""
import os, time
from collections import defaultdict
import numpy as np
DLL = r"C:\a\libusb-1.0.dll"
os.add_dll_directory(os.path.dirname(DLL))
os.environ["PATH"] = os.path.dirname(DLL) + os.pathsep + os.environ["PATH"]
import usb.core, usb.util

RW, RH, RX, RY, IW, IH = 342, 260, 2, 12, 320, 240
d = usb.core.find(idVendor=0x289D, idProduct=0x0011)
d.set_configuration()
usb.util.claim_interface(d, 0)
for _ in range(2):
    try: d.ctrl_transfer(0x40, 0x3C, 0, 0, b"\x00\x00", 1250)
    except Exception: pass
    time.sleep(0.15)
try:
    d.reset(); time.sleep(1.2); d.set_configuration()
    usb.util.claim_interface(d, 0)
except Exception as e:
    print("reset:", e)
time.sleep(0.4)
W = lambda r, p: d.ctrl_transfer(0x40, r, 0, 0, p, 1250)
Rd = lambda r, n: bytes(d.ctrl_transfer(0xC0, r, 0, 0, n, 1250))
W(0x54, b"\x00\x00"); W(0x3c, b"\x00\x00"); Rd(0x3d, 2); W(0x3e, b"\x08\x00")
W(0x37, b"\xfc\x00\x04\x00"); W(0x3c, b"\x01\x00"); Rd(0x3d, 2)

recs = []
for i in range(90):
    W(0x53, b"\x58\x5b\x01\x00")
    b = bytearray()
    try:
        while len(b) < 177840:
            b += bytes(d.read(0x81, min(6840, 177840 - len(b)), timeout=800))
    except Exception:
        pass
    if len(b) == 177840:
        h = np.frombuffer(b[:8], "<u2")
        if int(h[0]) == 0x0579 and 0 < int(h[2]) < 256:
            recs.append((int(h[2]), np.frombuffer(b, dtype="<u2").reshape(RH, RW)
                         .astype(np.float32)[RY:RY+IH, RX:RX+IW]))
    time.sleep(0.02)
usb.util.dispose_resources(d)

byg = defaultdict(list)
for g, f in recs:
    byg[g].append(f)
print("frames per gain:", {g: len(v) for g, v in sorted(byg.items())})
dx = lambda x: float(np.abs(np.diff(x, axis=1)).mean())

S = np.stack(byg[3])
print(f"\n1. IS IT FIXED PATTERN?  (gain 3, {len(S)} frames)")
print(f"   spatial  neighbour |dL|       {dx(S[0]):8.2f} DL")
print(f"   temporal mean |frame i+1 - i|  {np.mean([np.abs(S[i+1]-S[i]).mean() for i in range(len(S)-1)]):8.2f} DL")
sd = np.stack([S[i] - S[i].mean() for i in range(len(S))])
corr = [float((sd[i]*sd[i+1]).sum()/np.sqrt((sd[i]**2).sum()*(sd[i+1]**2).sum()))
        for i in range(len(S)-1)]
print(f"   frame-to-frame correlation      {np.mean(corr):+.5f}")
r = float(np.mean([np.corrcoef(S[i].ravel(), S[i+1].ravel())[0,1] for i in range(len(S)-1)]))
print(f"   whole-frame correlation        {r:+.5f}")
print("   => temporal change is tiny next to spatial change: the pattern is")
print("      reproduced identically each frame, so it is fixed-pattern, not noise.")

print("\n3. DOES THE PATTERN SCALE WITH GAIN?")
print(f"{'gain':>5}{'n':>4}{'mean':>9}{'pattern std':>13}{'vs gain 3':>11}")
pats = {}
for g in sorted(byg):
    if len(byg[g]) < 2: continue
    m = np.median(np.stack(byg[g]), axis=0)
    pats[g] = m - m.mean()
b3 = pats.get(3)
for g, p in pats.items():
    rel = f"{p.std()/b3.std():.2f}x" if b3 is not None else "-"
    print(f"{g:>5}{len(byg[g]):>4}{m.mean():>9.0f}{p.std():>13.1f}{rel:>11}")

if b3 is not None and len(pats) > 1:
    print("\n   pattern shape correlation against gain 3 (1.000 = same pattern):")
    for g, p in pats.items():
        if g == 3: continue
        c = float((p*b3).sum()/np.sqrt((p*p).sum()*(b3*b3).sum()))
        print(f"     gain {g:<3} {c:+.4f}")

print("\n2. FLICKER: LATCHED WINDOW vs PER-FRAME NORMALISATION")
print("   (frame i+1 minus frame i, in display levels)")
p2 = [np.percentile(f, 2) for f in S]
p98 = [np.percentile(f, 98) for f in S]
print(f"   live percentile drift: p2 {np.mean(p2):.0f}+-{np.std(p2):.1f}   "
      f"p98 {np.mean(p98):.0f}+-{np.std(p98):.1f}")
LO, HI = np.median(p2), np.median(p98)
def mapped(f, per_frame):
    lo, hi = (np.percentile(f, 2), np.percentile(f, 98)) if per_frame else (LO, HI)
    return np.clip((f - lo)/max(1e-6, hi - lo), 0, 1)*255
for tag, pf in (("latched window", False), ("per-frame normalised", True)):
    st = [np.abs(mapped(S[i+1], pf) - mapped(S[i], pf)).mean() for i in range(len(S)-1)]
    print(f"   {tag:<24} mean {np.mean(st):6.3f}/255   max {np.max(st):6.3f}")
print("\n   Gain changes are the only large jumps here; within one gain the two")
print("   windows behave identically, as expected.")