"""Chaos gauntlet: adversarial red-team tests for engine/resize.py.

Writes hostile retargeting tasks to input/chaos/ (the official runner only
reads input/*.json, so they never leak into a submission run), runs a
candidate engine over them through the real pipeline (`python -m
engine.main` in sandboxes, as benchmarks/fixture_suite.py does) and checks
the outputs with deterministic geometry and pixel tests: no judge, no
vision model.

Four transformation vectors:
  v1 zero-delta      same size; fixed point (an output fed back to its own
                     size); round-trip ping-pong; sub-pixel and 1px epsilon;
                     canvases 1px either side of every aspect-ratio branch
                     threshold in the engine
  v2 scale           32768x32768 megascale, 38x38 watch face, 1x1 atom,
                     prime 9973x9967
  v3 quadrilateral   100x2000 ribbon <-> 2000x100 ticker, sheared
                     parallelogram and keystone trapezoid safe zones
  v4 exotic          circle, semicircle arch, annulus (forbidden centre),
                     5-point star and bottle-silhouette safe zones
A non-rectangular target is a rectangular canvas with
`target_canvas["safe_mask"]`; anything drawn outside the mask is an
off-canvas violation. The engine is not told about masks unless it reads
that key.

Ten black swans (b01-b10): invisible ink, decoy giant, shattered badge,
flush-edge gaze, rotation AABB explosion, unbreakable strings, homogeneous
twins, z-index occlusion, phantom giant box, corrupted-but-legal values.

Verifiers (--verify): hard rules (ids, canvas, fields, off-canvas, image
aspect), rotated corners and safe-mask bleed, per-glyph occlusion by
higher layers, WCAG contrast under each text, Kendall-tau reading order,
weighted centre of mass against the safe zone's centroid, and layout drift
for the same-size, fixed-point, ping-pong and epsilon tasks. A crash, a
hard-rule violation or a text occlusion disqualifies the candidate.

  python -m benchmarks.chaos_gauntlet --build
  python -m benchmarks.chaos_gauntlet --run [--engine engine/resize.py] [--out DIR]
  python -m benchmarks.chaos_gauntlet --verify --out DIR
"""
import argparse
import io
import itertools
import json
import math
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.designs import BG, GOLD, I, ORANGE, PLUM, S, T, button, designs  # noqa: E402
from benchmarks.designs import W as WHITE  # noqa: E402
from benchmarks.fixture_suite import run_engine  # noqa: E402

CHAOS = ROOT / "input" / "chaos"
CHAOS_ASSETS = CHAOS / "assets"
RUNS = ROOT / "benchmarks" / "runs" / "chaos"
KIT = RUNS / "brand_kit"  # the repo's brand kit plus the generated chaos assets
REAL_KIT = ROOT / "brand_kit"
LIGHT = "rgba(246,238,222,1)"
RED = "rgba(214,40,52,1)"
LOGO, BOTTLE = "solstice_logo_lockup.png", "solstice_bottle.png"
HEAVY_PX = 20e6  # targets bigger than this run in their own slow lane
ANALYSIS = 1024  # max side of the verification renders


# ------------------------------------------------------------------ assets
def make_assets() -> None:
    """Assets the brand kit doesn't have: a half-dark / half-light
    background, a frame with a transparent centre, foreground leaves, and a
    bottle cut flat at its right edge."""
    CHAOS_ASSETS.mkdir(parents=True, exist_ok=True)
    w, h = 1200, 1500
    t = np.clip((np.arange(h) - (h / 2 - 30)) / 60, 0, 1)[:, None, None]
    top, bottom = np.array([30, 18, 34]), np.array([246, 238, 222])
    split = (top * (1 - t) + bottom * t) * np.ones((1, w, 1))
    Image.fromarray(split.astype(np.uint8), "RGB").save(CHAOS_ASSETS / "chaos_split_bg.png")

    frame = Image.new("RGBA", (1000, 1000), (0, 0, 0, 0))
    d = ImageDraw.Draw(frame)
    d.rounded_rectangle((0, 0, 999, 999), radius=60, fill=(240, 178, 66, 255))
    d.rounded_rectangle((70, 70, 929, 929), radius=40, fill=(0, 0, 0, 0))
    d.rounded_rectangle((90, 90, 909, 909), radius=30, outline=(232, 106, 58, 255), width=8)
    frame.save(CHAOS_ASSETS / "chaos_frame.png")

    leaf = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    for (cx, cy, rx, ry, ang, col) in ((150, 150, 140, 55, -25, (92, 140, 84, 255)),
                                       (270, 130, 110, 45, 30, (120, 168, 96, 255))):
        layer = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=col)
        leaf = Image.alpha_composite(leaf, layer.rotate(ang, center=(cx, cy)))
    leaf.save(CHAOS_ASSETS / "chaos_leaf.png")

    # the bottle's visible pixels span x 230-571 of 800: cut at its middle
    with Image.open(REAL_KIT / "assets" / BOTTLE) as im:
        im.crop((0, 0, 400, 800)).save(CHAOS_ASSETS / "chaos_cropped_bottle.png")


def brand_kit() -> Path:
    """benchmarks/runs/chaos/brand_kit: the real fonts and assets plus the chaos assets."""
    if not (CHAOS_ASSETS / "chaos_split_bg.png").exists():
        make_assets()
    shutil.rmtree(KIT, ignore_errors=True)
    (KIT / "assets").mkdir(parents=True)
    os.symlink(REAL_KIT / "fonts", KIT / "fonts")
    for f in list((REAL_KIT / "assets").iterdir()) + list(CHAOS_ASSETS.iterdir()):
        if f.is_file():
            os.symlink(f.resolve(), KIT / "assets" / f.name)
    return KIT


# ------------------------------------------------------------------ safe masks
def _star(cx, cy, r_out, r_in, n=5):
    pts = []
    for k in range(2 * n):
        r = r_out if k % 2 == 0 else r_in
        a = -math.pi / 2 + k * math.pi / n
        pts.append([round(cx + r * math.cos(a), 2), round(cy + r * math.sin(a), 2)])
    return pts


def _bottle_canvas():
    with Image.open(REAL_KIT / "assets" / BOTTLE) as im:
        x0, y0, x1, y1 = im.getchannel("A").getbbox()
    return round(1350 * (x1 - x0) / (y1 - y0)), 1350


MASKS = {
    "parallelogram": (1920, 1080, {"type": "polygon", "shape": "sheared parallelogram",
                                   "points": [[320, 0], [1920, 0], [1600, 1080], [0, 1080]]}),
    "keystone": (1200, 900, {"type": "polygon", "shape": "keystone trapezoid",
                             "points": [[0, 0], [1200, 0], [900, 900], [300, 900]]}),
    "circle": (1080, 1080, {"type": "circle", "cx": 540, "cy": 540, "r": 540}),
    "arch": (1920, 960, {"type": "semicircle", "cx": 960, "cy": 960, "r": 960}),
    "annulus": (1080, 1080, {"type": "annulus", "cx": 540, "cy": 540, "r_outer": 540, "r_inner": 180}),
    "star": (1080, 1080, {"type": "polygon", "shape": "5-point star", "points": _star(540, 540, 540, 220)}),
    "bottle": (*_bottle_canvas(), {"type": "asset_alpha", "asset_id": BOTTLE}),
}


def mask_array(spec, W, H, s=1.0) -> np.ndarray:
    """Boolean (h, w) raster of the safe zone at scale s (True = usable)."""
    w, h = max(1, round(W * s)), max(1, round(H * s))
    if not spec:
        return np.ones((h, w), bool)
    ys, xs = np.mgrid[0:h, 0:w]
    X, Y = (xs + 0.5) / s, (ys + 0.5) / s
    k = spec["type"]
    if k in ("circle", "semicircle"):
        m = (X - spec["cx"]) ** 2 + (Y - spec["cy"]) ** 2 <= spec["r"] ** 2
        return m & (Y <= spec["cy"]) if k == "semicircle" else m
    if k == "annulus":
        d2 = (X - spec["cx"]) ** 2 + (Y - spec["cy"]) ** 2
        return (d2 <= spec["r_outer"] ** 2) & (d2 >= spec["r_inner"] ** 2)
    if k == "polygon":
        im = Image.new("L", (w, h), 0)
        ImageDraw.Draw(im).polygon([(x * s, y * s) for x, y in spec["points"]], fill=255)
        return np.asarray(im) > 127
    if k == "asset_alpha":
        with Image.open(KIT / "assets" / spec["asset_id"]) as im:
            a = im.getchannel("A")
            a = a.crop(a.getbbox())
        k_ = min(w / a.width, h / a.height)
        aw, ah = max(1, round(a.width * k_)), max(1, round(a.height * k_))
        out = Image.new("L", (w, h), 0)
        out.paste(a.resize((aw, ah)), ((w - aw) // 2, (h - ah) // 2))
        return np.asarray(out) > 127
    raise ValueError(f"unknown safe_mask type {k}")


# ------------------------------------------------------------------ source designs
def _ribbon():
    """A 100x2000 vertical ribbon: everything stacked top to bottom."""
    return {"canvas": {"width": 100, "height": 2000}, "elements": [
        BG(100, 2000),
        I("logo", 10, 40, 80, 27, LOGO, z=3),
        T("headline", 6, 120, 88, 260, "Chase the light", 22, "Solstice Serif", font_weight="bold",
          text_align="center"),
        I("product", 0, 420, 100, 100, BOTTLE),
        T("subhead", 6, 560, 88, 300, "Small-batch botanical sparkling infusions.", 14, text_align="center"),
        *button("cta", 6, 1700, 88, 40, "Shop", 14),
        T("legal", 4, 1900, 92, 60, "Enjoy responsibly.", 8, text_align="center",
          color="rgba(255,255,255,0.6)"),
    ]}


def _ticker():
    """A 2000x100 horizontal ticker: everything left to right."""
    return {"canvas": {"width": 2000, "height": 100}, "elements": [
        BG(2000, 100),
        I("logo", 30, 20, 180, 60, LOGO, z=3),
        T("headline", 240, 22, 720, 56, "Chase the light - summer drop", 40, "Solstice Serif",
          font_weight="bold"),
        I("product", 1000, 0, 100, 100, BOTTLE),
        T("subhead", 1130, 30, 520, 40, "Small-batch botanical sparkling infusions.", 20),
        *button("cta", 1700, 25, 260, 50, "Shop now", 22),
    ]}


def _swans() -> dict:
    """{name: (design, [targets], trap)} for the ten black swans."""
    out = {}
    sq = {"width": 1080, "height": 1080}

    def ink(bg_elems):
        return {"canvas": {"width": 1080, "height": 1350}, "elements": [
            *bg_elems,
            I("logo", 390, 60, 300, 100, LOGO, z=3),
            T("headline", 90, 200, 900, 240, "White ink on the dark half", 80, "Solstice Serif",
              font_weight="bold", text_align="center"),
            T("subhead", 120, 460, 840, 120, "Everything up here is light text on a dark ground.", 32,
              text_align="center", color="rgba(255,255,255,0.9)"),
            I("product", 340, 700, 400, 400, BOTTLE),
            *button("cta", 340, 1130, 400, 80, "Shop the drop", 28),
            T("legal", 90, 1240, 900, 50, "Dark fine print on the light half. Enjoy responsibly.", 20,
              text_align="center", color=PLUM),
        ]}
    tg = [(1920, 1080), (1600, 400), (728, 90), (1080, 1920), (1080, 1080)]
    out["b01_invisible_ink"] = (ink([I("bg", 0, 0, 1080, 1350, "chaos_split_bg.png", fit="cover", z=0)]), tg,
                                "white text must stay on the dark half, dark legal on the light half")
    out["b01_invisible_ink_shapes"] = (ink([S("bg_dark", 0, 0, 1080, 675, "rgba(30,18,34,1)", z=0),
                                            S("bg_light", 0, 675, 1080, 675, LIGHT, z=0)]), tg,
                                       "two half-height background shapes instead of one image")

    tg = [(1920, 1080), (1080, 1920), (728, 90), (300, 250)]
    base = [S("bg", 0, 0, 1080, 1080, PLUM, z=0),
            I("logo", 480, 90, 120, 60, LOGO, z=5),
            T("headline", 90, 420, 900, 200, "The real message is small", 72, "Solstice Serif",
              font_weight="bold", text_align="center", z=5)]
    out["b02_decoy_giant"] = ({"canvas": sq, "elements": [
        *base, S("watermark", 40, 40, 1000, 1000, WHITE, "ellipse", z=1, opacity=0.05),
        *button("cta", 340, 760, 400, 90, "Shop now", 30, z=5)]}, tg,
        "a 5%-opacity 1000x1000 watermark must not become the hero")
    out["b02_decoy_image"] = ({"canvas": sq, "elements": [
        *base, I("ghost_bottle", 40, 40, 1000, 1000, BOTTLE, z=1, opacity=0.05),
        I("product", 440, 640, 200, 200, BOTTLE, z=4),
        *button("cta", 340, 880, 400, 90, "Shop now", 30, z=5)]}, tg,
        "a faint giant image vs the small opaque product: which one is the hero?")

    out["b03_shattered_badge"] = ({"canvas": sq, "elements": [
        BG(1080, 1080),
        I("logo", 390, 50, 300, 100, LOGO, z=3),
        T("headline", 90, 180, 900, 150, "Summer sale", 96, "Solstice Serif", font_weight="bold",
          text_align="center"),
        I("product", 240, 340, 600, 600, BOTTLE),
        S("badge_disc", 760, 380, 240, 240, RED, "ellipse", z=5),
        S("badge_ring", 780, 400, 200, 200, "rgba(0,0,0,0)", "ellipse", z=6, stroke_color=WHITE, stroke_width=6),
        T("badge_text", 800, 465, 160, 70, "SALE 50% OFF", 26, font_weight="bold", text_align="center", z=7),
        *button("cta", 340, 960, 400, 80, "Shop the sale", 28),
    ]}, [(1920, 1080), (728, 90), (1080, 1920), (1600, 400)],
        "three ungrouped, concentric pieces of one badge must move as one body")

    def gaze(px):
        return {"canvas": sq, "elements": [
            BG(1080, 1080),
            I("product", px, 80, 400, 800, "chaos_cropped_bottle.png"),
            I("logo", 60, 60, 270, 90, LOGO, z=3),
            T("headline", 60, 220, 580, 260, "It leans off the edge", 80, "Solstice Serif", font_weight="bold"),
            T("subhead", 60, 500, 560, 120, "A flat cut on the right wall.", 30),
            *button("cta", 60, 700, 360, 80, "Look closer", 28),
        ]}
    tg = [(1920, 1080), (1600, 400), (1080, 1920), (728, 90)]
    out["b04_gaze_flush"] = (gaze(680), tg, "a product cut flat at the right wall must stay flush right")
    out["b04_gaze_bleed"] = (gaze(780), tg, "the same product bleeding 100px past the right wall")

    out["b05_rotation"] = ({"canvas": sq, "elements": [
        BG(1080, 1080),
        I("logo", 390, 50, 300, 100, LOGO, z=3),
        T("headline", 90, 200, 900, 150, "Turn it up", 96, "Solstice Serif", font_weight="bold",
          text_align="center"),
        I("product", 340, 380, 400, 400, BOTTLE, rotation=30),
        S("ribbon_bg", 700, 160, 400, 80, RED, rotation=45, group_id="ribbon", z=6),
        T("ribbon_label", 700, 180, 400, 40, "LIMITED", 30, font_weight="bold", text_align="center",
          rotation=45, group_id="ribbon", z=7),
        T("side_label", -160, 520, 400, 40, "SMALL BATCH - SMALL BATCH", 22, rotation=-90, z=4),
        S("stamp", 120, 820, 200, 200, ORANGE, rotation=405, corner_radius=20, z=5),
        T("stamp_label", 120, 890, 200, 60, "NEW", 40, font_weight="bold", text_align="center", rotation=405, z=6),
        *button("cta", 600, 880, 400, 80, "Spin again", 28),
    ]}, [(1080, 1080), (728, 90), (1080, 1920), (1920, 1080), (300, 250)],
        "45-degree ribbon (AABB 339x339 from a 400x80 box), 30/-90/405-degree elements")

    out["b06_unbreakable"] = ({"canvas": sq, "elements": [
        BG(1080, 1080),
        I("logo", 390, 50, 300, 100, LOGO, z=3),
        T("headline", 60, 180, 960, 200, "Donaudampfschifffahrtsgesellschaftskapitänswitwe", 72, "Solstice Serif",
          font_weight="bold", text_align="center"),
        T("url", 90, 420, 900, 60, "HTTPS://SOLSTICE-BOTANICALS.COM/SUMMER-DROP", 34, font_weight="bold",
          text_align="center"),
        T("descenders", 90, 540, 900, 60, "gjpqy | ÅÖÜ ÇŞ gjpqy", 60, line_height=0.8, text_align="center"),
        *button("cta", 340, 800, 400, 90, "Read the label", 30),
    ]}, [(728, 90), (320, 50), (160, 600), (300, 250), (1080, 1920)],
        "no spaces to wrap at; descenders in a box exactly lines x font_size tall")

    def twins(contents):
        return {"canvas": sq, "elements": [BG(1080, 1080)] + [
            T(f"item_{i + 1}", 140, 220 + i * 130, 800, 60, c, 24, text_align="center")
            for i, c in enumerate(contents)]}
    tg = [(1920, 1080), (728, 90), (1080, 1920), (300, 250)]
    out["b07_twins"] = (twins(["Friday - The Marigolds", "Saturday - Low Sun Choir", "Sunday - Amber Static",
                               "Monday - The Late Bloomers", "Tuesday - Honey & Brass"]), tg,
                        "five equal list items: none may become a headline or a button")
    out["b07_twins_identical"] = (twins(["SOLD OUT"] * 5), tg, "five identical texts: order must stay stable")

    def occl(frame):
        return {"canvas": sq, "elements": [
            BG(1080, 1080),
            I("logo", 390, 50, 300, 100, LOGO, z=3),
            T("headline", 90, 170, 900, 150, "Framed and leafy", 88, "Solstice Serif", font_weight="bold",
              text_align="center"),
            I("product", 290, 360, 500, 500, BOTTLE),
            I("leaf", 620, 330, 300, 225, "chaos_leaf.png", z=10),
            *button("cta", 340, 900, 400, 80, "Step inside", 28),
            frame,
        ]}
    tg = [(1920, 1080), (728, 90), (1080, 1920), (1600, 400)]
    out["b08_z_occlusion"] = (occl(I("frame", 0, 0, 1080, 1080, "chaos_frame.png", z=99)), tg,
                              "a z=99 full-canvas frame and z=10 leaves must not cover copy or product")
    out["b08_product_frame"] = (occl(I("frame", 240, 310, 600, 600, "chaos_frame.png", z=99)), tg,
                                "a z=99 frame around the product only")

    out["b09_phantom_box"] = ({"canvas": sq, "elements": [
        BG(1080, 1080),
        T("new_tag", 90, 80, 900, 400, "NEW", 48, font_weight="bold", color=GOLD),
        I("product", 390, 500, 300, 300, BOTTLE),
        T("headline", 90, 820, 900, 90, "Small word, huge box", 56, "Solstice Serif", font_weight="bold",
          text_align="center"),
        *button("cta", 340, 940, 400, 80, "Shop", 28),
    ]}, [(1920, 1080), (728, 90), (1080, 1920)],
        "a 3-letter word in a 900x400 box must not push everything away")

    noz = T("no_z", 600, 760, 400, 40, "No z-index at all", 24)
    noz.pop("z_index")
    out["b10_corrupted"] = ({"canvas": sq, "elements": [
        BG(1080, 1080),
        I("logo", 60, 50, 270, 90, LOGO, z=3),
        T("headline", 90, 170, 640, 150, "Still standing", 88, "Solstice Serif", font_weight="bold"),
        I("holiday_badge", 760, 60, 240, 240, "solstice_holiday_badge_2025.png", z=4),
        T("ghost_text", 90, 340, 900, 60, "You cannot see me", 40, opacity=0.0),
        I("ghost_img", 820, 380, 200, 200, BOTTLE, opacity=0.0),
        S("spacer", 540, 540, 1, 1, PLUM),
        S("zero_box", 300, 300, 0, 0, ORANGE),
        T("dup_a", 90, 420, 600, 60, "Same layer A", 32, z=5),
        T("dup_b", 120, 430, 600, 60, "Same layer B", 32, z=5),
        noz,
        T("tiny", 90, 520, 400, 20, "one pixel type", 1),
        T("empty", 90, 560, 400, 40, "", 24),
        T("newlines", 90, 610, 400, 60, "\n\n", 24),
        T("clear_ink", 90, 680, 600, 50, "Transparent ink", 32, color="rgba(0,0,0,0)"),
        T("one_line", 90, 740, 480, 40, "max_lines is one but this sentence is much longer than that", 22,
          max_lines=1),
        T("runs", 90, 800, 600, 50, "Runs past the end", 30, style_runs=[{"start": 5, "end": 400, "color": GOLD}]),
        T("squeezed", 90, 860, 600, 50, "Negative letter spacing", 30, letter_spacing=-5),
        T("crushed", 90, 920, 600, 30, "Line height a tenth two lines here", 28, line_height=0.1),
        T("giant", 700, 860, 300, 120, "W", 2000),
        T("emoji", 600, 640, 420, 50, "🌞 Sunburst ☀️ ‍ zero​width", 28),
        T("rtl", 600, 700, 420, 50, "شمس الصيف", 30),
        T("far_away", 5000, 5000, 200, 50, "Off in the distance", 24),
        S("spun", 900, 900, 120, 60, ORANGE, rotation=3600),
        *button("cta", 340, 980, 400, 70, "Shop anyway", 26),
    ]}, [(1080, 1080), (728, 90), (38, 38), (1920, 1080)],
        "missing asset, opacity 0, 1x1 and 0x0 shapes, duplicate/missing z, 1px and 2000px type, "
        "empty/newline/emoji/RTL text, bad style runs, off-canvas source element")
    return out


# ------------------------------------------------------------------ the task set
def _task(tid, vector, trap, source, W, H, mask=None, note=""):
    tc = {"width": W, "height": H}
    if mask:
        tc["safe_mask"] = mask
    return {"task_id": tid, "tier": "chaos", "chaos": {"vector": vector, "trap": trap, "note": note},
            "source": source, "target_canvas": tc}


def _thresholds(src):
    """(name, aspect) for every aspect-ratio branch threshold of the engine,
    absolute or relative to this source's shape."""
    a = src["canvas"]["width"] / src["canvas"]["height"]
    return [("abs0.9", 0.9), ("abs1.0", 1.0), ("abs1.2", 1.2), ("abs2.0", 2.0), ("abs2.2", 2.2),
            ("same_up", a * 1.08), ("same_down", a / 1.08),
            ("reshape_up", a * 2 ** 1.5), ("reshape_down", a / 2 ** 1.5)]


def build() -> dict:
    """Write input/chaos/*.json and chains.json; returns {vector: task count}."""
    make_assets()
    brand_kit()
    CHAOS.mkdir(parents=True, exist_ok=True)
    for f in CHAOS.glob("*.json"):
        f.unlink()
    ds = designs(ROOT / "input")
    tasks, chains = {}, {}

    def add(t):
        tasks[t["task_id"]] = t

    # v1 zero-delta
    for d, src in ds.items():
        W, H = src["canvas"]["width"], src["canvas"]["height"]
        add(_task(f"v1_same__{d}", "v1", "exact same size: the designer's layout should survive untouched", src, W, H))
        add(_task(f"v1_eps_float__{d}", "v1", "sub-pixel float canvas (outside the integer schema)", src,
                  W + 0.001, H - 0.001))
        add(_task(f"v1_eps_int__{d}", "v1", "1px epsilon: must look like the same-size output", src, W + 1, H - 1))
        alt = (1920, 1080) if W <= H else (1080, 1920)
        chains[f"v1_pingpong__{d}"] = {"vector": "v1", "trap": "round trip: hysteresis / topological drift",
                                       "stages": [list(alt), [W, H], list(alt), [W, H]]}
        chains[f"v1_fixed__{d}"] = {"vector": "v1", "trap": "fixed point: an output fed back to its own size",
                                    "stages": [[1080, 1350], [1080, 1350], [1080, 1350]]}
    for d in ("s1", "s2", "s3"):
        src = ds[d]
        for name, r in _thresholds(src):
            H = 1000
            for side, W in (("lo", math.floor(H * r) - 1), ("hi", math.ceil(H * r) + 1)):
                add(_task(f"v1_edge__{d}__{name}_{side}", "v1",
                          f"aspect {W / H:.4f} straddles branch threshold {r:.4f}", src, W, H))
        for side, (W, H) in (("lo", (599, 200)), ("hi", (601, 200)), ("h250", (1000, 250)), ("h251", (1000, 251))):
            add(_task(f"v1_edge__{d}__banner_{side}", "v1", "straddles the banner rule (W/H >= 3 and H <= 250)",
                      src, W, H))
    for cid, c in chains.items():
        W, H = c["stages"][0]
        add(_task(f"{cid}__k1", c["vector"], c["trap"], ds[cid.split("__")[1]], W, H, note="chain stage 1"))

    # v2 scale
    for d, src in ds.items():
        add(_task(f"v2_watch__{d}", "v2", "38x38 watch face: legibility floors vs proportional shrink", src, 38, 38))
        add(_task(f"v2_atom__{d}", "v2", "1x1 canvas", src, 1, 1))
        add(_task(f"v2_prime__{d}", "v2", "prime 9973x9967: no integer grid divides it", src, 9973, 9967))
    for d in ("s1", "s2", "s3"):
        add(_task(f"v2_mega__{d}", "v2", "32768x32768 megascale: hard-coded px constants vanish", ds[d], 32768, 32768))

    # v3 quadrilaterals
    add(_task("v3_ribbon_to_ticker", "v3", "100x2000 -> 2000x100: top-to-bottom becomes left-to-right",
              _ribbon(), 2000, 100))
    add(_task("v3_ribbon_to_square", "v3", "100x2000 ribbon -> 1080x1080", _ribbon(), 1080, 1080))
    add(_task("v3_ticker_to_ribbon", "v3", "2000x100 -> 100x2000: left-to-right becomes top-to-bottom",
              _ticker(), 100, 2000))
    add(_task("v3_ticker_to_story", "v3", "2000x100 ticker -> 1080x1920", _ticker(), 1080, 1920))
    for d, src in ds.items():
        add(_task(f"v3_ribbon__{d}", "v3", "any design -> 100x2000 ribbon", src, 100, 2000))
        add(_task(f"v3_ticker__{d}", "v3", "any design -> 2000x100 ticker", src, 2000, 100))
        for m in ("parallelogram", "keystone"):
            W, H, spec = MASKS[m]
            add(_task(f"v3_{m}__{d}", "v3", f"{spec['shape']} safe zone", src, W, H, spec))

    # v4 exotic topologies
    for d, src in ds.items():
        for m in ("circle", "arch", "annulus", "star", "bottle"):
            W, H, spec = MASKS[m]
            add(_task(f"v4_{m}__{d}", "v4", f"{m} safe zone: everything outside is off-canvas", src, W, H, spec))

    # black swans
    for name, (src, targets, trap) in _swans().items():
        for W, H in targets:
            add(_task(f"{name}__{W}x{H}", "swan", trap, src, W, H))

    for tid, t in tasks.items():
        (CHAOS / f"{tid}.json").write_text(json.dumps(t, ensure_ascii=False))
    (CHAOS / "chains.json").write_text(json.dumps(chains, indent=1))
    counts = {}
    for t in tasks.values():
        counts[t["chaos"]["vector"]] = counts.get(t["chaos"]["vector"], 0) + 1
    counts["chain stages (generated during the run)"] = sum(len(c["stages"]) - 1 for c in chains.values())
    return counts


def load_tasks() -> list[Path]:
    if not any(CHAOS.glob("*.json")):
        build()
    return sorted(CHAOS.glob("*.json"))


# ------------------------------------------------------------------ running
def run(engine: Path, out: Path, shards: int = 12) -> dict:
    """Run the engine over every chaos task (chain stages one after another).
    Writes out/{json,png,tasks}/ and out/run.json; returns the run records."""
    engine, out = Path(engine).resolve(), Path(out).resolve()
    kit = brand_kit()
    files = [f for f in load_tasks() if f.name != "chains.json"]
    shutil.rmtree(out, ignore_errors=True)
    (out / "tasks").mkdir(parents=True)
    for f in files:
        shutil.copy(f, out / "tasks" / f.name)
    meta = {f.stem: json.loads(f.read_text()) for f in files}

    def area(t):
        tc = t["target_canvas"]
        return tc["width"] * tc["height"]
    normal = [f for f in files if area(meta[f.stem]) <= HEAVY_PX]
    heavy = [f for f in files if area(meta[f.stem]) > HEAVY_PX]
    results, t0 = {}, time.time()
    print(f"chaos: {len(normal)} tasks + {len(heavy)} heavy", flush=True)
    results.update(run_engine(engine, normal, out, shards=shards, brand_kit=kit))
    print(f"chaos: normal lane done in {time.time() - t0:.0f}s", flush=True)
    results.update(run_engine(engine, heavy, out, shards=2, brand_kit=kit, retry_timeout=1800))
    print(f"chaos: heavy lane done in {time.time() - t0:.0f}s", flush=True)

    chains = json.loads((CHAOS / "chains.json").read_text())
    for k in range(2, 1 + max(len(c["stages"]) for c in chains.values())):
        stage = []
        for cid, c in chains.items():
            if len(c["stages"]) < k:
                continue
            tid, prev = f"{cid}__k{k}", out / "json" / f"{cid}__k{k - 1}.json"
            if not prev.exists():
                results[tid] = {"skipped": f"stage {k - 1} produced no output"}
                continue
            W, H = c["stages"][k - 1]
            t = _task(tid, c["vector"], c["trap"], json.loads(prev.read_text()), W, H,
                      note=f"chain stage {k}: source is the engine's own stage-{k - 1} output")
            p = out / "tasks" / f"{tid}.json"
            p.write_text(json.dumps(t, ensure_ascii=False))
            meta[tid] = t
            stage.append(p)
        if stage:
            results.update(run_engine(engine, stage, out, shards=shards, brand_kit=kit))
    print(f"chaos: chains done in {time.time() - t0:.0f}s", flush=True)

    recs = {}
    for tid, t in sorted(meta.items()):
        r = results.get(tid, {"crash": "no output (crash or timeout)"})
        tc = t["target_canvas"]
        recs[tid] = {"vector": t["chaos"]["vector"], "trap": t["chaos"]["trap"],
                     "target": f"{tc['width']}x{tc['height']}" + (f" {tc['safe_mask']['type']}"
                                                                  if tc.get("safe_mask") else ""), **r}
    (out / "run.json").write_text(json.dumps(recs, indent=1, ensure_ascii=False))
    return recs


def run_summary(recs: dict) -> dict:
    """What happened when the tasks ran (no grading): per vector, how many
    completed, raised the pipeline's self-check warnings, crashed or were skipped."""
    s = {}
    for tid, r in recs.items():
        v = s.setdefault(r["vector"], {"tasks": 0, "ok": 0, "self_check_warnings": 0, "crashed": [],
                                       "skipped": [], "max_secs": 0.0})
        v["tasks"] += 1
        if "crash" in r:
            v["crashed"].append(tid)
        elif "skipped" in r:
            v["skipped"].append(tid)
        else:
            v["ok" if r["status"] == "OK" else "self_check_warnings"] += 1
            v["max_secs"] = max(v["max_secs"], r["secs"])
    return s


# ------------------------------------------------------------------ previews
_R = None


def _renderer():
    global _R
    if _R is None:
        from renderer.converter.convert import _get_renderer
        _R = _get_renderer(str(KIT / "fonts"))
    return _R


def render(design: dict, max_side: int, keep=None) -> np.ndarray:
    """RGBA render of the design (or the elements `keep` accepts) with the
    longest side at max_side px."""
    from renderer.converter.convert import convert_chameleon_to_crello
    els = [e for e in design["elements"] if keep is None or keep(e)]
    if not els:  # the schema needs one element: an invisible speck
        els = [{"id": "__void", "type": "shape", "x": 0, "y": 0, "width": 1, "height": 1,
                "shape_type": "rectangle", "fill_color": "rgba(0,0,0,0)", "opacity": 0}]
    ex, _ = convert_chameleon_to_crello(dict(design, elements=els), _renderer().font_manager, str(KIT / "assets"))
    im = Image.open(io.BytesIO(_renderer().render(ex, max_size=max(1, max_side), format="png"))).convert("RGBA")
    return np.asarray(im)


def _fit(im: Image.Image, w: int, h: int) -> Image.Image:
    k = min(w / im.width, h / im.height)
    size = (max(1, round(im.width * k)), max(1, round(im.height * k)))
    return im.resize(size, Image.NEAREST if k > 1 else Image.LANCZOS)


def _shade_mask(im: Image.Image, spec, W, H) -> Image.Image:
    """Tint everything outside the safe zone red so a bleed is visible."""
    if not spec:
        return im
    m = mask_array(spec, W, H, im.width / W)
    m = np.asarray(Image.fromarray(m).resize(im.size))
    a = np.asarray(im.convert("RGB")).astype(float)
    a[~m] = 0.55 * a[~m] + 0.45 * np.array([255, 0, 0])
    return Image.fromarray(a.astype(np.uint8))


def sheets(out: Path, cell: int = 300) -> list[Path]:
    """Contact sheets for eyeballing a run: one per vector / black swan,
    each row the source and its output(s) (whole chains on one row), safe
    zones shaded red outside."""
    out = Path(out)
    Image.MAX_IMAGE_PIXELS = None
    recs = json.loads((out / "run.json").read_text())
    tasks = {p.stem: json.loads(p.read_text()) for p in (out / "tasks").glob("*.json")}
    font = ImageFont.load_default()
    groups = {}
    for tid in sorted(tasks):
        t = tasks[tid]
        g = t["chaos"]["vector"] if t["chaos"]["vector"] != "swan" else tid.split("__")[0]
        if "__k" in tid:
            g = "v1_chains"
        groups.setdefault(g, []).append(tid)
    src_cache, made = {}, []
    (out / "sheets").mkdir(exist_ok=True)

    def src_thumb(src):
        key = json.dumps(src, sort_keys=True)
        if key not in src_cache:
            try:
                src_cache[key] = Image.fromarray(render(src, cell)).convert("RGB")
            except Exception:
                src_cache[key] = Image.new("RGB", (cell, cell), (90, 90, 90))
        return src_cache[key]

    def out_thumb(tid):
        p = out / "png" / f"{tid}.png"
        t = tasks[tid]
        if not p.exists():
            im = Image.new("RGB", (cell, cell // 2), (120, 20, 20))
            ImageDraw.Draw(im).text((8, 8), "NO OUTPUT", fill=(255, 255, 255), font=font)
            return im
        with Image.open(p) as im:
            im = _fit(im.convert("RGB"), cell, cell)
        tc = t["target_canvas"]
        return _shade_mask(im, tc.get("safe_mask"), tc["width"], tc["height"])

    for g, tids in groups.items():
        rows = []
        if g == "v1_chains":
            for cid in sorted({t.rsplit("__k", 1)[0] for t in tids}):
                ks = sorted((t for t in tids if t.startswith(cid + "__k")), key=lambda t: int(t.rsplit("k", 1)[1]))
                rows.append((cid, [src_thumb(tasks[ks[0]]["source"])] + [out_thumb(k) for k in ks],
                             ["source"] + [f"k{k.rsplit('k', 1)[1]} {recs.get(k, {}).get('target', '')} "
                                           f"{recs.get(k, {}).get('status', 'CRASH')}" for k in ks]))
        else:
            for tid in tids:
                st = recs.get(tid, {})
                status = st.get("status") or ("CRASH" if "crash" in st else "skipped")
                rows.append((tid, [src_thumb(tasks[tid]["source"]), out_thumb(tid)],
                             ["source", f"{recs.get(tid, {}).get('target', '')}  {status}"]))
        per_row = max(len(r[1]) for r in rows)
        cols = 1 if per_row > 2 else 3
        cw = per_row * (cell + 8) + 8
        rh = cell + 44
        n = math.ceil(len(rows) / cols)
        sheet = Image.new("RGB", (cols * cw, n * rh), (24, 24, 28))
        d = ImageDraw.Draw(sheet)
        for i, (label, ims, caps) in enumerate(rows):
            ox, oy = (i % cols) * cw, (i // cols) * rh
            d.text((ox + 8, oy + 4), label, fill=(255, 255, 255), font=font)
            for j, (im, cap) in enumerate(zip(ims, caps)):
                x = ox + 8 + j * (cell + 8)
                sheet.paste(im, (x + (cell - im.width) // 2, oy + 20 + (cell - im.height) // 2))
                d.text((x, oy + 22 + cell), cap, fill=(200, 200, 200), font=font)
        p = out / "sheets" / f"{g}.jpg"
        sheet.save(p, quality=85)
        made.append(p)
    return made


# ------------------------------------------------------------------ verification
def _corners(el):
    x, y, w, h = el["x"], el["y"], el["width"], el["height"]
    r = math.radians(float(el.get("rotation", 0) or 0))
    cx, cy = x + w / 2, y + h / 2
    c, s = math.cos(r), math.sin(r)
    return [(cx + dx * c - dy * s, cy + dx * s + dy * c) for dx, dy in
            ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]


def _dilate(m: np.ndarray) -> np.ndarray:
    p = np.pad(m, 1)
    return np.logical_or.reduce([p[1 + dy:p.shape[0] - 1 + dy, 1 + dx:p.shape[1] - 1 + dx]
                                 for dy in (-1, 0, 1) for dx in (-1, 0, 1)])


def _lum(rgb):
    from evaluator.judge_agent import lum
    return lum(np.asarray(rgb, float))


def _norm_boxes(design):
    cw, ch = design["canvas"]["width"], design["canvas"]["height"]
    return {e["id"]: (e["x"] / cw, e["y"] / ch, (e["x"] + e["width"]) / cw, (e["y"] + e["height"]) / ch)
            for e in design["elements"] if e.get("width", 0) > 0 and e.get("height", 0) > 0}


def layout_iou(a: dict, b: dict) -> float:
    """Mean IoU of the same elements in two designs, each in its own canvas's
    normalised coordinates (1.0 = the same layout)."""
    A, B = _norm_boxes(a), _norm_boxes(b)
    ious = []
    for i in set(A) & set(B):
        p, q = A[i], B[i]
        inter = max(0, min(p[2], q[2]) - max(p[0], q[0])) * max(0, min(p[3], q[3]) - max(p[1], q[1]))
        u = (p[2] - p[0]) * (p[3] - p[1]) + (q[2] - q[0]) * (q[3] - q[1]) - inter
        ious.append(inter / u if u > 0 else 1.0)
    return float(np.mean(ious)) if ious else 1.0


def _kendall(src_keys: dict, out_keys: dict):
    ids = [i for i in src_keys if i in out_keys]
    disc = tot = 0
    inv = []
    for a, b in itertools.combinations(ids, 2):
        ds, do = src_keys[a] - src_keys[b], out_keys[a] - out_keys[b]
        if ds == 0 or do == 0:
            continue
        tot += 1
        if ds * do < 0:
            disc += 1
            if abs(do) > 0.1:
                inv.append(f"{a}<->{b}")
    return (disc / tot if tot else 0.0), inv


def verify_task(task: dict, out: dict | None, crash: str | None = None) -> dict:
    """Every check on one task. Returns {task, disqualify: [...], flags: [...], metrics: {...}}."""
    from evaluator.judge_agent import hard_rules, is_background, is_decor
    tid = task["task_id"]
    rec = {"task": tid, "vector": task["chaos"]["vector"], "disqualify": [], "flags": [], "metrics": {}}
    if out is None:
        rec["disqualify"].append(f"CRASH: {(crash or 'no output')[-300:]}")
        return rec
    src, tgt = task["source"], task["target_canvas"]
    W, H = tgt["width"], tgt["height"]
    sw, sh = src["canvas"]["width"], src["canvas"]["height"]
    for h in hard_rules(src, out, tgt):
        rec["disqualify"].append(f"HARD RULE: {h}")
    els = out["elements"]
    so = {e["id"]: e for e in src["elements"]}
    s = min(1.0, ANALYSIS / max(W, H))
    side = max(1, round(max(W, H) * s))
    spec = tgt.get("safe_mask")
    M = mask_array(spec, W, H, s)
    M_tol = _dilate(M)  # one analysis pixel of tolerance
    order = sorted(range(len(els)), key=lambda k: (els[k].get("z_index", 0) or 0, k))
    pos = {els[k]["id"]: p for p, k in enumerate(order)}
    full = render(out, side)
    void = render(out, side, keep=lambda e: False)

    def same_shape(a):
        if a.shape[:2] != M.shape:
            a = np.asarray(Image.fromarray(a).resize((M.shape[1], M.shape[0])))
        return a
    full, void = same_shape(full), same_shape(void)

    def footprint(e):
        r = same_shape(render(out, side, keep=lambda x: x["id"] == e["id"]))
        return (np.abs(r.astype(int) - void.astype(int)).max(axis=2) > 8)

    contrast = {}
    for e in els:
        visible = float(e.get("opacity", 1) if e.get("opacity") is not None else 1) > 0 \
            and e.get("width", 0) > 0 and e.get("height", 0) > 0
        if not visible or e["id"] not in so:
            continue
        bg_el = is_background(so[e["id"]], sw, sh)
        # rotated corners inside the canvas and the safe zone
        if not bg_el:
            for (x, y) in _corners(e):
                px, py = int(math.floor(x * s)), int(math.floor(y * s))
                inside = 0 <= px < M.shape[1] and 0 <= py < M.shape[0] and M_tol[py, px]
                if not inside and not (x < -0.5 or y < -0.5 or x > W + 0.5 or y > H + 0.5) and spec:
                    rec["disqualify"].append(f"OFF-MASK VERTEX: {e['id']} ({x:.0f},{y:.0f}) outside the {spec['type']}")
                    break
                if x < -0.5 or y < -0.5 or x > W + 0.5 or y > H + 0.5:
                    rec["disqualify"].append(f"ROTATED CORNER OFF-CANVAS: {e['id']} ({x:.0f},{y:.0f})")
                    break
        A = footprint(e)
        if spec and not bg_el and A.any():
            bleed = float((A & ~M_tol).sum() / A.sum())
            if bleed > 0.005:
                rec["disqualify"].append(f"OFF-CANVAS VIOLATION (mask): {e['id']} {bleed:.1%} outside the {spec['type']}")
        if e["type"] != "text" or not str(e.get("content", "")).strip():
            continue
        if not A.any():
            if e.get("font_size", 0) * s >= 2 and "rgba(0,0,0,0)" not in str(e.get("color")):
                rec["flags"].append(f"TEXT NOT RENDERED: {e['id']}")
            continue
        # occlusion by higher layers
        below = {els[k]["id"] for k in order[:pos[e["id"]] + 1]}
        upto = same_shape(render(out, side, keep=lambda x: x["id"] in below))
        occ = A & (np.abs(full.astype(int) - upto.astype(int)).max(axis=2) > 24)
        frac = float(occ.sum() / A.sum())
        rec["metrics"].setdefault("occlusion", {})[e["id"]] = round(frac, 4)
        if frac > 0.02:
            rec["disqualify"].append(f"CRITICAL TEXT OCCLUSION FAILURE: {e['id']} {frac:.1%} of its glyphs covered")
        # contrast: the text's strongest pixels against what is under them
        bg = same_shape(render(out, side, keep=lambda x: x["id"] != e["id"]))
        lt, lb = _lum(full[..., :3][A]), _lum(bg[..., :3][A])
        core = np.abs(lt - lb) >= np.quantile(np.abs(lt - lb), 0.5)
        a, b = float(np.median(lt[core])), float(np.median(lb[core]))
        c = (max(a, b) + 0.05) / (min(a, b) + 0.05)
        contrast[e["id"]] = c
        rec["metrics"].setdefault("contrast", {})[e["id"]] = round(c, 2)
        if c < 3.0:
            rec["flags"].append(f"ILLEGIBLE CONTRAST COLLISION: {e['id']} {c:.2f}:1")

    # reading order: content items (groups as one) by y + 0.3 x of their centre
    def keys(design, cw, ch):
        items = {}
        for e in design["elements"]:
            se = so.get(e["id"])
            if se is None or is_background(se, sw, sh) or is_decor(se) or e.get("width", 0) <= 0 \
                    or e.get("height", 0) <= 0 or not float(e.get("opacity", 1) if e.get("opacity") is not None else 1):
                continue
            k = se.get("group_id") or e["id"]
            b = items.get(k)
            box = (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
            items[k] = box if b is None else (min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]), max(b[3], box[3]))
        return {k: ((b[1] + b[3]) / 2 / ch) + 0.3 * ((b[0] + b[2]) / 2 / cw) for k, b in items.items()}
    tau, inv = _kendall(keys(src, sw, sh), keys(out, W, H))
    rec["metrics"]["kendall_tau_distance"] = round(tau, 3)
    if tau > 0.3 or inv:
        rec["flags"].append(f"READING ORDER: tau distance {tau:.2f}" + (f", inversions {inv[:6]}" if inv else ""))

    # centre of mass against the safe zone's centroid
    tot = mx = my = 0.0
    for e in els:
        se = so.get(e["id"])
        if se is None or is_background(se, sw, sh):
            continue
        x0, y0 = max(0, e["x"]), max(0, e["y"])
        x1, y1 = min(W, e["x"] + e["width"]), min(H, e["y"] + e["height"])
        a = max(0, x1 - x0) * max(0, y1 - y0) * float(e.get("opacity", 1) if e.get("opacity") is not None else 1)
        a *= min(contrast.get(e["id"], 7.0), 7.0) / 7.0
        tot, mx, my = tot + a, mx + a * (x0 + x1) / 2, my + a * (y0 + y1) / 2
    ys, xs = np.nonzero(M)
    if tot > 0 and len(xs):
        cx, cy = (xs.mean() + 0.5) / s, (ys.mean() + 0.5) / s
        ex, ey = (xs.max() - xs.min() + 1) / s, (ys.max() - ys.min() + 1) / s
        dx, dy = (mx / tot - cx) / ex, (my / tot - cy) / ey
        rec["metrics"]["mass_offset"] = [round(dx, 3), round(dy, 3)]
        if abs(dx) > 0.2 or abs(dy) > 0.2:
            rec["flags"].append(f"OPTICAL SKEW: mass centre off the safe zone's centroid by ({dx:+.2f}, {dy:+.2f})")

    # drift: same size and fixed points should keep the layout
    if tid.startswith("v1_same__"):
        iou = layout_iou(src, out)
        rec["metrics"]["iou_vs_source"] = round(iou, 3)
        if iou < 0.9:
            rec["flags"].append(f"LAYOUT DRIFT: same-size output differs from the designer's layout (IoU {iou:.2f})")
    return rec


def _verify_one(args):
    task_path, out_dir, crash = args
    task = json.loads(Path(task_path).read_text())
    j = Path(out_dir) / "json" / f"{task['task_id']}.json"
    try:
        return verify_task(task, json.loads(j.read_text()) if j.exists() else None, crash)
    except Exception as ex:  # a verifier bug must not hide the other tasks
        return {"task": task["task_id"], "vector": task["chaos"]["vector"], "disqualify": [],
                "flags": [f"VERIFIER ERROR: {type(ex).__name__}: {ex}"], "metrics": {}}


def verify(out: Path, workers: int = 12) -> dict:
    """Verify a run directory; writes out/verify.jsonl and out/verify_summary.json."""
    out = Path(out)
    brand_kit()
    recs = json.loads((out / "run.json").read_text())
    jobs = [(str(p), str(out), recs.get(p.stem, {}).get("crash")) for p in sorted((out / "tasks").glob("*.json"))
            if "skipped" not in recs.get(p.stem, {})]
    with ProcessPoolExecutor(workers) as ex:
        res = list(ex.map(_verify_one, jobs))
    by = {r["task"]: r for r in res}
    # cross-task drift: ping-pong / fixed-point chains and epsilon canvases
    def design(tid):
        p = out / "json" / f"{tid}.json"
        return json.loads(p.read_text()) if p.exists() else None
    chains = json.loads((CHAOS / "chains.json").read_text())
    for cid, c in chains.items():
        outs = [design(f"{cid}__k{k}") for k in range(1, len(c["stages"]) + 1)]
        if any(o is None for o in outs):
            continue
        src = json.loads((out / "tasks" / f"{cid}__k1.json").read_text())["source"]
        last = by.get(f"{cid}__k{len(outs)}")
        if last is None:
            continue
        if "pingpong" in cid:
            iou = layout_iou(src, outs[-1])
            last["metrics"]["iou_after_round_trips"] = round(iou, 3)
            if iou < 0.8:
                last["flags"].append(f"HYSTERESIS: two round trips leave IoU {iou:.2f} with the source")
        else:
            iou = layout_iou(outs[0], outs[-1])
            last["metrics"]["iou_fixed_point"] = round(iou, 3)
            if iou < 0.95:
                last["flags"].append(f"LAYOUT DRIFT: re-running at the same size moved things (IoU {iou:.2f})")
    for tid, r in by.items():
        if tid.startswith("v1_eps_"):
            base = design(tid.replace("v1_eps_float__", "v1_same__").replace("v1_eps_int__", "v1_same__"))
            o = design(tid)
            if base and o:
                iou = layout_iou(base, o)
                r["metrics"]["iou_vs_same_size"] = round(iou, 3)
                if iou < 0.9:
                    r["flags"].append(f"BRANCH FLIP: a 1px / sub-pixel change rearranged the layout (IoU {iou:.2f})")
        if tid.startswith("v1_edge__") and tid.endswith("_hi"):
            lo, hi = design(tid[:-3] + "_lo"), design(tid)
            if lo and hi:
                iou = layout_iou(lo, hi)
                r["metrics"]["iou_across_threshold"] = round(iou, 3)
                if iou < 0.6:
                    r["flags"].append(f"BRANCH FLIP: 2px across a threshold rearranged the layout (IoU {iou:.2f})")
    with open(out / "verify.jsonl", "w") as f:
        for r in res:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    kinds = {}
    for r in res:
        for x in r["disqualify"] + r["flags"]:
            k = x.split(":")[0]
            kinds[k] = kinds.get(k, 0) + 1
    dq = sorted(r["task"] for r in res if r["disqualify"])
    summary = {"tasks": len(res), "disqualified": bool(dq), "disqualified_tasks": dq,
               "reason": next((r["disqualify"][0] for r in res if r["disqualify"]), None), "by_kind": kinds}
    (out / "verify_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    return summary


def evaluate(engine: Path, out: Path, workers: int = 12) -> dict:
    """Run + verify; what the overnight tournament calls for a candidate."""
    run(engine, out, shards=workers)
    return verify(out, workers=workers)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="(re)write input/chaos/")
    ap.add_argument("--run", action="store_true", help="run the engine over the gauntlet (no grading)")
    ap.add_argument("--verify", action="store_true", help="run the pixel and geometry checks on --out")
    ap.add_argument("--engine", type=Path, default=ROOT / "engine" / "resize.py")
    ap.add_argument("--out", type=Path, default=RUNS / "latest")
    ap.add_argument("--shards", type=int, default=12)
    a = ap.parse_args()
    if a.build or not any(CHAOS.glob("*.json")):
        print(json.dumps(build(), indent=1))
    if a.run:
        recs = run(a.engine, a.out, a.shards)
        print(json.dumps(run_summary(recs), indent=1))
        for p in sheets(a.out):
            print("sheet", p)
    if a.verify:
        print(json.dumps(verify(a.out, a.shards), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
