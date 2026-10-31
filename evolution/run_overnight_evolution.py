"""Overnight evolutionary tournament for engine/resize.py.

Each generation:
  1. the champion's genome seeds `--per-family` mutants of each variant
     strategy A-D (see evolution/mutator.py), plus a synthesis candidate
     that crosses the previous generation's improvers;
  2. every candidate runs the fixture suite through `python -m engine.main`
     (benchmarks/fixture_suite.py) and is scored by the judge
     (evaluator/judge_agent.py);
  3. candidates are ranked on benchmarks/leaderboard.json; a candidate with
     a crash or a hard-rule violation is disqualified;
  4. the best one must also pass the regression gate (162 format tasks + 15
     edge cases it was never selected on: no crash, no hard violation, no
     new overflow or collision, no drop in the judge's mean) to replace the
     champion;
  5. with --chaos, it must also survive the chaos gauntlet
     (benchmarks/chaos_gauntlet.py): a crash, a hard-rule violation or a
     text occlusion on any adversarial task disqualifies it;
  6. the generation is committed to branch gen-<G>-<strategy> and tagged
     with its score.
Finally the best generation is merged into `--finalize-to` (checked out)
and OVERNIGHT_REPORT.md is written.

A later campaign continues the same leaderboard: `--reseed` scores the
current engine (e.g. with new strategy code) as the new champion and builds
the next generations on the current HEAD.

Commits are made with git plumbing on a temporary index (explicit files
only, the working tree is never touched until the final checkout), authored
by the repository's configured user.

  python -m evolution.run_overnight_evolution --generations 6 --auto-branch --finalize-to final
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from benchmarks.fixture_suite import RATIOS, run_engine, tasks
from evaluator.judge_agent import judge_run
from evolution.mutator import FAMILIES, diff, mutate, read_genome, synthesize, write_engine

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "benchmarks" / "runs"
STATE = RUNS / "state.json"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ git (plumbing only)
def git(*args, env=None, check=True) -> str:
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(["git", *args], cwd=ROOT, env=e, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def ref_exists(ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=ROOT,
                          capture_output=True).returncode == 0


def commit_files(parent: str, files: dict, message: str) -> str:
    """Commit {repo path: local file} on top of `parent` without touching the
    working tree or the real index. Returns the new commit sha."""
    parent_sha = git("rev-parse", parent)
    with tempfile.TemporaryDirectory() as td:
        env = {"GIT_INDEX_FILE": str(Path(td) / "index")}
        git("read-tree", parent_sha, env=env)
        for repo_path, local in files.items():
            blob = git("hash-object", "-w", str(local))
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{repo_path}", env=env)
        tree = git("write-tree", env=env)
    return git("commit-tree", tree, "-p", parent_sha, "-m", message)


def new_branch(name: str, sha: str) -> str:
    """Create branch `name` (or name-rN if it already exists) at sha."""
    cand, n = name, 2
    while ref_exists(f"refs/heads/{cand}"):
        cand, n = f"{name}-r{n}", n + 1
    git("branch", cand, sha)
    return cand


def new_tag(name: str, sha: str, message: str) -> str:
    cand, n = name, 2
    while ref_exists(f"refs/tags/{cand}"):
        cand, n = f"{name}-r{n}", n + 1
    git("tag", "-a", cand, sha, "-m", message)
    return cand


# ------------------------------------------------------------------ evaluation
def evaluate(engine: Path, out: Path, set_name: str, workers: int) -> dict:
    tf = tasks(set_name)
    t0 = time.time()
    run = run_engine(engine, tf, out, shards=workers)
    summary = judge_run(out, tf, workers=workers)
    agg = summary["aggregate"]
    secs = [v["secs"] for v in run.values() if "secs" in v]
    agg["max_secs"] = round(max(secs), 2) if secs else None
    agg["wall_secs"] = round(time.time() - t0, 1)
    return agg


def fitness(agg: dict) -> float:
    """Mean judge score; crashes and hard violations disqualify; every
    overflow / collision costs extra on top of what the judge already took."""
    if agg["crashes"] or agg["hard_violations"] or agg.get("judge_errors"):
        return -1.0
    return round(agg["mean_total"] - 0.5 * (agg["overflow"] + agg["collisions"]), 3)


def gate(cand: dict, champ: dict) -> tuple[bool, str]:
    if cand["crashes"] or cand["hard_violations"] or cand.get("judge_errors"):
        return False, "crash / hard violation on the regression set"
    if cand["overflow"] > champ["overflow"]:
        return False, f"overflow {cand['overflow']} > {champ['overflow']}"
    if cand["collisions"] > champ["collisions"]:
        return False, f"collisions {cand['collisions']} > {champ['collisions']}"
    if cand["mean_total"] < champ["mean_total"] - 0.25:
        return False, f"regression mean {cand['mean_total']:.2f} < {champ['mean_total']:.2f} - 0.25"
    return True, "pass"


# ------------------------------------------------------------------ previews
def contact_sheet(run_dir: Path, out: Path, title: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    pngs = sorted((run_dir / "png").glob("*.png"))
    designs = sorted({p.stem.split("__")[0] for p in pngs})
    cols = list(RATIOS)
    cell, pad, head, left = 200, 8, 40, 170
    try:
        font = ImageFont.truetype(str(ROOT / "brand_kit/fonts/SolsticeSans-Regular.ttf"), 15)
        big = ImageFont.truetype(str(ROOT / "brand_kit/fonts/SolsticeSans-Bold.ttf"), 22)
    except Exception:
        font = big = ImageFont.load_default()
    W = left + len(cols) * (cell + pad)
    H = head + 30 + len(designs) * (cell + pad)
    sheet = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(sheet)
    d.text((pad, 6), title, fill="black", font=big)
    for j, c in enumerate(cols):
        d.text((left + j * (cell + pad), head), c, fill=(60, 60, 60), font=font)
    for i, dn in enumerate(designs):
        y = head + 30 + i * (cell + pad)
        d.text((pad, y + cell // 2 - 8), dn[:22], fill=(60, 60, 60), font=font)
        for j, c in enumerate(cols):
            p = run_dir / "png" / f"{dn}__{c}.png"
            if not p.exists():
                continue
            im = Image.open(p).convert("RGB")
            im.thumbnail((cell, cell), Image.LANCZOS)
            sheet.paste(im, (left + j * (cell + pad) + (cell - im.width) // 2, y + (cell - im.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=82, optimize=True)
    return out


# ------------------------------------------------------------------ state
def load_state(seed_engine: Path, parent: str) -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"created": datetime.now().isoformat(timespec="seconds"), "parent": parent,
            "baseline": None, "champion": None, "generations": [], "entries": [],
            "strength": 0.25, "pending_synthesis": None, "seed_engine": str(seed_engine)}


def save_state(st: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def leaderboard(st: dict) -> dict:
    """The committed leaderboard: every candidate ever scored, best first."""
    return {"updated": datetime.now().isoformat(timespec="seconds"),
            "champion": st["champion"], "baseline": st["baseline"],
            "generations": [{k: g[k] for k in ("gen", "branch", "tag", "winner", "fitness", "delta",
                                               "seed_fitness", "regression", "strength")} for g in st["generations"]],
            "ranking": sorted(st["entries"], key=lambda e: -e["fitness"])}


# ------------------------------------------------------------------ one generation
def run_generation(G: int, st: dict, a) -> dict:
    champ = st["champion"]
    seed_engine = Path(champ["engine"])
    seed = read_genome(seed_engine)
    rng = random.Random(a.rng_seed * 1000 + G)
    gdir = RUNS / f"gen-{G}"
    shutil.rmtree(gdir, ignore_errors=True)
    cands = []  # (name, family, genome, changed)
    for fam in FAMILIES:
        seen = set()
        for i in range(a.per_family):
            for _ in range(10):
                g, changed = mutate(seed, fam, rng, st["strength"])
                key = json.dumps(g, sort_keys=True)
                if changed and key not in seen:
                    break
            seen.add(key)
            cands.append((f"{fam}{i + 1}", fam, g, changed))
    if st.get("pending_synthesis"):
        g = st["pending_synthesis"]
        if g != seed:
            cands.append(("S", "S", g, sorted(diff(seed, g))))
    results = []
    for name, fam, g, changed in cands:
        eng = write_engine(seed_engine, g, gdir / name / "resize.py")
        try:
            agg = evaluate(eng, gdir / name / "fixtures", "fixtures", a.workers)
        except Exception as ex:  # a broken candidate must not stop the night
            log(f"  gen {G} {name}: evaluation failed: {ex}")
            agg = {"mean_total": 0.0, "crashes": 1, "hard_violations": 0, "overflow": 0, "collisions": 0}
        f = fitness(agg)
        rec = {"gen": G, "name": f"gen{G}-{name}", "family": fam,
               "strategy": FAMILIES[fam][0] if fam in FAMILIES else "synthesis",
               "changed": {k: [seed.get(k), g[k]] for k in changed}, "fitness": f,
               "agg": {k: agg.get(k) for k in ("mean_total", "hierarchy", "optical", "space", "readability",
                                                "overflow", "collisions", "crashes", "hard_violations",
                                                "critical_tasks", "max_secs", "by_target", "by_tier")},
               "engine": str(eng), "genome": g}
        results.append(rec)
        st["entries"].append({k: rec[k] for k in ("gen", "name", "family", "strategy", "changed", "fitness")})
        log(f"  gen {G} {name:3s} [{rec['strategy']:17s}] fitness {f:7.3f} "
            f"(champion {champ['fitness']:.3f}) {json.dumps(rec['changed'])}")
        save_state(st)
    results.sort(key=lambda r: -r["fitness"])
    improvers = [r for r in results if r["fitness"] > champ["fitness"] + a.min_gain]
    winner, reg, why = None, None, "no candidate beat the champion"
    for r in improvers:
        out = gdir / r["name"].split("-", 1)[1] / "regression"
        reg = evaluate(Path(r["engine"]), out, "regression", a.workers)
        ok, why = gate(reg, champ["regression"])
        log(f"  gen {G} gate {r['name']}: {why} (regression mean {reg['mean_total']:.3f})")
        if ok and a.chaos:
            from benchmarks.chaos_gauntlet import evaluate as chaos_evaluate
            ch = chaos_evaluate(Path(r["engine"]), out.parent / "chaos", a.workers)
            if ch["disqualified"]:
                ok, why = False, f"chaos gauntlet: {len(ch['disqualified_tasks'])} task(s), {ch['reason']}"
            log(f"  gen {G} chaos {r['name']}: {'pass' if ok else why}")
        if ok:
            winner = r
            break
    seed_fit = champ["fitness"]
    if winner is not None:
        champ = {"gen": G, "name": winner["name"], "fitness": winner["fitness"], "genome": winner["genome"],
                 "engine": winner["engine"], "fixtures": winner["agg"],
                 "regression": {k: reg.get(k) for k in ("mean_total", "hierarchy", "optical", "space",
                                                        "readability", "overflow", "collisions", "crashes",
                                                        "hard_violations", "critical_tasks", "max_secs")},
                 "strategy": winner["strategy"]}
        st["champion"] = champ
        st["strength"] = min(0.35, st["strength"] * 1.1)
        others = [(r["family"], r["genome"]) for r in improvers if r is not winner and r["family"] in FAMILIES]
        st["pending_synthesis"] = synthesize(seed, winner["genome"], others) if others else None
        descriptor = winner["strategy"] if winner["family"] != "S" else "synthesis"
    else:
        st["strength"] = max(0.06, st["strength"] * 0.75)
        st["pending_synthesis"] = None
        descriptor = "consolidation"
    # commit the generation
    champ_run = Path(champ["engine"]).parent / "fixtures"
    preview = contact_sheet(champ_run, RUNS / f"gen-{G}-preview.jpg",
                            f"Generation {G} champion: {champ['name']}  fitness {champ['fitness']:.2f}")
    gen_rec = {"gen": G, "seed_fitness": seed_fit, "winner": champ["name"], "fitness": champ["fitness"],
               "delta": round(champ["fitness"] - seed_fit, 3), "regression": champ["regression"]["mean_total"],
               "strength": st["strength"], "note": why if winner is None else "champion replaced",
               "candidates": [{k: r[k] for k in ("name", "strategy", "changed", "fitness", "agg")} for r in results]}
    st["generations"].append(dict(gen_rec, branch=None, tag=None))
    if a.auto_branch:
        lb, gj = RUNS / "leaderboard.json", RUNS / f"gen-{G}.json"
        lb.write_text(json.dumps(leaderboard(st), indent=1))
        gj.write_text(json.dumps(gen_rec, indent=1))
        parent = st.get("lineage") or (st["generations"][-2]["branch"] if len(st["generations"]) > 1
                                        and st["generations"][-2]["branch"] else st["parent"])
        msg = (f"Generation {G}: {descriptor} ({champ['name']}), fitness {champ['fitness']:.2f} "
               f"({gen_rec['delta']:+.2f})\n\n"
               + "\n".join(f"{r['name']:12s} {r['strategy']:17s} {r['fitness']:7.3f}" for r in results))
        sha = commit_files(parent, {"engine/resize.py": champ["engine"], "benchmarks/leaderboard.json": lb,
                                    f"benchmarks/generations/gen-{G}.json": gj,
                                    f"benchmarks/previews/gen-{G}.jpg": preview}, msg)
        branch = new_branch(f"gen-{G}-{descriptor}", sha)
        tag = new_tag(f"gen-{G}-score-{champ['fitness']:.2f}", sha,
                      json.dumps({"gen": G, "champion": champ["name"], "fitness": champ["fitness"],
                                  "fixture_tasks": len(tasks("fixtures")),
                                  "regression_mean": champ["regression"]["mean_total"],
                                  "seed_fitness": seed_fit, "candidates": len(results),
                                  "date": datetime.now().isoformat(timespec="seconds")}))
        st["generations"][-1].update(branch=branch, tag=tag)
        st["lineage"] = sha
        log(f"gen {G}: committed {sha[:8]} on {branch}, tagged {tag}")
    save_state(st)
    return st


# ------------------------------------------------------------------ finalize
def finalize(st: dict, branch: str):
    best = max((g for g in st["generations"] if g["branch"]), key=lambda g: g["fitness"], default=None)
    if best is None:
        log("finalize: no generation branches")
        return
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        log(f"finalize: tracked files are modified, not checking out:\n{dirty}")
        return
    # generated files would block the checkout of a tree that contains them
    for p in ("benchmarks/leaderboard.json", "OVERNIGHT_REPORT.md"):
        q = ROOT / p
        if q.exists() and not git("ls-files", p):
            q.unlink()
    for p in (ROOT / "benchmarks" / "generations").glob("*.json"):
        if not git("ls-files", str(p.relative_to(ROOT))):
            p.unlink()
    for p in (ROOT / "benchmarks" / "previews").glob("*.jpg"):
        if not git("ls-files", str(p.relative_to(ROOT))):
            p.unlink()
    git("checkout", "-B", branch, st["parent"])
    git("merge", "--no-ff", best["branch"], "-m",
        f"Merge {best['branch']} into {branch}: tournament champion, fitness {best['fitness']:.2f}")
    report = ROOT / "OVERNIGHT_REPORT.md"
    report.write_text(write_report(st, best))
    git("add", "OVERNIGHT_REPORT.md")
    git("commit", "-q", "-m", f"Overnight report: {len(st['generations'])} generations, "
                              f"champion {st['champion']['name']} ({st['champion']['fitness']:.2f})")
    log(f"finalize: {branch} checked out at {git('rev-parse', '--short', 'HEAD')}")


def write_report(st: dict, best: dict) -> str:
    base, champ = st["baseline"], st["champion"]
    L = ["# Overnight evolution report", "",
         f"Run started {st['created']}, finalized {datetime.now().isoformat(timespec='seconds')}.",
         f"{len(st['generations'])} generations, {len(st['entries'])} candidates scored on "
         f"{len(tasks('fixtures'))} fixture tasks (9 designs x 7 aspect ratios); every new champion also "
         f"passed a {len(tasks('regression'))}-task regression gate it was never selected on.", "",
         "## Winner", "",
         f"**{champ['name']}** ({champ['strategy']}), fitness **{champ['fitness']:.2f}** "
         f"vs baseline {base['fitness']:.2f} ({champ['fitness'] - base['fitness']:+.2f}).", "",
         "Genome changes vs the baseline:", "", "| gene | baseline | winner |", "|---|---|---|"]
    for k, (a_, b_) in diff(base["genome"], champ["genome"]).items():
        L.append(f"| `{k}` | {a_} | {b_} |")
    L += ["", "## Scores", "", "| | baseline | winner |", "|---|---|---|"]
    for k in ("mean_total", "hierarchy", "optical", "space", "readability", "overflow", "collisions",
              "critical_tasks"):
        L.append(f"| fixtures {k} | {base['fixtures'].get(k)} | {champ['fixtures'].get(k)} |")
    for k in ("mean_total", "overflow", "collisions", "crashes", "hard_violations", "max_secs"):
        L.append(f"| regression {k} | {base['regression'].get(k)} | {champ['regression'].get(k)} |")
    L += ["", "By aspect ratio (fixture mean):", "", "| ratio | baseline | winner |", "|---|---|---|"]
    for r in RATIOS:
        L.append(f"| {r} | {base['fixtures']['by_target'].get(r)} | {champ['fixtures']['by_target'].get(r)} |")
    camps = st.get("campaigns") or []
    if camps:
        L += ["", "## Campaigns", "", f"- Campaign 1 (generations 1-{camps[0]['start_gen'] - 1}): "
              "parameter tournament over the strategy genes."]
        for i, c in enumerate(camps):
            L.append(f"- Campaign {i + 2} (from generation {c['start_gen']}): {c['note'] or 'new seed'} "
                     f"Seed {c['seed_fitness']:.2f} vs previous champion {c['previous_champion']} "
                     f"{c['previous_fitness']:.2f}.")
    L += ["", "## Generations", "", "| gen | branch | tag | champion | fitness | delta | regression |",
          "|---|---|---|---|---|---|---|"]
    for g in st["generations"]:
        L.append(f"| {g['gen']} | `{g['branch']}` | `{g['tag']}` | {g['winner']} | {g['fitness']:.2f} | "
                 f"{g['delta']:+.2f} | {g['regression']:.2f} |")
    L += ["", "## Previews", ""]
    for g in st["generations"]:
        # each preview lives on its own generation branch (relative GitHub link)
        L.append(f"- Generation {g['gen']} (`{g['branch']}`): [gen-{g['gen']}.jpg]"
                 f"(../{g['branch']}/benchmarks/previews/gen-{g['gen']}.jpg)")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generations", type=int, default=6, help="generations to run in this session")
    ap.add_argument("--auto-branch", action="store_true", help="commit each generation to gen-<G>-<strategy>")
    ap.add_argument("--finalize-to", default=None, help="merge the best generation into this branch at the end")
    ap.add_argument("--seed-engine", type=Path, default=ROOT / "engine" / "resize.py")
    ap.add_argument("--parent", default="HEAD", help="commit the first generation branch builds on")
    ap.add_argument("--per-family", type=int, default=3, help="mutants per variant strategy per generation")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--min-gain", type=float, default=0.05)
    ap.add_argument("--rng-seed", type=int, default=2026)
    ap.add_argument("--patience", type=int, default=0,
                    help="with >0: keep going past --generations until this many gens bring no gain")
    ap.add_argument("--max-generations", type=int, default=30)
    ap.add_argument("--reseed", action="store_true",
                    help="start a new campaign: the seed engine becomes the champion, built on --parent")
    ap.add_argument("--note", default="", help="what is new in this campaign (for the report)")
    ap.add_argument("--chaos", action="store_true",
                    help="a new champion must also survive the chaos gauntlet (benchmarks/chaos_gauntlet.py)")
    a = ap.parse_args()

    st = load_state(a.seed_engine, git("rev-parse", a.parent))
    if a.reseed and st["champion"] is not None:
        G0 = len(st["generations"]) + 1
        seed = RUNS / f"gen-{G0 - 1}" / "reseed" / "resize.py"
        seed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(a.seed_engine, seed)
        log(f"reseed: scoring {a.seed_engine}")
        fx = evaluate(seed, seed.parent / "fixtures", "fixtures", a.workers)
        rg = evaluate(seed, seed.parent / "regression", "regression", a.workers)
        keep = ("mean_total", "hierarchy", "optical", "space", "readability", "overflow", "collisions",
                "crashes", "hard_violations", "critical_tasks", "max_secs")
        prev = st["champion"]
        st["champion"] = {"gen": G0 - 1, "name": f"gen{G0 - 1}-reseed", "fitness": fitness(fx),
                          "genome": read_genome(seed), "engine": str(seed),
                          "fixtures": {k: fx.get(k) for k in keep + ("by_target", "by_tier")},
                          "regression": {k: rg.get(k) for k in keep}, "strategy": "reseed"}
        base = git("rev-parse", a.parent)
        st.setdefault("campaigns", []).append({"start_gen": G0, "base": base, "note": a.note,
                                               "seed_fitness": st["champion"]["fitness"],
                                               "previous_champion": prev["name"],
                                               "previous_fitness": prev["fitness"]})
        st["parent"] = st["lineage"] = base
        st["pending_synthesis"] = None
        st["strength"] = 0.25
        save_state(st)
        log(f"reseed fitness {st['champion']['fitness']:.3f} (was {prev['fitness']:.3f}), "
            f"regression mean {rg['mean_total']:.3f}")
    if st["champion"] is None:
        seed = RUNS / "gen-0" / "baseline" / "resize.py"
        seed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(a.seed_engine, seed)
        log("scoring the seed engine")
        fx = evaluate(seed, seed.parent / "fixtures", "fixtures", a.workers)
        rg = evaluate(seed, seed.parent / "regression", "regression", a.workers)
        keep = ("mean_total", "hierarchy", "optical", "space", "readability", "overflow", "collisions",
                "crashes", "hard_violations", "critical_tasks", "max_secs")
        st["champion"] = {"gen": 0, "name": "gen0-baseline", "fitness": fitness(fx), "genome": read_genome(seed),
                          "engine": str(seed), "fixtures": {k: fx.get(k) for k in keep + ("by_target", "by_tier")},
                          "regression": {k: rg.get(k) for k in keep}, "strategy": "baseline"}
        st["baseline"] = dict(st["champion"])
        save_state(st)
        log(f"seed fitness {st['champion']['fitness']:.3f}, regression mean {rg['mean_total']:.3f}")
    start = len(st["generations"]) + 1
    G, quiet = start, 0
    while True:
        if G >= start + a.generations and (a.patience <= 0 or quiet >= a.patience or G > a.max_generations):
            break
        if G > a.max_generations:
            break
        log(f"=== generation {G} (strength {st['strength']:.3f}, champion {st['champion']['name']} "
            f"{st['champion']['fitness']:.3f})")
        before = st["champion"]["fitness"]
        st = run_generation(G, st, a)
        quiet = 0 if st["champion"]["fitness"] > before else quiet + 1
        G += 1
    lb = ROOT / "benchmarks" / "runs" / "leaderboard.json"
    lb.write_text(json.dumps(leaderboard(st), indent=1))
    if a.finalize_to:
        finalize(st, a.finalize_to)
    log(f"done: champion {st['champion']['name']} fitness {st['champion']['fitness']:.3f} "
        f"(baseline {st['baseline']['fitness']:.3f})")


if __name__ == "__main__":
    sys.exit(main())
