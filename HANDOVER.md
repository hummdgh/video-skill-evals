# Handover notes

Written at the end of the build session, for whoever picks this up next —
including future me. This is the *build* record: what was asked for, what got
made, what is trustworthy, and what is not.

For what the project *is*, read [`README.md`](README.md). For the grading
argument, read [`RUBRIC.md`](RUBRIC.md).

---

## 1. What was asked for

> "Create a new repo that makes an evaluator pipeline for this skill. It needs
> to be robust. A script that will automatically prompt via a CLI the LLM to
> use the skill to generate a video based on some predefined prompts and then
> evaluate the results. Improve on the quality rubrics in Sam's repo."

Two inputs:

- **The skill under test** — a video-composition agent skill (Jaime Antolin's
  `video-composition-skill`). An agent reads its `SKILL.md` and drives Node
  scripts that call Veo, Gemini TTS/image and Lyria.
- **The prior art** — the `video-judge` component of
  `samelhousseini/agentic-video-composer`, whose rubric this improves on.

Neither is vendored here. The skill is invoked as an external process; the
prior art was read, credited, and reimplemented rather than copied.

## 2. What got built

A four-stage pipeline, each stage independently runnable and resumable:

```
generate  ->  measure  ->  score  ->  report
```

40 files, ~8,600 lines of Python, ~750 of docs. **Python standard library
only** — verified by AST audit, not by grep.

Key design decisions, and why:

| Decision | Reason |
|---|---|
| Runtime is a **config-driven adapter** | We do not know which agent runtime the user has. Guessing in code would be expensive to undo; a command template makes switching a config edit. |
| **Mock by default**, `--live` opt-in | A full live suite is 36 videos of real API spend. Nobody should trigger that by accident. |
| **Stdlib only** | A dependency you do not have cannot break your build, fail an audit, or create an obligation you did not plan for. |
| **No bundled FFmpeg** | Both common npm wrappers are licence traps. See `LICENCE-NOTES.md`. |
| **Unverified ≠ pass** | A check that could not run fails the gate. A battery that skips what it cannot measure produces a meaningless green. |

## 3. Provenance — read this before trusting anything

The build was originally delegated to background workers. **Almost all of them
failed**: roughly eleven attempts, nine of which died with the same
infrastructure error, several producing nothing at all. I diagnosed it wrongly
twice (first as nested delegation, then as payload size) before concluding it
was simply intermittent and unfixable from my side.

The practical consequence is that this repo has **two tiers of provenance**, and
they deserve different levels of trust:

### Written directly and verified under test

| File | Verified by |
|---|---|
| `config.py` | round-trip, validation of the live-without-command case |
| `runstate.py` | resume, atomic write, stale-running recovery |
| `generate.py` | full 24-unit run, then a resume that correctly skipped all 24 |
| `adapters/` (all 6) | registry, misconfiguration rejection, mock end-to-end |
| `measure/srt_lint.py` | the orphan-cue case that defeats substring fidelity |
| `measure/tech.py` | real video, missing file, and a text file renamed `.mp4` |
| `measure/audio.py` | real ebur128 parse (−21.8 LUFS on the mock tone) |
| `measure/secrets.py` | real keys caught + cited; placeholders ignored; uncited claim non-fatal |
| `measure/__init__.py` | battery across all 24 units |
| `score.py` | full gate, taxonomy, aggregation over 24 units |
| `report/` | self-containment audit + visual render |
| `cli.py`, `__main__.py` | `--help` on every subcommand, four-command acceptance run |
| `suites/default.json` | schema validation, 12 cases, 7 failure modes |
| All docs | — |

### Salvaged from workers that died mid-task

| File | Lines | Status |
|---|---|---|
| `schema.py` | 1,097 | imports clean, 26 public types, used by everything |
| `judge/client.py` | 1,020 | imports clean; **never run against a live model** |
| `judge/stabilise.py` | 422 | imports clean; not exercised |
| `judge/ab.py` | 454 | imports clean; not exercised |
| `execmetrics.py` | 638 | **exercised** — produced correct Tier B scores |
| `judge/prompts/*.md` | 262 | not exercised |
| `pyproject.toml`, `.gitignore`, `suites/schema.json` | — | fine |

**Roughly a third of the code is salvaged.** It all imports and the parts that
were exercised behave correctly. But it has had materially less scrutiny than
the rest, and the judge layer in particular is structurally verified only.

One salvaged file — the original `evals/__init__.py` — imported a type
(`CaseRun`) that never existed in the run-state design I actually wrote. It
failed loudly on first import and I replaced it. That is the good outcome, but
treat it as evidence that salvage needs checking rather than trusting.

## 4. What is verified working

The full pipeline, end to end, on the mock adapter:

```
generate  → 36 units (12 cases × 3 repeats), 34 ok, 2 failed, 0 infra
measure   → 36 units measured
score     → PASS 0  ADVISORY 0  FAIL 36 | mean 67 | gate FAILED
report    → self-contained HTML + Markdown
```

**The failures are correct.** The mock renders a 2-second test pattern with a
440 Hz tone. A case declaring a 50–75 s window gets 2 s, so the duration
assertion rightly fails; the tone reads −21.8 LUFS against a −14 target, so
loudness rightly fails. A mock that passed everything would prove nothing.

Three of the seven rubric improvements are demonstrated under test rather than
merely implemented:

1. **Orphan-cue detection** — verbatim fidelity PASSES while the orphan check
   FAILS on the same input. That is precisely the gap in the prior rubric.
2. **Tier B** — flagged a *simple* case burning $4.53 and 15 turns against
   budgets of $1.50 and 8, while a *hard* case at $0.59 passed. The prior
   rubric scores both purely on the video.
3. **Secrets doctrine** — an uncited judge claim recorded as unverified without
   failing the run; real credentials caught with literal, redacted citations.

## 5. What is not done

| Gap | Consequence |
|---|---|
| **Caption geometry** unimplemented | Stubbed, reports unverified, **fails by default**. Four CRITICAL thresholds. The gate cannot pass until it exists or is waived. Deliberately fails loudly rather than being omitted. |
| **Judge layer untested live** | Structure verified, behaviour unknown. |
| **`--live` never exercised** | Plumbing tested through mock only. No real runtime has been driven. |
| **No unit tests, no CI** | The acceptance run is the only regression guard. |
| **`calibrate` not written** | CLI wires it; the module is absent and reports so. |
| **Scene counting** unimplemented | `min_scenes` assertions report unverified (non-MUST). |
| **Golden set is seed data** | Invented scores. Calibration is meaningless until replaced; the tool warns. |

## 6. Things a reader will otherwise trip over

- **Mock determinism is intentional.** Results are seeded from
  `(case_id, repeat_idx)`, so the same suite always yields the same numbers —
  which is what makes scoring and regression testable. Repeats vary from one
  another so variance machinery has real input.
- **The mock renders a real video when ffmpeg is present**, and a text
  placeholder when it is not. Both paths work; the battery degrades to
  "unverified" on the placeholder rather than passing it.
- **The mock deliberately fails adversarial cases** on `repeat_idx % 3 == 2`,
  so the failure path gets exercised. This only shows up at 3+ repeats.
- **`score` exits non-zero when the gate fails.** Intentional, for CI. It is
  not an error in the tool.
- **Reports say "mock run" prominently.** Do not let a green harness be
  mistaken for a good skill.

## 7. Open decisions

Three, all needing a human:

1. **Which agent runtime?** The `claude-code` / `gemini-cli` / `antigravity`
   adapters ship conventional command templates as *defaults only*. They are
   educated guesses. One config line makes the real one correct.
2. **Copyright holder.** `LICENSE` names Google LLC as an unverified
   placeholder. This must be settled before publishing — the same defect the
   licence review found in the skill's own repo, and it would be careless to
   reproduce it here.
3. **Is caption geometry worth building?** Until it exists the gate cannot
   pass. Either implement it or explicitly waive those four thresholds.

## 8. Picking this up

```bash
# prove the harness works, no cost, no network
python3 -m evals generate --suite suites/default.json --mock --repeats 2 --run-dir runs/smoke
python3 -m evals measure  --run-dir runs/smoke
python3 -m evals score    --run-dir runs/smoke
python3 -m evals report   --run-dir runs/smoke --html runs/smoke/report.html
```

Then, in order of value:

1. Point the adapter at your real runtime and run **one** case live
   (`--case simple-01-single-topic --repeats 1`) before anything larger.
2. Implement `measure/caption_geometry.py`. Frames can be extracted as PGM via
   ffmpeg and parsed in pure Python — no numpy needed.
3. Write unit tests. The modules were built to be testable; there simply was
   not time.
4. Replace `golden/labels.json` with real human labels, then `calibrate`.

## 9. Git state

`git init` has been run. **Nothing is staged or committed** — I do not commit
without being asked, so the first diff is yours to review.

The repo has never been pushed. Publishing needs credentials that were not
available in the build environment, so `git remote add` and `git push` are
manual steps.
