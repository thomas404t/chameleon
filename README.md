# CHAMELEON — resize engine

## Our approach

A deterministic layout solver in `engine/resize.py`. No ML, no network, about
1 second per task.

1. **Classify elements.** A full-bleed image or shape becomes the *background*
   and fills the new canvas (fit `cover` crops it instead of distorting it).
   Ungrouped translucent shapes and missing-asset images are *decor*.
   Everything else is *content*. The largest prominent image (8% or more of the
   source) is marked as the *hero*.
2. **Rigid blocks.** Elements that share a `group_id`, or that are stacked on
   each other, scale together. Images never change aspect ratio.
3. **Rows.** Blocks that sit side by side in the source form a row, and rows
   are kept in reading order. A row too wide for its column stacks its items
   vertically, and sibling blocks in a row share one scale, so badges stay
   consistent with each other.
4. **Stage 1: feasibility search.** Every in-order split of the rows into 1–6
   columns is tried, plus *rebalanced* variants where tail rows (badges, CTA,
   legal) move under the hero. That produces the "visual + CTA | copy" ad
   layout. For each candidate, a binary search finds the largest scale that
   fits. Lone text re-wraps to its column, using the renderer's own glyph
   measurement.
5. **Readable at any size.** Legibility floors apply to text and images (the
   hero gets more). Under space pressure, **type-scale compression** makes
   rendered size grow like f^0.6 instead of f: big type gives room to small
   type while the size order stays strictly preserved.
6. **Stage 2: quality re-ranking.** The top 12 candidates are fully placed and
   scored on legibility, overall scale, hero prominence, reading-order
   fidelity, structure fidelity (similar shape → keep the source structure;
   no stranded single-line columns), balance, and fill.
7. **Brand-layout rules (from market research).** We studied how Apple,
   McDonald's, Chipotle, and Taco Bell, plus IAB banner guidance, lay out
   ads. The engine detects the **logo** and the **CTA** and applies:
   - *Logo leads.* It is centered on top in a masthead band whenever the
     canvas allows (the Apple / McDonald's-arches pattern). On wide, short
     banners (more than 2.2:1) it goes left, following the Gutenberg reading
     pattern (logo → message → CTA, left to right).
   - *Logo presence.* Sized to about 4.5% of the canvas area (banner specs
     use ~6–8%), measured on the asset's **visible pixels**, not its padded
     PNG box, and optically centered.
   - *CTA is terminal.* It stays last in reading order (bottom, or far right).
   - *Symmetry.* A mirror-symmetry score on a 32×32 occupancy grid.
   Creative control: `STYLE` in `resize.py`, or a per-task
   `target_canvas["style"]` with `logo_position` ("auto" / "center" /
   "left"), `symmetry` (0–1), `brand_weight`, and `center_rows`.
8. **Alignment follows placement.** A standalone text block that ends up
   centered in its column gets `text_align: center`, one flush to a side
   reads from that side, and one whose margins are too thin (spanning about
   90% of the width) is centered. Button and badge labels are untouched.
   **Decide by format:** on landscape canvases (wider than 1.2:1), a column
   whose source copy was left- or right-aligned becomes one flush block on
   that side (editorial style). Portrait and square canvases keep a centered
   stack. Text beside a product hugs it.
   Set `style["text_align"] = "keep_source"` to disable this.
9. **Guarantees.** A final pass enforces type hierarchy (a smaller source
   font never renders larger), moves opaque decor off content, and clamps
   everything on-canvas.

Ids, `content`, `font_family` and asset pixels are never changed. Only geometry, `font_size`, `text_align` and `line_height` are.

### Validation (`tools/`)

- `python -m tools.evaluate input output`: hard-rule checks plus quality
  metrics (collisions, min font, hierarchy, hero size, fill, balance).
- `python -m tools.fuzz 500`: 500 random designs × random targets.
  Current result: **0 crashes, 0 rule violations**, slowest about 1.3s.

Across 30 real-format tests (3 samples × 10 formats), the uniform-stretch
baseline averaged **2.2 hard-rule violations per design**. This engine has
**0**, with zero new collisions and 100% preserved type hierarchy.

### Changes

Design fixes from a review of before/after outputs. Every rule is general
and applies to every design, with no tuning for individual designs.

- **Logo sizing.** On a canvas shaped like the source, the logo keeps its
  source footprint relative to the canvas. On much wider or shorter
  canvases it blends toward a fixed banner footprint. It never drops
  below 22px of visible height.
- **Glow centering.** The background's light (the brightness centroid of
  the visible crop) is found, and the body slides so the hero sits on it.
  The logo, the fine print and the margins are never crossed.
- **Translucent decor.** Circles keep their size relative to the canvas
  and their source anchor. They never touch the logo, stay off text and
  buttons, and spread out.
- **Buttons and fine print.** Side-by-side buttons get breathing room
  between them. Legal fine print docks near the bottom edge, with less
  space below it than above it.
- **Like items stay side by side.** A row of like items (a set of
  options, a badge strip) keeps its horizontal arrangement. It wraps into
  a grid only when its text would become unreadable. Short captions are
  glued to their image.
- **Short banners.** The copy becomes a flush-left column beside the
  visual, and a long headline sets tighter.
- **Posters fill the page.** The growth cap on large canvases was
  removed, so posters are no longer under-filled.
- **Crash safety.** A zero-width or zero-height element can no longer
  raise an error. If anything unexpected happens, the engine falls back to
  a plain fit-inside layout that still satisfies every hard rule, so one
  odd design can't take down a whole batch.

### Round 3

- **Product shots pack by what is drawn.** Transparent padding around a
  picture no longer takes up layout width, so a row of products is larger
  and evenly spaced.
- **Columns negotiate width.** A column of long copy takes width from a
  column that doesn't need it before anything shrinks.
- **Short banners put the headline and CTA first.** Both get extra type
  scale.
- **Same-shape canvases keep the original arrangement.**
- **Translucent circles stay in their source corner and off the product.**

### Round 4 (chaos gauntlet review)

From a visual review of the adversarial chaos-gauntlet outputs:

- **Near-misses snap into alignment.** When two stacked items almost share
  a centre line or edge (a badge over the product, the logo over a badge),
  the smaller one slides onto it. A lone button under a row of badges
  centres on the row. A move is kept only if it stays inside the margins
  and touches nothing new.
- **Round trips stay orderly.** Rows are detected by drawn ink, not padded
  boxes, so a product row with wide captions stays one row. A set of like
  items (a badge wall) is read as one unit and laid back out as a grid,
  and the grid is never split across columns. A band already set in
  columns is read column by column. Resizing a design back and forth no
  longer staircases the products or scatters the badges.
- **Copy-only bodies keep their place.** On a background with a glow, a
  text-only body is pulled mostly toward where the designer set it, so it
  no longer rides up against the logo.
- **Logo on top, copy centred beneath, on short banners too**, when the
  design is just a logo over a short centred text stack (banners at least
  150 px tall).
- **Rotated corner tags stay in their corner.** A slanted "SALE" ribbon
  tucked into a corner goes back to that corner at the fit-inside scale.
  If copy now sits there, it moves to the other corner on the same side.

Fixtures 90.62 → 90.91 and regression 88.17 → 88.55 on the judge. In the
Linux scoring image they score 90.78 and 88.42, with 0 crashes, rule
violations and collisions. 500 fuzzed designs give 0 failures.

## Overnight evolution

Every layout heuristic lives in one `GENOME` block at the top of
`engine/resize.py`. An evolutionary tournament tunes that block, and it
also decides whether each newer strategy is switched on.

```bash
python -m evolution.run_overnight_evolution --generations 6 --auto-branch --finalize-to final
```

- **Fixture suite** (`benchmarks/fixture_suite.py`): 9 source designs (the
  3 official tiers plus 6 extra structures) on 7 common aspect ratios
  (1:1, 4:5, 9:16, 16:9, 4:1, 8:1, 3:4). Candidates run through the real
  pipeline, `python -m engine.main`, in parallel sandboxes.
- **Judge** (`evaluator/judge_agent.py`): scores the output JSON and the
  rendered pixels from 0 to 100. The four rubrics, 25 points each, are
  hierarchy & focal anchoring, optical centering & gridding, negative
  space, and collisions & readability.
  - A usability gate sinks the score of any layout with unreadable text
    or content shrunk into a corner.
  - Any hard-rule violation scores 0.
  - Calibration: naive stretch scores 18, a uniform fit-inside scores 72,
    and this engine scores 88.6 before tuning.
- **Mutator** (`evolution/mutator.py`): four variant strategies, each
  mutating its own genes:
  - A, hero-centric anchor
  - B, dynamic grid & zoning
  - C, typography & text flow
  - D, elastic repulsion

  A synthesis candidate also crosses the previous generation's improvers.
- **Regression gate.** A new champion must also pass 162 ad-format tasks
  and 15 edge cases that are never used for ranking: no crash, no rule
  violation, no new overflow or collision, no drop in the judge's mean.
- **Git.** Each generation is committed to `gen-<G>-<strategy>` and tagged
  with its score. The best generation is merged into `final`.

Strategies added along the way:

- **Header zones.** The logo and headline span the full width, with visual
  and copy side by side underneath.
- **Same-shape fidelity measured on drawn text.** A box drawn slightly
  short for its ink grows instead of forcing a re-layout.
- **Banner spread.** On sparse banners, logo, message and CTA spread
  across the strip.
- **Card reflow.** An opaque card re-wraps its re-laid-out copy instead of
  shrinking with it.
- **Elastic clean-up pass.** Rejected whenever it would make any two drawn
  elements overlap.
- **Placeholders.** Missing-asset placeholders tuck into a corner.

Result over 14 generations and 204 candidates:

| Score | Baseline (round 3) | Final |
|---|---|---|
| Fixture judge score | 88.55 | 90.62 |
| Regression judge score | 86.86 | 88.17 |
| 8:1 leaderboard | 77.6 | 85.0 |
| 16:9 | 86.4 | 89.8 |

The final engine has 0 crashes, 0 rule violations and 0 collisions,
both locally and in the Linux scoring image. See
`OVERNIGHT_REPORT.md` for the full log and previews.

---

# Starter repo notes

This is the scaffolding for your engine. It handles input loading, output
writing, rendering, and Docker packaging — so the only file you need to
edit is **`engine/resize.py`**.

```
input:  a task JSON (source design + target canvas)
              |
              v
        engine/resize.py   <-- YOU IMPLEMENT THIS
              |
              v
output: <task>_output.json  (schema-valid resized design)
        <task>_output.png   (rendered preview)
```

Both the JSON and the PNG are what get evaluated — see `RULES.md`.

## Repo layout

```
engine/
  resize.py        <- implement your approach here
  main.py           <- orchestrator (input -> resize() -> output). Don't need to edit this.
renderer/           <- the real renderer (transformer-convertor + cr-renderer), already vendored
brand_kit/
  assets/           <- image files referenced by asset_id in the sample designs
  fonts/            <- font files matching the schema's font_family values
input/               <- drop task JSON files here for local testing (3 samples included)
output/              <- results land here
chameleon_design_schema_v0.9.json   <- full field-by-field spec for the design format
Dockerfile
RULES.md
```

## Quick start (no Docker needed for iterating on `resize()`)

```bash
pip install -r requirements.txt
python -m engine.main
```

This processes every `*.json` file in `input/` (the 3 sample designs are
already there — one per difficulty level) and writes an `_output.json` and
`_output.png` for each into `output/`.

The included baseline is a **uniform stretch** — deliberately the wrong
approach (see the main brief: "a naive program stretches the picture; a
good one moves the furniture"). It's there so the pipeline runs on day
one. Run it once, look at `output/*_output.png`, and you'll see exactly
why it's not good enough — that's the point. Replace the body of
`resize()` in `engine/resize.py`, not just its constants.

## What the self-check does (and doesn't do)

`main.py` runs a quick structural check after every task — same element
`id`s as the source, required fields present, nothing off-canvas. **This
is not the official scorer.** It exists to catch obvious mistakes before
you waste a submission on them. See `RULES.md` for what's actually
checked and graded.

A self-check warning does not fail the run — your JSON and PNG are still
written either way. Read the warning; decide whether it's a real problem
or, like the Level 2 sample's intentionally corner-bleeding decorative
circle, an expected one.

## The renderer

`main.py` calls `renderer/converter/convert.py`'s `render_design_to_png()`
(a vendored copy of `transformer-convertor`, wrapping `cr-renderer`) to
produce each task's PNG — already wired in, no setup needed. It reads
fonts/assets from `brand_kit/` and writes a real rendered preview, not a
placeholder. If you ever see plain labeled boxes instead of real text and
images in `output/*.png`, that means the import in `main.py` failed (e.g.
`cr-renderer` didn't install — check `requirements.txt` installed cleanly)
and it silently fell back to the box-and-label placeholder; don't judge
your layout quality by that fallback.

## Docker (how you'll actually be run)

```bash
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none \
  -v $(pwd)/input:/app/input \
  -v $(pwd)/output:/app/output \
  my-chameleon-engine
```

`--network none` matches how stage-one scoring runs: everything your
engine needs must be installed at **build** time (add it to
`requirements.txt`), not fetched at run time. If you're using a locally
hosted model, bake its weights into the image during the build step.

## See also

- `chameleon_design_schema_v0.9.json` — full input/output field spec
- `RULES.md` — what's checked, what's graded, and what disqualifies a submission
- FAQ — for anything not covered above
