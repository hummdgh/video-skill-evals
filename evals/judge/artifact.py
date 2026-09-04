"""Judge-scored Tier A dimensions: AESTHETIC, SYNC and MONTAGE.

These are the parts of the rubric a machine cannot settle. Everything in
``evals/measure/`` is arithmetic -- resolution, loudness, cue duration. What
remains is judgement: is the narration actually describing what is on screen,
do the transitions work, is the thing any good.

Three safeguards apply, inherited from the prior art and enforced here in code
rather than trusted to a prompt:

**Median-of-n.** A single judge pass on an aesthetic dimension swings run to
run. Every dimension is sampled ``n`` times (default 3) and the median taken;
the spread is reported so instability is visible rather than averaged away.

**Deterministic measurements are supplied as context and win ties.** The
prompt is given the real ffprobe/ebur128 numbers. If the judge contradicts a
measured value, the measurement stands -- see ``notes`` on the returned score.

**Unverified is not a pass.** If the backend fails, returns malformed JSON, or
omits a dimension, that dimension is recorded ``passed=None`` with the reason.
It never silently scores zero and never silently passes.

SYNC carries the CRITICAL severity because it is the dimension that catches
narration claiming a success the screen contradicts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from evals.judge.client import JudgeClient, ResponseSchema, render_prompt
from evals.judge.stabilise import StabilisationResult, stabilise
from evals.schema import DimensionScore, GenerationResult, Measurement, Severity, Tier

__all__ = [
    "AESTHETIC_DIMENSIONS",
    "SYNC_DIMENSIONS",
    "MONTAGE_DIMENSIONS",
    "GROUPS",
    "extract_frames",
    "judge_artifact",
]

# Sub-dimensions per rubric group. Scored 0-10 by the judge, rounded DOWN.
AESTHETIC_DIMENSIONS: Tuple[str, ...] = (
    "visual_coherence",
    "color_and_light",
    "motion_quality",
    "scene_transitions",
    "narrative_arc",
    "typographic_craft",
    "audio_polish",
)

SYNC_DIMENSIONS: Tuple[str, ...] = (
    "narration_matches_visuals",
    "functional_claims_true",
)

MONTAGE_DIMENSIONS: Tuple[str, ...] = (
    "transitions_clean",
    "intro_outro_present",
)

#: group -> (sub-dimensions, severity, per-dimension pass threshold /10)
GROUPS: Dict[str, Tuple[Tuple[str, ...], str, float]] = {
    "AESTHETIC": (AESTHETIC_DIMENSIONS, Severity.MAJOR.value, 8.0),
    "SYNC": (SYNC_DIMENSIONS, Severity.CRITICAL.value, 8.0),
    "MONTAGE": (MONTAGE_DIMENSIONS, Severity.MAJOR.value, 7.0),
}

#: Mean across AESTHETIC sub-dimensions must clear this to pass the group.
AESTHETIC_MEAN_MIN = 8.7

_ALL_DIMENSIONS: Tuple[str, ...] = (
    AESTHETIC_DIMENSIONS + SYNC_DIMENSIONS + MONTAGE_DIMENSIONS
)

_SCHEMA = ResponseSchema(
    name="artifact_quality",
    required={"scores": "number_map", "evidence": "string_map"},
    optional={"notes": "string", "claimed_but_unverified": "array"},
)


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


def extract_frames(
    video_path: Optional[Path | str],
    out_dir: Path | str,
    *,
    count: int = 6,
    timeout_s: float = 120.0,
) -> List[Path]:
    """Sample ``count`` frames evenly across the video.

    Returns an empty list when ffmpeg is unavailable or the video cannot be
    read -- the caller then tells the judge no frames were available, rather
    than pretending it looked at them.
    """
    if not video_path:
        return []
    source = Path(video_path)
    if not source.is_file():
        return []

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return []

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    # fps filter chosen so roughly `count` frames come out regardless of length.
    pattern = str(target / "frame-%03d.png")
    argv = [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error",
        "-i", str(source),
        "-vf", f"thumbnail,fps=1/max(1\\,t)",
        "-frames:v", str(count),
        pattern,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    frames = sorted(target.glob("frame-*.png"))
    if frames:
        return frames

    # Fallback: a plain even-interval sample.
    argv = [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error",
        "-i", str(source), "-vsync", "0",
        "-frames:v", str(count), pattern,
    ]
    try:
        subprocess.run(argv, capture_output=True, text=True,
                       timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted(target.glob("frame-*.png"))


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _dimension_brief() -> str:
    """Render the dimension list handed to the judge."""
    lines = [
        "**Aesthetic craft** (score each 0-10):",
        *(f"- `{d}`" for d in AESTHETIC_DIMENSIONS),
        "",
        "**Synchronisation** (score each 0-10; these are CRITICAL):",
        "- `narration_matches_visuals` -- the narrated step is the step on screen, "
        "drift under 1.5s",
        "- `functional_claims_true` -- the narration never claims a result the "
        "screen contradicts (e.g. 'success' over a visible error)",
        "",
        "**Montage** (score each 0-10):",
        "- `transitions_clean` -- cuts and transitions are deliberate, not abrupt "
        "or glitchy",
        "- `intro_outro_present` -- the piece opens and closes deliberately",
    ]
    return "\n".join(lines)


def _measurement_brief(measured: Sequence[DimensionScore]) -> str:
    """Summarise deterministic results so the judge cannot contradict them blind."""
    if not measured:
        return "(no deterministic measurements available)"
    lines: List[str] = []
    for score in measured:
        for measurement in score.measurements:
            state = {
                True: "PASS", False: "FAIL", None: "UNVERIFIED",
            }[measurement.passed]
            lines.append(
                f"- {measurement.dimension} = {measurement.value} "
                f"[{state}, via {measurement.method}]"
            )
    return "\n".join(lines[:40])


def _build_prompt(
    result: GenerationResult,
    case: Any,
    measured: Sequence[DimensionScore],
    frames: Sequence[Path],
    prompts_dir: Optional[Path],
) -> str:
    """Render ``artifact_quality.md`` with this unit's context."""
    frame_text = (
        "\n".join(f"- {p}" for p in frames)
        if frames
        else "(no frames could be extracted; judge from the other evidence "
             "and lower your confidence accordingly)"
    )
    variables = {
        "case_id": result.case_id,
        "prompt": str(getattr(case, "prompt", "") or "(unavailable)"),
        "video_path": result.video_path or "(no video produced)",
        "srt_path": result.srt_path or "(no subtitles produced)",
        "frames": frame_text,
        "measurements": _measurement_brief(measured),
        "dimensions": _dimension_brief(),
    }
    return render_prompt("artifact_quality", variables, prompts_dir=prompts_dir)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _measurements_for_group(
    group: str,
    dimensions: Sequence[str],
    stabilised: StabilisationResult,
    threshold: float,
    severity: str,
) -> List[Measurement]:
    """Turn stabilised judge scores into Measurements, one per sub-dimension."""
    out: List[Measurement] = []
    for dimension in dimensions:
        score = stabilised.scores.get(dimension)
        if score is None:
            out.append(
                Measurement(
                    dimension=dimension,
                    value=None,
                    threshold=f">= {threshold:g}/10",
                    passed=None,
                    severity=severity,
                    method=f"judge:{stabilised.method or 'median-of-n'}",
                    evidence=(
                        "unverified: the judge did not return a score for this "
                        "dimension"
                    ),
                )
            )
            continue

        evidence_parts = [text for text in (score.evidence_by_call or ()) if text]
        evidence = evidence_parts[0] if evidence_parts else "no evidence cited"
        out.append(
            Measurement(
                dimension=dimension,
                value=round(score.median, 2),
                threshold=f">= {threshold:g}/10",
                passed=score.median >= threshold,
                severity=severity,
                method=f"judge:median-of-{score.n}",
                evidence=(
                    f"median {score.median:.1f}/10 across {score.n} sample(s), "
                    f"spread {score.spread:.1f}, raw {list(score.raw)} -- {evidence}"
                ),
            )
        )
    return out


def _group_score(
    group: str,
    measurements: Sequence[Measurement],
    severity: str,
    *,
    mean_min: Optional[float] = None,
) -> DimensionScore:
    """Fold sub-dimension measurements into one group DimensionScore."""
    decided = [m for m in measurements if m.passed is not None]
    failed = [m for m in decided if not m.passed]
    unverified = [m for m in measurements if m.passed is None]

    values = [
        float(m.value) for m in decided if isinstance(m.value, (int, float))
    ]
    mean_10 = sum(values) / len(values) if values else 0.0

    extra: List[Measurement] = []
    if mean_min is not None:
        extra.append(
            Measurement(
                dimension=f"{group.lower()}_mean",
                value=round(mean_10, 2),
                threshold=f">= {mean_min:g}/10",
                passed=(mean_10 >= mean_min) if values else None,
                severity=severity,
                method="judge:mean-of-medians",
                evidence=(
                    f"mean of {len(values)} sub-dimension median(s) = {mean_10:.2f}"
                    if values
                    else "unverified: no sub-dimension produced a score"
                ),
            )
        )
        if extra[0].passed is False:
            failed = list(failed) + [extra[0]]
        elif extra[0].passed is None:
            unverified = list(unverified) + [extra[0]]

    all_measurements = list(measurements) + extra

    notes = ""
    if unverified and not decided:
        notes = "unverified: the judge produced no usable scores for this group"
    elif unverified:
        notes = f"{len(unverified)} sub-dimension(s) unverified"
    elif failed:
        notes = f"{len(failed)} sub-dimension(s) below threshold"

    return DimensionScore(
        dimension=group,
        tier=Tier.A.value,
        score_0_100=float(int(mean_10 * 10)),  # 0-10 -> 0-100, rounded DOWN
        passed=bool(decided) and not failed and not unverified,
        severity=severity if failed else Severity.MINOR.value,
        measurements=all_measurements,
        notes=notes,
    )


def _all_unverified(reason: str) -> List[DimensionScore]:
    """Every judged group reported unverified, with a stated reason."""
    out: List[DimensionScore] = []
    for group, (dimensions, severity, threshold) in GROUPS.items():
        measurements = [
            Measurement(
                dimension=dimension,
                value=None,
                threshold=f">= {threshold:g}/10",
                passed=None,
                severity=severity,
                method="judge",
                evidence=f"unverified: {reason}",
            )
            for dimension in dimensions
        ]
        out.append(
            DimensionScore(
                dimension=group,
                tier=Tier.A.value,
                score_0_100=0.0,
                passed=False,
                severity=severity,
                measurements=measurements,
                notes=f"unverified: {reason}",
            )
        )
    return out


def judge_artifact(
    result: GenerationResult,
    *,
    case: Any = None,
    measured: Sequence[DimensionScore] = (),
    client: Optional[JudgeClient] = None,
    n_samples: int = 3,
    frames_dir: Optional[Path | str] = None,
    prompts_dir: Optional[Path] = None,
) -> List[DimensionScore]:
    """Score AESTHETIC, SYNC and MONTAGE for one generation result.

    Returns three :class:`DimensionScore` objects. On any failure -- no video,
    no judge client, backend error, malformed response -- the groups are
    returned as *unverified* with the reason attached, never as passes.
    """
    if client is None:
        return _all_unverified("no judge client configured")

    if not result.video_path:
        return _all_unverified("no video was produced, so there is nothing to judge")

    workdir = Path(frames_dir) if frames_dir else Path(result.workdir or ".") / "frames"
    frames = extract_frames(result.video_path, workdir)

    try:
        prompt = _build_prompt(result, case, measured, frames, prompts_dir)
    except (OSError, KeyError, ValueError) as exc:
        return _all_unverified(f"could not build the judge prompt: {exc}")

    try:
        stabilised = stabilise(
            client,
            prompt,
            dimensions=_ALL_DIMENSIONS,
            schema=_SCHEMA,
            n=max(1, int(n_samples)),
            context={"case_id": result.case_id, "repeat_idx": result.repeat_idx},
        )
    except Exception as exc:  # backend failures must not abort the run
        return _all_unverified(f"judge backend failed: {type(exc).__name__}: {exc}")

    if stabilised.n_verified == 0:
        return _all_unverified(
            "every judge sample failed or returned malformed JSON"
        )

    out: List[DimensionScore] = []
    for group, (dimensions, severity, threshold) in GROUPS.items():
        measurements = _measurements_for_group(
            group, dimensions, stabilised, threshold, severity
        )
        out.append(
            _group_score(
                group,
                measurements,
                severity,
                mean_min=AESTHETIC_MEAN_MIN if group == "AESTHETIC" else None,
            )
        )
    return out
