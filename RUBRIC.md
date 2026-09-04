# The rubric

This document defines how `video-skill-evals` grades a video-composition skill,
and argues every point where it departs from its prior art.

**Prior art.** The rubric in [`samelhousseini/agentic-video-composer`][prior]
(the `video-judge` component) is the direct ancestor of this one. It is a good
piece of work and most of its spine is preserved here unchanged. Sections below
are explicit about what is inherited, what is altered, and why.

[prior]: https://github.com/samelhousseini/agentic-video-composer

---

## 1. What we keep, and why it is right

### Default FAIL, round DOWN

The prior rubric's disposition is: award PASS only when every critical gate is
proven by evidence; when uncertain, FAIL; round down. We keep this exactly.

An evaluator that defaults to PASS measures how hard it is to *notice* a
defect. An evaluator that defaults to FAIL measures how hard it is to *prove
absence* of one. Only the second is useful as a release gate, because only the
second gets stricter as your evidence gets thinner.

### Deterministic measurement overrides holistic opinion

The prior rubric calls this the anti-confabulation doctrine, and it is the
single most valuable idea in it. Where a property can be settled by pixels,
`ffprobe`, or a file diff, that result wins over any LLM judgement that
contradicts it.

The reasoning is asymmetric reliability. A holistic pass can hallucinate a
caption mismatch, a visible secret, or a path that was never on screen. It
cannot, however, measure loudness to a tenth of a LUFS. Each method is trusted
only where it is actually authoritative.

We inherit this wholesale, and the `method` field on every `Measurement`
records which technique produced it, so the precedence rule can be applied
mechanically rather than by memory.

### A secrets claim requires a literal citation

The prior rubric requires that before recording a secrets or PII finding, the
judge must cite the exact frame and the literal visible text; absent that, the
finding is recorded as *claimed-but-unverified* and does **not** fail the run.

This is subtle and correct. Secrets findings are the highest-consequence output
of the whole battery and simultaneously the most hallucination-prone, because
a language model asked "are there any credentials on screen?" is being invited
to invent one. Requiring a citation makes the claim falsifiable. We keep it,
and `measure/secrets.py` enforces it in code rather than trusting a prompt.

### Caption geometry is measured in pixels, filtered to the bottom band

Captions are measured by sampling frames and computing bounding boxes, with the
measurement restricted to the lower portion of the frame (`center_y > 0.7`).

The filter is the interesting half. Intro and outro title cards sit in the
centre of frame. Include them and the vertical spread of "subtitles" explodes,
failing a video whose actual subtitles were perfectly placed. This is a
measurement artefact that the prior rubric identified and fixed, and we inherit
both the fix and the threshold.

### Median-of-n judge stabilisation

Aesthetic scores from a single LLM pass swing run to run. The prior rubric
takes the median of three. We keep this, and generalise `n` to a config value
while keeping 3 as the default.

### Mirrored A/B panels, tallied per dimension

When comparing against a reference video, the prior rubric runs the comparison
in both slot orders and discards the raw "overall" vote as position-biased,
counting only the per-dimension tally, and requiring at least three dimensions
better with none worse to call a win.

This is a more careful treatment of LLM comparison bias than most evaluation
harnesses bother with. Kept unchanged.

---

## 2. The seven improvements

Each subsection states the gap, why it matters *specifically* for grading a
reusable skill rather than a single video, and what we do instead.

### 2.1 Two tiers: the artifact, and the execution that produced it

**The gap.** The prior judge is explicit: *"You judge the artifact only — you
don't know or care how hard its maker tried."*

**Why it matters here.** That is exactly right for grading a video, and exactly
wrong for grading a *skill*. A skill is a reusable capability, and its cost of
use is part of its quality. Consider two runs producing an identical video:

| | Run A | Run B |
|---|---|---|
| Turns | 8 | 41 |
| Retries | 0 | 6 |
| Cost | $1.90 | $12.40 |
| Wall time | 4 min | 26 min |

The prior rubric scores these identically. They are not remotely the same
skill. Run B is one API price change away from being unusable, and it will fail
under concurrency long before Run A does.

**What we do.** Two tiers. **Tier A** grades the artifact and extends the prior
rubric's dimensions largely intact. **Tier B** grades the execution: turns,
retries, wall time, cost, adherence to the skill's own documented procedure,
and whether the run tripped a known failure mode.

Tier B is **non-blocking by default** (`tier_b_blocking: false`). A skill that
produces a flawless video expensively should be reported, not rejected — the
decision of what to do about it belongs to a human. Set the flag to make it
blocking once you have baselines you trust.

### 2.2 Calibration against human labels

**The gap.** The prior rubric asserts thresholds — aesthetic ≥ 8/10 per
dimension, mean ≥ 8.7, composite ≥ 95 — without any stated validation against
human judgement.

**Why it matters here.** For a one-off gate, an asserted threshold is a
reasonable expression of taste. For a suite you intend to run repeatedly and
trust as a signal, an uncalibrated judge is a measuring instrument nobody has
checked against a ruler. If the judge's 8/10 corresponds to a human's 6/10,
every downstream number inherits that skew silently.

**What we do.** Ship a `golden/` set of human-labelled fixtures and an
`evals calibrate` command reporting judge-versus-human agreement: Spearman rank
correlation, exact-agreement and within-±1 rates, broken down per dimension so
you can see *which* dimensions the judge is unreliable on.

We are honest about the limit here: the shipped golden set is illustrative seed
data with invented scores. It must be replaced with real human labels before
any calibration figure means anything, and the tool prints a warning until it
is.

### 2.3 Repeats and variance

**The gap.** Median-of-n damps *judge* variance. It does nothing about
*generation* variance.

**Why it matters here.** Video generation is stochastic in a way that video
grading is not. The same prompt, run three times, produces three different
videos — sometimes differing by more than the pass threshold. A suite that runs
each prompt once is sampling a distribution once and reporting the sample as if
it were the parameter.

**What we do.** `--repeats N`, default 3. We report per-dimension score spread,
a flake rate (proportion of repeats disagreeing with the modal verdict), and a
stability grade per case. A case that passes twice and fails once is a
materially different signal from one that passes three times, and the report
says so.

### 2.4 Continuous scores and regression detection

**The gap.** A binary gate at composite ≥ 95 cannot distinguish 99 from 96.

**Why it matters here.** A skill under active development degrades gradually.
The run where quality crosses the threshold is the run where you find out, and
by then the regression may be several changes old and expensive to bisect.

**What we do.** Keep the gate — it is what CI needs — but persist continuous
per-dimension scores and diff every run against a stored baseline
(`evals baseline`). A significant drop is flagged as a regression **even when
the run still passes**. The gate answers "can we ship?"; the regression report
answers "are we getting worse?", and those are different questions.

### 2.5 Declarative, machine-checkable per-case assertions

**The gap.** The prior pipeline pairs its universal rubric with a hand-written
`job-spec.md` per run, carrying the request-specific criteria.

**Why it matters here.** Hand-written criteria are fine for one video and
unusable for a repeatable suite: they cannot be diffed, cannot be validated,
and drift as whoever writes them changes their mind. For regression testing,
the per-case criteria must be as stable as the universal ones.

**What we do.** Every case in the suite declares its criteria as data:

```json
"assertions": {
  "duration_s": [50, 75],
  "min_scenes": 5,
  "aspect_ratio": "16:9",
  "required_concepts": ["capture", "schedule", "review"],
  "language": "en"
}
```

These are checked deterministically and validated against a schema before the
suite runs, so a malformed assertion fails immediately rather than silently
passing every case for a month.

### 2.6 Failure taxonomy

**The gap.** The prior rubric has one failure mode: the video is bad.

**Why it matters here.** Running a suite repeatedly against live APIs means a
meaningful fraction of failures are not about video quality at all — a Veo
timeout, an expired credential, a missing binary. Folding those into the
quality score makes the metric a measure of your API provider's uptime.

**What we do.** Every failure is classified:

- **`infra`** — the harness or environment failed. The agent never got a fair
  attempt: runtime missing, timeout, crash before any artefact. Triggers
  retry, and is **excluded from quality aggregates**.
- **`agent`** — the agent ran and failed to produce a usable artefact.
- **`quality`** — an artefact was produced and did not meet the bar.

Only `quality` failures are what the rubric is actually about. The other two
are reported separately so they are visible rather than averaged away.

### 2.7 Cost and latency as first-class dimensions

**The gap.** Neither appears in the prior rubric.

**Why it matters here.** For a skill being evaluated for customer delivery,
per-video cost and wall time are as much a product property as caption
placement. A skill that costs $12 per video has a different addressable market
from one that costs $2.

**What we do.** Both are scored Tier B dimensions with per-band budgets in
config, and cost is read from the skill's own spend ledger where available
rather than estimated.

---

## 3. Tier A — artifact quality

Extends the prior rubric. Thresholds are inherited unchanged unless noted in
§5.

| Group | Metric | Threshold | Severity | Method |
|---|---|---|---|---|
| **TECH** | resolution | ≥ 1920×1080 | CRITICAL | ffprobe |
| | frame rate | ≥ 30 fps | MAJOR | ffprobe |
| | audio stream present | yes | CRITICAL | ffprobe |
| | not truncated | final ≥ intended content | MAJOR | ffprobe + scan |
| | no black/frozen frames mid-video | none | MAJOR | judge (holistic) |
| **DURATION** | within declared window | per-case `duration_s` | MAJOR | ffprobe + assertion |
| **AUDIO** | integrated loudness | −14 ± 1.5 LUFS | MAJOR | ebur128 |
| | true peak | ≤ −1.0 dBTP | MAJOR | ebur128 |
| | VO intelligible | > −50 LUFS floor | CRITICAL | ebur128 + judge |
| | music ducked under VO | yes | MINOR | judge (holistic) |
| **CAPTION_GEOMETRY** | centre-x | ∈ [0.45, 0.55] | CRITICAL | pixel |
| | centre-y spread | ≤ 0.02 | CRITICAL | pixel |
| | cap-height | ∈ [2%, 5%] of frame | MAJOR | pixel |
| | contrast | ≥ 4.5:1 | CRITICAL | pixel |
| **CAPTION_FIDELITY** | verbatim to VO | every cue ⊂ spoken script | MAJOR | substring |
| **CAPTION_CUES** | min cue duration | ≥ 0.6 s | MAJOR | srt-lint |
| | orphan cues | 0 | MAJOR | srt-lint |
| | overlapping cues | 0 | MAJOR | srt-lint |
| | timecodes valid | 0 inverted | CRITICAL | srt-lint |
| **SYNC** | narrated step matches visible action | drift ≤ 1.5 s | CRITICAL | judge (holistic) |
| | functional claims true on screen | no success claim over visible error | CRITICAL | judge (holistic) |
| **SECRETS** | keys/tokens/paths/project-ids visible | none, literal citation required | CRITICAL | regex + judge + allowlist |
| **MONTAGE** | transitions clean; intro and outro present | yes | MAJOR | judge (holistic) |
| **AESTHETIC** | per-dimension craft scores | each ≥ 8/10 | MAJOR | judge, median of n |
| | aesthetic mean | ≥ 8.7/10 | MAJOR | judge, median of n |

## 4. Tier B — skill execution

New. Non-blocking by default; always reported.

| Group | Metric | Threshold | Severity | Method |
|---|---|---|---|---|
| **EFFICIENCY** | turns | ≤ band budget | MAJOR | transcript |
| | retries | ≤ 2 | MAJOR | transcript |
| **COST** | cost per video | ≤ band budget (USD) | MAJOR | spend ledger, else transcript |
| **LATENCY** | wall time | ≤ band budget (s) | MINOR | harness clock |
| **ADHERENCE** | followed documented procedure | yes | MAJOR | judge over transcript |
| | tripped a documented failure mode | none | MAJOR | probe matching |

Default per-band budgets:

| Band | Cost | Wall time |
|---|---|---|
| simple | $2.00 | 10 min |
| medium | $4.00 | 20 min |
| hard | $8.00 | 30 min |
| adversarial | $8.00 | 30 min |

## 5. Diff against the prior rubric

| | Item | Justification |
|---|---|---|
| **Kept** | Default FAIL, round DOWN | Correct disposition for a release gate |
| **Kept** | Deterministic overrides holistic | Trust each method only where authoritative |
| **Kept** | Secrets need a literal citation | Highest-consequence, most hallucination-prone check |
| **Kept** | Pixel caption geometry, bottom-band filter | Prevents title cards polluting the measurement |
| **Kept** | Median-of-n stabilisation | Damps judge variance |
| **Kept** | Mirrored A/B, per-dimension tally | Correctly handles position bias |
| **Kept** | Orphan/short-cue lint | The prior rubric's own hard-won lesson |
| **Changed** | `n` in median-of-n is configurable | Was fixed at 3; 3 remains the default |
| **Changed** | Per-case criteria are declarative data | Was a hand-written per-run document |
| **Changed** | Composite gate retained *and* continuous scores persisted | Binary alone hides gradual regression |
| **Added** | Tier B execution metrics | A skill's cost of use is part of its quality |
| **Added** | Calibration against human labels | Thresholds should be validated, not asserted |
| **Added** | Repeats, variance, flake rate | Generation is stochastic; one sample is not a measurement |
| **Added** | Regression detection vs baseline | Catches decay before it crosses the gate |
| **Added** | Failure taxonomy | Stops API flakes polluting quality metrics |
| **Added** | Cost and latency dimensions | Product properties for a delivered skill |
| **Removed** | *(nothing)* | No prior dimension or threshold was dropped or softened |

## 6. Known limitations

Stated plainly, because a rubric that oversells itself is worse than a modest
one.

- **The golden set is seed data.** It ships with invented scores as a worked
  example. Until real human labels replace it, every calibration figure is
  meaningless, and the tool says so on each run.
- **We do not measure persuasiveness or brand fit.** Nothing here knows whether
  a video is compelling, on-brand, or appropriate for its audience. Those are
  the judgements most worth having and the ones we are least able to automate.
- **We do not verify factual accuracy of narration.** The SYNC dimension checks
  that narration does not contradict *what is on screen*. It cannot tell you
  the video is wrong about its subject matter.
- **Judge drift is unaddressed.** Scores from a hosted model will move as that
  model is updated. Calibration detects this only if you re-run it; the harness
  does not currently pin or fingerprint the judge model.
- **Aesthetic thresholds remain inherited assertions.** Improvement §2.2 gives
  the machinery to validate them; it does not by itself make the inherited
  numbers correct.
- **Tier B adherence scoring is judge-based** and therefore the softest
  measurement in the system. Treat it as a signal, not a verdict.
- **Mock mode proves the pipeline, not the skill.** A green mock run means the
  harness works. It says nothing about video quality.
