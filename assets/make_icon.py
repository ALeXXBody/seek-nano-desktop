#!/usr/bin/env python3
"""Generate the Seek Nano Viewer emblem icon (v2).

Design: dark rounded tile, HUD ring with 4 gaps in cyan, thermal 'lens'
target with a true ironbow heat core (black/purple core -> orange -> hot
yellow rim) and four orange HUD ticks. Reads at 16 px, emblem at 256.
"""
import math
import os
import numpy as np
from PIL import Image, ImageDraw

S = 1024
OUT = os.path.dirname(os.path.abspath(__file__))
BG = (10, 14, 20, 255)
BG2 = (18, 26, 36, 255)
CYAN = (38, 198, 218, 255)
ORANGE = (255, 107, 53, 255)

def ironbow(t):
    """Match the viewer's ramp: 0 -> near black/purple, 1 -> hot yellow."""
    t = np.clip(t, 0, 1)
    r = np.clip(2.2 * t - 0.15, 0, 1)
    g = np.clip(2.0 * t - 1.0, 0, 1) * (0.35 + 0.65 * t)
    b = np.clip(1.0 - 2.5 * t, 0, 1) * (1 - 0.55 * t)
    return r, g, b

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# ---------------- tile ----------------
m = 30
d.rounded_rectangle([m, m, S-m, S-m], radius=190, fill=BG)
# subtle inner glow edge
d.rounded_rectangle([m+22, m+22, S-m-22, S-m-22], radius=168,
                    outline=BG2, width=6)

cx = cy = S / 2.0

def ring(rad, color, width, a0=0, a1=360):
    d.arc([cx-rad, cy-rad, cx+rad, cy+rad], a0, a1, fill=color, width=width)

# ---------------- HUD ring: 4 arc segments ----------------
for a0 in (8, 98, 188, 278):
    ring(430, CYAN, 12, a0, a0 + 68)

# ---------------- heat core: circular ironbow gradient ----------------
N = 520
yy, xx = np.mgrid[0:N, 0:N]
r = np.hypot(yy - N/2, xx - N/2) / (N/2)
heat = np.clip(1.12 - r, 0, 1)                  # bright rim -> dark core
heat = np.clip(heat - 0.35, 0, 1) / 0.65        # dead zone near center
r8, g8, b8 = ironbow(heat)
a8 = np.clip(1.0 - r, 0, 1)                     # circular alpha mask
rgba = np.zeros((N, N, 4), dtype=np.uint8)
rgba[..., 0] = (r8 * 255).astype(np.uint8)
rgba[..., 1] = (g8 * 255).astype(np.uint8)
rgba[..., 2] = (b8 * 255).astype(np.uint8)
rgba[..., 3] = (a8 * 255).astype(np.uint8)
img.paste(Image.fromarray(rgba), (int(cx - N/2), int(cy - N/2)), Image.fromarray(rgba))

# ---------------- HUD dark rings over the gradient ----------------
ring(290, BG, 8)
ring(210, BG, 5)
ring(120, BG, 5)
# center pupil
ring(58, BG, 10)

# ---------------- orange crosshair ticks ----------------
for ang in (45, 135, 225, 315):
    ar = math.radians(ang)
    x1 = cx + 250 * math.cos(ar); y1 = cy + 250 * math.sin(ar)
    x2 = cx + 320 * math.cos(ar); y2 = cy + 320 * math.sin(ar)
    d.line([x1, y1, x2, y2], fill=ORANGE, width=16)

d.ellipse([cx-8, cy-8, cx+8, cy+8], fill=ORANGE)

big = img.resize((256, 256), Image.LANCZOS)
big.save(os.path.join(OUT, "icon_256.png"))
big.save(os.path.join(OUT, "icon.ico"), format="ICO",
         sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
print("ok")
