# Mirrored blind A/B — one panel

You are comparing **two videos, labelled A and B**. You are not told which is which, and
there is nothing to work out: the slot order is randomised and this exact comparison is
run twice with the two videos swapped. Guessing which is the "real" one is wasted effort,
and a vote that only survives one slot order is discarded automatically.

Judge the artifacts only.

## Disposition (binding)

- **Default to `tie`.** A dimension is `A` or `B` only when the difference is *visible
  and citable*. A hunch is a tie. A preference you cannot point at is a tie.
- **Round DOWN, which here means: prefer the tie.** If you would call it 51/49, it is a
  tie.
- **When uncertain, `tie`.** Uncertainty must never be resolved in favour of either slot.

## Anti-confabulation doctrine (binding)

- **A deterministic measurement always overrides your holistic opinion.** Where a
  difference can be settled by `ffprobe`, a pixel measurement, or a file diff, the
  measured result wins. Any measured values supplied below are ground truth; never vote
  against them.
- **Cite literal evidence for every non-tie vote.** Each entry in `evidence` must name a
  timestamp (`00:00:31.200`), a frame file (`frame_0288.png`), or a measured number with
  its unit. "B feels more cinematic" is not evidence. "B holds the grade across the cut
  at 00:00:31.200 where A shifts warm" is.
- **A secrets or PII claim without a literal citation is `claimed-but-unverified`, not a
  finding.** Do not vote a slot down over a secret you cannot quote from a named frame.

## How your vote is used

Be aware of exactly what happens to this panel, because it should change how you answer:

- **Your per-dimension votes are the only thing that counts.** They are tallied across
  both slot orders. A dimension counts as a genuine difference only when both orders
  agree; where they disagree, the judgement is treated as position bias and discarded.
- **Your `overall` vote is recorded and then discarded.** It is required by the output
  format and it is not used in any decision. A holistic pairwise preference tracks
  recency and loudness more than quality, so it is not trusted here. Do not try to make
  `overall` "carry" a judgement your per-dimension votes did not support — it cannot.

So: put your real reasoning into the per-dimension votes, and cite it.

## Slots

- **Slot A:** `{{slot_a}}`
- **Slot B:** `{{slot_b}}`
- Case id: `{{case_id}}`
- Deterministic measurements, if any (ground truth): {{measurements}}

## Dimensions to compare

Vote `"A"`, `"B"` or `"tie"` on each of these, and no others:

{{dimensions}}

| Dimension | The question |
|---|---|
| `visual_coherence` | Which holds one consistent world across its shots? |
| `color_and_light` | Which has the more deliberate, consistent grade? |
| `motion_quality` | Which has smoother, more intentional camera and subject motion? |
| `scene_transitions` | Which cuts land better — on the beat, no black gaps? |
| `narrative_arc` | Which resolves rather than merely stopping? |
| `audio_polish` | Which has the more intelligible voiceover and better-ducked music? |

## Output format (strict)

Reply with **one JSON object and nothing else**. No prose before it, no prose after it,
no markdown fence.

```
{
  "per_dimension": { "<dimension>": "A" | "B" | "tie", ... },
  "overall": "A" | "B" | "tie",
  "evidence": { "<dimension>": "<literal timestamp, frame file, or measured number>", ... },
  "notes": "<optional>"
}
```

`per_dimension` must contain an entry for **every** dimension listed above, and every
value in `per_dimension` and `overall` must be exactly `"A"`, `"B"` or `"tie"`. Any other
top-level key, or any other value, is a schema violation and your reply will be rejected
and retried.
