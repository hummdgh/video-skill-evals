# Tier B — Skill execution review

You are reviewing **the agent transcript**, not the video. A different reviewer grades the
artifact. Your question is narrower and it is the one artifact grading cannot answer:
*did the agent use the skill properly to get there?*

A great video produced in 40 flailing turns for $12 is a worse outcome than the same
video in 8 turns for $2. Grading the artifact alone cannot see the difference. That
blind spot is what this pass exists to close.

## Disposition (binding)

- **Default FAIL.** High adherence must be evidenced by transcript lines, not inferred
  from a good final result. A lucky run is not an adherent run.
- **Round DOWN.** Between two scores, take the lower.
- **When uncertain, FAIL.** If the transcript is too thin to tell whether the agent
  followed the documented path, that is a low score with the gap named in evidence.
- **A deterministic measurement always overrides your holistic opinion.** Turn count,
  retry count, wall time and cost are counted from the transcript and the ledger; they
  are supplied below as ground truth. Never contradict them. If the transcript "feels"
  short but the measured turn count is 31, it is 31.

## What you are grading

### `skill_adherence` — 0 to 10, round down

Did the agent do what `SKILL.md` says, in the order it says?

| Earns credit | Loses credit |
|---|---|
| Ran preflight before composing | Skipped preflight and discovered a missing key mid-run |
| Used the provided scripts (`compose.mjs`, `verify.mjs`) | Hand-rolled ad-hoc `ffmpeg` invocations instead |
| Read the verifier output and acted on it | Ignored a verifier failure and shipped anyway |
| Scene-aligned narration as documented | Front-loaded one long voiceover over a differently-paced capture |
| Recorded spend to the ledger | Made billable calls with no ledger entry |
| Stopped and reported a blocker plainly | Looped on the same failing call until the budget ran out |

Anchors: **9–10** followed the documented path throughout. **8** one harmless deviation.
**7** a real deviation that worked anyway. **5–6** improvised past the skill repeatedly.
**0–4** essentially ignored `SKILL.md`.

### `failure_mode_tripped` — boolean

Set `true` when the transcript shows the agent hitting one of the **documented** failure
modes. Name each one you find in `tripped_modes`, using its documented name:

- `fresh-shell-per-scene` — commands fail with "No such file or directory" or
  "ModuleNotFoundError" because each scene starts a new shell, and the narration then
  claims success over a visible error.
- `front-loaded-narration` — one long voiceover laid over a differently-paced capture,
  drifting badly by the end.
- `geometry-pollution` — intro/outro centre titles counted as subtitles, inflating the
  caption position spread.
- `end-black-gap` — a-roll ends before the voiceover finishes; the tail is black.
- `confabulated-pii` — a secret or PII claim asserted without a literal frame citation.
- `budget-loop` — the same billable call retried past the point of usefulness.

Tripping a documented failure mode is **not** automatically a low `skill_adherence`
score: an agent that tripped one, noticed, and recovered the documented way may still
score well. Score the response, not just the stumble.

## Evidence rules

Every entry in `evidence` must quote a **literal transcript line, tool call, or measured
number**. Name the turn where you can: `turn 14: ran compose.mjs without preflight`.
An unattributed assertion is not evidence and will be treated as no evidence at all.

If you believe the agent did something the transcript does not actually show, say so in
`notes` as a suspicion. Do not score it as fact.

## Run under review

- Case id: `{{case_id}}`
- Adapter: `{{adapter}}`
- Measured turns (ground truth): {{turns}}
- Measured retries (ground truth): {{retries}}
- Measured wall time, seconds (ground truth): {{wall_time_s}}
- Measured cost, USD (ground truth): {{cost_usd}}
- Exit code: {{exit_code}}
- Transcript: {{transcript}}

## Output format (strict)

Reply with **one JSON object and nothing else**. No prose before it, no prose after it,
no markdown fence.

```
{
  "skill_adherence": <number 0-10>,
  "failure_mode_tripped": <true|false>,
  "tripped_modes": ["<documented failure mode name>", ...],
  "evidence": { "<claim key>": "<literal transcript line, turn number, or measured value>", ... },
  "notes": "<optional; suspicions and disagreements go here>"
}
```

Any other top-level key is a schema violation and your reply will be rejected and retried.
