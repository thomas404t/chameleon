"""
Local quality evaluator for CHAMELEON outputs (dev tool, not the official scorer).

Hard checks (mirror RULES.md):   ids preserved, nothing off-canvas,
                                 non-cover images keep aspect ratio (<5% drift)
Soft metrics (our proxies for the AI rubric):
  collisions   content pieces that overlap in the output but did NOT in the source
  min_font     smallest rendered text size (px)
  hierarchy    share of text pairs whose size order survived (1.0 = perfect)
  hero         largest non-background image's short side / canvas short side
  fill         content bounding area / canvas area
  balance      1 - distance of content centroid from canvas centre (1.0 = centred)

Usage: python -m tools.evaluate <task_dir> <output_dir> [--engine engine.resize]
"""
import importlib, json, math, sys
from pathlib import Path


def _box(e):
    return (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])


def _inter(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def evaluate(src: dict, out: dict) -> dict:
    W, H = out["canvas"]["width"], out["canvas"]["height"]
    sw, sh = src["canvas"]["width"], src["canvas"]["height"]
    S = {e["id"]: e for e in src["elements"]}
    O = {e["id"]: e for e in out["elements"]}
    hard = []
    if set(S) != set(O) or len(O) != len(out["elements"]):
        hard.append("id mismatch")
    for e in out["elements"]:
        b = _box(e)
        if b[0] < -0.5 or b[1] < -0.5 or b[2] > W + 0.5 or b[3] > H + 0.5:
            hard.append(f"off-canvas {e['id']}")
        if e["type"] == "image" and e.get("fit", "contain") != "cover":
            s = S[e["id"]]
            r0, r1 = s["width"] / s["height"], e["width"] / max(e["height"], 1e-6)
            if abs(r1 / r0 - 1) > 0.05:
                hard.append(f"aspect {e['id']}")

    def is_bg(e):
        return _area((max(e["x"], 0), max(e["y"], 0), min(e["x"] + e["width"], sw), min(e["y"] + e["height"], sh))) >= 0.9 * sw * sh

    def is_decor(e):
        return e["type"] == "shape" and not e.get("group_id") and e.get("opacity", 1) <= 0.5

    content = [i for i, e in S.items() if not is_bg(e) and not is_decor(e)]
    collisions = 0
    for i in range(len(content)):
        for j in range(i + 1, len(content)):
            a, b = content[i], content[j]
            ga, gb = S[a].get("group_id"), S[b].get("group_id")
            if ga and ga == gb:
                continue
            src_ov = _inter(_box(S[a]), _box(S[b]))
            out_ov = _inter(_box(O[a]), _box(O[b]))
            small = min(_area(_box(O[a])), _area(_box(O[b]))) or 1
            if src_ov == 0 and out_ov / small > 0.02:
                collisions += 1
    texts = [i for i in content if S[i]["type"] == "text"]
    fonts = [O[i]["font_size"] for i in texts]
    pairs = ok = 0
    for i in texts:
        for j in texts:
            if S[i]["font_size"] > S[j]["font_size"]:
                pairs += 1
                ok += O[i]["font_size"] >= O[j]["font_size"] * 0.98
    imgs = [i for i in content if S[i]["type"] == "image"]
    hero = max((min(O[i]["width"], O[i]["height"]) / min(W, H) for i in imgs), default=0)
    boxes = [_box(O[i]) for i in content]
    if not boxes:
        return {"hard_fail": len(hard), "collisions": 0, "min_font": None, "hierarchy": 1.0,
                "hero": 0, "fill": 0, "balance": 1.0}
    u = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    tot = sum(_area(b) for b in boxes) or 1
    cx = sum((b[0] + b[2]) / 2 * _area(b) for b in boxes) / tot
    cy = sum((b[1] + b[3]) / 2 * _area(b) for b in boxes) / tot
    bal = 1 - math.hypot((cx - W / 2) / W, (cy - H / 2) / H) * 2
    return {
        "hard_fail": len(hard), "collisions": collisions,
        "min_font": round(min(fonts), 1) if fonts else None,
        "hierarchy": round(ok / pairs, 2) if pairs else 1.0,
        "hero": round(hero, 2), "fill": round(_area(u) / (W * H), 2), "balance": round(bal, 2),
    }


def main():
    tasks, outdir = Path(sys.argv[1]), Path(sys.argv[2])
    rows = []
    for t in sorted(tasks.glob("*.json")):
        task = json.loads(t.read_text())
        o = outdir / f"{t.stem}_output.json"
        if not o.exists():
            continue
        r = evaluate(task["source"], json.loads(o.read_text()))
        rows.append((t.stem, r))
    keys = ["hard_fail", "collisions", "min_font", "hierarchy", "hero", "fill", "balance"]
    for name, r in rows:
        print(f"{name:28s} " + " ".join(f"{k}={r[k]}" for k in keys))
    agg = {k: round(sum((r[k] or 0) for _, r in rows) / len(rows), 3) for k in keys}
    print("MEAN", json.dumps(agg))


if __name__ == "__main__":
    main()
