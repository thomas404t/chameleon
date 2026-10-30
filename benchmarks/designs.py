"""Source designs for the benchmark suites.

The three official samples (input/) are the warm-up, core and advanced tiers.
Six extra designs, built from the same brand kit, cover structures the
samples don't (left-aligned editorial copy, a tall story source, text on a
light solid panel, a product grid, a fill-fit background with a card, a
badge wall), so the tournament can't win by fitting three designs. Edge
cases (zero sizes, giant words, sixty texts...) are crash and rule tests.
"""
import copy
import json
from pathlib import Path

W = "rgba(255,255,255,1)"
ORANGE = "rgba(232,106,58,1)"
GOLD = "rgba(240,178,66,1)"
PLUM = "rgba(43,27,46,1)"


def T(id, x, y, w, h, content, size, family="Solstice Sans", **kw):
    d = dict(id=id, type="text", x=x, y=y, width=w, height=h, content=content,
             font_family=family, font_size=size, color=kw.pop("color", W), z_index=kw.pop("z", 3))
    d.update(kw)
    return d


def I(id, x, y, w, h, asset, fit="contain", z=2, **kw):
    d = dict(id=id, type="image", x=x, y=y, width=w, height=h, asset_id=asset, fit=fit, z_index=z)
    d.update(kw)
    return d


def S(id, x, y, w, h, fill, shape="rectangle", z=1, **kw):
    d = dict(id=id, type="shape", x=x, y=y, width=w, height=h, shape_type=shape, fill_color=fill, z_index=z)
    d.update(kw)
    return d


def button(prefix, x, y, w, h, label, size, fill=ORANGE, z=3):
    return [S(f"{prefix}_bg", x, y, w, h, fill, corner_radius=h / 2, group_id=prefix, z=z),
            T(f"{prefix}_label", x, y + h * 0.25, w, h * 0.5, label, size, font_weight="bold",
              text_align="center", group_id=prefix, z=z + 1)]


BG = lambda w, h: I("bg", 0, 0, w, h, "solstice_bg_gradient.png", fit="cover", z=0)

# --- holdout designs: structures the engine was not tuned on -----------------
HOLDOUTS = {}

# H1: landscape editorial, LEFT-aligned copy column + product on the right
HOLDOUTS["h1_editorial_landscape"] = {"canvas": {"width": 1200, "height": 628}, "elements": [
    BG(1200, 628),
    I("logo", 60, 40, 210, 70, "solstice_logo_lockup.png", z=3),
    T("headline", 60, 150, 560, 150, "Golden hour, bottled.", 64, "Solstice Serif", font_weight="bold"),
    T("subhead", 60, 310, 540, 90, "Four new botanical infusions, small-batch and lightly sparkling.", 24,
      color="rgba(255,255,255,0.85)"),
    *button("cta", 60, 440, 300, 70, "Find a store", 22),
    I("product", 700, 40, 460, 548, "solstice_bottle.png", z=2),
]}

# H2: tall story source: logo, headline, big product, subhead, CTA, legal
HOLDOUTS["h2_story_source"] = {"canvas": {"width": 1080, "height": 1920}, "elements": [
    BG(1080, 1920),
    I("logo", 390, 80, 300, 100, "solstice_logo_lockup.png", z=3),
    T("headline", 80, 240, 920, 260, "Summer tastes like this", 96, "Solstice Serif", font_weight="bold",
      text_align="center"),
    I("product", 190, 540, 700, 700, "solstice_bottle.png"),
    T("subhead", 120, 1290, 840, 140, "Blood orange, wild honey and rosemary. Pour over ice.", 34,
      text_align="center", color="rgba(255,255,255,0.9)"),
    *button("cta", 290, 1500, 500, 110, "Order now", 36),
    T("legal", 80, 1800, 920, 50, "Please enjoy responsibly. Offer valid while supplies last.", 18,
      text_align="center", color="rgba(255,255,255,0.6)"),
]}

# H3: text-only, SOLID SHAPE background (light), dark text -> contrast + shape-bg handling
HOLDOUTS["h3_text_only_light"] = {"canvas": {"width": 1080, "height": 1080}, "elements": [
    S("bg_panel", 0, 0, 1080, 1080, "rgba(250,240,225,1)", z=0),
    S("accent_bar", 90, 140, 120, 12, ORANGE, z=1),
    T("kicker", 90, 180, 900, 50, "THE SUMMER EDIT", 28, font_weight="bold", color=ORANGE),
    T("headline", 90, 250, 900, 330, "Light, bright and a little bit golden.", 88, "Solstice Serif",
      font_weight="bold", color=PLUM),
    T("body", 90, 620, 800, 160, "Our botanical sparkling range is back with two limited flavours. "
      "Available in stores from June 1.", 30, color="rgba(43,27,46,0.85)"),
    *button("cta", 90, 860, 360, 90, "Discover the range", 26),
]}

# H4: three-product grid + headline + CTA
HOLDOUTS["h4_product_grid"] = {"canvas": {"width": 1080, "height": 1080}, "elements": [
    BG(1080, 1080),
    I("logo", 400, 50, 280, 90, "solstice_logo_lockup.png", z=3),
    T("headline", 90, 170, 900, 120, "Pick your golden hour", 64, "Solstice Serif", font_weight="bold",
      text_align="center"),
    I("product_a", 60, 330, 300, 300, "solstice_bottle.png"),
    I("product_b", 390, 330, 300, 300, "solstice_bottle.png"),
    I("product_c", 720, 330, 300, 300, "solstice_bottle.png"),
    T("name_a", 60, 650, 300, 40, "Blood Orange", 26, text_align="center", font_weight="bold"),
    T("name_b", 390, 650, 300, 40, "Wild Honey", 26, text_align="center", font_weight="bold"),
    T("name_c", 720, 650, 300, 40, "Rosemary", 26, text_align="center", font_weight="bold"),
    *button("cta", 340, 790, 400, 90, "Shop the trio", 28),
    T("legal", 90, 990, 900, 36, "Prices may vary by retailer.", 16, text_align="center",
      color="rgba(255,255,255,0.6)"),
]}

# H5: full-bleed background with fit="fill" (not cover) + opaque content card behind text
HOLDOUTS["h5_fill_bg_card"] = {"canvas": {"width": 1080, "height": 1350}, "elements": [
    I("bg", 0, 0, 1080, 1350, "solstice_bg_gradient.png", fit="fill", z=0),
    S("card", 120, 700, 840, 520, "rgba(250,240,225,1)", corner_radius=24, z=1),
    I("product", 290, 80, 500, 560, "solstice_bottle.png"),
    T("headline", 180, 760, 720, 160, "Chase the Light", 80, "Solstice Serif", font_weight="bold",
      color=PLUM, text_align="center"),
    T("subhead", 180, 940, 720, 110, "Botanical sparkling infusions, crafted for golden hour.", 30,
      color=PLUM, text_align="center"),
    *button("cta", 340, 1090, 400, 90, "Shop now", 28, z=2),
]}

# H6: many elements (badge wall) -> scale / performance
_badges = []
for i in range(12):
    r, c = divmod(i, 4)
    _badges += button(f"tag{i}", 60 + c * 250, 760 + r * 70, 220, 50,
                      ["ORGANIC", "VEGAN", "NO SUGAR", "LOCAL", "GLUTEN FREE", "SPARKLING", "SMALL BATCH",
                       "CANE FREE", "KETO", "LOW CAL", "NEW", "AWARD 2026"][i], 15,
                      fill=ORANGE if i % 2 == 0 else PLUM)
HOLDOUTS["h6_badge_wall"] = {"canvas": {"width": 1080, "height": 1080}, "elements": [
    BG(1080, 1080),
    I("logo", 400, 40, 280, 90, "solstice_logo_lockup.png", z=3),
    I("product", 340, 150, 400, 400, "solstice_bottle.png"),
    T("headline", 90, 570, 900, 90, "Everything you want, nothing you don't", 44, "Solstice Serif",
      font_weight="bold", text_align="center"),
    T("subhead", 140, 670, 800, 60, "Twelve reasons to switch this summer.", 24, text_align="center"),
    *_badges,
]}


def edge_cases():
    base = HOLDOUTS["h2_story_source"]
    out = {}

    def mk(name, design, tgt):
        out[name] = {"task_id": name, "source": design, "target_canvas": {"width": tgt[0], "height": tgt[1]}}

    d = copy.deepcopy(base); d["elements"][3]["width"] = 0
    mk("e01_zero_width_image", d, (1200, 628))
    d = copy.deepcopy(base); d["elements"][3]["height"] = 0
    mk("e02_zero_height_image", d, (1080, 1080))
    d = copy.deepcopy(base); d["elements"][2]["content"] = ""
    mk("e03_empty_text", d, (1200, 628))
    d = copy.deepcopy(base); d["elements"][2]["content"] = "Pneumonoultramicroscopicsilicovolcanoconiosis-Supercalifragilistic"
    mk("e04_giant_word", d, (300, 250))
    mk("e05_bg_only", {"canvas": {"width": 1080, "height": 1080}, "elements": [BG(1080, 1080)]}, (728, 90))
    d = copy.deepcopy(base); d["elements"].append(T("offcanvas", 1300, -200, 300, 80, "Off canvas", 30))
    mk("e06_offcanvas_source", d, (1080, 1080))
    d = copy.deepcopy(base); d["elements"][2]["content"] = "Line one\nLine two\nLine three"
    mk("e07_newlines", d, (1600, 400))
    d = copy.deepcopy(base); d["elements"][2]["content"] = "Été ☀️ 夏 — Ünïcödé “quotes”"
    mk("e08_unicode", d, (1080, 1080))
    d = copy.deepcopy(base)
    for e in d["elements"]:
        e.pop("z_index", None); e.pop("fit", None); e.pop("text_align", None)
    mk("e09_minimal_fields", d, (1280, 720))
    d = copy.deepcopy(base); d["elements"][3]["rotation"] = 90
    mk("e10_rotated_image", d, (1584, 396))
    d = copy.deepcopy(base); d["elements"][1]["asset_id"] = "brand_mark.png"
    mk("e11_logo_renamed_missing", d, (728, 90))
    big = {"canvas": {"width": 1080, "height": 1080}, "elements": [BG(1080, 1080)]}
    for i in range(60):
        r, c = divmod(i, 6)
        big["elements"].append(T(f"t{i}", 20 + c * 175, 20 + r * 105, 160, 90, f"Item number {i}", 18))
    mk("e12_sixty_texts", big, (1600, 400))
    d = copy.deepcopy(base); d["elements"].append(S("solo_group", 10, 10, 50, 50, ORANGE, group_id="g_solo"))
    mk("e13_single_member_group", d, (1080, 1080))
    d = copy.deepcopy(base); d["elements"].append(T("tiny", 10, 10, 2, 2, "x", 1))
    mk("e14_tiny_text_1px", d, (160, 600))
    mk("e15_same_size", copy.deepcopy(base), (1080, 1920))
    return out


OFFICIAL = {
    "s1": "chameleon_sample_level1_warmup.json",
    "s2": "chameleon_sample_level2_core.json",
    "s3": "chameleon_sample_level3_advanced.json",
}

# difficulty tier of each design (official samples define the tiers)
TIERS = {
    "warmup": ["s1", "h3_text_only_light", "h5_fill_bg_card"],
    "core": ["s2", "h1_editorial_landscape", "h4_product_grid"],
    "advanced": ["s3", "h2_story_source", "h6_badge_wall"],
}


def designs(input_dir: Path = Path("input")) -> dict:
    """All source designs by name: official samples first, then the extras."""
    out = {}
    for name, fname in OFFICIAL.items():
        out[name] = json.loads((input_dir / fname).read_text())["source"]
    out.update(copy.deepcopy(HOLDOUTS))
    return out


def tier_of(design: str) -> str:
    return next((t for t, ds in TIERS.items() if design in ds), "edge")
