"""Stage 2: the deterministic measurement battery.

Runs every objective check that can be settled without asking a language
model, and returns one :class:`DimensionScore` per group.

The design rule throughout this package: **a measurement that could not be
taken is not a pass.** Each module reports ``passed=None`` (unverified) when a
tool is missing or an artefact is unreadable, and the scorer treats unverified
as failing the gate. A battery that quietly skips what it cannot measure
produces a green dashboard that means nothing.

Caption *geometry* (pixel analysis of burned-in subtitles) is not implemented
here yet; it is reported as an explicit unverified dimension rather than
silently omitted, so its absence is visible in every report.

Standard library only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from evals.measure.audio import measure_audio
from evals.measure.caption_geometry import measure_caption_geometry
from evals.measure.secrets import measure_secrets
from evals.measure.srt_lint import measure_captions
from evals.measure.tech import measure_tech
from evals.schema import DimensionScore, GenerationResult, Measurement, Severity, Tier

__all__ = ["measure_all", "measure_result", "CAPTION_GEOMETRY_DIMENSION"]

CAPTION_GEOMETRY_DIMENSION = "CAPTION_GEOMETRY"


def _thresholds(config: Any) -> Any:
    """Return the thresholds block, tolerating a bare or absent config."""
    return getattr(config, "thresholds", None)


def _get(thresholds: Any, name: str, fallback: Any) -> Any:
    if thresholds is None:
        return fallback
    return getattr(thresholds, name, fallback)


def _expected_duration(case: Any) -> Optional[Tuple[float, float]]:
    """Read the declared duration window off a suite case, if present."""
    assertions = getattr(case, "assertions", None)
    if assertions is None:
        return None
    window = getattr(assertions, "duration_s", None)
    if not window:
        return None
    try:
        lo, hi = float(window[0]), float(window[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (lo, hi)


def _caption_geometry_placeholder(video_path: Optional[str]) -> DimensionScore:
    """Report caption geometry as explicitly unverified.

    Stated rather than skipped: the rubric lists four CRITICAL geometry
    thresholds, and a report that simply omitted them would imply they had
    been checked.
    """
    reason = (
        "not implemented: pixel caption-geometry analysis is pending. "
        "Centre-x, centre-y spread, cap-height and contrast are unmeasured."
    )
    return DimensionScore(
        dimension=CAPTION_GEOMETRY_DIMENSION,
        tier=Tier.A.value,
        score_0_100=0.0,
        passed=False,
        severity=Severity.CRITICAL.value,
        measurements=[
            Measurement(
                dimension="caption_geometry",
                value=None,
                threshold="centre-x [0.45,0.55]; centre-y spread <= 0.02; "
                          "cap-height [2%,5%]; contrast >= 4.5:1",
                passed=None,
                severity=Severity.CRITICAL.value,
                method="pixel",
                evidence=f"unverified: {reason}",
            )
        ],
        notes=reason,
    )


def measure_result(
    result: GenerationResult,
    *,
    case: Any = None,
    config: Any = None,
    include_geometry: bool = True,
) -> List[DimensionScore]:
    """Run the full deterministic battery over one generation result.

    Args:
        result: The unit to measure.
        case: The originating suite case, used for declared assertions such as
            the duration window. Optional.
        config: Resolved ``Config`` supplying thresholds. Optional; sensible
            defaults are used when absent.
        include_geometry: Emit the caption-geometry placeholder dimension.

    Returns:
        One :class:`DimensionScore` per group, in stable order.
    """
    thresholds = _thresholds(config)
    scores: List[DimensionScore] = []

    # -- TECH --------------------------------------------------------------
    scores.append(
        measure_tech(
            result.video_path,
            min_width=int(_get(thresholds, "min_width", 1920)),
            min_height=int(_get(thresholds, "min_height", 1080)),
            min_fps=float(_get(thresholds, "min_fps", 30.0)),
            require_audio=bool(_get(thresholds, "require_audio_stream", True)),
            expected_duration_s=_expected_duration(case) if case is not None else None,
        )
    )

    # -- AUDIO -------------------------------------------------------------
    scores.append(
        measure_audio(
            result.video_path,
            target_lufs=float(_get(thresholds, "loudness_target_lufs", -14.0)),
            tolerance_lufs=float(_get(thresholds, "loudness_tolerance_lufs", 1.5)),
            true_peak_max_dbtp=float(_get(thresholds, "true_peak_max_dbtp", -1.0)),
            intelligibility_floor_lufs=float(
                _get(thresholds, "vo_intelligibility_floor_lufs", -50.0)
            ),
        )
    )

    # -- CAPTION_CUES ------------------------------------------------------
    scores.append(
        measure_captions(
            result.srt_path,
            script_path=result.script_path,
            min_cue_duration_s=float(_get(thresholds, "caption_min_cue_duration_s", 0.6)),
        )
    )

    # -- CAPTION_GEOMETRY --------------------------------------------------
    if include_geometry:
        scores.append(
            measure_caption_geometry(
                result.video_path,
                center_x_range=tuple(
                    _get(thresholds, "caption_center_x", (0.45, 0.55))
                ),
                center_y_spread_max=float(
                    _get(thresholds, "caption_center_y_spread_max", 0.02)
                ),
                cap_height_range=tuple(
                    _get(thresholds, "caption_cap_height", (0.02, 0.05))
                ),
                contrast_min=float(_get(thresholds, "caption_contrast_min", 4.5)),
                band_min_y=float(_get(thresholds, "caption_bottom_band_min_y", 0.70)),
            )
        )

    # -- SECRETS -----------------------------------------------------------
    sources = {}
    if result.srt_path:
        sources["captions"] = result.srt_path
    if result.script_path:
        sources["narration"] = result.script_path
    if result.transcript_path:
        sources["transcript"] = result.transcript_path
    scores.append(measure_secrets(sources or None))

    return scores


def measure_all(
    results: Sequence[GenerationResult],
    *,
    suite: Any = None,
    config: Any = None,
    include_geometry: bool = True,
) -> dict:
    """Measure every result, keyed by ``"<case_id>#<repeat_idx>"``.

    Failures inside one unit never abort the batch: a unit that cannot be
    measured contributes a failing TECH dimension carrying the reason.
    """
    by_case = {}
    if suite is not None:
        for case in getattr(suite, "cases", []) or []:
            by_case[getattr(case, "id", None)] = case

    out = {}
    for result in results:
        key = f"{result.case_id}#{result.repeat_idx}"
        try:
            out[key] = measure_result(
                result,
                case=by_case.get(result.case_id),
                config=config,
                include_geometry=include_geometry,
            )
        except OSError as exc:
            out[key] = [
                DimensionScore(
                    dimension="TECH",
                    tier=Tier.A.value,
                    score_0_100=0.0,
                    passed=False,
                    severity=Severity.CRITICAL.value,
                    measurements=[
                        Measurement(
                            dimension="measurement_error",
                            value=None,
                            threshold="battery completes",
                            passed=False,
                            severity=Severity.CRITICAL.value,
                            method="harness",
                            evidence=f"measurement failed: {exc}",
                        )
                    ],
                    notes=f"measurement failed: {exc}",
                )
            ]
    return out
