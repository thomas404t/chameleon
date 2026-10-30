# Overnight evolution report

Run started 2026-10-30T07:22:57, finalized 2026-10-30T07:34:04.
6 generations, 75 candidates scored on 63 fixture tasks (9 designs x 7 aspect ratios); every new champion also passed a 177-task regression gate it was never selected on.

## Winner

**gen4-S** (synthesis), fitness **89.48** vs baseline 88.55 (+0.93).

Genome changes vs the baseline:

| gene | baseline | winner |
|---|---|---|
| `margin` | 0.045 | 0.0398 |
| `gap` | 0.025 | 0.0227 |
| `pad_y` | 2.0 | 3.0029 |
| `body_gap_cap` | 1.25 | 1.0732 |
| `col_gap` | 0.02 | 0.01 |
| `hero_floor` | 0.55 | 0.6027 |
| `logo_area` | 0.045 | 0.0307 |
| `w_hero` | 0.18 | 0.1177 |
| `w_order` | 0.12 | 0.1309 |
| `symmetry` | 0.6 | 0.6819 |
| `split_bias` | 0.0 | -0.8 |
| `zone_top_align` | 0.0 | 0.2183 |
| `zone_header` | 0.0 | 1.0 |
| `relax_iters` | 0 | 10 |
| `relax_clear` | 0.02 | 0.0077 |

## Scores

| | baseline | winner |
|---|---|---|
| fixtures mean_total | 88.554 | 89.484 |
| fixtures hierarchy | 23.141 | 23.391 |
| fixtures optical | 18.407 | 18.971 |
| fixtures space | 23.222 | 23.207 |
| fixtures readability | 23.784 | 23.915 |
| fixtures overflow | 0 | 0 |
| fixtures collisions | 0 | 0 |
| fixtures critical_tasks | 0 | 0 |
| regression mean_total | 86.858 | 87.076 |
| regression overflow | 1 | 1 |
| regression collisions | 0 | 0 |
| regression crashes | 0 | 0 |
| regression hard_violations | 0 | 0 |
| regression max_secs | 3.21 | 3.22 |

By aspect ratio (fixture mean):

| ratio | baseline | winner |
|---|---|---|
| 1x1_square | 89.613 | 91.106 |
| 4x5_portrait | 92.12 | 92.023 |
| 9x16_story | 93.083 | 92.783 |
| 16x9_landscape | 86.354 | 88.352 |
| 4x1_header | 88.46 | 89.494 |
| 8x1_leaderboard | 77.598 | 80.066 |
| 3x4_poster | 92.652 | 92.562 |

## Generations

| gen | branch | tag | champion | fitness | delta | regression |
|---|---|---|---|---|---|---|
| 1 | `gen-1-elastic-repulsion` | `gen-1-score-88.85` | gen1-D2 | 88.85 | +0.29 | 86.80 |
| 2 | `gen-2-grid-zoning` | `gen-2-score-89.06` | gen2-B1 | 89.06 | +0.21 | 86.78 |
| 3 | `gen-3-grid-zoning` | `gen-3-score-89.23` | gen3-B3 | 89.23 | +0.18 | 86.92 |
| 4 | `gen-4-synthesis` | `gen-4-score-89.48` | gen4-S | 89.48 | +0.25 | 87.08 |
| 5 | `gen-5-consolidation` | `gen-5-score-89.48` | gen4-S | 89.48 | +0.00 | 87.08 |
| 6 | `gen-6-consolidation` | `gen-6-score-89.48` | gen4-S | 89.48 | +0.00 | 87.08 |

## Previews

- Generation 1: [benchmarks/previews/gen-1.jpg](benchmarks/previews/gen-1.jpg)
- Generation 2: [benchmarks/previews/gen-2.jpg](benchmarks/previews/gen-2.jpg)
- Generation 3: [benchmarks/previews/gen-3.jpg](benchmarks/previews/gen-3.jpg)
- Generation 4: [benchmarks/previews/gen-4.jpg](benchmarks/previews/gen-4.jpg)
- Generation 5: [benchmarks/previews/gen-5.jpg](benchmarks/previews/gen-5.jpg)
- Generation 6: [benchmarks/previews/gen-6.jpg](benchmarks/previews/gen-6.jpg)
