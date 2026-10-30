# Overnight evolution report

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

- Generation 1: [benchmarks/previews/gen-1.jpg](benchmarks/previews/gen-1.jpg)
- Generation 2: [benchmarks/previews/gen-2.jpg](benchmarks/previews/gen-2.jpg)
- Generation 3: [benchmarks/previews/gen-3.jpg](benchmarks/previews/gen-3.jpg)
- Generation 4: [benchmarks/previews/gen-4.jpg](benchmarks/previews/gen-4.jpg)
- Generation 5: [benchmarks/previews/gen-5.jpg](benchmarks/previews/gen-5.jpg)
- Generation 6: [benchmarks/previews/gen-6.jpg](benchmarks/previews/gen-6.jpg)
- Generation 7: [benchmarks/previews/gen-7.jpg](benchmarks/previews/gen-7.jpg)
- Generation 8: [benchmarks/previews/gen-8.jpg](benchmarks/previews/gen-8.jpg)
- Generation 9: [benchmarks/previews/gen-9.jpg](benchmarks/previews/gen-9.jpg)
- Generation 10: [benchmarks/previews/gen-10.jpg](benchmarks/previews/gen-10.jpg)
- Generation 11: [benchmarks/previews/gen-11.jpg](benchmarks/previews/gen-11.jpg)
- Generation 12: [benchmarks/previews/gen-12.jpg](benchmarks/previews/gen-12.jpg)
- Generation 13: [benchmarks/previews/gen-13.jpg](benchmarks/previews/gen-13.jpg)
- Generation 14: [benchmarks/previews/gen-14.jpg](benchmarks/previews/gen-14.jpg)
