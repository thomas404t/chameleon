"""Candidate generation: mutate the engine's GENOME block.

engine/resize.py keeps every layout heuristic in one literal dict between
`# >>> GENOME` and `# <<< GENOME`. A candidate is a copy of a seed engine with
that block rewritten, so every candidate is a complete, standalone
resize.py, the only file a submission contains.

Four variant strategies, each a design hypothesis with its own genes:
  A hero-centric anchor      optical centre of gravity, hero prominence, glow
  B dynamic grid & zoning    columns / zones per format, shared top lines
  C typography & text flow   type-scale compression, banner priority, cohesion
  D constraint & elastic     repulsion pass, clearance, spacing springs
"""
import ast
import json
import random
import re
from pathlib import Path

MARK = re.compile(r"(# >>> GENOME\n)(.*?)(# <<< GENOME)", re.S)

FAMILIES = {
    "A": ("hero-anchor", ["optical_lift", "hero_mass", "focus_blend", "hero_floor", "image_floor",
                          "w_hero", "w_bal", "logo_area"]),
    "B": ("grid-zoning", ["split_bias", "zone_top_align", "zone_header", "col_penalty", "w_struct", "w_order",
                          "col_gap", "w_fill", "symmetry", "brand_weight"]),
    "C": ("type-flow", ["type_gamma", "type_k", "priority_boost", "min_font", "min_font_frac", "text_gap",
                        "headline_lead", "w_leg", "w_size"]),
    "D": ("elastic-repulsion", ["relax_iters", "relax_clear", "relax_spring", "margin", "gap", "pad_y",
                                "body_gap_cap", "button_gap"]),
}

# (low, high, kind); "int" genes are rounded; "switch" genes are off at 0
BOUNDS = {
    "margin": (0.025, 0.08, "float"), "gap": (0.012, 0.045, "float"), "pad_y": (0.5, 4.0, "float"),
    "body_gap_cap": (0.8, 2.5, "float"), "col_gap": (0.01, 0.05, "float"), "button_gap": (0.15, 0.6, "float"),
    "text_gap": (0.5, 1.6, "float"),
    "type_gamma": (0.35, 0.9, "float"), "type_k": (3.0, 10.0, "float"), "priority_boost": (1.0, 2.6, "float"),
    "min_font": (8.0, 16.0, "float"), "min_font_frac": (0.05, 0.16, "float"),
    "hero_floor": (0.35, 0.75, "float"), "image_floor": (0.15, 0.45, "float"), "logo_area": (0.025, 0.08, "float"),
    "w_leg": (0.08, 0.4, "float"), "w_size": (0.06, 0.32, "float"), "w_hero": (0.06, 0.32, "float"),
    "w_order": (0.04, 0.25, "float"), "w_struct": (0.04, 0.25, "float"), "w_bal": (0.02, 0.2, "float"),
    "w_fill": (0.02, 0.2, "float"),
    "col_penalty": (0.8, 1.0, "float"), "brand_weight": (0.1, 0.5, "float"), "symmetry": (0.0, 1.0, "float"),
    "focus_blend": (0.0, 1.0, "float"), "optical_lift": (0.0, 0.12, "switch"), "hero_mass": (0.0, 3.0, "switch"),
    "split_bias": (-0.8, 0.8, "float"), "zone_top_align": (0.0, 1.0, "switch"), "zone_header": (0.0, 1.0, "flag"),
    "headline_lead": (0.6, 1.8, "float"),
    "relax_iters": (0, 12, "int"), "relax_clear": (0.005, 0.05, "float"), "relax_spring": (0.0, 0.9, "switch"),
}


def read_genome(engine: Path) -> dict:
    m = MARK.search(Path(engine).read_text())
    if not m:
        raise ValueError(f"{engine}: no GENOME block")
    body = m.group(2)
    return ast.literal_eval(body[body.index("{"):body.rindex("}") + 1])


def format_genome(g: dict) -> str:
    lines = ["GENOME = {"]
    for k, v in g.items():
        lines.append(f"    {k!r}: {v!r},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_engine(seed: Path, genome: dict, out: Path) -> Path:
    """Copy `seed` to `out` with its GENOME block replaced by `genome`."""
    src = Path(seed).read_text()
    cur = read_genome(seed)
    merged = dict(cur)
    merged.update({k: v for k, v in genome.items() if k in cur})
    text = MARK.sub(lambda m: m.group(1) + format_genome(merged) + m.group(3), src, count=1)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out


def _clip(gene, v):
    lo, hi, kind = BOUNDS[gene]
    v = min(max(v, lo), hi)
    if kind == "int":
        return int(round(v))
    if kind == "flag":
        return 1.0 if v >= 0.5 else 0.0
    return round(float(v), 4)


def mutate(seed: dict, family: str, rng: random.Random, strength: float = 0.25) -> tuple[dict, list]:
    """A variant of `seed` that changes 2-4 genes of one family (and, rarely,
    one gene from another, to keep the search from tunnelling).
    Returns (genome, [changed genes])."""
    g = dict(seed)
    genes = [x for x in FAMILIES[family][1] if x in g and x in BOUNDS]
    picks = rng.sample(genes, k=min(len(genes), rng.randint(2, 4)))
    if rng.random() < 0.15:
        other = [x for f, (_, gs) in FAMILIES.items() if f != family for x in gs if x in g and x in BOUNDS]
        picks.append(rng.choice(other))
    for gene in picks:
        lo, hi, kind = BOUNDS[gene]
        v = g[gene]
        if kind in ("switch", "flag") and (v == 0 or rng.random() < 0.2):
            # strategy genes: switch on at a random strength (or flip a flag)
            v = (1.0 - (1.0 if v else 0.0)) if kind == "flag" else rng.uniform(lo + 0.15 * (hi - lo), hi)
        elif kind == "int":
            v = v + rng.choice([-3, -2, -1, 1, 2, 3]) if v else rng.randint(2, int(hi))
        else:
            v = v + rng.gauss(0, strength * (hi - lo))
        g[gene] = _clip(gene, v)
    changed = [x for x in picks if g[x] != seed[x]]
    if not changed:  # make sure the variant differs
        gene = picks[0]
        lo, hi, _ = BOUNDS[gene]
        g[gene] = _clip(gene, rng.uniform(lo, hi))
        changed = [gene] if g[gene] != seed[gene] else []
    return g, changed


def synthesize(seed: dict, winner: dict, improvers: list[tuple[str, dict]]) -> dict:
    """Crossover for the next generation: start from the winner and adopt the
    family genes of every other variant that beat the seed."""
    g = dict(winner)
    for family, genome in improvers:
        for gene in FAMILIES[family][1]:
            if gene in genome and genome[gene] != seed.get(gene) and g.get(gene) == seed.get(gene):
                g[gene] = genome[gene]
    return g


def diff(a: dict, b: dict) -> dict:
    return {k: (a.get(k), b[k]) for k in b if a.get(k) != b[k]}


if __name__ == "__main__":
    import sys
    print(json.dumps(read_genome(Path(sys.argv[1] if len(sys.argv) > 1 else "engine/resize.py")), indent=1))
