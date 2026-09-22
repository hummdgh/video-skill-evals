# video-skill-evals

An evaluator pipeline for a video-composition agent skill. It drives an LLM
agent across a fixed prompt suite, measures what comes out, and scores it
against a two-tier rubric.

The point is repeatability. Anyone can watch one generated video and form an
opinion. This answers a harder question: *is the skill getting better or worse,
and how much does it cost to use?*

```
   suites/default.json
           |
           v
   +------------+   +------------+   +------------+   +----------------+
   |  generate  |-->|  measure   |-->|   judge    |-->| score / report |
   +------------+   +------------+   +------------+   +----------------+
   drive the        deterministic    LLM-scored       rubric, gate,
   agent across     battery          aesthetic,       regressions,
   the suite,       (no opinions)    sync, montage,   HTML + Markdown
   n times each                      adherence
        |                 |                |                  |
        v                 v                v                  v
   cases/**/        measurements     judgements         report.json
   result.json      .json            .json              report.html
                                     reviews.json
```

Each stage is independently runnable and resumable. A live run can cost real
money and take hours, so an interruption must never mean starting over.

The split between `measure` and `judge` is deliberate. Everything a machine can
settle — resolution, loudness, cue timing, credentials on screen — is decided by
arithmetic and always overrides the model. Only what genuinely needs judgement
goes to `judge`, which receives the deterministic results as context so it
cannot contradict a measured number blind.

## Quickstart

No API keys, no cost, no network:

```bash
python3 -m evals generate --suite suites/default.json --mock --repeats 2 --run-dir runs/smoke
python3 -m evals measure  --run-dir runs/smoke
python3 -m evals judge    --run-dir runs/smoke
python3 -m evals score    --run-dir runs/smoke
python3 -m evals report   --run-dir runs/smoke --html runs/smoke/report.html
```

`judge` is optional. Skip it and you still get every deterministic dimension;
the aesthetic, sync and montage groups are simply absent from the report.

Open `runs/smoke/report.html`. It is fully self-contained — no CDN, no scripts,
no external fetches — so it renders from `file://` on an airgapped machine.

A mock run exercises **the harness, not the skill**. Every report says so at the
top. Do not mistake a green mock for a good skill.

## Requirements

- Python 3.11+, standard library only. Nothing to install.
- `ffmpeg` and `ffprobe` on `PATH` for live measurement. Not bundled, on
  purpose — see [`LICENCE-NOTES.md`](LICENCE-NOTES.md). Mock mode needs neither.
- An agent runtime, for `--live`. See below.

## Configure your agent runtime before a live run

The harness does not know which agent runtime you have, and never needs to. The
runtime is described by a command template in config, so switching it is a
config edit rather than a code change:

```json
{
  "adapter": {
    "name": "generic",
    "command": "my-agent run --skill video-composition --file {prompt_file} --out {output_dir}",
    "timeout_s": 1800
  }
}
```

| Placeholder | Meaning |
|---|---|
| `{prompt_file}` | absolute path to the rendered prompt |
| `{workdir}` | this unit's isolated working directory |
| `{output_dir}` | where artefacts should be written |
| `{case_id}`, `{repeat_idx}` | unit identity, for a run label |

Built-in adapters: `generic` (default), `claude-code`, `gemini-cli`,
`antigravity`, `mock`. The three named runtimes are thin subclasses supplying a
conventional command as a **default only** — CLI flags change between versions,
so verify against your installed version and override `adapter.command` if it
differs.

## Cost warning

`--live` invokes a real agent, which calls real video, speech and music APIs.
Cost scales with `repeats`:

```
12 cases x 3 repeats = 36 videos
```

That is a meaningful bill and can take hours. Mock is the default precisely for
this reason. Per-band cost and latency budgets live in config and are scored as
Tier B dimensions, so overruns show up in the report rather than only on an
invoice.

Start with a single case:

```bash
python3 -m evals generate --live --case simple-01-single-topic --repeats 1 --run-dir runs/first-live
```

## Commands

| Command | Does |
|---|---|
| `generate` | drives the agent across the suite; resumable |
| `measure` | deterministic battery — ffprobe, loudness, cue lint, caption geometry, secrets |
| `judge` | LLM-scored dimensions — aesthetic, sync, montage, skill adherence |
| `score` | applies the rubric, produces verdicts, detects regressions |
| `report` | renders Markdown / HTML / JSON |
| `baseline` | promotes the current run to the regression baseline |
| `calibrate` | judge-vs-human agreement on the golden set *(not yet implemented)* |

Common flags: `--run-dir`, `--config`, `--suite`, `--repeats`, `--adapter`,
`--case`, `--mock` / `--live`, `--html`, `--markdown`, `--baseline`.
`judge` also takes `--n-samples` (median-of-n, default 3) and `--backend`
(`mock` or `http`). Every command takes `--help`.

`score` exits non-zero when the gate fails, so CI can depend on it.

## The rubric, briefly

Full detail in [`RUBRIC.md`](RUBRIC.md), including the argument for every
departure from the prior art it descends from.

**Tier A — the artifact.** Resolution, frame rate, loudness, true peak, caption
geometry, caption fidelity, cue linting, A/V sync, secrets, montage,
aesthetics.

**Tier B — the execution.** Turns, retries, wall time, cost, adherence to the
skill's documented procedure, and whether a known failure mode was tripped.
Non-blocking by default but always reported, because a skill that produces a
good video after 40 turns and $12 is not the same as one that does it in 8
turns for $2.

Three rules run through the whole thing:

- **Default FAIL, round DOWN.** Uncertainty fails.
- **Deterministic beats holistic.** A measured number overrides a model's
  opinion wherever they disagree.
- **Unverified is not a pass.** A check that could not run reports as
  unverified and fails the gate. A battery that quietly skips what it cannot
  measure produces a green dashboard that means nothing.

## Failure taxonomy

Every failure is classified, so infrastructure noise cannot masquerade as
quality signal:

| Class | Meaning | Counted in quality? |
|---|---|---|
| `infra` | runtime missing, timeout, crash before any artefact | **no** — retried |
| `agent` | agent ran, produced nothing usable | yes |
| `quality` | artefact produced, did not meet the bar | yes |

## Repeats, variance and regressions

Generation is stochastic. One sample per prompt is not a measurement, so the
default is 3 repeats, and the report carries score spread, flake rate and a
stability grade per case.

The gate is binary but the scores are continuous and stored. Promote a good run
with `baseline`, and later runs are diffed against it — a slide from 99 to 96
is flagged as a regression even though it still passes.

## Current limitations

Stated plainly:

- **Caption geometry is unreliable on busy backgrounds.** It is implemented and
  accurate where captions have normal treatment: measured against burned-in
  ground truth it returns centre-x `0.4992` for a centred line and correctly
  fails an off-centre one at `0.1529`. But where bright content sits flush
  against the caption with no scrim, background pixels are absorbed into the
  text region and cap-height is over-estimated — roughly 2× on a `testsrc`
  pattern. The row-coverage heuristic wants replacing with connected-component
  analysis. Treat cap-height on visually busy footage with suspicion.
- **No unit tests and no CI.** The acceptance run is the only regression guard.
  Modules were built to be testable; the tests were never written.
- **`calibrate` is not implemented.** The CLI wires it and reports cleanly that
  it is unavailable. Judge scores are therefore uncalibrated against human
  judgement — see `RUBRIC.md` §2.2 for why that matters.
- **The golden set is illustrative seed data** with invented scores.
  Calibration figures would be meaningless until real human labels replace it,
  and the tool says so.
- **Scene counting is not implemented**, so `min_scenes` assertions report as
  unverified. They are marked non-MUST so they do not fail the gate.
- **Aesthetic thresholds are inherited assertions.** The machinery to validate
  them exists; the validation has not been done.

## Layout

```
evals/
  schema.py        frozen data contract; everything crosses stages as these types
  config.py        layered config: defaults <- file <- env <- CLI
  runstate.py      resumable run manifest, atomic writes
  cli.py           single entry point for every stage

  generate.py      stage 1: drive the agent across the suite
  adapters/        runtime adapters: generic, claude-code, gemini-cli,
                   antigravity, and the fully offline mock

  measure/         stage 2: deterministic battery
    tech.py            ffprobe: resolution, fps, audio stream, duration
    audio.py           ebur128: loudness, true peak, intelligibility floor
    srt_lint.py        cue duration, orphans, overlaps, verbatim fidelity
    caption_geometry.py  pixel analysis via PGM; no numpy, no PIL
    secrets.py         regex scan; findings require a literal citation

  judge/           stage 3: LLM-scored dimensions
    artifact.py        aesthetic, sync, montage (median-of-n stabilised)
    client.py          backends: offline mock, and HTTP for a real model
    stabilise.py       median-of-n and spread
    ab.py              mirrored blind A/B against a reference
    prompts/           the judge prompt templates

  execmetrics.py   Tier B: turns, retries, wall time, cost, adherence
  score.py         stage 4: assertions, gate, aggregation, regressions
  report/          stage 5: markdown, self-contained HTML, leaderboard

suites/            prompt suites and their JSON Schema
golden/            human-labelled calibration set (seed data only)
```

## Licence

MIT — but **the copyright holder is a placeholder and must be confirmed before
publishing**. See [`LICENCE-NOTES.md`](LICENCE-NOTES.md).

Prior art for the rubric is credited in [`ATTRIBUTION.md`](ATTRIBUTION.md).
