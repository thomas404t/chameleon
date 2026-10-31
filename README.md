# CHAMELEON: Layout-Aware Resize Engine

Submission for **Project CHAMELEON** (Adobe University Hackathon 2026), stage one.

`resize(source, target_canvas)` adapts a finished design to a new canvas size
by re-laying out its existing elements instead of stretching them. Logo,
headline, product, copy and call to action are arranged as a designer would
for the new format.

- **Deterministic.** No ML models, no network access, no generated pixels.
- **Fast.** Typically under 1 s per task on CPU.
- **Rule-safe.** Element ids, text content, fonts and image pixels are never
  changed, and images keep their aspect ratio.

## Results

Measured in the official scoring image (linux/amd64, `--network none`).
Scores come from our local judge (`evaluator/judge_agent.py`), which models
the four rubric areas: hierarchy, optical centring, negative space and
readability.

| Benchmark | Tasks | Judge score (0–100) | Crashes / rule violations / collisions |
|---|---|---|---|
| Fixture suite (9 designs × 7 aspect ratios) | 63 | **90.8** | 0 / 0 / 0 |
| Regression set (real ad formats + edge cases) | 177 | **88.4** | 0 / 0 / 0 |
| Fuzz (random designs × random targets) | 500 | n/a | 0 / 0 / n/a |

For reference, on the same judge a naive stretch scores 18 and a uniform
fit-inside scores 72.

## Quick start

### Option A: Docker (the official scoring setup)

1. Build the image:
   ```bash
   docker build --platform linux/amd64 -t chameleon .
   ```
2. Put task files in `input/`. Three official samples are included.
3. Run:
   ```bash
   docker run --rm --network none \
     -v "$(pwd)/input:/app/input" \
     -v "$(pwd)/output:/app/output" \
     chameleon
   ```
4. Each `input/<task>.json` produces `output/<task>_output.json` (the resized
   design) and `output/<task>_output.png` (its rendering).

### Option B: Local Python (3.11+)

```bash
pip install -r requirements.txt
python -m engine.main
```

This reads the same `input/` folder and writes to the same `output/` folder.

### Task format

A task is `{"source": <design>, "target_canvas": {"width": W, "height": H}}`.
The design format is defined in `chameleon_design_schema_v0.9.json`.

## How it works

The engine lives in `engine/resize.py`.

1. **Classify.** Each element is classified as background (full-bleed, so it
   fills the new canvas with `cover`), decor (translucent shapes, missing
   assets) or content. The largest prominent image is the hero; the logo,
   call to action and fine print are detected too.
2. **Group.** Grouped and layered elements become rigid blocks that scale as
   one, and short captions attach to their image. Blocks form rows in reading
   order, measured on drawn pixels rather than padded boxes. Sets of like
   items, such as badge walls and product options, are kept together as a grid.
3. **Search.** Every in-order split of the rows into 1–6 columns is tried,
   plus a "visual + CTA | copy" ad variant and logo-masthead and header-zone
   layouts. For each, a binary search finds the largest scale that fits. Text
   re-wraps using the renderer's own glyph metrics, so heights are exact.
4. **Rank.** The best candidates are fully placed and scored on legibility,
   size, hero prominence, reading order, structure fidelity, balance, fill
   and brand conventions (logo leads, CTA last).
5. **Refine.**
   - The body centres on the background's light.
   - Fine print docks to the bottom.
   - An elastic pass clears crowding without creating overlaps.
   - Near-miss alignments snap onto a shared line.
   - Decor and rotated corner tags return to their corners.
6. **Enforce.** Type hierarchy is preserved (a smaller source font never
   renders larger), and everything is clamped on-canvas. If anything
   unexpected happens, the engine falls back to a plain fit-inside layout
   that still meets every hard rule.

When the target is the same shape as the source (within 8%), the original
arrangement is scaled as-is instead of being re-laid out.

All tunable heuristics live in one `GENOME` block at the top of
`resize.py`. Per-task creative overrides can be passed as
`target_canvas["style"]`: `logo_position`, `symmetry`, `brand_weight`,
`center_rows`, `text_align`.

## Validation

| Command | What it does |
|---|---|
| `python -m tools.evaluate input output` | Hard-rule checks and quality metrics on a run |
| `python -m tools.fuzz 500` | 500 random designs × random targets; reports crashes and violations |
| `python -m benchmarks.fixture_suite --engine engine/resize.py --set fixtures --judge` | Fixture suite through the real pipeline, scored by the judge (`--set regression` for the regression set) |
| `python -m benchmarks.chaos_gauntlet --build` then `--run` | Adversarial stress tests (see below); contact sheets in `benchmarks/runs/chaos/latest/sheets/` |

The chaos gauntlet covers:

- same-size and round-trip resizes;
- canvases 1 px either side of each layout threshold;
- extreme scales (38×38 to 32768²);
- ribbon ↔ ticker inversions;
- non-rectangular safe zones;
- ten hand-built trap designs.

Its tasks are written to `input/chaos/`, which the official runner never
reads. In the latest run, the only failures are the three 32768² tasks: the
bundled renderer refuses images that large, and the engine itself completes
all three.

## Development

- **Evolutionary tuning.** `python -m evolution.run_overnight_evolution`
  mutates the `GENOME` across four strategies, gates each candidate on the
  regression set, and commits every generation. Fourteen generations raised
  the fixture score from 88.6 to 90.6. See `OVERNIGHT_REPORT.md`.
- **Design review.** Four rounds of visual review against before/after
  renders, the last on the chaos gauntlet, drove the alignment snapping,
  round-trip stability and corner-tag handling.

The generation history is kept as `gen-*-score-*` tags.

## Repository layout

```
engine/          resize.py (the engine), main.py (input -> resize -> output runner)
renderer/        vendored transformer-convertor + cr-renderer
brand_kit/       assets and fonts referenced by the designs
input/, output/  task files in, results out
evaluator/       local judge (rubric proxy)
benchmarks/      fixture suite, regression set, chaos gauntlet
evolution/       GENOME mutator and tournament runner
tools/           evaluate and fuzz utilities
```

## References

- `chameleon_design_schema_v0.9.json`: design format
- `RULES.md`: what is checked, graded and disqualifying
- `FAQ.md`: organiser FAQ
- `OVERNIGHT_REPORT.md`: tuning log and previews
