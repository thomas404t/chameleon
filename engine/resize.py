"""
CHAMELEON resize engine — "move the furniture, don't stretch the picture".

Approach (deterministic, no ML, runs offline):
  1. Classify each element:
       - background: covers ~the whole source canvas -> stretched to the full
         target canvas (images use fit=cover, so they crop, not distort)
       - decor: ungrouped, semi-transparent shapes, plus images whose asset
         file is missing -> keep their relative position, scale modestly
       - content: everything else
  2. Build "blocks" from content: elements sharing a group_id, or stacked on
     top of each other in the source, become one rigid block that scales
     uniformly (so a button's label stays inside its pill).
  3. Group blocks into "rows" (blocks that sit side by side in the source),
     in reading order.
  4. Try every way of splitting the rows, in order, into 1..K columns. For
     each split, binary-search the largest uniform scale `s` at which all
     columns fit inside the target canvas. Text that sits alone in its row is
     re-wrapped to the column width, using the same glyph measurement the
     official renderer uses, so its height is exact.
  5. Pick the split with the best score (largest scale, slight penalty per
     extra column), then distribute leftover space evenly.

Hard rules honoured: same ids, nothing added/removed, no pixel regeneration,
images keep aspect ratio (only the full-bleed background is stretched, and it
is fit=cover), font_family/content untouched, nothing off-canvas.
"""

from __future__ import annotations

import copy
import itertools
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

ASSETS_DIR = Path("brand_kit/assets")
FONTS_DIR = Path("brand_kit/fonts")
DEFAULT_LINE_HEIGHT = 1.2  # matches the schema default

# Images whose asset file is missing render as a grey "missing asset" box.
# False (default): keep it visible, small, at its relative source position.
# True: also set its opacity to 0 so the placeholder doesn't show. The id and
# geometry are still returned, so nothing is removed from the output.
HIDE_MISSING_ASSETS = False

# Creative control. Defaults follow brand-ad conventions (Apple / McDonald's
# masthead: logo centered on top; otherwise logo top-left per the Gutenberg
# reading pattern; CTA last in reading order). Override per task by passing
# target_canvas["style"] = {...} with any of these keys.
STYLE = {
    "logo_position": "auto",  # "auto" (center if possible, else left) | "center" | "left"
    "symmetry": 0.6,          # 0 = ignore, 1 = strongly prefer mirror-symmetric layouts
    "brand_weight": 0.3,      # how much these brand rules weigh vs. the base objective
    "center_rows": True,      # single-column: center every row (symmetry) vs keep source x-anchoring
    "text_align": "follow_layout",  # "follow_layout": alignment matches placement | "keep_source"
}

# Layout heuristics as one tunable genome. The overnight tournament
# (evolution/) mutates the block between the markers; these values are the
# tournament's best so far. Strategy genes default to "off" (0) so each one
# only changes a layout when it has earned its place.
# >>> GENOME
GENOME = {
    'margin': 0.0398,
    'gap': 0.0227,
    'pad_y': 3.0029,
    'body_gap_cap': 1.0732,
    'col_gap': 0.01,
    'button_gap': 0.3,
    'text_gap': 1.0,
    'type_gamma': 0.6,
    'type_k': 6.0,
    'priority_boost': 1.6,
    'min_font': 11.0,
    'min_font_frac': 0.09,
    'hero_floor': 0.6027,
    'image_floor': 0.3,
    'logo_area': 0.0307,
    'w_leg': 0.22,
    'w_size': 0.18,
    'w_hero': 0.1177,
    'w_order': 0.1309,
    'w_struct': 0.12,
    'w_bal': 0.09,
    'w_fill': 0.0359,
    'col_penalty': 0.94,
    'brand_weight': 0.3,
    'symmetry': 0.6819,
    'focus_blend': 0.5,
    'optical_lift': 0.0,
    'hero_mass': 0.0,
    'ink_v': 0.0,
    'shortcut_ink': 1.0,
    'split_bias': -0.7983,
    'zone_top_align': 0.2017,
    'zone_header': 1.0,
    'banner_spread': 0.6983,
    'panel_reflow': 1.0,
    'headline_lead': 1.0,
    'relax_iters': 10,
    'relax_clear': 0.0192,
    'relax_spring': 0.0,
}
# <<< GENOME

# Target logo footprint as a share of canvas area (see _logo_scale).
LOGO_AREA = GENOME["logo_area"]
# Minimum visible logo height (px), so the mark stays legible on small canvases.
LOGO_MIN_PX = 22.0

# Type-scale compression (see _bscale): exponent and boost ceiling.
TYPE_GAMMA = GENOME["type_gamma"]
TYPE_K = GENOME["type_k"]

# Minimum spacing between neighbouring buttons / badges, as a share of their height.
BUTTON_GAP = GENOME["button_gap"]
# Short banners: extra type scale for the headline and call to action
PRIORITY_BOOST = GENOME["priority_boost"]
STYLE["brand_weight"] = GENOME["brand_weight"]
STYLE["symmetry"] = GENOME["symmetry"]

# --------------------------------------------------------------------------
# Text measurement (uses the renderer's own wrapping so heights are exact)
# --------------------------------------------------------------------------

_fm = None
_measure_ok = None


def _font_manager():
    global _fm, _measure_ok
    if _measure_ok is None:
        try:
            from renderer.converter.brand_fonts import load_brand_font_manager

            _fm = load_brand_font_manager(str(FONTS_DIR))
            _measure_ok = True
        except Exception:
            _measure_ok = False
    return _fm if _measure_ok else None


def _style(el: dict) -> tuple[str, str]:
    bold = (el.get("font_weight") or "regular").lower() == "bold"
    italic = el.get("font_style") == "italic"
    weight = "bold" if bold else "regular"
    style = "bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular"
    return weight, style


@lru_cache(maxsize=50_000)
def _char_widths(content: str, family: str, weight: str, style: str, size: float) -> tuple:
    """Glyph advances at one size (the renderer's own measurement), cached:
    the solver re-wraps the same text at the same size for many widths."""
    from renderer.converter.wrap import char_widths

    return tuple(char_widths(_font_manager(), family, weight, style, size, content))


@lru_cache(maxsize=200_000)
def _measure(content: str, family: str, weight: str, style: str, size: float, width: float):
    """Return (n_lines, widest_word_px) when `content` is wrapped to `width`."""
    fm = _font_manager()
    if fm is not None:
        from renderer.converter.wrap import wrap_text

        widths = list(_char_widths(content, family, weight, style, size))
        wrapped = wrap_text(content, widths, width)
    else:  # fallback estimate if the renderer isn't importable
        widths = [size * 0.56] * len(content)
        wrapped, line, last = list(content), 0.0, None
        for i, c in enumerate(wrapped):
            if line + widths[i] > width and last is not None:
                wrapped[last] = "\n"
                line = sum(widths[last + 1 : i + 1])
                last = None
            else:
                line += widths[i]
            if c == " ":
                last = i
        wrapped = "".join(wrapped)
    # widest single word (a word wider than the box overflows, so we must avoid it)
    widest, cur = 0.0, 0.0
    for i, c in enumerate(content):
        if c in " \n":
            widest, cur = max(widest, cur), 0.0
        else:
            cur += widths[i] if i < len(widths) else size * 0.56
    widest = max(widest, cur)
    return wrapped.count("\n") + 1, widest


def _fits_box(el: dict, font_size: float, width: float, height: float) -> bool:
    """Does `el` at this size, wrapped to `width`, stay inside `height`?"""
    if el.get("rotation") or not el.get("content"):
        return True
    need, fits = text_box_height(el, font_size, width)
    return fits and need <= height * 1.02 + 0.5


@lru_cache(maxsize=64)
def _font_extent(family: str, weight: str, style: str) -> float:
    """Height of one line of ink (descent - ascent) per px of font size."""
    try:
        import skia

        font = skia.Font(skia.Typeface.MakeFromData(_font_manager().lookup(family, weight, style)), 100)
        m = font.getMetrics()
        return (m.fDescent - m.fAscent) / 100
    except Exception:
        return 1.25


def _ink_fits(el: dict, font_size: float, width: float, height: float) -> bool:
    """Does the text as the renderer draws it (wrapped at the float size,
    glyphs at int(size)) stay inside its box, within 4%? A designer's box is
    often drawn a little tighter than the line-height maths, but what counts
    is the ink."""
    if el.get("rotation") or not el.get("content"):
        return True
    if width <= 0 or height <= 0:
        return False
    weight, style = _style(el)
    lines, widest = _measure(el["content"], el["font_family"], weight, style, font_size, width)
    lh = el.get("line_height", DEFAULT_LINE_HEIGHT) or DEFAULT_LINE_HEIGHT
    ink = (lines - 1) * font_size * lh + int(font_size) * _font_extent(el["font_family"], weight, style)
    return widest <= width and ink <= height * 1.04


def text_box_height(el: dict, font_size: float, width: float) -> tuple[float, bool]:
    """Height needed for `el` at `font_size` wrapped to `width`; bool = fits horizontally."""
    weight, style = _style(el)
    # Measure wrapping against a slightly narrower width than we output, so the
    # box is never shorter than what the renderer actually draws (glyph
    # hinting at small sizes can add a line break we didn't predict).
    lines, widest = _measure(
        el["content"], el["font_family"], weight, style, round(font_size, 2), round(width * 0.92, 1)
    )
    if lines > 1:
        # the renderer wraps with these same glyph widths at the exact output
        # size (rounded down to 2 decimals): measured that way with a 3% margin,
        # a line that only wrapped because of the 8% safety margin doesn't
        # reserve an empty extra line
        l2, w2 = _measure(el["content"], el["font_family"], weight, style,
                          math.floor(font_size * 100 + 1e-9) / 100, round(width * 0.97, 1))
        if l2 < lines and w2 <= width * 0.97:
            lines = l2
    lh = el.get("line_height", DEFAULT_LINE_HEIGHT)
    return lines * font_size * lh + font_size * 0.3, widest <= width * 0.92 + 0.5


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------


def _rot_bbox(el: dict) -> tuple[float, float, float, float]:
    """Axis-aligned bbox of the element including rotation (union with the unrotated box)."""
    x, y, w, h = el["x"], el["y"], el["width"], el["height"]
    r = math.radians(el.get("rotation", 0) or 0)
    if not r:
        return x, y, x + w, y + h
    cx, cy = x + w / 2, y + h / 2
    rw = abs(w * math.cos(r)) + abs(h * math.sin(r))
    rh = abs(w * math.sin(r)) + abs(h * math.cos(r))
    return min(x, cx - rw / 2), min(y, cy - rh / 2), max(x + w, cx + rw / 2), max(y + h, cy + rh / 2)


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _overlap_frac(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return ix * iy / small


@lru_cache(maxsize=256)
def _alpha_bounds(asset_id: str | None):
    """Visible (non-transparent) bounds of an asset as fractions of its size,
    plus its aspect ratio. Falls back to the full image when unknown."""
    try:
        from PIL import Image

        with Image.open(ASSETS_DIR / asset_id) as im:
            iw, ih = im.size
            bb = im.getchannel("A").getbbox() if "A" in im.getbands() else None
            bb = bb or (0, 0, iw, ih)
            return (bb[0] / iw, bb[1] / ih, bb[2] / iw, bb[3] / ih, iw / ih, iw, ih)
    except Exception:
        return None


def _visible_box(el: dict):
    """Where an image's visible pixels land inside its element box (fit=contain)."""
    ab = _alpha_bounds(el.get("asset_id")) if el["type"] == "image" else None
    x, y, w, h = el["x"], el["y"], el["width"], el["height"]
    if ab is None or el.get("fit") == "cover" or w <= 0 or h <= 0:
        return (x, y, x + w, y + h)
    fx0, fy0, fx1, fy1, ar = ab[:5]
    dw, dh = (w, w / ar) if w / h < ar else (h * ar, h)  # drawn (letterboxed) size
    ox, oy = x + (w - dw) / 2, y + (h - dh) / 2
    return (ox + fx0 * dw, oy + fy0 * dh, ox + fx1 * dw, oy + fy1 * dh)


def _asset_exists(asset_id: str | None) -> bool:
    if not asset_id:
        return False
    if not ASSETS_DIR.exists():  # can't tell -> assume present
        return True
    return (ASSETS_DIR / asset_id).exists()


# --------------------------------------------------------------------------
# Classification and blocks
# --------------------------------------------------------------------------


def _classify(el: dict, cw: float, ch: float) -> str:
    x0, y0, x1, y1 = el["x"], el["y"], el["x"] + el["width"], el["y"] + el["height"]
    cover = max(0, min(x1, cw) - max(x0, 0)) * max(0, min(y1, ch) - max(y0, 0)) / (cw * ch)
    if el["type"] in ("image", "shape") and cover >= 0.9 and not el.get("group_id"):
        return "background"
    if el["type"] == "image" and not _asset_exists(el.get("asset_id")):
        return "decor"
    if el["type"] == "shape" and not el.get("group_id") and (el.get("opacity", 1) or 0) <= 0.5:
        return "decor"
    return "content"


class Block:
    def __init__(self, elements: list[dict]):
        self.elements = elements
        self.box = _union([_rot_bbox(e) for e in elements])
        self._ink = {}

    def ink_x(self, k: float = 1.0) -> tuple[float, float]:
        """Horizontal extent of what is actually drawn (visible image pixels,
        text ink), in source px, for the block drawn at scale k (tiny type
        doesn't shrink linearly, so text is measured at its real size).
        Transparent padding is left out."""
        key = round(k, 3)
        if key not in self._ink:
            spans = []
            for e in self.elements:
                if e["type"] == "image" and not e.get("rotation"):
                    vb = _visible_box(e)
                    spans.append((vb[0], vb[2]))
                elif e["type"] == "text" and not e.get("rotation"):
                    try:
                        sc = dict(e, font_size=e["font_size"] * key, width=e["width"] * key)
                        tw = min(_content_width(sc) / key, e["width"])
                    except Exception:
                        tw = e["width"]
                    a = e.get("text_align", "left")
                    x0 = e["x"] if a == "left" else e["x"] + e["width"] - tw if a == "right" \
                        else e["x"] + (e["width"] - tw) / 2
                    pad = 0.04 * tw  # small type doesn't scale perfectly linearly
                    spans.append((max(e["x"], x0 - pad), min(e["x"] + e["width"], x0 + tw + pad)))
                else:
                    b = _rot_bbox(e)
                    spans.append((b[0], b[2]))
            self._ink[key] = (min(a for a, _ in spans), max(b for _, b in spans))
        return self._ink[key]

    def ink_y(self) -> tuple[float, float]:
        """Vertical extent of what is drawn (visible image pixels; other
        elements by their box), in source px."""
        if not hasattr(self, "_iy"):
            spans = []
            for e in self.elements:
                b = _visible_box(e) if e["type"] == "image" and not e.get("rotation") else _rot_bbox(e)
                spans.append((b[1], b[3]))
            self._iy = (min(a for a, _ in spans), max(b for _, b in spans))
        return self._iy

    @property
    def w(self):
        return self.box[2] - self.box[0]

    @property
    def h(self):
        return self.box[3] - self.box[1]

    @property
    def cx(self):
        return (self.box[0] + self.box[2]) / 2

    @property
    def flexible_text(self) -> bool:
        e = self.elements
        return len(e) == 1 and e[0]["type"] == "text" and not e[0].get("rotation")


def _build_blocks(content: list[dict]) -> list[Block]:
    # 1) group_id unions
    groups: dict[str, list[dict]] = {}
    singles: list[list[dict]] = []
    for el in content:
        gid = el.get("group_id")
        if gid:
            groups.setdefault(gid, []).append(el)
        else:
            singles.append([el])
    clusters = list(groups.values()) + singles
    # 2) merge clusters that are stacked on each other in the source (layered art)
    merged = True
    while merged:
        merged = False
        for i, j in itertools.combinations(range(len(clusters)), 2):
            a = _union([_rot_bbox(e) for e in clusters[i]])
            b = _union([_rot_bbox(e) for e in clusters[j]])
            if _overlap_frac(a, b) > 0.5:
                clusters[i] = clusters[i] + clusters[j]
                del clusters[j]
                merged = True
                break
    # 3) captions: a short text right under (or over) exactly one image travels
    #    with it, so an option's label can never be separated from its picture
    merged = True
    while merged:
        merged = False
        for i, c in enumerate(clusters):
            if len(c) != 1 or c[0]["type"] != "text" or c[0].get("group_id"):
                continue
            hits = [j for j, d in enumerate(clusters)
                    if j != i and any(e["type"] == "image" and _is_caption(c[0], e) for e in d)]
            if len(hits) == 1:
                clusters[hits[0]] = clusters[hits[0]] + c
                del clusters[i]
                merged = True
                break
    return [Block(c) for c in clusters]


def _is_caption(t: dict, img: dict) -> bool:
    if "logo" in (img.get("asset_id") or "").lower() or len(t["content"].split()) > 6:
        return False
    if img["width"] <= 0 or img["height"] <= 0 or t["width"] > 1.25 * img["width"]:
        return False
    ov = min(t["x"] + t["width"], img["x"] + img["width"]) - max(t["x"], img["x"])
    if ov < 0.6 * t["width"]:
        return False
    below = t["y"] - (img["y"] + img["height"])
    above = img["y"] - (t["y"] + t["height"])
    g = below if below >= -0.1 * t["height"] else above
    return -0.1 * t["height"] <= g <= 0.3 * img["height"]


def _build_rows(blocks: list[Block]) -> list[list[Block]]:
    blocks = sorted(blocks, key=lambda b: (b.box[1] + b.box[3]) / 2)
    rows: list[list[Block]] = []
    row_box = None
    for b in blocks:
        if rows:
            iy = min(row_box[3], b.box[3]) - max(row_box[1], b.box[1])
            # side-by-side only if they overlap vertically AND don't overlap
            # horizontally with anything already in the row (else: stacked)
            x_clash = any(
                min(o.box[2], b.box[2]) - max(o.box[0], b.box[0]) > 0.1 * min(o.w, b.w)
                for o in rows[-1]
            )
            if iy > 0.3 * min(b.h, row_box[3] - row_box[1]) and not x_clash:
                rows[-1].append(b)
                row_box = (row_box[0], min(row_box[1], b.box[1]), row_box[2], max(row_box[3], b.box[3]))
                continue
        rows.append([b])
        row_box = b.box
    for r in rows:
        r.sort(key=lambda b: b.box[0])
    return rows


# --------------------------------------------------------------------------
# Layout solver
# --------------------------------------------------------------------------


class Ctx:
    def __init__(self, src_w, src_h, W, H):
        self.src_w, self.src_h, self.W, self.H = src_w, src_h, W, H
        self.margin = max(3.0, GENOME["margin"] * min(W, H))
        self.gap_src = GENOME["gap"] * min(src_w, src_h)  # base gap in source px
        # legibility floor for text (px). Small canvases get a smaller floor.
        self.min_font = min(GENOME["min_font"], max(7.0, GENOME["min_font_frac"] * min(W, H)))
        self.f_ref = 12.0  # set from the design's smallest font in resize()
        self.hero_id = None  # largest content image, set in resize()
        self.center_rows = True
        self.logo_id = None
        self.logo_left = False
        self.side_align = True
        self.canvas_ratio = W / H  # the full target's shape (bodies under a masthead keep it)
        self.banner = W / H >= 3 and H <= 250  # short, wide ad banners
        self.fine_ids: set = set()  # fine print (legal lines), set in resize()
        # short banners can't fit every line legibly: the headline and the call
        # to action carry the ad, so they take size first (set in resize())
        self.priority_ids: set = set()

    def font_floor(self, f: float) -> float:
        """Minimum rendered size for a source font of size f. Grows with f
        so the type hierarchy (headline > subhead > legal) is preserved."""
        return min(f, self.min_font * (f / self.f_ref) ** 0.6)


def _bscale(b: "Block", s: float, ctx: "Ctx") -> float:
    """Per-block scale: uniform `s`, boosted (up to 3.5x) when the block would
    become unreadable: text below the legibility floor, or an image whose
    short side drops below ~30% of the canvas's short side. Never boosted
    above the element's source size."""
    need = s
    fonts = [e["font_size"] for e in b.elements if e["type"] == "text"]
    if fonts:
        need = max(need, max(ctx.font_floor(f) / f for f in fonts))
    imgs = [e for e in b.elements if e["type"] == "image"]
    if imgs:
        short = min(min(e["width"], e["height"]) for e in imgs)
        if short > 0:
            is_hero = any(e["id"] == ctx.hero_id for e in imgs)
            floor = (GENOME["hero_floor"] if is_hero else GENOME["image_floor"]) * min(ctx.W, ctx.H)
            need = max(need, min(floor, short) / short)
        if any(e["id"] == ctx.logo_id for e in imgs):
            need = max(need, _logo_scale(b, ctx))
    # a boost must never make the block itself too big for the canvas; blocks
    # in the same row (siblings, e.g. a set of badges) share the tightest cap
    # so they stay visually consistent with each other
    fit_cap = getattr(b, "row_cap", None) or _fit_cap(b, ctx)
    if ctx.logo_id is not None and any(e["id"] == ctx.logo_id for e in b.elements):
        return min(_logo_scale(b, ctx), fit_cap * 0.98)
    if fonts:
        # Type-scale compression: under space pressure, rendered size grows
        # like f^0.6 rather than f, so big type yields room to small type.
        # Still strictly increasing in f -> hierarchy order is preserved.
        fmin = min(fonts)
        cap = TYPE_K * s * (ctx.f_ref / fmin) ** (1 - TYPE_GAMMA)
        if any(e["id"] in ctx.priority_ids for e in b.elements):
            cap *= PRIORITY_BOOST
    else:
        cap = s * 3.5
    return max(s, min(need, cap, fit_cap * 0.98))


def _logo_scale(b: "Block", ctx: "Ctx") -> float:
    """Logo scale. On a canvas shaped like the source the logo keeps its source
    footprint relative to the canvas (area ratio), so it stays clearly
    identifiable without being overdone. As the shape changes a lot (wide or
    short banners) it blends toward a banner-spec footprint of ~LOGO_AREA of
    the canvas (e.g. 138x40 on 728x90). Never upscaled past the asset file."""
    vb = _visible_box(b.elements[0])
    vw, vh = max(vb[2] - vb[0], 1e-6), max(vb[3] - vb[1], 1e-6)
    bw, bh = max(b.w, 1e-6), max(b.h, 1e-6)
    FW, FH = getattr(ctx, "full", (ctx.W, ctx.H))  # inside a zone: the whole canvas
    rel = math.sqrt(FW * FH / (ctx.src_w * ctx.src_h))
    spec = math.sqrt(LOGO_AREA * FW * FH / (vw * vh))
    shape_change = abs(math.log2((FW / FH) / (ctx.src_w / ctx.src_h)))
    t = min(1.0, max(0.0, shape_change - 1.0))  # 0 up to a 2x ratio change, 1 from 4x
    a = rel ** (1 - t) * spec ** t
    a = max(a, LOGO_MIN_PX / vh)  # small canvases: the mark must stay legible
    ab = _alpha_bounds(b.elements[0].get("asset_id"))
    native = min(ab[5] / bw, ab[6] / bh) if ab else 1.0
    return min(a, 0.6 * ctx.W / vw, 0.45 * ctx.H / vh,
               (ctx.W - 2 * ctx.margin) / bw, (ctx.H - 2 * ctx.margin) / bh, max(1.0, native))


def _fit_cap(b: "Block", ctx: "Ctx") -> float:
    if b.flexible_text:  # re-wraps to any width, so its source box is irrelevant
        return math.inf
    return min((ctx.W - 2 * ctx.margin) / max(b.w, 1e-6), (ctx.H - 2 * ctx.margin) / max(b.h, 1e-6))


def _has_shape(row) -> bool:
    return any(e["type"] == "shape" for b in row for e in b.elements)


def _is_peer_row(row) -> bool:
    """A row of like items (a set of options, a badge strip): same make-up and
    similar size. Its horizontal arrangement is a design choice to keep."""
    if len(row) < 2:
        return False
    if len({tuple(sorted(e["type"] for e in b.elements)) for b in row}) != 1:
        return False
    ws, hs = [b.w for b in row], [b.h for b in row]
    return min(ws) > 0 and min(hs) > 0 and max(ws) <= 1.6 * min(ws) and max(hs) <= 1.35 * min(hs)


def _is_ink_row(row) -> bool:
    """Rows of fixed art pack by what is drawn: a peer row (product shots +
    labels, badges) or a lone image (logo, product shot)."""
    if any(b.flexible_text for b in row):
        return False
    if len(row) == 1:
        return any(e["type"] == "image" for e in row[0].elements)
    return _is_peer_row(row)


def _ink_sizes(row, sizes, ctx, edges=(True, True)):
    """Slot widths for an ink row: each item's drawn width, so transparent
    padding doesn't eat the layout (padding may overlap neighbours, unseen).
    The outer items keep just enough padding that their boxes stay inside the
    page margin, on the sides where the column meets the canvas edge
    (`edges` = left, right). Returns (sizes, left pads, source gap between drawn items)."""
    out, pads, src_gaps = [], [], []
    for i, (b, (w, h)) in enumerate(zip(row, sizes)):
        k = w / max(b.w, 1e-6)
        i0, i1 = b.ink_x(k)
        pl = max(0.0, (i0 - b.box[0]) * k - ctx.margin) if i == 0 and edges[0] else 0.0
        pr = max(0.0, (b.box[2] - i1) * k - ctx.margin) if i == len(row) - 1 and edges[1] else 0.0
        sw = (i1 - i0) * k + pl + pr
        if sw >= w:
            sw, pl = w, (i0 - b.box[0]) * k
        out.append((sw, h))
        pads.append(pl)
        if i:
            src_gaps.append((i0 - row[i - 1].ink_x(k)[1]) * k)
    return out, pads, (sorted(src_gaps)[len(src_gaps) // 2] if src_gaps else 0.0)


def _vtrim(b: "Block", h: float, ctx) -> tuple[float, float]:
    """How much of a picture's transparent top / bottom padding (at height h)
    its row may let overlap the neighbours: at most a margin each, so the
    padded box always stays on the canvas."""
    if not GENOME["ink_v"] or b.flexible_text or not any(e["type"] == "image" for e in b.elements):
        return 0.0, 0.0
    k = h / max(b.h, 1e-6)
    y0, y1 = b.ink_y()
    return min(max(0.0, (y0 - b.box[1]) * k), ctx.margin), min(max(0.0, (b.box[3] - y1) * k), ctx.margin)


def _is_text_row(row) -> bool:
    return len(row) == 1 and row[0].flexible_text


def _first_text_row(col) -> int:
    return next((i for i, row in enumerate(col) if _is_text_row(row)), -1)


def _is_fine_row(row, ctx) -> bool:
    return len(row) == 1 and row[0].flexible_text and row[0].elements[0]["id"] in ctx.fine_ids


def _col_gaps(col, row_ms, gap: float, ctx) -> list[float]:
    """Vertical gap below each row of a column (len = rows - 1). Buttons and
    badges get breathing room; fine print sits clearly apart from what's above."""
    gaps = []
    for i in range(1, len(col)):
        g = gap
        if _is_text_row(col[i - 1]) and _is_text_row(col[i]):
            # copy reads as one cluster: headline + subhead sit closer (or apart)
            g = gap * GENOME["text_gap"] * (GENOME["headline_lead"] if i == _first_text_row(col) + 1 else 1.0)
        if _has_shape(col[i - 1]) and _has_shape(col[i]):
            g = max(g, BUTTON_GAP * min(row_ms[i - 1][1], row_ms[i][1]))
        if _is_fine_row(col[i], ctx):
            g = max(g, ctx.margin)
        gaps.append(g)
    return gaps


def _row_metrics(row: list[Block], s: float, col_w: float, ctx: Ctx, edges=(True, True)):
    """Return (row_width, row_height, item_sizes, meta) or None if it can't fit
    col_w. meta: gap between items, "mult" (extra shrink for peer rows),
    "stacked" (items placed vertically)."""
    gap = ctx.gap_src * s
    if len(row) == 1 and row[0].flexible_text:
        el = row[0].elements[0]
        bs = _bscale(row[0], s, ctx)
        fs = el["font_size"] * bs
        # don't let a lone line of text become absurdly wide relative to its source
        w = min(col_w, max(el["width"] * bs * 1.8, 1.0))
        h, fits = text_box_height(el, fs, w)
        if not fits:
            return None
        return w, h, [(w, h)], {"gap": gap}
    sizes = []
    for b in row:
        bs = _bscale(b, s, ctx)
        if b.flexible_text:
            el = b.elements[0]
            w = el["width"] * bs
            h, fits = text_box_height(el, el["font_size"] * bs, w)
            if not fits:
                return None
            sizes.append((w, max(h, b.h * bs)))
        else:
            sizes.append((b.w * bs, b.h * bs))
    n = len(row)
    btn = n > 1 and _has_shape(row)
    hmin = min(h for _, h in sizes)
    rgap = max(gap, BUTTON_GAP * hmin) if btn else gap
    ink = _is_ink_row(row)
    if ink:
        sizes, pads, sgap = _ink_sizes(row, sizes, ctx, edges)
        # like items keep their source spacing while it fits; the spacing gives
        # way (down to the base gap) before the items themselves shrink
        if n > 1:
            rgap = max(rgap, min(sgap, (col_w - sum(w for w, _ in sizes)) / (n - 1)))
    rw = sum(w for w, _ in sizes) + rgap * (n - 1)
    if rw <= col_w + 0.5:
        vt = [_vtrim(b, h, ctx) for b, (_, h) in zip(row, sizes)] if ink else None
        rh = max(h - (vt[i][0] + vt[i][1] if vt else 0.0) for i, (_, h) in enumerate(sizes))
        return rw, rh, sizes, {"gap": rgap, "pads": pads if ink else None, "vtrim": vt}
    # Too wide side by side. Like items (options, badges) keep their side-by-side
    # arrangement, shrinking to fit, as long as their text stays readable;
    # otherwise they wrap into the widest grid that is. Other rows just stack.
    shaped = _has_shape(row)
    for c in (range(n, 0, -1) if _is_peer_row(row) else [1]):
        if c == 1:
            f = 1.0
            if max(w for w, _ in sizes) > col_w + 0.5:
                return None
        else:
            widest = max(sum(w for w, _ in sizes[i:i + c]) for i in range(0, n, c))
            f = 1.0
            for _ in range(3):
                g = max(gap, BUTTON_GAP * hmin * f) if shaped else gap
                f = min(1.0, (col_w - g * (c - 1)) / widest)
            if f < 0.25 or not _row_legible(row, s, f, ctx):
                continue
        g = max(gap, BUTTON_GAP * hmin * f) if shaped else gap
        sz = [(w * f, h * f) for w, h in sizes]
        vt = [(a * f, z * f) for a, z in (_vtrim(b, h, ctx) for b, (_, h) in zip(row, sizes))] if ink else None
        eff = [h - (vt[i][0] + vt[i][1] if vt else 0.0) for i, (_, h) in enumerate(sz)]
        lines = [list(range(i, min(i + c, n))) for i in range(0, n, c)]
        gw = max(sum(sz[j][0] for j in ln) + g * (len(ln) - 1) for ln in lines)
        gh = sum(max(eff[j] for j in ln) for ln in lines) + g * (len(lines) - 1)
        return gw, gh, sz, {"gap": g, "mult": f, "grid": c,
                            "pads": [p * f for p in pads] if ink else None, "vtrim": vt}
    return None


def _row_legible(row, s: float, mult: float, ctx) -> bool:
    """Would every text in the row stay near its readable floor at this shrink?"""
    for b in row:
        bs = _bscale(b, s, ctx) * mult
        for e in b.elements:
            if e["type"] == "text" and e["font_size"] * bs < 0.85 * ctx.font_floor(e["font_size"]):
                return False
    return True


def _preferred_col_width(col: list[list[Block]], s: float, ctx: Ctx, edges=(True, True)) -> tuple[float, float]:
    """(min_width, preferred_width) for a column at scale s."""
    gap = ctx.gap_src * s
    mins, prefs = [], []
    for row in col:
        if len(row) == 1 and row[0].flexible_text:
            el = row[0].elements[0]
            bs = _bscale(row[0], s, ctx)
            _, widest = _measure(el["content"], el["font_family"], *_style(el),
                                 round(el["font_size"] * bs, 2), 1e9)
            mins.append(widest + 1)
            prefs.append(el["width"] * bs)
        else:
            ws = [(b.elements[0]["width"] if b.flexible_text else b.w) * _bscale(b, s, ctx) for b in row]
            g = gap
            if _is_ink_row(row):
                sz, _, sgap = _ink_sizes(row, [(w, 0.0) for w in ws], ctx, edges)
                ws, g = [w for w, _ in sz], max(gap, sgap)
            mins.append(max(ws))
            prefs.append(sum(ws) + g * (len(row) - 1))
    return max(mins), max(max(prefs), max(mins))


def _try_layout(columns: list[list[list[Block]]], s: float, ctx: Ctx):
    """Return a layout plan dict or None if infeasible at scale s."""
    AW = ctx.W - 2 * ctx.margin
    AH = ctx.H - 2 * ctx.margin
    gap = ctx.gap_src * s
    k = len(columns)
    col_gap = max(gap * 2, GENOME["col_gap"] * ctx.W)
    usable = AW - col_gap * (k - 1)
    if usable <= 0:
        return None
    edges = [(i == 0, i == k - 1) for i in range(k)]  # which columns meet the canvas sides
    mp = [_preferred_col_width(c, s, ctx, e) for c, e in zip(columns, edges)]
    mins = [m for m, _ in mp]
    prefs = [p for _, p in mp]
    if sum(mins) > usable + 0.5:
        return None
    if sum(prefs) <= usable:
        widths = prefs[:]
        # give spare width to text-bearing columns (fewer wrapped lines), up to 1.5x
        spare = usable - sum(widths)
        has_text = [any(len(r) == 1 and r[0].flexible_text for r in c) for c in columns]
        tot = sum(p for p, t in zip(prefs, has_text) if t)
        if tot > 0:
            for i in range(k):
                if has_text[i]:
                    widths[i] = min(prefs[i] * 1.5, widths[i] + spare * prefs[i] / tot)
        if k == 1:
            widths[0] = usable
    else:
        flex = sum(p - m for p, m in zip(prefs, mins)) or 1.0
        short = sum(prefs) - usable
        widths = [p - (p - m) * short / flex for p, m in zip(prefs, mins)]

    def fit(col, cw, e):
        rows = []
        for row in col:
            m = _row_metrics(row, s, cw, ctx, e)
            if m is None:
                return None
            rows.append(m)
        total = sum(m[1] for m in rows) + sum(_col_gaps(col, rows, gap, ctx))
        return (cw, rows, total) if total <= AH + 0.5 else None

    plan_cols = [fit(col, cw, e) for col, cw, e in zip(columns, widths, edges)]
    # (skipped on very crowded designs, where it would cost too much time)
    if None in plan_cols and k > 1 and sum(len(r) for c in columns for r in c) <= 20:
        # width negotiation: a column too tall at its share (long copy) takes
        # width from columns that don't need it. Each column gets the narrowest
        # width at which it fits the height; the spare is shared out as before.
        need = []
        for col, m, e, pc, cw in zip(columns, mins, edges, plan_cols, widths):
            lo_w, hi_w = m, usable - (sum(mins) - m)
            if pc is not None and not any(b.flexible_text for row in col for b in row):
                need.append(cw)  # fixed art that fits: keep its share
                continue
            if fit(col, lo_w, e) is not None:
                need.append(lo_w)
                continue
            if hi_w <= lo_w or fit(col, hi_w, e) is None:
                return None
            for _ in range(5):
                mid = (lo_w + hi_w) / 2
                if fit(col, mid, e) is not None:
                    hi_w = mid
                else:
                    lo_w = mid
            need.append(hi_w)
        spare = usable - sum(need)
        if spare < -0.5:
            return None
        tot = sum(prefs) or 1.0
        widths = [n + max(0.0, spare) * p / tot for n, p in zip(need, prefs)]
        plan_cols = [fit(col, cw, e) for col, cw, e in zip(columns, widths, edges)]
    if None in plan_cols:
        return None
    return {"s": s, "widths": widths, "cols": plan_cols, "col_gap": col_gap}


def _partitions(n: int, k: int):
    for cuts in itertools.combinations(range(1, n), k - 1):
        bounds = (0,) + cuts + (n,)
        yield [(bounds[i], bounds[i + 1]) for i in range(k)]


def _is_copy_row(row, f_ref) -> bool:
    return any(b.flexible_text and b.elements[0]["font_size"] > 1.15 * f_ref for b in row)


def _candidates(rows, k, f_ref=0.0, hero_id=None):
    """Column arrangements to try: every in-order split of the rows into k
    columns, plus 'rebalanced' variants where the last 1-3 rows of the final
    column (typically badges / CTA / legal) move under the first column
    (typically logo + hero image) -- the classic 'visual + CTA | copy' ad
    layout that pure reading-order splits can never produce."""
    for part in _partitions(len(rows), k):
        cols = [rows[a:b] for a, b in part]
        yield cols
        hero_first = hero_id is not None and any(
            e["id"] == hero_id for r in cols[0] for b in r for e in b.elements)
        if k >= 2 and hero_first:  # only tuck rows under the product, not under a lone logo
            last = cols[-1]
            for j in range(1, min(3, len(last) - 1) + 1):
                if any(_is_copy_row(r, f_ref) for r in last[-j:]):
                    break  # never split body copy away from its headline
                yield [cols[0] + last[-j:]] + cols[1:-1] + [last[:-j]]


def _solve(rows, ctx: Ctx, content=None, want_all=False):
    """Stage 1: for every candidate arrangement, binary-search the largest
    feasible scale (cheap). Stage 2: fully place the most promising
    candidates and re-rank them with a design-quality objective."""
    n = len(rows)
    area_ratio = (ctx.W * ctx.H) / (ctx.src_w * ctx.src_h)
    # big canvases (posters) may grow the design as far as it fits
    s_hi = max(1.0, math.sqrt(area_ratio)) * 1.05
    found = []
    max_k = min(n, 6)
    for k in range(1, max_k + 1):
        for cols in _candidates(rows, k, ctx.f_ref, ctx.hero_id):
            lo, hi = 0.0, s_hi
            if _try_layout(cols, hi, ctx):
                lo = hi
            else:
                for _ in range(14):
                    mid = (lo + hi) / 2
                    if _try_layout(cols, mid, ctx):
                        lo = mid
                    else:
                        hi = mid
            if lo <= 1e-3:
                continue
            plan = _try_layout(cols, lo, ctx)
            if plan is None:
                continue
            fills = [c[2] for c in plan["cols"]]
            balance = min(fills) / max(fills) if max(fills) > 0 else 1
            fast = lo * (GENOME["col_penalty"] ** (k - 1)) * (0.9 + 0.1 * balance)
            found.append((fast, cols, plan))
    if not found:
        return None
    found.sort(key=lambda t: -t[0])
    if content is None:
        return found if want_all else found[0]
    s_best = max(p["s"] for _, _, p in found)
    ranked = sorted(((_quality(cols, plan, ctx, content, s_best), cols, plan)
                     for _, cols, plan in found[:12]), key=lambda t: -t[0])
    return ranked if want_all else ranked[0]


def _quality(cols, plan, ctx: Ctx, content, s_best: float = 1.0) -> float:
    """Design-quality objective for a fully placed candidate (0..1)."""
    sim = {e["id"]: dict(e) for e in content}
    _apply(cols, plan, ctx, sim)
    return _score(sim, cols, ctx, content, plan["s"] / s_best if s_best > 0 else 1.0)


def _score(sim, cols, ctx: Ctx, content, size: float) -> float:
    """Score a placed layout: legibility, overall size (vs the best candidate),
    hero prominence, reading order, structure, balance and fill (0..1)."""
    texts = [e for e in content if e["type"] == "text"]
    # legibility: how close each text gets to its readable floor
    leg = (sum(min(1.0, sim[e["id"]]["font_size"] / ctx.font_floor(e["font_size"])) for e in texts)
           / len(texts)) if texts else 1.0
    # hero prominence
    if ctx.hero_id and ctx.hero_id in sim:
        h = sim[ctx.hero_id]
        hero = min(1.0, min(h["width"], h["height"]) / (0.8 * min(ctx.W, ctx.H)))
    else:
        hero = 1.0
    # reading-order fidelity: pairs of blocks that keep their source order
    # (source: top-to-bottom; output: left column first, then top-to-bottom)
    order_src, order_out = {}, {}
    i = 0
    for ci, col in enumerate(cols):
        for row in col:
            for b in row:
                order_out[id(b)] = (ci, i); i += 1
    blocks = [b for col in cols for row in col for b in row]
    for b in blocks:
        order_src[id(b)] = (b.box[1] + b.box[3]) / 2
    pairs = agree = 0
    for a, b in itertools.combinations(blocks, 2):
        if abs(order_src[id(a)] - order_src[id(b)]) < 1e-6:
            continue
        pairs += 1
        agree += (order_src[id(a)] < order_src[id(b)]) == (order_out[id(a)] < order_out[id(b)])
    order = agree / pairs if pairs else 1.0
    # balance + fill
    boxes = [_rot_bbox(sim[e["id"]]) for e in content]
    u = _union(boxes)
    fill = (u[2] - u[0]) * (u[3] - u[1]) / (ctx.W * ctx.H)
    cx, cy = (u[0] + u[2]) / 2, (u[1] + u[3]) / 2
    bal = 1 - 2 * math.hypot((cx - ctx.W / 2) / ctx.W, (cy - ctx.H / 2) / ctx.H)
    # structure fidelity: a target shaped like the source should keep the
    # source's single-column structure; wider targets earn more columns
    r = (ctx.W / ctx.H) / (ctx.src_w / ctx.src_h)
    k_exp = max(1, round(r + (GENOME["split_bias"] if r > 1.5 else 0.0)))
    k = len(cols)
    struct = 1.0 if k <= k_exp else 0.5 ** (k - k_exp)
    # orphan columns: a column holding nothing but one short text block looks
    # stranded (e.g. a CTA floating alone, far from its headline)
    if k > 1:
        for col in cols:
            if len(col) == 1 and len(col[0]) == 1:
                b = col[0][0]
                short_text = b.flexible_text and len(b.elements[0]["content"]) < 40
                small = b.w * b.h < 0.03 * ctx.src_w * ctx.src_h and b.elements[0]["id"] != ctx.logo_id
                if short_text or small:
                    struct *= 0.6
    g = GENOME
    return (g["w_leg"] * leg + g["w_size"] * size + g["w_hero"] * hero + g["w_order"] * order
            + g["w_struct"] * struct + g["w_bal"] * max(0.0, bal) + g["w_fill"] * min(1.0, fill / 0.7))


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


def _place_block(b: Block, x: float, y: float, w: float, h: float, s: float, out: dict, ctx=None,
                 mult: float = 1.0, pad: float | None = None):
    if ctx is not None:
        s = _bscale(b, s, ctx)
    s *= mult
    if b.flexible_text:
        el = out[b.elements[0]["id"]]
        el.update(x=x, y=y, width=w, height=h, font_size=max(1.0, b.elements[0]["font_size"] * s))
        return
    bx0, by0 = b.box[0], b.box[1]
    # center the scaled block inside the slot
    ox = x + (w - b.w * s) / 2
    if pad is not None:  # slot sized to what is drawn: place the ink, not the box
        ox = x + pad - (b.ink_x(s / mult if mult else s)[0] - bx0) * s
    oy = y + (h - b.h * s) / 2
    for src in b.elements:
        el = out[src["id"]]
        el["x"] = ox + (src["x"] - bx0) * s
        el["y"] = oy + (src["y"] - by0) * s
        el["width"] = src["width"] * s
        el["height"] = src["height"] * s
        if src["type"] == "text":
            el["font_size"] = max(1.0, src["font_size"] * s)


def _apply(cols, plan, ctx: Ctx, out: dict):
    s = plan["s"]
    gap = ctx.gap_src * s
    widths = plan["widths"]
    k = len(cols)
    AW = ctx.W - 2 * ctx.margin
    AH = ctx.H - 2 * ctx.margin
    free_x = AW - sum(widths) - plan["col_gap"] * (k - 1)
    pad_x = free_x / (k + 1)
    x = ctx.margin + pad_x
    for col, cw, (_, row_ms, total) in zip(cols, widths, plan["cols"]):
        free_y = max(0.0, AH - total)
        n = len(col)
        gaps = _col_gaps(col, row_ms, gap, ctx)
        side = _column_side(col, ctx)
        # spread rows a little, but keep the stack visually together:
        # inner gaps grow at most to ~3x the base gap, rest is split top/bottom
        pad_y = min(free_y / (n + 1), gap * GENOME["pad_y"]) if n > 1 else 0.0
        top = (free_y - pad_y * (n - 1)) / 2
        if k > 1 and GENOME["zone_top_align"] > 0:
            # zoning: columns hang from one shared top line (the tallest
            # column's top) instead of each floating at its own center
            tallest = max(t for _, _, t in plan["cols"])
            shared = (max(0.0, AH - tallest) - pad_y * (n - 1)) / 2
            top += GENOME["zone_top_align"] * (max(0.0, shared) - top)
        # default: compact stack, vertically centered
        row_ys, yy = [], ctx.margin + top
        for i, m in enumerate(row_ms):
            row_ys.append(yy)
            yy += m[1] + (gaps[i] if i < n - 1 else 0.0) + pad_y
        if k == 1:
            # single column: keep each row near its relative source height
            # (top stays top, bottom stays bottom), as long as nothing collides
            anchored = []
            for row, m in zip(col, row_ms):
                cy = sum((b.box[1] + b.box[3]) / 2 for b in row) / len(row)
                ty = ctx.margin + cy / ctx.src_h * AH - m[1] / 2
                anchored.append(min(max(ty, ctx.margin), ctx.H - ctx.margin - m[1]))
            # push down to resolve overlaps, then check we still fit
            for i in range(1, n):
                prev_bottom = anchored[i - 1] + row_ms[i - 1][1] + gaps[i - 1]
                anchored[i] = max(anchored[i], prev_bottom)
            _compress_body(col, row_ms, gaps, anchored, s, ctx)
            if n and anchored[-1] + row_ms[-1][1] <= ctx.H - ctx.margin + 0.5:
                row_ys = anchored
        for row, (rw, rh, sizes, meta), y in zip(col, row_ms, row_ys):
            mult, rgap = meta.get("mult", 1.0), meta.get("gap", gap)
            if meta.get("grid"):  # wrapped: `grid` items per line (1 = stacked)
                c, yy = meta["grid"], y
                for i in range(0, len(row), c):
                    line = list(zip(row[i:i + c], sizes[i:i + c]))
                    vt = meta.get("vtrim")
                    tr = [vt[i + j] if vt else (0.0, 0.0) for j in range(len(line))]
                    lw = sum(w for _, (w, _) in line) + rgap * (len(line) - 1)
                    lh = max(h - t[0] - t[1] for (_, (_, h)), t in zip(line, tr))
                    cur = x + (cw - lw) / 2
                    if side == "left":
                        cur = x
                    elif side == "right":
                        cur = x + cw - lw
                    for j, (b, (w, h)) in enumerate(line):
                        pads = meta.get("pads")
                        t0, t1 = tr[j]
                        _place_block(b, cur, yy + (lh - (h - t0 - t1)) / 2 - t0, w, h, s, out, ctx, mult,
                                     pads[i + j] if pads else None)
                        _mark_col(b, out, x, cw)
                        cur += w + rgap
                    yy += lh + rgap
                continue
            # horizontal positions inside the column
            if k == 1 and n > 0 and not ctx.center_rows:
                # keep the source's horizontal anchoring (left/center/right)
                xs = []
                for b, (w, _) in zip(row, sizes):
                    norm = b.cx / ctx.src_w
                    xs.append(min(max(x + norm * cw - w / 2, x), x + cw - w))
                ok = all(xs[i] + sizes[i][0] + gap * 0.5 <= xs[i + 1] for i in range(len(xs) - 1))
                if not ok:
                    xs = None
            else:
                xs = None
            if xs is None:
                cur = x + (cw - rw) / 2
                if ctx.logo_left and any(e["id"] == ctx.logo_id for b in row for e in b.elements):
                    cur = x  # creative control: logo flush-left in its column
                if side == "left":
                    cur = x
                elif side == "right":
                    cur = x + cw - rw
                xs = []
                for w, _ in sizes:
                    xs.append(cur)
                    cur += w + rgap
            pads = meta.get("pads") or [None] * len(row)
            vts = meta.get("vtrim") or [(0.0, 0.0)] * len(row)
            for b, (w, h), bx, pd, (t0, t1) in zip(row, sizes, xs, pads, vts):
                _place_block(b, bx, y + (rh - (h - t0 - t1)) / 2 - t0, w, h, s, out, ctx, mult, pd)
                _mark_col(b, out, x, cw)
        x += cw + plan["col_gap"] + pad_x


def _compress_body(col, row_ms, gaps, ys, s, ctx):
    """Extra canvas height goes around the body, not between its rows: a gap
    between two body rows grows at most to its source size x scale x 1.25.
    The logo (top) and the fine print (bottom) keep their anchors; the
    tightened body stays centered where it was."""
    body = [i for i, row in enumerate(col) if not _is_fine_row(row, ctx)
            and not any(e["id"] == ctx.logo_id for b in row for e in b.elements)]
    if len(body) < 2 or body != list(range(body[0], body[-1] + 1)):
        return
    new = {body[0]: ys[body[0]]}
    for a, b in zip(body, body[1:]):
        sg = max(0.0, min(bl.box[1] for bl in col[b]) - max(bl.box[3] for bl in col[a]))
        cur = ys[b] - ys[a] - row_ms[a][1]
        new[b] = new[a] + row_ms[a][1] + min(cur, max(gaps[a], sg * s * GENOME["body_gap_cap"]))
    last = body[-1]
    shift = ((ys[body[0]] + ys[last]) - (new[body[0]] + new[last])) / 2
    for i in body:
        ys[i] = new[i] + shift


def _has_hero(row, ctx) -> bool:
    return any(e["id"] == ctx.hero_id for b in row for e in b.elements)


def _column_side_for_text(e, ctx) -> str | None:
    if ctx.side_align and ctx.banner:
        return "left"
    if not ctx.side_align or ctx.canvas_ratio <= 1.2:
        return None
    a = e.get("text_align", "left")
    return a if a in ("left", "right") else None


def _column_side(col, ctx) -> str | None:
    """'Decide by format': on landscape canvases, a column whose standalone
    copy was left- (or right-) aligned in the source becomes a flush block on
    that side (editorial / Taco Bell style). Portrait and square canvases keep
    the centered, Apple-style stack."""
    if ctx.side_align and ctx.banner and sum(b.flexible_text for row in col for b in row) >= 2:
        return "left"  # short banners: the copy reads as one flush-left column beside the visual
    if not ctx.side_align or ctx.canvas_ratio <= 1.2:
        return None
    votes = {"left": 0, "right": 0, "center": 0}
    for row in col:
        for b in row:
            if b.flexible_text:
                votes[b.elements[0].get("text_align", "left")] = votes.get(b.elements[0].get("text_align", "left"), 0) + 1
    side = max(votes, key=votes.get)
    return side if side in ("left", "right") and votes[side] > votes["center"] else None


def _mark_col(b, out, x, cw):
    """Remember which column a standalone text block landed in (for alignment)."""
    if b.flexible_text:
        out[b.elements[0]["id"]]["_col"] = (x, x + cw)


def _content_width(el: dict) -> float:
    """Width of the widest wrapped line actually drawn in this text box."""
    fm = _font_manager()
    if fm is None:
        return el["width"]
    from renderer.converter.wrap import char_widths, wrap_text
    weight, style = _style(el)
    widths = char_widths(fm, el["font_family"], weight, style, el["font_size"], el["content"])
    wrapped = wrap_text(el["content"], widths, el["width"])
    best = cur = 0.0
    i = 0
    for ch in wrapped:
        if ch == "\n":
            best, cur = max(best, cur), 0.0
            if i < len(el["content"]) and el["content"][i] in " \n":
                i += 1
            continue
        cur += widths[i] if i < len(widths) else 0
        i += 1
    return max(best, cur)


def _wrapped(el: dict, width: float) -> str:
    """The text as the renderer wraps it at this width."""
    from renderer.converter.wrap import wrap_text
    widths = list(_char_widths(el["content"], el["font_family"], *_style(el), el["font_size"]))
    return wrap_text(el["content"], widths, width)


def _align_text(sim: dict, content, ctx: Ctx, style: dict):
    """Text alignment follows placement: a block centered in its column reads
    centered, a block flush to a side reads from that side, and a block whose
    margins are too thin to read as intentional is simply centered."""
    mode = style.get("text_align", "follow_layout")
    if mode == "keep_source":
        return
    for e in content:
        el = sim[e["id"]]
        if e["type"] != "text" or e.get("group_id") or e.get("rotation") or "_col" not in el:
            continue
        c0, c1 = el["_col"]
        cw = max(c1 - c0, 1e-6)
        x0, x1 = el["x"], el["x"] + el["width"]
        left_gap, right_gap = x0 - c0, c1 - x1
        tol = 0.04 * cw
        try:
            text_w = _content_width(el)
        except Exception:
            text_w = el["width"]
        if text_w >= 0.9 * (ctx.W - 2 * ctx.margin):
            el["text_align"] = "center"          # text itself spans almost everything
        elif left_gap <= tol and right_gap <= tol:
            # box fills its column: follow the column's side, else center
            side = _column_side_for_text(e, ctx)
            el["text_align"] = side or "center"
        elif abs(left_gap - right_gap) <= tol:
            el["text_align"] = "center"
        elif left_gap <= tol:
            el["text_align"] = "left"
        elif right_gap <= tol:
            el["text_align"] = "right"
        else:
            el["text_align"] = "left" if left_gap < right_gap else "right"


def _place_decor(el: dict, src: dict, ctx: Ctx):
    sc = min(ctx.W / ctx.src_w, ctx.H / ctx.src_h)
    w, h = src["width"] * sc, src["height"] * sc
    # never larger than the canvas
    shrink = min(1.0, ctx.W / w if w else 1, ctx.H / h if h else 1)
    w, h = w * shrink, h * shrink
    cx = (src["x"] + src["width"] / 2) / ctx.src_w * ctx.W
    cy = (src["y"] + src["height"] / 2) / ctx.src_h * ctx.H
    el.update(x=cx - w / 2, y=cy - h / 2, width=w, height=h)


def _enforce_hierarchy(src_elements, out, content):
    """Guarantee: if text A was bigger than text B in the source, A is not
    smaller than B in the output. Only ever shrinks fonts, so fit is kept."""
    texts = sorted((e for e in content if e["type"] == "text"), key=lambda e: -e["font_size"])
    ceiling = math.inf
    i = 0
    while i < len(texts):
        f = texts[i]["font_size"]
        tier = [e for e in texts[i:] if e["font_size"] == f]
        for e in tier:
            o = out[e["id"]]
            o["font_size"] = max(1.0, min(o["font_size"], ceiling))
        ceiling = min(out[e["id"]]["font_size"] for e in tier)
        i += len(tier)


def _avoid_content(decor, content, out, ctx: Ctx):
    """Opaque decor (e.g. a missing-asset placeholder) shouldn't sit on top of
    real content. Try a few candidate spots and keep the least-overlapping.
    Translucent shapes are meant to layer, so they're left alone."""
    boxes = [_rot_bbox(out[e["id"]]) for e in content]

    def overlap(el):
        b = (el["x"], el["y"], el["x"] + el["width"], el["y"] + el["height"])
        return sum(max(0, min(b[2], c[2]) - max(b[0], c[0])) * max(0, min(b[3], c[3]) - max(b[1], c[1]))
                   for c in boxes)

    m = ctx.margin * 0.5
    for src in decor:
        if src["type"] == "shape" and (src.get("opacity", 1) or 0) <= 0.5:
            continue
        el = out[src["id"]]
        x0, y0, w0, h0 = el["x"], el["y"], el["width"], el["height"]
        # the biggest size that sits clear of all content, nearest its own
        # spot; a 9 x 9 grid of positions over the canvas at each size
        best = None
        for sc in (1.0, 0.8, 0.64, 0.5, 0.4, 0.3, 0.2):
            w, h = w0 * sc, h0 * sc
            cands = [(x0 + (w0 - w) / 2, y0 + (h0 - h) / 2)]
            for fx in range(9):
                for fy in range(9):
                    cands.append((m + fx / 8 * max(0.0, ctx.W - w - 2 * m), m + fy / 8 * max(0.0, ctx.H - h - 2 * m)))
            for (x, y) in cands:
                el.update(x=x, y=y, width=w, height=h)
                ov = overlap(el)
                key = (round(ov / max(w * h, 1e-6), 2), 1 - sc, math.hypot(x - x0, y - y0))
                if best is None or key < best[0]:
                    best = (key, x, y, w, h)
            if best[0][0] == 0:
                break
        el.update(x=best[1], y=best[2], width=best[3], height=best[4])


def _inter(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _place_soft_decor(soft, out, content, ctx: Ctx, logo):
    """Translucent decor (circles, blobs) frames the composition: it keeps its
    size relative to the canvas and its source anchor (corner / edge), never
    touches the logo, stays off text and buttons, and spreads out evenly."""
    if not soft:
        return
    W, H = ctx.W, ctx.H
    r = min(W, H) / min(ctx.src_w, ctx.src_h)
    logo_ids = {e["id"] for e in logo.elements} if logo is not None else set()
    prot = []
    for e in content:
        el = out[e["id"]]
        if e["id"] in logo_ids:
            prot.append((_visible_box(el), 1000.0))
        elif e["type"] in ("text", "shape"):
            prot.append((_rot_bbox(el), 6.0))
        else:
            prot.append((_visible_box(el), 2.0))  # over a product it reads as a shadow
    placed, centers = [], []
    for src in sorted(soft, key=lambda e: -e["width"] * e["height"]):
        el = out[src["id"]]
        w, h = src["width"] * r, src["height"] * r
        cap = min(1.0, 0.9 * min(W, H) / max(w, h, 1e-6))
        w, h = max(w * cap, 1.0), max(h * cap, 1.0)
        u = min(max((src["x"] + src["width"] / 2) / ctx.src_w, 0.0), 1.0)
        v = min(max((src["y"] + src["height"] / 2) / ctx.src_h, 0.0), 1.0)
        # every size from full down: the decor stays near its source corner and
        # would rather shrink a little than drift away; it never touches the logo
        best, fallback, sc = None, None, 1.0
        for _ in range(8):
            cw_, ch_ = w * sc, h * sc
            for i in range(11):
                for j in range(11):
                    x, y = max(0.0, W - cw_) * i / 10, max(0.0, H - ch_) * j / 10
                    box, a = (x, y, x + cw_, y + ch_), cw_ * ch_
                    ov = sum(wt * _inter(box, pb) for pb, wt in prot) / a
                    ov += sum(2.0 * _inter(box, pb) for pb in placed) / a
                    cx, cy = (x + cw_ / 2) / W, (y + ch_ / 2) / H
                    sep = min((math.hypot(cx - px, cy - py) for px, py in centers), default=1.0)
                    cost = ov + 1.5 * math.hypot(cx - u, cy - v) - 0.4 * sep + 0.6 * (1 - sc)
                    cand = (cost, x, y, cw_, ch_)
                    if any(wt >= 1000 and _inter(box, pb) > 0 for pb, wt in prot):
                        if fallback is None or cost < fallback[0]:
                            fallback = cand
                    elif best is None or cost < best[0]:
                        best = cand
            sc *= 0.8
        best = best or fallback
        w, h = best[3], best[4]
        el.update(x=best[1], y=best[2], width=w, height=h)
        placed.append((best[1], best[2], best[1] + w, best[2] + h))
        centers.append(((best[1] + w / 2) / W, (best[2] + h / 2) / H))


def _spread_banner(sim, content, ctx: Ctx):
    """Short banners whose content leaves a lot of the width empty: the
    content's groups (logo, message, call to action...) spread out so the
    gaps between them and to the margins are even, instead of everything
    packing to the left. Text boxes are first tightened to their ink (same
    line breaks: a box at least as wide as its widest line wraps the same)."""
    t = GENOME["banner_spread"]
    if t <= 0 or not ctx.banner or len(content) < 2:
        return
    W, m = ctx.W, ctx.margin
    before = {e["id"]: (sim[e["id"]]["x"], sim[e["id"]]["width"]) for e in content}
    ext = {}
    for e in content:
        el = sim[e["id"]]
        if e["type"] == "text" and not e.get("group_id") and not e.get("rotation") and el.get("content"):
            try:
                # the renderer's wrap counts the space after a line's last word,
                # so keep a space of slack, and keep the box only if the line
                # breaks come out identical
                cw = min(el["width"], _content_width(el) + 0.45 * el["font_size"] + 2.0)
                if _wrapped(el, cw) != _wrapped(el, el["width"]):
                    cw = el["width"]
            except Exception:
                cw = el["width"]
            a = el.get("text_align", "left")
            x0 = el["x"] if a == "left" else el["x"] + el["width"] - cw if a == "right" \
                else el["x"] + (el["width"] - cw) / 2
            el["x"], el["width"] = x0, cw
        b = _ink_box(el) if e["type"] != "text" else _rot_bbox(el)
        ext[e["id"]] = (b[0], b[2])
    # groups: elements whose ink overlaps horizontally, and group members
    ids = [e["id"] for e in content]
    parent = {i: i for i in ids}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    gid = {}
    for e in content:
        if e.get("group_id"):
            if e["group_id"] in gid:
                parent[find(e["id"])] = find(gid[e["group_id"]])
            gid.setdefault(e["group_id"], e["id"])
    for a, b in itertools.combinations(ids, 2):
        if min(ext[a][1], ext[b][1]) - max(ext[a][0], ext[b][0]) > -0.5 * m:
            parent[find(a)] = find(b)
    groups = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    spans = sorted(((min(ext[i][0] for i in g), max(ext[i][1] for i in g), g) for g in groups.values()),
                   key=lambda s_: s_[0])
    if len(spans) < 2:
        return
    free = W - 2 * m - sum(b - a for a, b, _ in spans)
    if free < 0.12 * W:
        return
    gap = free / (len(spans) + 1)
    x = m + gap
    for a, b, g in spans:
        dx = t * (x - a)
        for i in g:
            sim[i]["x"] += dx
        x += (b - a) + gap
    if any(sim[i]["x"] < -0.5 or sim[i]["x"] + sim[i]["width"] > W + 0.5 for i in ids):
        for i, (x0, w0) in before.items():
            sim[i]["x"], sim[i]["width"] = x0, w0


def _dock_fine_print(sim, content, ctx: Ctx):
    """Fine print hugs the bottom edge: less space below it than above it."""
    for fid in ctx.fine_ids:
        if fid not in sim:
            continue
        el = sim[fid]
        x0, x1, bottom = el["x"], el["x"] + el["width"], el["y"] + el["height"]
        blocked = False
        for o in content:
            if o["id"] == fid:
                continue
            b = _rot_bbox(sim[o["id"]])
            if b[1] >= bottom - 1 and min(x1, b[2]) - max(x0, b[0]) > 0:
                blocked = True
                break
        if not blocked:
            el["y"] = max(el["y"], ctx.H - 0.5 * ctx.margin - el["height"])


@lru_cache(maxsize=64)
def _bg_focus(asset_id: str, fit: str, W: int, H: int):
    """Where the background's light sits on the target canvas (brightness
    centroid of the visible crop), or None if it has no clear focal glow."""
    if fit not in ("cover", "fill"):
        return None
    try:
        from PIL import Image, ImageFilter

        with Image.open(ASSETS_DIR / asset_id) as im:
            iw, ih = im.size
            k = 80 / max(iw, ih)
            sw, sh = max(1, round(iw * k)), max(1, round(ih * k))
            g = im.convert("L").resize((sw, sh)).filter(ImageFilter.GaussianBlur(3))
        px = list(g.getdata())
    except Exception:
        return None
    med = sorted(px)[len(px) // 2]
    if fit == "cover":
        sc = max(W / iw, H / ih)
        cw, ch = W / sc * k, H / sc * k
        x0, y0 = (sw - cw) / 2, (sh - ch) / 2
    else:
        cw, ch, x0, y0 = sw, sh, 0.0, 0.0
    tot = sx = sy = peak = 0.0
    for yy in range(sh):
        if not y0 <= yy < y0 + ch:
            continue
        for xx in range(sw):
            if not x0 <= xx < x0 + cw:
                continue
            e = px[yy * sw + xx] - med
            if e > 2:
                e *= e
                tot, sx, sy, peak = tot + e, sx + e * xx, sy + e * yy, max(peak, e)
    if tot <= 0 or peak < 64:
        return None
    return ((sx / tot - x0) / cw * W, (sy / tot - y0) / ch * H)


def _center_on_focus(sim, content, ctx: Ctx, logo, focus):
    """Slide the body vertically so its main element (hero, else the whole body)
    sits on the background's light, without crossing the logo, the fine
    print or the margins."""
    if focus is None:
        return
    band = _body_band(sim, content, ctx, logo)
    if band is None:
        return
    body, fixed, bb, lo, hi = band
    prim = ctx.hero_id if ctx.hero_id and ctx.hero_id not in fixed and ctx.hero_id in sim else None
    # the hero sits on the light; without one, the body as a whole does
    pb = _rot_bbox(sim[prim]) if prim is not None else bb
    dy = focus[1] - (pb[1] + pb[3]) / 2
    if prim is None:
        # a small text cluster sits right on the light; a tall body (no single
        # hero) leans toward it but stays balanced in its free band
        band = (hi - lo) + (bb[3] - bb[1])
        t = GENOME["focus_blend"] * min(1.0, max(0.0, ((bb[3] - bb[1]) / max(band, 1e-6) - 0.3) / 0.3))
        dy = (1 - t) * dy + t * (lo + hi) / 2
    dy = min(max(dy, lo), hi)
    if abs(dy) >= 1:
        for e in body:
            sim[e["id"]]["y"] += dy


def _body_band(sim, content, ctx: Ctx, logo):
    """The body (everything but the logo and the fine print) and how far it
    may slide vertically: (body, fixed ids, body box, lowest dy, highest dy),
    or None when it can't move without crossing the logo, fine print or margins."""
    fixed = ({e["id"] for e in logo.elements} if logo is not None else set()) | ctx.fine_ids
    body = [e for e in content if e["id"] not in fixed]
    if not body:
        return None
    bb = _union([_rot_bbox(sim[e["id"]]) for e in body])
    lo, hi = ctx.margin - bb[1], ctx.H - ctx.margin - bb[3]
    for oid in fixed:
        if oid not in sim:
            continue
        ob = _rot_bbox(sim[oid])
        if ob[2] <= bb[0] or ob[0] >= bb[2]:
            continue  # beside the body, can't collide
        if ob[3] <= bb[1] + 1:
            lo = max(lo, ob[3] + 0.5 * ctx.margin - bb[1])
        elif ob[1] >= bb[3] - 1:
            hi = min(hi, ob[1] - (ctx.margin if oid in ctx.fine_ids else 0.5 * ctx.margin) - bb[3])
        else:
            return None
    if lo > hi:
        return None
    return body, fixed, bb, lo, hi


def _ink_box(el: dict):
    """What is drawn: visible pixels for images, the box for text and shapes."""
    if el["type"] == "image" and not el.get("rotation"):
        return _visible_box(el)
    return _rot_bbox(el)


def _optical_anchor(sim, content, ctx: Ctx, logo):
    """No background glow to sit on: slide the body so its visual mass (drawn
    area, the hero weighted up) sits on the optical center of its free band,
    a little above the geometric middle, where the eye expects it."""
    lift, hero_w = GENOME["optical_lift"], GENOME["hero_mass"]
    if lift <= 0 and hero_w <= 0:
        return
    band = _body_band(sim, content, ctx, logo)
    if band is None:
        return
    body, _, bb, lo, hi = band
    tot = my = 0.0
    for e in body:
        b = _ink_box(sim[e["id"]])
        a = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
        if e["type"] == "text":
            a *= 0.5  # glyphs cover about half their box
        if e["id"] == ctx.hero_id:
            a *= 1 + hero_w
        tot, my = tot + a, my + a * (b[1] + b[3]) / 2
    if tot <= 0:
        return
    top, bottom = bb[1] + lo, bb[3] + hi  # the band the body can occupy
    target = (top + bottom) / 2 - lift * (bottom - top)
    dy = min(max(target - my / tot, lo), hi)
    if abs(dy) >= 1:
        for e in body:
            sim[e["id"]]["y"] += dy


def _relax(sim, blocks, ctx: Ctx, logo):
    """Elastic clean-up: rigid blocks that sit closer than a minimum clearance
    push apart along the axis that needs the smaller move, stay inside the
    margins, and (with a spring) the gaps of a vertical stack even out. The
    logo and fine print are anchors. Kept only if nothing ends up overlapping
    that didn't before and crowding goes down."""
    iters = int(GENOME["relax_iters"])
    if (iters <= 0 and GENOME["relax_spring"] <= 0) or len(blocks) < 2:
        return
    W, H, m = ctx.W, ctx.H, ctx.margin
    clear = GENOME["relax_clear"] * min(W, H)
    pinned = ({e["id"] for e in logo.elements} if logo is not None else set()) | ctx.fine_ids
    groups = [[e["id"] for e in b.elements] for b in blocks]
    free = [not any(i in pinned for i in g) for g in groups]
    src_box = [b.box for b in blocks]
    layered = {(i, j) for i, j in itertools.combinations(range(len(blocks)), 2)
               if _inter(src_box[i], src_box[j]) > 0}
    before = {i: (sim[i]["x"], sim[i]["y"]) for g in groups for i in g}

    def box(k):
        return _union([_ink_box(sim[i]) for i in groups[k]])

    def move(k, dx, dy):
        for i in groups[k]:
            sim[i]["x"] += dx
            sim[i]["y"] += dy

    def score():
        over = crowd = 0
        for i, j in itertools.combinations(range(len(groups)), 2):
            if (i, j) in layered:
                continue
            a, b = box(i), box(j)
            gx = max(a[0], b[0]) - min(a[2], b[2])
            gy = max(a[1], b[1]) - min(a[3], b[3])
            if gx < 0 and gy < 0:
                over += 1
            elif max(gx, gy) < clear:
                crowd += 1
        return over, crowd

    def keep_inside(k):
        b = box(k)
        dx = (m - b[0]) if b[0] < m else (W - m - b[2]) if b[2] > W - m else 0.0
        dy = (m - b[1]) if b[1] < m else (H - m - b[3]) if b[3] > H - m else 0.0
        if b[2] - b[0] > W - 2 * m:
            dx = (W - b[0] - b[2]) / 2
        if b[3] - b[1] > H - 2 * m:
            dy = (H - b[1] - b[3]) / 2
        move(k, dx, dy)

    s0 = score()
    for _ in range(iters):
        moved = False
        for i, j in itertools.combinations(range(len(groups)), 2):
            if (i, j) in layered or not (free[i] or free[j]):
                continue
            a, b = box(i), box(j)
            px = min(a[2], b[2]) - max(a[0], b[0]) + clear  # penetration incl. clearance
            py = min(a[3], b[3]) - max(a[1], b[1]) + clear
            if px <= 0 or py <= 0:
                continue
            share = (0.5, 0.5) if free[i] and free[j] else ((1.0, 0.0) if free[i] else (0.0, 1.0))
            if px < py:
                d = -1 if (a[0] + a[2]) < (b[0] + b[2]) else 1
                move(i, d * px * share[0], 0.0)
                move(j, -d * px * share[1], 0.0)
            else:
                d = -1 if (a[1] + a[3]) < (b[1] + b[3]) else 1
                move(i, 0.0, d * py * share[0])
                move(j, 0.0, -d * py * share[1])
            moved = True
        for k in range(len(groups)):
            if free[k]:
                keep_inside(k)
        if not moved:
            break
    if GENOME["relax_spring"] > 0:
        # a vertical stack of free blocks: gaps move toward their mean
        order = sorted((k for k in range(len(groups)) if free[k]), key=lambda k: box(k)[1])
        stack = [k for k in order]
        for a, b in zip(stack, stack[1:]):
            if min(box(a)[2], box(b)[2]) - max(box(a)[0], box(b)[0]) <= 0:
                stack = []  # side by side somewhere: not a simple stack
                break
        if len(stack) >= 3:
            gaps = [box(b)[1] - box(a)[3] for a, b in zip(stack, stack[1:])]
            if min(gaps) > 0:
                mean = sum(gaps) / len(gaps)
                c0 = (box(stack[0])[1] + box(stack[-1])[3]) / 2
                y = box(stack[0])[1]
                for k, g in zip(stack, gaps + [0.0]):
                    b = box(k)
                    move(k, 0.0, y - b[1])
                    y += (b[3] - b[1]) + g + GENOME["relax_spring"] * (mean - g)
                shift = c0 - (box(stack[0])[1] + box(stack[-1])[3]) / 2
                for k in stack:
                    move(k, 0.0, shift)
    s1 = score()
    inside = all(-0.5 <= sim[i]["x"] and sim[i]["x"] + sim[i]["width"] <= W + 0.5
                 and -0.5 <= sim[i]["y"] and sim[i]["y"] + sim[i]["height"] <= H + 0.5
                 for g in groups for i in g)
    if not inside or s1[0] > s0[0] or (s1[0] == s0[0] and s1[1] > s0[1]):
        for i, (x, y) in before.items():
            sim[i]["x"], sim[i]["y"] = x, y


def _clamp(el: dict, W: float, H: float):
    """Final guarantee: unrotated box (what the checker looks at) inside canvas.
    Images shrink proportionally so their aspect ratio is never changed."""
    if el["type"] == "image" and el.get("fit") != "cover":
        k = min(1.0, W / el["width"] if el["width"] > 0 else 1.0, H / el["height"] if el["height"] > 0 else 1.0)
        if k < 1.0:
            cx, cy = el["x"] + el["width"] / 2, el["y"] + el["height"] / 2
            el["width"] *= k
            el["height"] *= k
            el["x"], el["y"] = cx - el["width"] / 2, cy - el["height"] / 2
    if el["width"] > W:
        el["width"] = W
    if el["height"] > H:
        el["height"] = H
    el["x"] = min(max(el["x"], 0.0), W - el["width"])
    el["y"] = min(max(el["y"], 0.0), H - el["height"])


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _find_roles(blocks, content, src_w, src_h):
    """Return (logo_block, cta_block); either may be None."""
    logo = None
    singles = [b for b in blocks if len(b.elements) == 1 and b.elements[0]["type"] == "image"]
    named = [b for b in singles if "logo" in (b.elements[0].get("asset_id") or "").lower()]
    if named:
        logo = min(named, key=lambda b: b.box[1])
    else:
        small_top = [b for b in singles if b.w * b.h < 0.08 * src_w * src_h and b.box[1] < 0.3 * src_h]
        if small_top:
            logo = min(small_top, key=lambda b: b.box[1])
    buttons = []
    for b in blocks:
        types = {e["type"] for e in b.elements}
        texts = [e for e in b.elements if e["type"] == "text"]
        if {"shape", "text"} <= types and len(texts) == 1 and len(texts[0]["content"].split()) <= 5:
            buttons.append(b)
    cta = max(buttons, key=lambda b: (b.w * b.h, b.box[1])) if buttons else None
    return logo, cta


def _grid_symmetry(boxes, W, H, n=32) -> float:
    """Mirror symmetry about the vertical axis on an n x n occupancy grid."""
    if not boxes:
        return 1.0
    g = [[False] * n for _ in range(n)]
    for (x0, y0, x1, y1) in boxes:
        for gy in range(max(0, int(y0 / H * n)), min(n, int(math.ceil(y1 / H * n)))):
            for gx in range(max(0, int(x0 / W * n)), min(n, int(math.ceil(x1 / W * n)))):
                g[gy][gx] = True
    inter = union = 0
    for row in g:
        for i in range(n):
            a, b = row[i], row[n - 1 - i]
            inter += a and b
            union += a or b
    return inter / union if union else 1.0


def _brand_score(sim, ctx, content, logo, cta, style) -> float:
    W, H = ctx.W, ctx.H
    boxes = {e["id"]: _rot_bbox(sim[e["id"]]) for e in content}
    if logo is not None:
        lids = {e["id"] for e in logo.elements}
        lb = _visible_box(sim[logo.elements[0]["id"]])
        lcx = (lb[0] + lb[2]) / 2
        others = [b for i, b in boxes.items() if i not in lids]
        centered = abs(lcx - W / 2) < 0.04 * W and lb[1] <= min((b[1] for b in others), default=H) + 1
        box0 = sim[logo.elements[0]["id"]]["x"]  # padded box edge vs other boxes' edges
        leftmost = box0 <= min((b[0] for b in others), default=W) + 0.02 * W
        pos = style["logo_position"]
        if pos == "center":
            logo_t = 1.0 if centered else 0.2
        elif pos == "left":
            logo_t = 1.0 if leftmost else (0.5 if centered else 0.2)
        else:
            logo_t = 1.0 if centered else (0.8 if leftmost else 0.3)
    else:
        logo_t = 1.0
    if cta is not None:
        cids = {e["id"] for e in cta.elements}
        cb = _union([boxes[i] for i in cids])
        before = [e for e in content if e["id"] not in cids and (e["y"] + e["height"] / 2) < cta.box[1]]
        ok = 0
        for e in before:
            b = boxes[e["id"]]
            bcx, bcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            ok += (bcy <= (cb[1] + cb[3]) / 2 and bcx <= cb[2]) or (b[2] <= cb[0] + 1)
        cta_t = ok / len(before) if before else 1.0
    else:
        cta_t = 1.0
    sym_t = _grid_symmetry(list(boxes.values()), W, H)
    ws = style["symmetry"]
    return (0.45 * logo_t + 0.30 * cta_t + 0.25 * ws * sym_t) / (0.75 + 0.25 * ws)


def _sub_ctx(ctx: Ctx, W: float, H: float) -> Ctx:
    """A zone of the canvas: same rules and margins as the whole canvas."""
    c = Ctx(ctx.src_w, ctx.src_h, W, H)
    for k in ("margin", "f_ref", "hero_id", "min_font", "center_rows", "logo_id", "side_align", "logo_left",
              "canvas_ratio", "fine_ids", "banner", "priority_ids"):
        setattr(c, k, getattr(ctx, k))
    c.full = (ctx.W, ctx.H)
    return c


def _type_scale(sim, content) -> float:
    """Geometric mean of output / source size over the content."""
    logs = [math.log(max(sim[e["id"]]["width"], 1e-6) / e["width"]) for e in content if e["width"] > 0]
    return math.exp(sum(logs) / len(logs)) if logs else 1.0


def _header_zone(logo, body_rows, content, ctx: Ctx, base):
    """Header-zone candidates: the logo and the first text row (the headline)
    stack in a full-width band on top; the remaining rows are solved again
    in the space left beneath, so a tall source on a square or wide canvas
    reads headline first, then visual and copy side by side."""
    W, H = ctx.W, ctx.H
    if len(body_rows) < 3 or not _is_text_row(body_rows[0]) or H < 200 or not 0.9 <= W / H <= 2.0:
        return []
    head_rows = ([[logo]] if logo is not None else []) + body_rows[:1]
    rest = body_rows[1:]
    head_ids = {e["id"] for r in head_rows for b in r for e in b.elements}
    head_content = [e for e in content if e["id"] in head_ids]
    rest_content = [e for e in content if e["id"] not in head_ids]
    ts_ref = max(_type_scale(sim, content) for _, sim in base)
    out = []
    for f in (0.3, 0.38, 0.46):
        hctx = _sub_ctx(ctx, W, f * H)
        _set_row_caps(head_rows, hctx)
        heads = [c for c in (_solve(head_rows, hctx, head_content, want_all=True) or []) if len(c[1]) == 1]
        if not heads:
            continue
        _, hcols, hplan = heads[0]
        hsim = {e["id"]: dict(e) for e in content}
        _apply(hcols, hplan, hctx, hsim)
        # the rest starts right under what the header actually drew
        top = max(_rot_bbox(hsim[i])[3] for i in head_ids) + 2 * ctx.gap_src * hplan["s"]
        rctx = _sub_ctx(ctx, W, H - top + ctx.margin)
        if rctx.H < 0.35 * H:
            continue
        _set_row_caps(rest, rctx)
        for _, cols, plan in (_solve(rest, rctx, rest_content, want_all=True) or [])[:4]:
            sim = dict(hsim)
            sim.update({e["id"]: dict(e) for e in rest_content})
            _apply(cols, plan, rctx, sim)
            for e in rest_content:
                sim[e["id"]]["y"] += top - ctx.margin
            # score as one layout: head rows read first, then the rest's columns
            combined = [hcols[0] + cols[0]] + cols[1:]
            size = min(1.0, _type_scale(sim, content) / ts_ref) if ts_ref > 0 else 1.0
            out.append((_score(sim, combined, ctx, content, size), sim))
    return out


def _set_row_caps(rows, ctx):
    for row in rows:
        cap = min(_fit_cap(b, ctx) for b in row)
        for b in row:
            b.row_cap = cap if cap != math.inf else None


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    try:
        return _resize(source, target_canvas)
    except Exception:
        # one odd design must never take down a whole batch: fall back to a
        # plain fit-inside layout that still satisfies every hard rule
        return _safe_fallback(source, target_canvas)


def _safe_fallback(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    """Uniform fit-inside scale of the source, centered; backgrounds fill."""
    W, H = target_canvas["width"], target_canvas["height"]
    src_w = max(source["canvas"]["width"], 1)
    src_h = max(source["canvas"]["height"], 1)
    s = min(W / src_w, H / src_h)
    ox, oy = (W - src_w * s) / 2, (H - src_h * s) / 2
    elements = []
    for el in source["elements"]:
        o = copy.deepcopy(el)
        try:
            if _classify(el, src_w, src_h) == "background":
                o.update(x=0, y=0, width=W, height=H)
            else:
                o.update(x=ox + el["x"] * s, y=oy + el["y"] * s,
                         width=max(0.0, el["width"] * s), height=max(0.0, el["height"] * s))
                if el["type"] == "text":
                    o["font_size"] = max(1.0, el["font_size"] * s)
            _clamp(o, W, H)
        except Exception:
            o.update(x=0, y=0, width=0, height=0)
        elements.append(o)
    return {"canvas": {"width": W, "height": H}, "elements": elements}


def _same_shape(source, target_canvas, ctx: Ctx, k: float):
    """The designer's arrangement scaled as-is (same-shape canvas), if every
    text stays readable and its ink fits its box. A box drawn a little too
    short for its ink grows to fit, when the taller box touches nothing it
    didn't touch in the source. None if the layout can't be kept."""
    els = source["elements"]
    grow = {}
    for e in els:
        if e["type"] != "text" or not e.get("content"):
            continue
        fs = e["font_size"] * k
        if fs < 0.85 * ctx.font_floor(e["font_size"]):
            return None
        if _ink_fits(e, fs, e["width"] * k, e["height"] * k):
            continue
        weight, style = _style(e)
        lines, widest = _measure(e["content"], e["font_family"], weight, style, fs, e["width"] * k)
        if widest > e["width"] * k or e.get("rotation"):
            return None
        lh = e.get("line_height", DEFAULT_LINE_HEIGHT) or DEFAULT_LINE_HEIGHT
        need = ((lines - 1) * fs * lh + int(fs) * _font_extent(e["font_family"], weight, style)) / k * 1.01
        old = (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
        new = (old[0], old[1], old[2], old[1] + need)
        if new[3] > ctx.src_h:
            return None
        for o in els:
            if o is e or (o.get("group_id") and o.get("group_id") == e.get("group_id")) \
                    or _classify(o, ctx.src_w, ctx.src_h) != "content":
                continue
            ob = _rot_bbox(o)
            if _inter(new, ob) > _inter(old, ob) + 1e-6:
                return None
        grow[e["id"]] = need
    if grow:
        source = dict(source, elements=[dict(e, height=grow[e["id"]]) if e["id"] in grow else e for e in els])
    return _safe_fallback(source, target_canvas)


def _find_panels(content) -> dict:
    """Opaque cards behind copy: an ungrouped, opaque, unrotated shape whose
    box holds at least two other content elements (one of them text), all
    drawn above it, with every group either fully inside or fully outside.
    Returns {card id: (card, [member ids])}."""
    found = {}
    for p in content:
        if p["type"] != "shape" or p.get("group_id") or p.get("rotation") or (p.get("opacity", 1) or 0) <= 0.5:
            continue
        x0, y0, x1, y1 = p["x"] - 2, p["y"] - 2, p["x"] + p["width"] + 2, p["y"] + p["height"] + 2

        def inside(e):
            b = _rot_bbox(e)
            return b[0] >= x0 and b[1] >= y0 and b[2] <= x1 and b[3] <= y1
        members = [e for e in content if e is not p and inside(e)]
        if len(members) < 2 or not any(e["type"] == "text" for e in members):
            continue
        if any((e.get("z_index") or 0) <= (p.get("z_index") or 0) for e in members):
            continue
        gids = {e.get("group_id") for e in members if e.get("group_id")}
        if any(e.get("group_id") in gids and not inside(e) for e in content if e is not p):
            continue
        found[p["id"]] = (p, [e["id"] for e in members])
    return found


def _wrap_panels(panels, out, content, ctx: Ctx) -> bool:
    """Each card wraps its re-laid-out copy, with its source padding scaled
    like the copy. False if a card would cover anything that isn't its own."""
    for pid, (card, mids) in panels.items():
        u = _union([_rot_bbox(out[m]) for m in mids])
        su = _union([_rot_bbox(e) for e in content if e["id"] in mids])
        sw_, sh_ = max(su[2] - su[0], 1e-6), max(su[3] - su[1], 1e-6)
        k = math.sqrt(max(u[2] - u[0], 1e-6) * max(u[3] - u[1], 1e-6) / (sw_ * sh_))
        pads = (max(0.0, su[0] - card["x"]) * k, max(0.0, su[1] - card["y"]) * k,
                max(0.0, card["x"] + card["width"] - su[2]) * k, max(0.0, card["y"] + card["height"] - su[3]) * k)
        # the card keeps half a margin from the canvas edges (its padding gives way)
        edge = 0.5 * ctx.margin
        box = (min(u[0], max(edge, u[0] - pads[0])), min(u[1], max(edge, u[1] - pads[1])),
               max(u[2], min(ctx.W - edge, u[2] + pads[2])), max(u[3], min(ctx.H - edge, u[3] + pads[3])))
        for e in content:
            if e["id"] in mids or e["id"] == pid or _classify(e, ctx.src_w, ctx.src_h) != "content":
                continue
            if _inter(box, _ink_box(out[e["id"]])) > 0:
                return False
        out[pid].update(x=box[0], y=box[1], width=box[2] - box[0], height=box[3] - box[1])
    return True


def _resize(source: dict[str, Any], target_canvas: dict[str, Any], use_panels: bool = True) -> dict[str, Any]:
    src_w, src_h = source["canvas"]["width"], source["canvas"]["height"]
    W, H = target_canvas["width"], target_canvas["height"]
    ctx = Ctx(src_w, src_h, W, H)
    fonts = [e["font_size"] for e in source["elements"] if e["type"] == "text"]
    if fonts:
        ctx.f_ref = min(fonts)

    # Same shape: the designer's arrangement already fits, so don't rearrange
    # it for no reason; scale it faithfully as long as every text stays readable
    k = min(W / src_w, H / src_h)
    if abs(math.log((W / H) / (src_w / src_h))) < math.log(1.08):
        if GENOME["shortcut_ink"] >= 0.5:
            same = _same_shape(source, target_canvas, ctx, k)
            if same is not None:
                return same
        elif all(e["font_size"] * k >= 0.85 * ctx.font_floor(e["font_size"])
                 and _fits_box(e, e["font_size"] * k, e["width"] * k, e["height"] * k)
                 for e in source["elements"] if e["type"] == "text"):
            return _safe_fallback(source, target_canvas)

    out = {el["id"]: copy.deepcopy(el) for el in source["elements"]}
    by_class: dict[str, list[dict]] = {"background": [], "decor": [], "content": []}
    for el in source["elements"]:
        by_class[_classify(el, src_w, src_h)].append(el)

    for el in by_class["background"]:
        out[el["id"]].update(x=0, y=0, width=W, height=H)

    for el in by_class["decor"]:
        _place_decor(out[el["id"]], el, ctx)
        if HIDE_MISSING_ASSETS and el["type"] == "image" and not _asset_exists(el.get("asset_id")):
            out[el["id"]]["opacity"] = 0

    imgs = [e for e in by_class["content"] if e["type"] == "image"]
    if imgs:
        big = max(imgs, key=lambda e: e["width"] * e["height"])
        # only a genuinely prominent image is a "hero" (not e.g. a small logo)
        if big["width"] * big["height"] >= 0.08 * src_w * src_h:
            ctx.hero_id = big["id"]

    # fine print: the smallest text, near the bottom of the source, clearly
    # smaller than everything else (legal lines, disclaimers)
    texts = [e for e in by_class["content"] if e["type"] == "text"]
    if len(texts) >= 2:
        fmin = min(e["font_size"] for e in texts)
        if any(e["font_size"] > 1.15 * fmin for e in texts):
            ctx.fine_ids = {e["id"] for e in texts if e["font_size"] <= fmin + 1e-6
                            and not e.get("group_id") and e["y"] + e["height"] / 2 > 0.8 * src_h}
    # short banners: a long headline sets tighter so it can carry more weight
    if ctx.banner and texts:
        head = max(texts, key=lambda e: e["font_size"])
        if len(head["content"].split()) > 3:
            tight = dict(head, line_height=min(head.get("line_height", DEFAULT_LINE_HEIGHT), 1.05))
            by_class["content"] = [tight if e["id"] == head["id"] else e for e in by_class["content"]]

    focus = None
    for el in by_class["background"]:
        if el["type"] == "image" and el.get("asset_id"):
            focus = _bg_focus(el["asset_id"], el.get("fit", "contain"), W, H)
            break
    logo = None

    style = dict(STYLE)
    style.update(target_canvas.get("style") or {})
    ctx.center_rows = bool(style["center_rows"])
    ctx.logo_left = style["logo_position"] == "left"
    ctx.side_align = style["text_align"] == "follow_layout"

    content = by_class["content"]
    # cards behind copy step out of the layout and re-wrap it afterwards
    # (only when the canvas changes shape a lot: on a similar shape the card
    # keeps its proportions best by scaling with its copy)
    reshape = abs(math.log2((W / H) / (src_w / src_h)))
    panels = _find_panels(content) if use_panels and GENOME["panel_reflow"] >= 0.5 and reshape > 1.5 else {}
    if panels:
        content = [e for e in content if e["id"] not in panels]
    if content:
        blocks = _build_blocks(content)
        logo, cta = _find_roles(blocks, content, src_w, src_h)
        if logo is not None:
            ctx.logo_id = logo.elements[0]["id"]
        if ctx.banner:
            texts_ = [e for e in content if e["type"] == "text" and not e.get("group_id")]
            if texts_:
                ctx.priority_ids.add(max(texts_, key=lambda e: e["font_size"])["id"])
            if cta is not None:
                ctx.priority_ids |= {e["id"] for e in cta.elements}
        body_blocks = [b for b in blocks if b is not logo]
        body_rows = _build_rows(body_blocks) if body_blocks else []
        for row in body_rows:
            if _is_peer_row(row) and any(e["id"] == ctx.hero_id for b in row for e in b.elements):
                ctx.hero_id = None  # a set of equal options has no single hero
        rows = ([[logo]] + body_rows) if logo is not None else body_rows  # logo leads
        candidates = []  # (base_quality, sim)

        # A) logo leads the first column: centered when single-column, else top-left
        _set_row_caps(rows, ctx)
        for q, cols, plan in (_solve(rows, ctx, content, want_all=True) or []):
            sim = {e["id"]: dict(e) for e in content}
            _apply(cols, plan, ctx, sim)
            candidates.append((q, sim))

        # B) masthead: logo centered in a top band, body laid out beneath it
        # (wide, short banners read left-to-right instead: logo goes left there)
        if logo is not None and body_rows and style["logo_position"] != "left" \
                and (W / H <= 2.2 or style["logo_position"] == "center"):
            body_content = [e for e in content if e not in logo.elements]
            for f in (0.14, 0.2, 0.26):
                bh = f * H
                if bh < 40 or H < 200 or H - bh < 0.5 * H:
                    continue
                bctx = Ctx(src_w, src_h, W, H - bh)
                bctx.f_ref, bctx.hero_id, bctx.min_font = ctx.f_ref, ctx.hero_id, ctx.min_font
                bctx.center_rows = ctx.center_rows
                bctx.logo_id = ctx.logo_id
                bctx.side_align, bctx.logo_left = ctx.side_align, ctx.logo_left
                bctx.canvas_ratio = ctx.canvas_ratio
                bctx.fine_ids, bctx.banner = ctx.fine_ids, ctx.banner
                bctx.priority_ids = ctx.priority_ids
                _set_row_caps(body_rows, bctx)
                ranked = _solve(body_rows, bctx, body_content, want_all=True) or []
                for q, cols, plan in ranked[:6]:
                    sim = {e["id"]: dict(e) for e in content}
                    _apply(cols, plan, bctx, sim)
                    for e in body_content:
                        sim[e["id"]]["y"] += bh - bctx.margin * 0.5
                    vb0 = _visible_box(logo.elements[0])
                    vis_h = max(vb0[3] - vb0[1], 1e-6)  # visible height at scale 1
                    sc = min(bh * 0.72 / vis_h, (bh - 2) / max(logo.h, 1e-6) * 1.6, _logo_scale(logo, ctx))
                    lw, lh = logo.w * sc, logo.h * sc
                    lx, ly = (W - lw) / 2, ctx.margin * 0.6 + (bh - lh) / 2
                    _place_block(logo, lx, ly, lw, lh, sc, sim)
                    # optical centering: center the *visible* mark, not its padded box
                    vb = _visible_box(sim[logo.elements[0]["id"]])
                    le = sim[logo.elements[0]["id"]]
                    le["x"] += W / 2 - (vb[0] + vb[2]) / 2
                    le["y"] += (ctx.margin * 0.6 + bh / 2) - (vb[1] + vb[3]) / 2
                    # ...but its (padded) box must stay inside the masthead band
                    body_top = min(sim[e["id"]]["y"] for e in body_content)
                    le["y"] = max(0.0, min(le["y"], body_top - le["height"] - 1))
                    candidates.append((q, sim))
            _set_row_caps(rows, ctx)

        # C) header zone: the logo and the headline span the whole width on top
        # and the rest (visual + copy) is laid out beneath them
        if GENOME["zone_header"] >= 0.5 and candidates:
            candidates += _header_zone(logo, body_rows, content, ctx, candidates)
            _set_row_caps(rows, ctx)

        if candidates:
            bw = style["brand_weight"]
            best = max(candidates,
                       key=lambda c: (1 - bw) * c[0] + bw * _brand_score(c[1], ctx, content, logo, cta, style))
            sim = best[1]
            _dock_fine_print(sim, content, ctx)
            _center_on_focus(sim, content, ctx, logo, focus)
            if focus is None:
                _optical_anchor(sim, content, ctx, logo)
            _relax(sim, blocks, ctx, logo)
            _align_text(sim, content, ctx, style)
            _spread_banner(sim, content, ctx)
            for i, el in sim.items():
                out[i].update({k: el[k] for k in ("x", "y", "width", "height", "font_size", "text_align",
                                                  "line_height") if k in el})
        else:  # last resort: uniform fit-inside scale, centered
            s = min(W / src_w, H / src_h)
            ox, oy = (W - src_w * s) / 2, (H - src_h * s) / 2
            for el in content:
                o = out[el["id"]]
                o.update(x=ox + el["x"] * s, y=oy + el["y"] * s,
                         width=el["width"] * s, height=el["height"] * s)
                if el["type"] == "text":
                    o["font_size"] = max(1.0, el["font_size"] * s)

    _enforce_hierarchy(source["elements"], out, by_class["content"])
    if panels and not _wrap_panels(panels, out, by_class["content"], ctx):
        return _resize(source, target_canvas, use_panels=False)
    _place_soft_decor([e for e in by_class["decor"] if e["type"] == "shape"], out, by_class["content"], ctx, logo)
    _avoid_content(by_class["decor"], by_class["content"], out, ctx)

    elements = []
    for el in source["elements"]:  # preserve input order
        o = out[el["id"]]
        _clamp(o, W, H)
        for f in ("x", "y", "width", "height"):
            if f in o:
                o[f] = round(float(o[f]), 2)
        if "font_size" in o:
            # round down: the renderer draws int(font_size), and rounding
            # 12.99998 up to 13.0 would draw a size the layout never measured
            o["font_size"] = math.floor(float(o["font_size"]) * 100 + 1e-9) / 100
        elements.append(o)
    return {"canvas": {"width": W, "height": H}, "elements": elements}
