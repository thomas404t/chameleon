"""Robustness fuzzer: random designs x random targets. Fails loudly on any
crash or hard-rule violation (ids, off-canvas, image aspect ratio)."""
import random, sys, time, traceback
from engine.resize import resize
from tools.evaluate import evaluate

WORDS = "light summer golden botanical sparkle crafted limited edition taste hour".split()
ASSETS = ["solstice_bottle.png", "solstice_logo_lockup.png", "nope_missing.png"]
TARGETS = [(1080, 1920), (1280, 720), (300, 250), (160, 600), (728, 90), (1584, 396),
           (320, 50), (1000, 1500), (2000, 2000), (64, 64)]


def rand_design(rng):
    W, H = rng.choice([(1080, 1080), (1080, 1350), (1200, 628), (800, 2000)])
    els = []
    if rng.random() < 0.8:
        els.append({"id": "bg", "type": "image", "x": 0, "y": 0, "width": W, "height": H,
                    "z_index": 0, "asset_id": "solstice_bg_gradient.png", "fit": "cover"})
    for i in range(rng.randint(1, 18)):
        w, h = rng.uniform(20, W * 0.9), rng.uniform(10, H * 0.5)
        x, y = rng.uniform(-50, W - w + 50), rng.uniform(-50, H - h + 50)
        base = {"id": f"e{i}", "x": x, "y": y, "width": w, "height": h, "z_index": rng.randint(1, 6)}
        if rng.random() < 0.15:
            base["rotation"] = rng.uniform(-30, 30)
        if rng.random() < 0.2:
            base["group_id"] = f"g{rng.randint(0, 3)}"
        t = rng.choice(["text", "text", "image", "shape"])
        if t == "text":
            n = rng.randint(1, 30)
            content = " ".join(rng.choice(WORDS) for _ in range(n))
            if rng.random() < 0.1:
                content = "Supercalifragilisticexpialidocious" * rng.randint(1, 3)
            base.update(type="text", content=content, font_family=rng.choice(["Solstice Serif", "Solstice Sans"]),
                        font_size=rng.uniform(8, 90), color="rgba(255,255,255,1)",
                        text_align=rng.choice(["left", "center", "right"]))
        elif t == "image":
            base.update(type="image", asset_id=rng.choice(ASSETS), fit="contain")
        else:
            base.update(type="shape", shape_type=rng.choice(["rectangle", "ellipse"]),
                        fill_color="rgba(232,106,58,1)", opacity=rng.choice([1, 1, 0.2]))
        els.append(base)
    return {"canvas": {"width": W, "height": H}, "elements": els}


def main(n=300, seed=11):
    rng = random.Random(seed)
    fails, worst = 0, 0.0
    for i in range(n):
        src = rand_design(rng)
        W, H = rng.choice(TARGETS)
        t0 = time.time()
        try:
            out = resize(src, {"width": W, "height": H})
            r = evaluate(src, out)
            if r["hard_fail"]:
                fails += 1
                print(f"case {i}: HARD FAIL {r}")
        except Exception:
            fails += 1
            print(f"case {i}: CRASH"); traceback.print_exc()
        worst = max(worst, time.time() - t0)
    print(f"{n} cases, {fails} failures, slowest {worst:.2f}s")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 300))
