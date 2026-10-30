# Overnight evolution report

## Executive summary

The final engine (`final` branch) is the tournament champion **gen11-A1**,
plus two post-tournament fixes. Missing-asset placeholders tuck into a
corner. The elastic clean-up pass now checks overlaps per drawn element,
not per block, which closed a Linux-only collision on 600x200. On the
judge it scores:

| | Round 3 baseline | Final |
|---|---|---|
| Fixture suite (63 tasks, 7 aspect ratios) | 88.55 | **90.62** |
| Regression set (177 tasks, never used for ranking) | 86.86 | **88.17** |
| 8:1 leaderboard | 77.6 | **85.0** |
| 16:9 landscape | 86.4 | **89.8** |
| 4:1 header | 88.5 | **89.9** |
| 1:1 square | 89.6 | **91.3** |
| Crashes, rule violations, collisions | 0 / 0 / 0 | 0 / 0 / 0 |

The only overflow in both is the deliberately impossible giant-word edge
case. 500 fuzzed random designs give 0 failures. In the scoring Docker
image (linux/amd64, `--network none`, Linux font metrics) the three
official samples run cleanly. The 177 regression tasks give 0 crashes, 0
rule violations, 0 collisions and the same single giant-word overflow,
with a judge mean of 88.05. The 63 fixtures give 0 flags and a mean of
90.53. Under amd64 emulation on an ARM Mac, with 4 tasks in parallel, a
task takes 0.63 s on average and 8.3 s at worst.

### How it was run

- **Campaign 1 (generations 1-6).** A tournament over the round-3
  engine's heuristics, run as 4 variant strategies:
  - A: hero-centric anchor
  - B: dynamic grid & zoning
  - C: typography & text flow
  - D: elastic repulsion

  Each generation ran 3 mutants per strategy plus a synthesis candidate.
  It switched on header zones and the elastic clean-up pass, cut
  gratuitous column splits (`split_bias` -0.8), and tightened spacing.
- **Visual review.** A per-render review of the champion found flaws the
  genes couldn't reach. Each became new strategy code, was A/B'd alone,
  and was kept only if it helped on both task sets:
  - same-shape canvases measured on drawn text;
  - banner spread;
  - card reflow;
  - placeholder search.

  Vertical ink packing (`ink_v`) is left off: it caused collisions on
  320x50.
- **Campaign 2 (generations 7-14).** 4 mutants per strategy. It ran until
  3 generations in a row brought no gain (saturation at gen 14).
- **Regression gate.** 6 fixture "winners" were rejected because they
  introduced collisions or overflow on formats they were never ranked on.

### Biggest visual wins

These are shown in
[final-vs-baseline.jpg](benchmarks/previews/final-vs-baseline.jpg):
- **Card copy on 728x90.** The card's text was about 4px; the card now
  re-wraps readable copy.
- **Editorial 1200x628 to 16:9.** It keeps the designer's own layout
  instead of a three-column split.
- **Story to 1:1.** The headline spans the full width over a centered
  product.
- **Badge wall to 16:9.** It becomes a hero beside the copy and a clean
  4x3 badge grid.
- **Sparse leaderboard.** Logo and message balance across the strip.

### Caveats

- The judge is a heuristic proxy for the official AI rubric, not the
  rubric itself.
- It was calibrated against the review feedback from rounds 1-3. Naive
  stretch scores 18 and uniform fit scores 72.
- Every champion was also checked by eye.

---

Run started 2026-10-30T07:22:57, finalized 2026-10-30T07:54:51.
14 generations, 204 candidates scored on 63 fixture tasks (9 designs x 7 aspect ratios); every new champion also passed a 177-task regression gate it was never selected on.

## Winner

**gen11-A1** (hero-anchor), fitness **90.55** vs baseline 88.55 (+1.99).

Genome changes vs the baseline:

| gene | baseline | winner |
|---|---|---|
| `margin` | 0.045 | 0.0398 |
| `gap` | 0.025 | 0.0227 |
| `pad_y` | 2.0 | 2.5809 |
| `body_gap_cap` | 1.25 | 1.0732 |
| `col_gap` | 0.02 | 0.01 |
| `hero_floor` | 0.55 | 0.6236 |
| `logo_area` | 0.045 | 0.0307 |
| `w_hero` | 0.18 | 0.1177 |
| `w_order` | 0.12 | 0.1309 |
| `w_bal` | 0.09 | 0.038 |
| `w_fill` | 0.09 | 0.0359 |
| `symmetry` | 0.6 | 0.6819 |
| `focus_blend` | 0.5 | 0.3035 |
| `ink_v` | None | 0.0 |
| `shortcut_ink` | None | 1.0 |
| `split_bias` | 0.0 | -0.7983 |
| `zone_top_align` | 0.0 | 0.2017 |
| `zone_header` | 0.0 | 1.0 |
| `banner_spread` | None | 0.6983 |
| `panel_reflow` | None | 1.0 |
| `relax_iters` | 0 | 12 |
| `relax_clear` | 0.02 | 0.0084 |
| `relax_spring` | 0.0 | 0.4485 |

## Scores

| | baseline | winner |
|---|---|---|
| fixtures mean_total | 88.554 | 90.546 |
| fixtures hierarchy | 23.141 | 23.544 |
| fixtures optical | 18.407 | 19.476 |
| fixtures space | 23.222 | 23.414 |
| fixtures readability | 23.784 | 24.111 |
| fixtures overflow | 0 | 0 |
| fixtures collisions | 0 | 0 |
| fixtures critical_tasks | 0 | 0 |
| regression mean_total | 86.858 | 88.115 |
| regression overflow | 1 | 1 |
| regression collisions | 0 | 0 |
| regression crashes | 0 | 0 |
| regression hard_violations | 0 | 0 |
| regression max_secs | 3.21 | 3.33 |

By aspect ratio (fixture mean):

| ratio | baseline | winner |
|---|---|---|
| 1x1_square | 89.613 | 91.123 |
| 4x5_portrait | 92.12 | 92.218 |
| 9x16_story | 93.083 | 93.136 |
| 16x9_landscape | 86.354 | 89.68 |
| 4x1_header | 88.46 | 89.892 |
| 8x1_leaderboard | 77.598 | 85.021 |
| 3x4_poster | 92.652 | 92.751 |

## Campaigns

- Campaign 1 (generations 1-6): parameter tournament over the strategy genes.
- Campaign 2 (from generation 7): New strategy code from a visual review (ink-measured same-shape fidelity, banner spread, card reflow, placeholder search), seeded with the campaign-1 champion genome. Seed 90.25 vs previous champion gen4-S 89.48.

## Generations

| gen | branch | tag | champion | fitness | delta | regression |
|---|---|---|---|---|---|---|
| 1 | `gen-1-elastic-repulsion` | `gen-1-score-88.85` | gen1-D2 | 88.85 | +0.29 | 86.80 |
| 2 | `gen-2-grid-zoning` | `gen-2-score-89.06` | gen2-B1 | 89.06 | +0.21 | 86.78 |
| 3 | `gen-3-grid-zoning` | `gen-3-score-89.23` | gen3-B3 | 89.23 | +0.18 | 86.92 |
| 4 | `gen-4-synthesis` | `gen-4-score-89.48` | gen4-S | 89.48 | +0.25 | 87.08 |
| 5 | `gen-5-consolidation` | `gen-5-score-89.48` | gen4-S | 89.48 | +0.00 | 87.08 |
| 6 | `gen-6-consolidation` | `gen-6-score-89.48` | gen4-S | 89.48 | +0.00 | 87.08 |
| 7 | `gen-7-grid-zoning` | `gen-7-score-90.34` | gen7-B3 | 90.34 | +0.09 | 87.72 |
| 8 | `gen-8-grid-zoning` | `gen-8-score-90.39` | gen8-B3 | 90.39 | +0.06 | 88.07 |
| 9 | `gen-9-elastic-repulsion` | `gen-9-score-90.47` | gen9-D1 | 90.47 | +0.07 | 88.12 |
| 10 | `gen-10-consolidation` | `gen-10-score-90.47` | gen9-D1 | 90.47 | +0.00 | 88.12 |
| 11 | `gen-11-hero-anchor` | `gen-11-score-90.55` | gen11-A1 | 90.55 | +0.08 | 88.11 |
| 12 | `gen-12-consolidation` | `gen-12-score-90.55` | gen11-A1 | 90.55 | +0.00 | 88.11 |
| 13 | `gen-13-consolidation` | `gen-13-score-90.55` | gen11-A1 | 90.55 | +0.00 | 88.11 |
| 14 | `gen-14-consolidation` | `gen-14-score-90.55` | gen11-A1 | 90.55 | +0.00 | 88.11 |

## Previews

- Final engine, all 63 fixtures: [benchmarks/previews/final.jpg](benchmarks/previews/final.jpg)
- Baseline vs final, the biggest changes: [benchmarks/previews/final-vs-baseline.jpg](benchmarks/previews/final-vs-baseline.jpg)
- Each generation's champion, on its own branch:
  - Generation 1 (`gen-1-elastic-repulsion`): [gen-1.jpg](../gen-1-elastic-repulsion/benchmarks/previews/gen-1.jpg)
  - Generation 2 (`gen-2-grid-zoning`): [gen-2.jpg](../gen-2-grid-zoning/benchmarks/previews/gen-2.jpg)
  - Generation 3 (`gen-3-grid-zoning`): [gen-3.jpg](../gen-3-grid-zoning/benchmarks/previews/gen-3.jpg)
  - Generation 4 (`gen-4-synthesis`): [gen-4.jpg](../gen-4-synthesis/benchmarks/previews/gen-4.jpg)
  - Generation 5 (`gen-5-consolidation`): [gen-5.jpg](../gen-5-consolidation/benchmarks/previews/gen-5.jpg)
  - Generation 6 (`gen-6-consolidation`): [gen-6.jpg](../gen-6-consolidation/benchmarks/previews/gen-6.jpg)
  - Generation 7 (`gen-7-grid-zoning`): [gen-7.jpg](../gen-7-grid-zoning/benchmarks/previews/gen-7.jpg)
  - Generation 8 (`gen-8-grid-zoning`): [gen-8.jpg](../gen-8-grid-zoning/benchmarks/previews/gen-8.jpg)
  - Generation 9 (`gen-9-elastic-repulsion`): [gen-9.jpg](../gen-9-elastic-repulsion/benchmarks/previews/gen-9.jpg)
  - Generation 10 (`gen-10-consolidation`): [gen-10.jpg](../gen-10-consolidation/benchmarks/previews/gen-10.jpg)
  - Generation 11 (`gen-11-hero-anchor`): [gen-11.jpg](../gen-11-hero-anchor/benchmarks/previews/gen-11.jpg)
  - Generation 12 (`gen-12-consolidation`): [gen-12.jpg](../gen-12-consolidation/benchmarks/previews/gen-12.jpg)
  - Generation 13 (`gen-13-consolidation`): [gen-13.jpg](../gen-13-consolidation/benchmarks/previews/gen-13.jpg)
  - Generation 14 (`gen-14-consolidation`): [gen-14.jpg](../gen-14-consolidation/benchmarks/previews/gen-14.jpg)
