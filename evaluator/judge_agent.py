"""Human-taste design judge: scores an output design (JSON) and its rendered
PNG against art-direction criteria. Dev tool, not the official scorer.

It reads the pixels, not just the boxes:
  - visual mass comes from the difference between the real render and a
    background-only render, so transparent padding and thin glyphs weigh
    what they actually look like;
  - contrast is sampled from a render with the text removed;
  - text fit uses the renderer's own wrapping (float size) and drawing
    (int size) rules.

Rubric (0-25 each, total 0-100):
  hierarchy   type hierarchy kept, the focal element dominates, reading
              order intuitive, logo leads and CTA comes last
  optical     visual mass on the optical center, shared alignment axes
              (grid), left/right balance, fidelity to the source's intent
              (same-shape canvases keep the arrangement; decor keeps its
              corner and stays off the product)
  space       clean outer margins, balanced margins, breathing room between
              elements with an even rhythm, a fill that is neither empty nor
              cramped
  readability no overflow or cutoff, no collisions, legible sizes, contrast

Usability gate: every rubric is multiplied by
sqrt(0.4 + 0.6 * legibility) * sqrt(0.4 + 0.6 * occupancy), so unreadable
text or content shrunk into a corner of the canvas sinks the whole score
(a human wouldn't credit the margins of an ad nobody can read).

A hard-rule violation (ids, canvas, off-canvas, aspect ratio, changed
content/font) scores 0 and is flagged critical.

  python -m evaluator.judge_agent <run_dir> [--set fixtures]
"""
import argparse
import io
import itertools
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ASSETS = ROOT / "brand_kit" / "assets"
FONTS = str(ROOT / "brand_kit" / "fonts")
REQ = {"text": ["content", "font_family", "font_size", "color"], "image": ["asset_id"],
       "shape": ["shape_type", "fill_color"]}
FIXED = ["type", "content", "font_family", "font_weight", "font_style", "asset_id", "color",
         "fill_color", "group_id", "z_index", "shape_type", "fit"]
ANALYSIS = 1024  # max side of the analysis renders


# ------------------------------------------------------------------ renderer access
_r = None


def _renderer():
    global _r
    if _r is None:
        from renderer.converter.convert import _get_renderer
        _r = _get_renderer(FONTS)
    return _r


def _render(design, keep=None) -> Image.Image | None:
    from renderer.converter.convert import convert_chameleon_to_crello
    d = design if keep is None else dict(design, elements=[e for e in design["elements"] if keep(e)])
    if not d["elements"]:
        return None
    ex, _ = convert_chameleon_to_crello(d, _renderer().font_manager, str(ASSETS))
    return Image.open(io.BytesIO(_renderer().render(ex, max_size=ANALYSIS, format="png"))).convert("RGB")


def _wstyle(el):
    bold = (el.get("font_weight") or "regular").lower() == "bold"
    it = el.get("font_style") == "italic"
    return ("bold" if bold else "regular",
            "bolditalic" if bold and it else "bold" if bold else "italic" if it else "regular")


def text_layout(el):
    """Mirror converter + renderer: wrap at the float size, draw at int(size).
    Returns (lines, widest line px, ink height, ink box) or None."""
    import skia
    from renderer.converter.wrap import char_widths, wrap_text
    content = str(el.get("content", ""))
    fs = float(el.get("font_size", 0))
    if not content or fs <= 0:
        return None
    fm = _renderer().font_manager
    w, st = _wstyle(el)
    widths = char_widths(fm, el["font_family"], w, st, fs, content)
    lines = wrap_text(content, widths, float(el["width"])).split("\n")
    ifs = int(fs)
    if ifs <= 0:
        return None
    font = skia.Font(skia.Typeface.MakeFromData(fm.lookup(el["font_family"], w, st)), ifs)
    m = font.getMetrics()
    lws = [sum(font.getWidths(font.textToGlyphs(ln))) for ln in lines]
    ink_h = (len(lines) - 1) * fs * float(el.get("line_height", 1.2) or 1.2) + (m.fDescent - m.fAscent)
    mw = max(lws) if lws else 0.0
    al = el.get("text_align", "left")
    off = 0.0 if al == "left" else (el["width"] - mw) / 2 if al == "center" else el["width"] - mw
    return len(lines), mw, ink_h, (el["x"] + off, el["y"], el["x"] + off + mw, el["y"] + ink_h)


# ------------------------------------------------------------------ geometry / roles
_alpha = {}


def visible_box(el):
    x, y, w, h = el["x"], el["y"], el["width"], el["height"]
    if el["type"] != "image" or el.get("fit") == "cover" or w <= 0 or h <= 0:
        return (x, y, x + w, y + h)
    aid = el.get("asset_id")
    if aid not in _alpha:
        try:
            im = Image.open(ASSETS / aid)
            bb = im.getchannel("A").getbbox() if "A" in im.getbands() else None
            iw, ih = im.size
            bb = bb or (0, 0, iw, ih)
            _alpha[aid] = (bb[0] / iw, bb[1] / ih, bb[2] / iw, bb[3] / ih, iw / ih)
        except Exception:
            _alpha[aid] = None
    ab = _alpha[aid]
    if ab is None:
        return (x, y, x + w, y + h)
    fx0, fy0, fx1, fy1, ar = ab
    if el.get("fit") == "fill":
        dw, dh, ox, oy = w, h, x, y
    else:
        dw, dh = (w, w / ar) if w / h < ar else (h * ar, h)
        ox, oy = x + (w - dw) / 2, y + (h - dh) / 2
    return (ox + fx0 * dw, oy + fy0 * dh, ox + fx1 * dw, oy + fy1 * dh)


def rot_bbox(el):
    x, y, w, h = el["x"], el["y"], el["width"], el["height"]
    r = math.radians(float(el.get("rotation", 0) or 0))
    if not r:
        return (x, y, x + w, y + h)
    cx, cy = x + w / 2, y + h / 2
    rw = abs(w * math.cos(r)) + abs(h * math.sin(r))
    rh = abs(w * math.sin(r)) + abs(h * math.cos(r))
    return (cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2)


def ink_box(el):
    if el["type"] == "text":
        try:
            t = text_layout(el)
        except Exception:
            t = None
        if t is None:
            return None
        b = t[3]
        return rot_bbox(dict(el, x=b[0], y=b[1], width=b[2] - b[0], height=b[3] - b[1])) \
            if el.get("rotation") else b
    if el.get("rotation"):
        return rot_bbox(el)
    return visible_box(el)


def inter(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def area(a):
    return max(0, a[2] - a[0]) * max(0, a[3] - a[1])


def union(bs):
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def sep(a, b):
    """Clearance between two boxes (negative when they overlap)."""
    gx = max(a[0], b[0]) - min(a[2], b[2])
    gy = max(a[1], b[1]) - min(a[3], b[3])
    return max(gx, gy) if (gx >= 0 or gy >= 0) else -min(-gx, -gy)


def is_background(el, cw, ch):
    if el["type"] not in ("image", "shape"):
        return False
    x0, y0 = max(el["x"], 0), max(el["y"], 0)
    x1, y1 = min(el["x"] + el["width"], cw), min(el["y"] + el["height"], ch)
    return max(0, x1 - x0) * max(0, y1 - y0) >= 0.9 * cw * ch


def asset_missing(el):
    return el["type"] == "image" and not (ASSETS / (el.get("asset_id") or "__none__")).is_file()


def is_decor(el):
    if el["type"] == "shape" and not el.get("group_id"):
        from renderer.converter.colorutil import parse_rgba
        a = parse_rgba(el.get("fill_color"))[3] * float(el.get("opacity", 1) if el.get("opacity") is not None else 1)
        return a <= 0.5
    return False


def lum(rgb):
    c = rgb / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def clip01(v):
    return max(0.0, min(1.0, v))


def _glow(bg: Image.Image | None):
    """Brightness centroid of a background render (fractions of the canvas),
    or None when it has no clear focal light."""
    if bg is None:
        return None
    from PIL import ImageFilter
    g = bg.convert("L")
    k = 80 / max(g.size)
    g = g.resize((max(1, round(g.width * k)), max(1, round(g.height * k)))).filter(ImageFilter.GaussianBlur(3))
    a = np.asarray(g).astype(float)
    e = a - np.median(a)
    e = np.where(e > 2, e * e, 0.0)
    if e.sum() <= 0 or e.max() < 64:
        return None
    ys, xs = np.mgrid[0:a.shape[0], 0:a.shape[1]]
    return float((e * xs).sum() / e.sum()) / a.shape[1], float((e * ys).sum() / e.sum()) / a.shape[0]


# ------------------------------------------------------------------ hard rules
def hard_rules(src, out, tgt):
    hard = []
    W, H = tgt["width"], tgt["height"]
    sw, sh = src["canvas"]["width"], src["canvas"]["height"]
    if out.get("canvas", {}).get("width") != W or out.get("canvas", {}).get("height") != H:
        hard.append("canvas size mismatch")
    ids = [e.get("id") for e in out.get("elements", [])]
    if sorted(map(str, ids)) != sorted(str(e["id"]) for e in src["elements"]):
        hard.append("id set/count mismatch")
    so = {e["id"]: e for e in src["elements"]}
    for e in out.get("elements", []):
        s = so.get(e.get("id"))
        if s is None:
            continue
        for f in ["id", "type", "x", "y", "width", "height"] + REQ.get(e.get("type"), []):
            if f not in e:
                hard.append(f"{e['id']}: missing {f}")
        for f in FIXED:
            if s.get(f) != e.get(f):
                hard.append(f"{e['id']}: changed {f}")
        if e.get("width", 0) < 0 or e.get("height", 0) < 0:
            hard.append(f"{e['id']}: negative size")
        if e["x"] < -0.5 or e["y"] < -0.5 or e["x"] + e["width"] > W + 0.5 or e["y"] + e["height"] > H + 0.5:
            hard.append(f"{e['id']}: off-canvas")
        if e["type"] == "image" and e.get("fit") != "cover" and not is_background(s, sw, sh) \
                and s["width"] > 0 and s["height"] > 0 and e["height"] > 0:
            r = (e["width"] / e["height"]) / (s["width"] / s["height"])
            if abs(r - 1) > 0.05:
                hard.append(f"{e['id']}: aspect off {r:.2f}")
    return hard


# ------------------------------------------------------------------ the judge
def judge(task: dict, out: dict, png_path: Path | None = None) -> dict:
    src, tgt = task["source"], task["target_canvas"]
    W, H = tgt["width"], tgt["height"]
    sw, sh = src["canvas"]["width"], src["canvas"]["height"]
    short = min(W, H)
    flags, metrics = [], {}
    hard = hard_rules(src, out, tgt)
    if hard:
        return {"total_score": 0.0, "rubric": {"hierarchy": 0.0, "optical": 0.0, "space": 0.0,
                                               "readability": 0.0},
                "flags": ["CRITICAL hard-rule: " + h for h in hard], "metrics": {"hard": hard}}
    so = {e["id"]: e for e in src["elements"]}
    oo = {e["id"]: e for e in out["elements"]}

    def is_content(e, cw, ch):
        return not is_background(e, cw, ch) and not is_decor(e) and not asset_missing(e) \
            and e["width"] > 0 and e["height"] > 0
    cand = [e["id"] for e in src["elements"] if is_content(e, sw, sh)]
    sbox = {i: ink_box(so[i]) for i in cand}
    obox = {i: ink_box(oo[i]) for i in cand}
    cand = [i for i in cand if sbox[i] is not None and obox[i] is not None and area(obox[i]) > 0]

    # blocks: a group is one unit; ungrouped elements stand alone
    blocks = {}
    for i in cand:
        blocks.setdefault(so[i].get("group_id") or f"__{i}", []).append(i)
    bsrc = {k: union([sbox[i] for i in v]) for k, v in blocks.items()}
    bout = {k: union([obox[i] for i in v]) for k, v in blocks.items()}
    keys = list(blocks)
    layered = {(a, b) for a, b in itertools.combinations(keys, 2)
               if inter(bsrc[a], bsrc[b]) > 0.02 * min(area(bsrc[a]), area(bsrc[b]))}

    texts = [e for e in out["elements"] if e["type"] == "text" and e.get("content")]
    s_texts = [e for e in src["elements"] if e["type"] == "text" and e.get("content")]
    imgs = [i for i in cand if so[i]["type"] == "image"]
    hero = max(imgs, key=lambda i: so[i]["width"] * so[i]["height"]) if imgs else None
    if hero is not None and so[hero]["width"] * so[hero]["height"] < 0.08 * sw * sh:
        hero = None
    logo = next((i for i in imgs if "logo" in (so[i].get("asset_id") or "").lower()), None)

    # ---------------- renders for pixel analysis
    full = Image.open(png_path).convert("RGB") if png_path and Path(png_path).exists() else None
    scale = min(1.0, ANALYSIS / max(W, H))
    aw, ah = max(1, round(W * scale)), max(1, round(H * scale))
    if full is not None:
        full = full.resize((aw, ah))
    try:
        bg_only = _render(out, keep=lambda e: is_background(so[e["id"]], sw, sh) if e["id"] in so else False)
        no_text = _render(out, keep=lambda e: e["type"] != "text")
    except Exception:
        bg_only = no_text = None
    if bg_only is not None:
        bg_only = bg_only.resize((aw, ah))
    if no_text is not None:
        no_text = no_text.resize((aw, ah))

    # ================= 1) hierarchy & focal anchoring
    pairs = ok = 0
    for a in s_texts:
        for b in s_texts:
            if a["font_size"] > b["font_size"] * 1.05:
                pairs += 1
                ok += int(oo[a["id"]]["font_size"]) >= int(oo[b["id"]]["font_size"])
    hier_kept = ok / pairs if pairs else 1.0
    if hier_kept < 1:
        flags.append(f"hierarchy inverted ({hier_kept:.2f})")
    # the focal element: hero image, else the headline
    if hero is not None:
        hs_out, hs_src = area(obox[hero]) / (W * H), area(sbox[hero]) / (sw * sh)
        # it keeps most of its relative footprint, and it is the biggest thing drawn
        foot = clip01((hs_out / hs_src) / 0.8)
        others = [area(bout[k]) for k in keys if hero not in blocks[k]]
        dom = 1.0 if not others or area(obox[hero]) >= max(others) else 0.6
        focal = 0.7 * foot + 0.3 * dom
        metrics["hero_share"] = round(hs_out, 4)
    elif s_texts:
        head = max(s_texts, key=lambda e: e["font_size"])
        r = (oo[head["id"]]["font_size"] / short) / (head["font_size"] / min(sw, sh))
        focal = clip01(r / 0.8)
    else:
        focal = 1.0
    # reading order (Z pattern): source top-to-bottom vs output top-to-bottom, left-to-right
    def before(b1, b2):
        c1, c2 = (b1[1] + b1[3]) / 2, (b2[1] + b2[3]) / 2
        tol = 0.35 * min(b1[3] - b1[1], b2[3] - b2[1])
        if abs(c1 - c2) > tol:
            return c1 < c2
        return (b1[0] + b1[2]) < (b2[0] + b2[2])
    op = oa = 0
    for a, b in itertools.combinations(keys, 2):
        if (a, b) in layered:
            continue
        sa, sb = bsrc[a], bsrc[b]
        if abs((sa[1] + sa[3]) - (sb[1] + sb[3])) / 2 < 0.35 * min(sa[3] - sa[1], sb[3] - sb[1]):
            continue  # side by side in the source: no order to keep
        op += 1
        oa += before(sa, sb) == before(bout[a], bout[b])
    order = oa / op if op else 1.0
    metrics["reading_order"] = round(order, 3)
    # logo leads (top or left zone, legible) and the CTA comes last
    lead = 1.0
    if logo is not None:
        lb = obox[logo]
        lead = 0.5 * float(lb[1] < 0.35 * H or lb[0] < 0.3 * W) + 0.5 * clip01((lb[3] - lb[1]) / 20.0)
    cta_k = None
    btn = [k for k, v in blocks.items() if len(v) > 1 and {so[i]["type"] for i in v} >= {"shape", "text"}]
    if btn:
        cta_k = max(btn, key=lambda k: (area(bsrc[k]), bsrc[k][1]))
    last = 1.0
    if cta_k is not None:
        cb = bout[cta_k]
        after = [k for k in keys if k != cta_k and bsrc[k][1] < bsrc[cta_k][1]
                 and not before(bout[k], cb)]
        last = 1.0 - len(after) / max(1, len(keys) - 1)
    hierarchy = 8 * hier_kept + 7 * focal + 6 * order + 2 * lead + 2 * last

    # ================= 2) optical centering & gridding
    mass_c, lr = None, 1.0
    if full is not None:
        f = np.asarray(full).astype(float)
        if bg_only is not None:
            base = np.asarray(bg_only).astype(float)
        else:
            base = np.broadcast_to(np.median(f.reshape(-1, 3), axis=0), f.shape)
        d = np.abs(f - base).sum(axis=2)
        d = np.where(d > 24, d, 0.0)
        tot = d.sum()
        if tot > 0:
            ys, xs = np.mgrid[0:ah, 0:aw]
            mx, my = float((d * xs).sum() / tot) / aw, float((d * ys).sum() / tot) / ah
            mass_c = (mx, my)
            left, right = float(d[:, : aw // 2].sum()), float(d[:, aw - aw // 2:].sum())
            lr = 1 - abs(left - right) / max(left + right, 1e-6)
    # target: the optical center, pulled toward the background's glow when it has one
    tx, ty = 0.5, 0.48
    glow = _glow(bg_only)
    if glow is not None:
        tx, ty = 0.5 * tx + 0.5 * glow[0], 0.5 * ty + 0.5 * glow[1]
        metrics["glow"] = [round(glow[0], 3), round(glow[1], 3)]
    if mass_c is not None:
        dev = math.hypot(mass_c[0] - tx, (mass_c[1] - ty) * min(1.0, H / W + 0.3))
        center = clip01(1 - dev / 0.22)
        metrics["mass_center"] = [round(mass_c[0], 3), round(mass_c[1], 3)]
    else:
        center = 0.5
    # grid: share of blocks that share an alignment axis with another block
    tol = 0.012 * W
    if len(keys) >= 2:
        aligned = 0
        for k in keys:
            b = bout[k]
            axes = (b[0], (b[0] + b[2]) / 2, b[2])
            hit = False
            for o in keys:
                if o == k:
                    continue
                c = bout[o]
                if any(abs(p - q) <= tol for p, q in zip(axes, (c[0], (c[0] + c[2]) / 2, c[2]))):
                    hit = True
                    break
                # side-by-side items share a line (tops, centers or bottoms)
                if any(abs(p - q) <= 0.012 * H for p, q in
                       zip((b[1], (b[1] + b[3]) / 2, b[3]), (c[1], (c[1] + c[3]) / 2, c[3]))):
                    hit = True
                    break
            aligned += hit
        grid = aligned / len(keys)
    else:
        grid = 1.0
    balance = clip01((lr - 0.3) / 0.6)
    # fidelity to the source's intent
    shape_change = abs(math.log((W / H) / (sw / sh)))
    near = math.exp(-3 * shape_change)  # 1 for the same shape, ~0 for a big change
    if near > 0.05 and keys:
        disp = []
        for k in keys:
            s_, o_ = bsrc[k], bout[k]
            disp.append(math.hypot(((s_[0] + s_[2]) / 2 / sw) - ((o_[0] + o_[2]) / 2 / W),
                                   ((s_[1] + s_[3]) / 2 / sh) - ((o_[1] + o_[3]) / 2 / H)))
        keep = clip01(1 - (sum(disp) / len(disp)) / 0.15)
        arrangement = near * keep + (1 - near)
    else:
        arrangement = 1.0
    decor = [e for e in src["elements"] if is_decor(e) and e["width"] > 0 and e["height"] > 0]
    deco_t = 1.0
    if decor:
        vals = []
        for e in decor:
            o = oo[e["id"]]
            u0, v0 = (e["x"] + e["width"] / 2) / sw, (e["y"] + e["height"] / 2) / sh
            u1, v1 = (o["x"] + o["width"] / 2) / W, (o["y"] + o["height"] / 2) / H
            anchor = clip01(1 - math.hypot(min(max(u0, 0), 1) - u1, min(max(v0, 0), 1) - v1) / 0.5)
            cover = 0.0
            if hero is not None:
                hb = obox[hero]
                cover = inter(rot_bbox(o), hb) / max(area(hb), 1e-6)
            if logo is not None and inter(rot_bbox(o), obox[logo]) > 0:
                cover += 0.5
            vals.append(clip01(anchor - 1.5 * cover))
            if cover > 0.25:
                flags.append(f"decor {e['id']} over the product/logo ({cover:.2f})")
        deco_t = sum(vals) / len(vals)
    fidelity = 0.6 * arrangement + 0.4 * deco_t
    optical = 8 * center + 7 * grid + 5 * balance + 5 * fidelity

    # ================= 3) negative space & padding
    allb = [bout[k] for k in keys]
    if allb:
        u = union(allb)
        mg = [u[0], u[1], W - u[2], H - u[3]]
        edge = min(min(b[0], b[1], W - b[2], H - b[3]) for b in allb)
        margin = clip01(edge / (0.03 * short)) if edge >= 0 else 0.0
        if edge < 0.01 * short:
            flags.append("content touches the canvas edge")
        mbal = clip01(1 - (abs(mg[0] - mg[2]) / W + abs(mg[1] - mg[3]) / H) * 1.5)
        # clearance between unrelated blocks + rhythm of stacked gaps
        gaps = []
        for a, b in itertools.combinations(keys, 2):
            if (a, b) in layered:
                continue
            gaps.append(sep(bout[a], bout[b]))
        if gaps:
            worst = sorted(gaps)[:3]
            clear = sum(clip01(g / (0.02 * short)) for g in worst) / len(worst)
        else:
            clear = 1.0
        stack = sorted(keys, key=lambda k: bout[k][1])
        vg = []
        for a, b in zip(stack, stack[1:]):
            A, B = bout[a], bout[b]
            if min(A[2], B[2]) - max(A[0], B[0]) > 0 and B[1] >= A[3]:
                vg.append(B[1] - A[3])
        if len(vg) >= 2 and sum(vg) > 0:
            cv = float(np.std(vg) / max(np.mean(vg), 1e-6))
            rhythm = clip01(1 - (cv - 0.35) / 1.2)
        else:
            rhythm = 1.0
        breathing = 0.7 * clear + 0.3 * rhythm
        # fill: the union box and the occupied share of the canvas
        bbox_fill = area(u) / (W * H)
        g = np.zeros((60, 60), bool)
        for b in allb:
            x0, y0 = int(max(0, b[0]) / W * 60), int(max(0, b[1]) / H * 60)
            x1, y1 = int(math.ceil(min(W, b[2]) / W * 60)), int(math.ceil(min(H, b[3]) / H * 60))
            g[y0:y1, x0:x1] = True
        cov = float(g.mean())
        # occupancy vs the source's own: a sparse design may stay sparse, but
        # content shrunk into a corner of a big canvas (or a thumbnail in a
        # banner) reads as wasted space
        gs = np.zeros((60, 60), bool)
        for k in keys:
            b = bsrc[k]
            x0, y0 = int(max(0, b[0]) / sw * 60), int(max(0, b[1]) / sh * 60)
            x1, y1 = int(math.ceil(min(sw, b[2]) / sw * 60)), int(math.ceil(min(sh, b[3]) / sh * 60))
            gs[y0:y1, x0:x1] = True
        cov_src = max(float(gs.mean()), 1e-3)
        rho = cov / cov_src
        occupancy = clip01(rho / 0.85) ** 1.5 * (1 - 0.5 * clip01((cov - 0.75) / 0.2))
        if rho < 0.5:
            flags.append(f"content occupies {rho:.2f}x its source share of the canvas")
        fill = 0.3 * clip01(bbox_fill / 0.5) + 0.7 * occupancy
        metrics.update(coverage=round(cov, 3), occupancy=round(rho, 3), bbox_fill=round(bbox_fill, 3),
                       min_edge=round(edge, 1))
    else:
        margin = mbal = breathing = fill = occupancy = 1.0
    space = 5 * margin + 3 * mbal + 6 * breathing + 11 * fill

    # ================= 4) collision & readability
    overflow = []
    for e in texts:
        try:
            t = text_layout(e)
        except Exception:
            t = None
        if t is None:
            continue
        n, mw, ih, b = t
        if ih > e["height"] + max(2, 0.05 * e["height"]) or mw > e["width"] + 2:
            overflow.append(e["id"])
        elif b[2] > W + 1 or b[3] > H + 1 or b[0] < -1:
            overflow.append(e["id"] + "(ink off canvas)")
    for o in overflow:
        flags.append(f"CRITICAL text overflow: {o}")
    fit = 1 - min(1.0, len(overflow) / max(1, len(texts)) * 2)
    coll, t_img = [], []
    for a, b in itertools.combinations(cand, 2):
        if so[a].get("group_id") and so[a].get("group_id") == so[b].get("group_id"):
            continue
        if inter(sbox[a], sbox[b]) > 0.02 * min(area(sbox[a]), area(sbox[b])):
            continue
        if inter(obox[a], obox[b]) > 0.02 * max(1e-6, min(area(obox[a]), area(obox[b]))):
            coll.append(f"{a}x{b}")
            if {so[a]["type"], so[b]["type"]} == {"text", "image"}:
                t_img.append(f"{a}x{b}")
    for c in coll:
        flags.append(("CRITICAL text over image: " if c in t_img else "CRITICAL collision: ") + c)
    collide = clip01(1 - (len(coll) + len(t_img)) / 3)
    # legibility, judged in the canvas's real pixels (not the scaled-down PNG)
    fmax = max((e["font_size"] for e in s_texts), default=0)
    fmin = min((e["font_size"] for e in s_texts), default=0)
    leg = []
    tiny = 0
    for e in texts:
        f = int(e["font_size"])
        sf = so[e["id"]]["font_size"]
        need = 12.0 if sf >= fmax - 1e-6 else (7.0 if sf <= fmin + 1e-6 and fmax > 1.15 * fmin else 9.0)
        leg.append(clip01(f / need) ** 1.5)
        tiny += f < 6
    legible = (sum(leg) / len(leg)) * (1 - 0.15 * min(tiny, 3)) if leg else 1.0
    if tiny:
        flags.append(f"{tiny} text(s) under 6px")
    metrics["min_font_px"] = min((int(e["font_size"]) for e in texts), default=None)
    contrast = 1.0
    if no_text is not None and texts:
        from renderer.converter.colorutil import parse_rgba
        bg = np.asarray(no_text).astype(float)
        good = 0
        for e in texts:
            t = text_layout(e)
            if t is None:
                good += 1
                continue
            b = t[3]
            x0, y0 = int(max(0, b[0]) * scale), int(max(0, b[1]) * scale)
            x1, y1 = int(min(W, b[2]) * scale), int(min(H, b[3]) * scale)
            if x1 - x0 < 1 or y1 - y0 < 1:
                good += 1
                continue
            patch = bg[y0:y1, x0:x1].reshape(-1, 3)
            r_, g_, b_, a_ = parse_rgba(e["color"])
            med = np.median(patch, axis=0)
            fg = np.array([r_, g_, b_]) * a_ + med * (1 - a_)
            lf, lb = float(lum(fg[None])[0]), float(np.median(lum(patch)))
            cr = (max(lf, lb) + 0.05) / (min(lf, lb) + 0.05)
            good += clip01((cr - 1.5) / 3.0)
        contrast = good / len(texts)
    readability = 7 * fit + 6 * collide + 9 * legible + 3 * contrast

    # usability gate: a layout whose text can't be read, or whose content
    # shrank into a fraction of the canvas, fails as a whole, however tidy it is
    gate = math.sqrt(0.4 + 0.6 * legible) * math.sqrt(0.4 + 0.6 * occupancy)
    rub = {k: round(v * gate, 2) for k, v in (("hierarchy", hierarchy), ("optical", optical),
                                              ("space", space), ("readability", readability))}
    metrics.update(overflow=overflow, collisions=coll, legibility=round(legible, 3), gate=round(gate, 3))
    return {"total_score": round(sum(rub.values()), 2), "rubric": rub, "flags": flags, "metrics": metrics}


# ------------------------------------------------------------------ batch
def _judge_file(args):
    task_path, out_dir = args
    task = json.loads(Path(task_path).read_text())
    name = Path(task_path).stem
    j, p = Path(out_dir) / "json" / f"{name}.json", Path(out_dir) / "png" / f"{name}.png"
    rec = {"task": name, "tier": task.get("tier", "?"),
           "target": f'{task["target_canvas"]["width"]}x{task["target_canvas"]["height"]}'}
    if not j.exists():
        rec.update(total_score=0.0, rubric={}, flags=["CRITICAL crash: no output"], crash=True)
        return rec
    try:
        rec.update(judge(task, json.loads(j.read_text()), p))
    except Exception as ex:
        rec.update(total_score=0.0, rubric={}, flags=[f"CRITICAL judge error: {type(ex).__name__}: {ex}"],
                   judge_error=True)
    return rec


def judge_run(out_dir: Path, task_files, workers: int = 12) -> dict:
    """Judge every task of a run directory; writes judgement.jsonl and summary.json."""
    out_dir = Path(out_dir)
    jobs = [(str(t), str(out_dir)) for t in task_files]
    with ProcessPoolExecutor(workers) as ex:
        recs = list(ex.map(_judge_file, jobs, chunksize=2))
    with open(out_dir / "judgement.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    summary = {"aggregate": aggregate(recs), "tasks": len(recs)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def aggregate(recs) -> dict:
    def mean(v):
        v = list(v)
        return round(sum(v) / len(v), 3) if v else 0.0
    agg = {"mean_total": mean(r["total_score"] for r in recs)}
    for k in ("hierarchy", "optical", "space", "readability"):
        agg[k] = mean(r["rubric"].get(k, 0.0) for r in recs)
    for key in ("tier",):
        groups = {}
        for r in recs:
            groups.setdefault(r[key], []).append(r["total_score"])
        agg["by_" + key] = {g: mean(v) for g, v in sorted(groups.items())}
    by_ratio = {}
    for r in recs:
        by_ratio.setdefault(r["task"].split("__")[-1], []).append(r["total_score"])
    agg["by_target"] = {g: mean(v) for g, v in sorted(by_ratio.items())}
    fl = [f for r in recs for f in r.get("flags", [])]
    agg["crashes"] = sum(bool(r.get("crash")) for r in recs)
    agg["hard_violations"] = sum(f.startswith("CRITICAL hard-rule") for f in fl)
    agg["overflow"] = sum(f.startswith("CRITICAL text overflow") for f in fl)
    agg["collisions"] = sum(f.startswith("CRITICAL collision") or f.startswith("CRITICAL text over image")
                            for f in fl)
    agg["judge_errors"] = sum(bool(r.get("judge_error")) for r in recs)
    agg["critical_tasks"] = sum(any(f.startswith("CRITICAL") for f in r.get("flags", [])) for r in recs)
    return agg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--set", default="fixtures")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    from benchmarks.fixture_suite import tasks
    s = judge_run(a.run_dir, tasks(a.set), a.workers)
    print(json.dumps(s["aggregate"], indent=1))


if __name__ == "__main__":
    main()
