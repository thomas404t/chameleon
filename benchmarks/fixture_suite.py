"""Common aspect-ratio fixture suite.

Two task sets, both written to benchmarks/fixtures/<set>/:
  fixtures    every design x the 7 common aspect ratios (selection set)
  regression  every design x 18 real ad formats + 15 edge cases (gate set,
              never used for ranking, so a candidate can't overfit it)

A candidate engine is run through the real pipeline: each shard is a
sandbox holding engine/main.py, the candidate as engine/resize.py and links
to renderer/ and brand_kit/, and runs `python -m engine.main` there. The JSON
and PNG outputs are exactly what the official runner would produce.

  python -m benchmarks.fixture_suite --build
  python -m benchmarks.fixture_suite --engine engine/resize.py --out benchmarks/runs/try
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from benchmarks.designs import designs, edge_cases, tier_of

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "benchmarks" / "fixtures"

# the common aspect ratios every candidate is tested on
RATIOS = {
    "1x1_square": (1080, 1080),       # Instagram post
    "4x5_portrait": (1080, 1350),     # social feed
    "9x16_story": (1080, 1920),       # story / reel
    "16x9_landscape": (1920, 1080),   # landscape banner
    "4x1_header": (1600, 400),        # wide header
    "8x1_leaderboard": (728, 90),     # extreme leaderboard
    "3x4_poster": (1200, 1600),       # poster
}

# real ad formats for the regression gate
FORMATS = {
    "ig_post": (1080, 1080), "ig_portrait": (1080, 1350), "ig_story": (1080, 1920),
    "pinterest": (1000, 1500), "fb_link": (1200, 630), "x_post": (1600, 900),
    "yt_thumb": (1280, 720), "email_header": (600, 200), "x_header": (1500, 500),
    "fb_cover": (820, 312), "linkedin_banner": (1584, 396), "billboard": (970, 250),
    "leaderboard": (728, 90), "mobile_banner": (320, 50), "med_rect": (300, 250),
    "half_page": (300, 600), "skyscraper": (160, 600), "a4_poster": (2480, 3508),
}


def build(input_dir: Path = ROOT / "input") -> dict:
    """Write both task sets; returns {set: count}."""
    ds = designs(input_dir)
    counts = {}
    for name, targets in (("fixtures", RATIOS), ("regression", FORMATS)):
        d = FIXTURES / name
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        for dname, src in ds.items():
            for tname, (w, h) in targets.items():
                tid = f"{dname}__{tname}"
                (d / f"{tid}.json").write_text(json.dumps(
                    {"task_id": tid, "tier": tier_of(dname), "source": src,
                     "target_canvas": {"width": w, "height": h}}))
        if name == "regression":
            for tid, task in edge_cases().items():
                (d / f"{tid}.json").write_text(json.dumps(dict(task, tier="edge")))
        counts[name] = len(list(d.glob("*.json")))
    return counts


def tasks(set_name: str) -> list[Path]:
    d = FIXTURES / set_name
    if not d.is_dir() or not any(d.glob("*.json")):
        build()
    return sorted(d.glob("*.json"))


def _sandbox(d: Path, engine: Path, task_files: list[Path], brand_kit: Path = ROOT / "brand_kit") -> None:
    shutil.rmtree(d, ignore_errors=True)
    (d / "engine").mkdir(parents=True)
    (d / "input").mkdir()
    (d / "output").mkdir()
    shutil.copy(ROOT / "engine" / "__init__.py", d / "engine" / "__init__.py")
    shutil.copy(ROOT / "engine" / "main.py", d / "engine" / "main.py")
    shutil.copy(engine, d / "engine" / "resize.py")
    os.symlink(ROOT / "renderer", d / "renderer")
    os.symlink(brand_kit, d / "brand_kit")
    for t in task_files:
        shutil.copy(t, d / "input" / t.name)


_LINE = re.compile(r"^\[(?P<name>.+?)\.json\] (?P<status>.+?) in (?P<secs>[\d.]+)s")


def run_engine(engine: Path, task_files: list[Path], out: Path, shards: int = 12,
               python: str = sys.executable, timeout: int = 3600, brand_kit: Path = ROOT / "brand_kit",
               retry_timeout: int = 600) -> dict:
    """Run `python -m engine.main` for the candidate over the tasks, sharded.
    `brand_kit` replaces the repo's brand kit (e.g. one with extra assets).
    Collects out/json/<task>.json, out/png/<task>.png; returns
    {task: {"secs": .., "status": ..} | {"crash": ..}}."""
    engine = Path(engine).resolve()
    out = Path(out).resolve()
    (out / "json").mkdir(parents=True, exist_ok=True)
    (out / "png").mkdir(parents=True, exist_ok=True)
    box = out / ".sandbox"
    shards = max(1, min(shards, len(task_files)))
    # round-robin so slow designs spread across shards
    groups = [task_files[i::shards] for i in range(shards)]
    results: dict = {}

    def launch(group, tag):
        d = box / tag
        _sandbox(d, engine, group, brand_kit)
        log = open(d / "log.txt", "w")
        p = subprocess.Popen([python, "-m", "engine.main"], cwd=d, stdout=log, stderr=subprocess.STDOUT)
        return p, d, group, log

    def collect(d, group):
        for line in (d / "log.txt").read_text(errors="replace").splitlines():
            m = _LINE.match(line)
            if m:
                results[m["name"]] = {"secs": float(m["secs"]), "status": m["status"]}
        for t in group:
            j, p = d / "output" / f"{t.stem}_output.json", d / "output" / f"{t.stem}_output.png"
            if j.exists() and p.exists():
                shutil.copy(j, out / "json" / f"{t.stem}.json")
                shutil.copy(p, out / "png" / f"{t.stem}.png")

    t0 = time.time()
    procs = [launch(g, f"s{i}") for i, g in enumerate(groups) if g]
    for p, d, group, log in procs:
        try:
            p.wait(timeout=max(1, timeout - (time.time() - t0)))
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
        collect(d, group)
    # a crash stops engine.main for the rest of its shard: retry each missing
    # task alone, so one bad task can't hide the others
    missing = [t for t in task_files if not (out / "json" / f"{t.stem}.json").exists()]
    for i, t in enumerate(missing):
        p, d, group, log = launch([t], f"retry{i}")
        try:
            p.wait(timeout=retry_timeout)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
        collect(d, group)
        if not (out / "json" / f"{t.stem}.json").exists():
            tail = (d / "log.txt").read_text(errors="replace")[-1200:]
            results[t.stem] = {"crash": tail or "no output"}
    shutil.rmtree(box, ignore_errors=True)
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="(re)write the task sets")
    ap.add_argument("--engine", type=Path, help="candidate resize.py to run")
    ap.add_argument("--set", default="fixtures", choices=["fixtures", "regression"])
    ap.add_argument("--out", type=Path, default=ROOT / "benchmarks" / "runs" / "manual")
    ap.add_argument("--shards", type=int, default=12)
    ap.add_argument("--judge", action="store_true", help="also score the outputs with the judge")
    a = ap.parse_args()
    if a.build or not a.engine:
        print(build())
    if a.engine:
        tf = tasks(a.set)
        t0 = time.time()
        res = run_engine(a.engine, tf, a.out, a.shards)
        (a.out / "run.json").write_text(json.dumps(res, indent=1))
        crashes = [k for k, v in res.items() if "crash" in v]
        print(f"{len(tf)} tasks in {time.time() - t0:.0f}s, {len(crashes)} crashes -> {a.out}")
        if a.judge:
            from evaluator.judge_agent import judge_run
            summary = judge_run(a.out, tf)
            print(json.dumps(summary["aggregate"], indent=1))


if __name__ == "__main__":
    main()
