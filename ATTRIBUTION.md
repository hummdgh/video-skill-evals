# Attribution

## Prior art: `agentic-video-composer`

The rubric in this repository descends from the `video-judge` component of
[`samelhousseini/agentic-video-composer`][repo], by Samer El Housseini. That
work came first, it is good, and several of its best ideas are inherited here
directly.

[repo]: https://github.com/samelhousseini/agentic-video-composer

Specifically, the following are its ideas, not ours:

- **Default FAIL with round-DOWN scoring.** An evaluator that defaults to PASS
  measures how hard a defect is to notice; one that defaults to FAIL measures
  how hard absence is to prove. Only the second works as a release gate.
- **The anti-confabulation doctrine** — that a deterministic measurement
  overrides a holistic LLM opinion wherever the two disagree. This is the most
  valuable single idea in the prior rubric and the backbone of ours.
- **Requiring a literal citation for a secrets finding**, and recording an
  uncited claim as *claimed-but-unverified* rather than failing on it. Secrets
  checks are simultaneously the highest-consequence and most
  hallucination-prone part of any video battery; making the claim falsifiable
  is the right answer.
- **Measuring caption geometry in pixels, filtered to the bottom band** so that
  centred intro and outro title cards do not pollute the subtitle measurement.
  A measurement artefact worth knowing about, and already solved there.
- **Median-of-n stabilisation** of aesthetic scoring, because a single judge
  pass swings run to run.
- **Mirrored A/B panels scored per dimension**, discarding the raw overall vote
  as position-biased.
- **The orphan/short-cue lint**, which the prior work recorded as a lesson
  learned from its own baseline failing on a one-word trailing caption
  fragment.

`RUBRIC.md` documents where we depart from it and why, along with an explicit
Kept / Changed / Added / Removed table. Nothing from the prior rubric was
dropped, and no threshold was softened.

## Independence of implementation

**No code from that repository was copied into this one.** The prior art was
read and its approach studied — that is what the list above acknowledges — but
every module here is independently written, in a different language for most
of the battery, against a different data contract, with different structure.

Where a technique is genuinely the same idea (bottom-band caption filtering,
median-of-n, mirrored A/B), the idea is credited above and the implementation
is our own.

## Skill under test

The harness evaluates a video-composition skill developed separately. That
skill is the *subject* of evaluation; none of its code is vendored, imported or
redistributed here. The harness invokes whatever agent runtime the user
configures, as an external process.
