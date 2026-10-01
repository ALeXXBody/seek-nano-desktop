"""colmean_only won the last test, but that scene had a VERTICAL gradient,
which flatters any per-column method. Re-test with a strong HORIZONTAL scene
gradient (cold window left, warm heater right) - the case where naive column
subtraction destroys real content.

Adds a high-passed variant: estimate the column FPN from the column-mean of
(img - smooth(img)), so only high-frequency per-column structure is removed
and the smooth scene survives.
"""
import numpy as np, pathlib
from PIL import Image
from scipy.ndimage import uniform_filter as boxf   # scipy may be absent

ROOT = pathlib.Path(__file__).resolve().parent.parent
h, w = 240, 320
yy, xx = np.mgrid[0:h, 0:w]
rng = np.random.default_rng(11)

def smooth(a, k):
    if boxf is not None:
        return boxf(a, size=k, mode="nearest")
    pad = k//2
    p = np.pad(a, pad, mode="edge")
    c = np.cumsum(np.cumsum(p, 0), 1)
    c = np.pad(c, ((1,0),(1,0)))
    out = (c[k:,k:] - c[:-k,k:] - c[k:,:-k] + c[:-k,:-k])/(k*k)
    return out[:a.shape[0], :a.shape[1]]

# ---- static FPN ----------------------------------------------------------
col = rng.normal(0, 50, (1, w)).astype(np.float32)
row = rng.normal(0, 30, (h, 1)).astype(np.float32)
r = np.sqrt(((xx-w/2)/(w/2))**2 + ((yy-h/2)/(h/2))**2)
vig = (-420*(r**2)).astype(np.float32)
gain = 1.0 + rng.normal(0, 0.008, (h, w)).astype(np.float32)
FPN = ((col + row + vig) * gain).astype(np.float32)

def scene_vertical(k):
    s = (300 + 900*(yy/h)).astype(np.float32)
    s += 2600*np.exp(-(((xx-215)/26.)**2 + ((yy-150)/26.)**2))
    return s

def scene_horizontal(k):
    # ADVERSARIAL for per-column methods: real content varies across x
    s = (300 + 1800*(xx/w)).astype(np.float32)
    s += 2600*np.exp(-(((xx-215)/26.)**2 + ((yy-150)/26.)**2))
    s -= 1500*np.exp(-(((xx-70)/20.)**2 + ((yy-90)/20.)**2))
    return s

def surf_quad(img):
    hh, ww = img.shape
    y=(np.linspace(0,hh-1,hh,dtype=np.float32)[:,None]/max(1,hh-1))*2-1
    x=(np.linspace(0,ww-1,ww,dtype=np.float32)[None,:]/max(1,ww-1))*2-1
    Y,X=np.broadcast_arrays(y,x)
    M=np.stack([np.ones(hh*ww,np.float32),X.ravel(),Y.ravel(),
                (X*Y).ravel(),(X*X).ravel(),(Y*Y).ravel()],axis=1)
    c,*_=np.linalg.lstsq(M,img.ravel(),rcond=None)
    return (M@c).reshape(hh,ww)

def m_noop(x):        return x
def m_quad(x):        return x - surf_quad(x)
def m_colmean(x):     return x - x.mean(axis=0, keepdims=True)
def m_colmedian(x):   return x - np.median(x, axis=0, keepdims=True)
def m_colmean_hp(x):
    hp = x - smooth(x, 9)
    return x - hp.mean(axis=0, keepdims=True)
def m_colmedian_hp(x):
    hp = x - smooth(x, 9)
    return x - np.median(hp, axis=0, keepdims=True)
def m_col_hp_quarter(x):
    """weaker: half-strength high-pass column FPN"""
    hp = x - smooth(x, 9)
    return x - 0.5*hp.mean(axis=0, keepdims=True)

METHODS = [("no-op", m_noop), ("quad", m_quad), ("colmean", m_colmean),
           ("colmedian", m_colmedian), ("colmean_highpass", m_colmean_hp),
           ("colmedian_highpass", m_colmedian_hp), ("colmean_hp_50pct", m_col_hp_quarter)]

def run(scene_fn, label):
    print("="*76); print(label); print("="*76)
    print(f"{'method':<22}{'RMSE':>9}{'vs no-op':>11}{'corr w/FPN':>12}")
    print("-"*76)
    SC = scene_fn(0)
    OBS = SC*gain + FPN + rng.normal(0, 6, (h, w)).astype(np.float32)
    base = float(np.sqrt(((OBS-SC)**2).mean()))
    print(f"{'no-op':<22}{base:>9.1f}{'--':>11}{1.0:>12.3f}")
    for nm, fn in METHODS[1:]:
        raw_out = fn(OBS)
        got = raw_out - raw_out.mean() + SC.mean()
        rmse = float(np.sqrt(((got-SC)**2).mean()))
        c = float(np.corrcoef((OBS-raw_out).ravel(), FPN.ravel())[0,1])
        print(f"{nm:<22}{rmse:>9.1f}{rmse-base:>+11.1f}{c:>12.3f}")
    print()

run(scene_vertical,   "SCENE A - vertical gradient (favours per-column methods)")
run(scene_horizontal, "SCENE B - HORIZONTAL gradient (adversarial for per-column)")

outdir = ROOT/"nuc_out"; outdir.mkdir(exist_ok=True)
def stretch(x):
    lo, hi = np.percentile(x, 2), np.percentile(x, 98)
    return (np.clip((x-lo)/max(1., hi-lo), 0, 1)*255).astype(np.uint8)
SC = scene_horizontal(0)
OBS = SC*gain + FPN
for nm, fn in METHODS:
    o = fn(OBS); o = o - o.mean() + SC.mean()
    Image.fromarray(np.hstack([stretch(OBS), stretch(o)]), "L").save(outdir/f"adv_{nm}.png")
print("wrote nuc_out/adv_*.png  (observed | corrected) for the ADVERSARIAL scene")
