"""Hot-spot detection: find connected regions that are anomalously hot.

No absolute temperature is available from this camera - no calibration exists
in the app, the docs or the captured symbol dumps - so the detector works in
device units (DL after flat-field and bad-pixel correction) and labels results
relative to the scene.

The threshold is derived from the scene rather than fixed, for the same reason
the bad-pixel detector is: a fixed number would either fire on a uniform scene
or miss a real hotspot on a busy one. It is

    thresh = median + max(K * robust_sigma, MIN_DL)

which is scale-free and matches the bad-pixel criterion (median + k*spread),
so the two corrections cannot disagree about what "unusual" means.

Pure numpy - no scipy, matching the rest of the viewer.
"""
import numpy as np

HOTSPOT_K = 6.0           # robust sigmas above the median
HOTSPOT_MIN_DL = 120.0    # ...and at least this far, in device units
HOTSPOT_MIN_AREA = 6      # ignore single pixels and 2x2 specks
PEAK_RADIUS = 12          # top_n: peak separation and region-growth half-width


def _box_max(a, r):
    """Separable box maximum, edge-padded.

    Two passes of np.maximum over shifted views instead of a (2r+1)^2 window,
    which keeps peak finding at a fixed cost per frame instead of growing with
    the radius.
    """
    H, W = a.shape
    p = np.pad(a, r, mode="edge")
    # Original pixel (y, x) sits at padded (y+r, x+r), so its window is padded
    # rows y..y+2r. Both passes index from 0; adding a second pad would shift
    # the window by another r and quietly corrupt the edges, which is exactly
    # where a hotspot near the frame border lives.
    rows = np.maximum.reduce([p[dy:dy + H, :] for dy in range(2 * r + 1)])
    return np.maximum.reduce([rows[:, dx:dx + W] for dx in range(2 * r + 1)])


def _robust_stats(a):
    """Median and a sigma that ignores the outliers we are looking for."""
    flat = a.ravel()
    med = float(np.median(flat))
    mad = float(np.median(np.abs(flat - med)))
    return med, 1.4826 * mad


def _label(mask):
    """4-connected components. numpy-only, iterative flood fill.

    Returns (labels, count) with labels numbered from 1.
    """
    H, W = mask.shape
    labels = np.zeros((H, W), np.int32)
    cur = 0
    ys, xs = np.where(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if labels[sy, sx]:
            continue
        cur += 1
        stack = [(sy, sx)]
        labels[sy, sx] = cur
        while stack:
            y, x = stack.pop()
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] \
                        and not labels[ny, nx]:
                    labels[ny, nx] = cur
                    stack.append((ny, nx))
    return labels, cur


def find_hotspots(img, k=None, min_dl=None, min_area=None):
    """Regions far above the scene median, hottest first.

    Returns a list of dicts: y, x (centroid), area (pixels), peak (DL),
    over (DL above the threshold) and a bbox.

    The constants are resolved at CALL time, not as default arguments.
    Default arguments capture their value when the function is defined, so the
    sensitivity knob could not be moved at runtime - which is exactly what the
    UI needs it for.
    """
    if img is None or img.size == 0:
        return []
    k = HOTSPOT_K if k is None else k
    min_dl = HOTSPOT_MIN_DL if min_dl is None else min_dl
    min_area = HOTSPOT_MIN_AREA if min_area is None else min_area
    med, sig = _robust_stats(img)
    thr = med + max(k * sig, min_dl)
    mask = img >= thr
    if not mask.any():
        return []
    labels, n = _label(mask)
    if n == 0:
        return []
    # Split the components by sorting the masked pixels once, rather than
    # rescanning the whole image per component. `labels == i` allocates a
    # fresh 76,800-element boolean array for every component found, so a scene
    # with a few hundred candidates cost hundreds of full-image passes.
    ys, xs = np.nonzero(mask)
    vals = img[mask]
    labs = labels[mask]
    order = np.argsort(labs, kind="stable")
    labs, ys, xs, vals = labs[order], ys[order], xs[order], vals[order]
    ids = np.arange(1, n + 1)
    starts = np.searchsorted(labs, ids, side="left")
    ends = np.searchsorted(labs, ids, side="right")

    shape = img.shape
    out = []
    for k, (s, e) in enumerate(zip(starts.tolist(), ends.tolist())):
        area = e - s
        if area < min_area:
            continue
        v = vals[s:e]
        cy, cx = ys[s:e], xs[s:e]
        out.append({
            "y": float(cy.mean()),
            "x": float(cx.mean()),
            "area": area,
            "peak": float(v.max()),
            "over": float(v.max() - thr),
            "y0": int(cy.min()), "y1": int(cy.max()),
            "x0": int(cx.min()), "x1": int(cx.max()),
            # Flat indices rather than a full-size mask: merging then costs a
            # concatenate instead of an image-sized OR, and nothing downstream
            # needs the pixels individually.
            "idx": np.flatnonzero(mask)[order][s:e],
            "shape": shape,
        })
    out.sort(key=lambda r: -r["peak"])
    return out


def merge_nearby(regions, radius=24):
    """Fold spots that are really one region but split by a cooler gap.

    A hotspot with a cool stripe through it reads as two. Anything whose boxes
    are within `radius` pixels is treated as one, keeping the hottest peak.
    """
    if len(regions) < 2:
        return regions
    order = sorted(regions, key=lambda r: -r["peak"])
    kept = []
    for r in order:
        cy, cx = r["y"], r["x"]
        joined = False
        for q in kept:
            if np.hypot(cy - q["y"], cx - q["x"]) <= radius:
                q["area"] += r["area"]
                q["idx"] = np.concatenate([q["idx"], r["idx"]])
                # Flat pixel indices, not a full-size boolean mask per region.
                # Keeping the mask cost 76,800 bytes per region and made
                # merging an image-sized OR; with at most a handful of regions
                # live it is not huge, but top_n used to build one for every
                # component above a loose cut - hundreds of them - which is
                # where its 50-79 ms per frame went.
                ys, xs = np.unravel_index(q["idx"], q["shape"])
                q["y"], q["x"] = float(ys.mean()), float(xs.mean())
                q["peak"] = max(q["peak"], r["peak"])
                q["y0"], q["y1"] = int(ys.min()), int(ys.max())
                q["x0"], q["x1"] = int(xs.min()), int(xs.max())
                joined = True
                break
        if not joined:
            kept.append(dict(r))
    kept.sort(key=lambda r: -r["peak"])
    return kept


def top_n(img, n=3, min_area=HOTSPOT_MIN_AREA):
    """The n hottest coherent regions, whatever the threshold.

    A companion to find_hotspots, not a replacement. find_hotspots is selective
    and reports nothing when nothing stands out; this always returns the
    hottest regions so the user can judge for themselves. On a scene with
    strong texture there is no threshold that is both sensitive and
    false-positive-free - measured: a +/-160 DL periodic texture produces 21
    candidate regions and a genuine +300 DL hotspot is only ~2x the scene's
    own variation. That is a property of the scene, not of the detector, so the
    honest answer is to let the user choose.
    """
    if img is None or img.size == 0 or n <= 0:
        return []
    H, W = img.shape
    # Local maxima, then suppress those too close together, then grow each
    # survivor to its region. This replaces a percentile cut plus a flood fill,
    # which measured 50-79 ms per frame here and cut the paint rate to a third
    # of what it was with detection off. The percentile version masked ~10% of
    # the image, so the flood fill had hundreds of components to enumerate and
    # each one allocated a full-frame boolean mask. Peak suppression asks the
    # only question that matters - where is it hottest - and costs a handful of
    # vector passes regardless of how busy the scene is.
    bmax = _box_max(img, PEAK_RADIUS)
    cand = np.argwhere((img >= bmax) & (img >= float(np.percentile(img, 75.0))))
    if cand.size == 0:
        return []
    vals = img[cand[:, 0], cand[:, 1]]
    # Once, not per candidate. A flat scene makes every pixel a local maximum
    # candidate, so computing this inside the loop cost a full-image median for
    # each of them - 190 ms a call on the +0 DL case, measured.
    med, _sig = _robust_stats(img)
    order = np.argsort(-vals, kind="stable")
    cand, vals = cand[order], vals[order]

    taken_y, taken_x = [], []
    out = []
    for (cy, cx), pv in zip(cand.tolist(), vals.tolist()):
        if any(abs(cy - ty) <= PEAK_RADIUS and abs(cx - tx) <= PEAK_RADIUS
               for ty, tx in zip(taken_y, taken_x)):
            continue
        y0, y1 = max(0, cy - PEAK_RADIUS), min(H, cy + PEAK_RADIUS + 1)
        x0, x1 = max(0, cx - PEAK_RADIUS), min(W, cx + PEAK_RADIUS + 1)
        # Grow the peak to the pixels near it belonging to the same hot spot.
        # The cut is a fraction of THIS peak's own rise above the background,
        # not a multiple of the scene spread. A spread-scaled cut fails exactly
        # where it matters: on a +/-160 DL texture the spread is ~127 DL, so
        # 6 sigma came to 381 DL and a genuine +300 DL hotspot grew to nothing
        # and was reported as no region at all. Scaling by the rise adapts to
        # both cases - a tall hotspot keeps its full extent, a shallow one
        # keeps only its core - and costs nothing.
        cut = med + max(HOTSPOT_MIN_DL * 0.5, 0.4 * (pv - med))
        win = img[y0:y1, x0:x1]
        sub = (win >= cut)
        if int(sub.sum()) < min_area:
            continue
        yy, xx = np.nonzero(sub)
        taken_y.append(cy)
        taken_x.append(cx)
        out.append({
            "y": float(yy.mean() + y0), "x": float(xx.mean() + x0),
            "area": int(sub.sum()), "peak": float(pv),
            "over": float(pv - med),
            "y0": int(yy.min() + y0), "y1": int(yy.max() + y0),
            "x0": int(xx.min() + x0), "x1": int(xx.max() + x0),
            "idx": (yy + y0) * W + (xx + x0),
            "shape": img.shape,
        })
        if len(out) >= n:
            break
    out.sort(key=lambda r: -r["peak"])
    return out


def detect(img, sensitivity=None, mode="auto", max_spots=5):
    """One entry point for the viewer.

    mode "auto"  - selective: report only what stands out (find_hotspots)
    mode "top"   - always report the hottest max_spots regions

    `sensitivity` scales how far above the scene a region must be, in units of
    the robust spread. 1.0 is the calibrated default; lower is more sensitive.
    """
    if img is None:
        return []
    if mode == "top":
        return top_n(img, n=max_spots)
    k = HOTSPOT_K if sensitivity is None else max(0.5, HOTSPOT_K * sensitivity)
    return merge_nearby(find_hotspots(img, k=k))[:max_spots]