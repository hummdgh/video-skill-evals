"""Stage 3: assertions, the gate, aggregation and regression detection.

This module is where the rubric becomes arithmetic. Four responsibilities:

1. **Assertions** -- check each case's declared, machine-checkable criteria
   against what was actually measured.
2. **The gate** -- fold Tier A, Tier B and the assertions into one verdict,
   defaulting to FAIL and rounding down.
3. **Aggregation** -- collapse repeats of a case into a mean, a spread, a
   flake rate and a stability grade, because one sample is not a measurement.
4. **Regression** -- diff continuous per-dimension scores against a stored
   baseline, so decay is visible before it crosses the gate.

Two rules are load-bearing and appear throughout:

*Unverified is not a pass.* A ``DimensionScore`` whose measurements could not
be taken fails the gate. Silence is not evidence.

*Infra failures are quarantined.* A missing runtime or a timed-out API call is
not a statement about video quality, and is excluded from quality aggregates
rather than averaged into them.

Standard library only.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from evals.schema import (
    AssertionResult,
    CaseAggregate,
    CaseVerdict,
    DimensionScore,
    FailureClass,
    GenerationResult,
    Measurement,
    Regression,
    RunReport,
    Severity,
    Tier,
    Verdict,
)

__all__ = [
    "evaluate_assertions",
    "score_unit",
    "aggregate_case",
    "detect_regressions",
    "build_run_report",
    "REGRESSION_TOLERANCE",
]

#: A drop larger than this many points counts as a regression. Small movement
#: is judge noise; this threshold is what separates signal from jitter.
REGRESSION_TOLERANCE = 3.0


# ---------------------------------------------------------------------------
# 1. Assertions
# ---------------------------------------------------------------------------


def _measured(scores: Sequence[DimensionScore], dimension: str) -> Optional[Measurement]:
    """Find a single measurement by name across all dimension scores."""
    for score in scores:
        for measurement in score.measurements:
            if measurement.dimension == dimension:
                return measurement
    return None


def _measured_value(scores: Sequence[DimensionScore], dimension: str) -> Any:
    measurement = _measured(scores, dimension)
    return None if measurement is None else measurement.value


def evaluate_assertions(
    case: Any,
    tier_a: Sequence[DimensionScore],
    result: Optional[GenerationResult] = None,
) -> List[AssertionResult]:
    """Check a case's declared assertions against measured values.

    Every assertion is MUST by default: it was written down deliberately, so
    failing it fails the gate. An assertion that cannot be evaluated (because
    the underlying measurement was unavailable) fails rather than passes.
    """
    assertions = getattr(case, "assertions", None)
    out: List[AssertionResult] = []
    if assertions is None:
        return out

    # -- duration ----------------------------------------------------------
    window = getattr(assertions, "duration_s", None)
    if window:
        actual = _measured_value(tier_a, "duration_s")
        try:
            lo, hi = float(window[0]), float(window[1])
        except (TypeError, ValueError, IndexError):
            lo, hi = 0.0, 0.0
        if isinstance(actual, (int, float)):
            passed = lo <= float(actual) <= hi
            detail = f"measured {float(actual):.2f}s against [{lo:g}, {hi:g}]s"
        else:
            passed = False
            detail = "duration could not be measured"
        out.append(
            AssertionResult(
                name="duration_s",
                expected=[lo, hi],
                actual=actual,
                passed=passed,
                must=True,
                detail=detail,
            )
        )

    # -- aspect ratio ------------------------------------------------------
    aspect = getattr(assertions, "aspect_ratio", None)
    if aspect:
        resolution = _measured_value(tier_a, "resolution")
        passed = False
        detail = "resolution could not be measured"
        if isinstance(resolution, str) and "x" in resolution:
            try:
                width, height = (int(part) for part in resolution.split("x", 1))
                passed = _aspect_matches(width, height, str(aspect))
                detail = f"measured {resolution} against {aspect}"
            except (TypeError, ValueError):
                detail = f"could not parse resolution {resolution!r}"
        out.append(
            AssertionResult(
                name="aspect_ratio",
                expected=aspect,
                actual=resolution,
                passed=passed,
                must=True,
                detail=detail,
            )
        )

    # -- required concepts -------------------------------------------------
    concepts = list(getattr(assertions, "required_concepts", None) or [])
    if concepts:
        script = _read_script(result)
        if script is None:
            passed, missing = False, concepts
            detail = "narration script unavailable"
        else:
            lowered = script.lower()
            missing = [c for c in concepts if str(c).lower() not in lowered]
            passed = not missing
            detail = (
                f"all {len(concepts)} concept(s) present"
                if passed
                else f"missing: {', '.join(str(m) for m in missing)}"
            )
        out.append(
            AssertionResult(
                name="required_concepts",
                expected=concepts,
                actual=[c for c in concepts if c not in missing] if script else [],
                passed=passed,
                must=True,
                detail=detail,
            )
        )

    # -- minimum scenes ----------------------------------------------------
    min_scenes = getattr(assertions, "min_scenes", None)
    if min_scenes:
        # No deterministic scene detector yet; report honestly rather than
        # inventing a number or quietly passing.
        out.append(
            AssertionResult(
                name="min_scenes",
                expected=min_scenes,
                actual=None,
                passed=False,
                must=False,
                detail="unverified: scene counting is not implemented",
            )
        )

    return out


def _aspect_matches(width: int, height: int, expected: str, tolerance: float = 0.02) -> bool:
    """Compare a measured resolution against a ``"16:9"``-style ratio."""
    if height <= 0 or ":" not in expected:
        return False
    try:
        num, den = (float(part) for part in expected.split(":", 1))
    except (TypeError, ValueError):
        return False
    if den <= 0:
        return False
    return abs((width / height) - (num / den)) <= tolerance * (num / den)


def _read_script(result: Optional[GenerationResult]) -> Optional[str]:
    if result is None or not result.script_path:
        return None
    from pathlib import Path

    try:
        return Path(result.script_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 2. The gate
# ---------------------------------------------------------------------------


def _classify(
    result: Optional[GenerationResult],
    tier_a: Sequence[DimensionScore],
) -> Optional[str]:
    """Attribute a failure to infra, agent or quality.

    Order matters. Infra is checked first because an environment failure means
    the agent never had a fair attempt, and folding that into quality metrics
    makes them a measure of your API provider's uptime.
    """
    if result is None:
        return FailureClass.INFRA.value

    error = (result.error or "").lower()
    infra_markers = (
        "timed out", "timeout", "not found on path", "executable not found",
        "could not start", "permission denied", "adapter error",
        "filesystem error", "connection", "network", "unreachable",
    )
    if any(marker in error for marker in infra_markers):
        return FailureClass.INFRA.value
    if result.exit_code is None and result.error:
        return FailureClass.INFRA.value

    # Ran, but produced nothing usable.
    if not result.video_path:
        return FailureClass.AGENT.value
    if result.error:
        return FailureClass.AGENT.value

    # An artefact exists and was measured: any failure is about quality.
    if any(not score.passed for score in tier_a):
        return FailureClass.QUALITY.value
    return None


def _critical_failures(scores: Sequence[DimensionScore]) -> List[DimensionScore]:
    return [
        s for s in scores
        if not s.passed and s.severity == Severity.CRITICAL.value
    ]


def _tier_a_score(scores: Sequence[DimensionScore]) -> float:
    """Mean of Tier A dimension scores, rounded DOWN."""
    values = [s.score_0_100 for s in scores]
    if not values:
        return 0.0
    return float(int(statistics.fmean(values)))


def score_unit(
    case: Any,
    result: Optional[GenerationResult],
    tier_a: Sequence[DimensionScore],
    tier_b: Sequence[DimensionScore] = (),
    *,
    tier_b_blocking: bool = False,
) -> CaseVerdict:
    """Fold everything about one unit into a verdict.

    Gate::

        PASS  iff  every Tier-A CRITICAL dimension passes
              and  every MUST assertion passes
              and  (Tier B passes, when tier_b_blocking)

    A Tier-A CRITICAL failure is always FAIL. Failures confined to MAJOR or
    MINOR yield PASS_WITH_ADVISORY. Default disposition is FAIL, and
    ``gate_reasons`` is always populated when the verdict is not PASS.
    """
    tier_a = list(tier_a)
    tier_b = list(tier_b)
    assertions = evaluate_assertions(case, tier_a, result)
    reasons: List[str] = []

    # Did the unit even produce something?
    if result is None:
        reasons.append("no generation result for this unit")
    elif result.error:
        reasons.append(f"generation failed: {result.error}")

    critical = _critical_failures(tier_a)
    for score in critical:
        reasons.append(
            f"CRITICAL {score.dimension} failed: {score.notes or 'see measurements'}"
        )

    major = [
        s for s in tier_a
        if not s.passed and s.severity == Severity.MAJOR.value
    ]
    for score in major:
        reasons.append(
            f"MAJOR {score.dimension} failed: {score.notes or 'see measurements'}"
        )

    failed_musts = [a for a in assertions if a.must and not a.passed]
    for assertion in failed_musts:
        reasons.append(f"assertion {assertion.name} failed: {assertion.detail}")

    blocking_tier_b: List[DimensionScore] = []
    if tier_b_blocking:
        blocking_tier_b = [s for s in tier_b if not s.passed]
        for score in blocking_tier_b:
            reasons.append(
                f"Tier B {score.dimension} failed: {score.notes or 'over budget'}"
            )

    generation_failed = result is None or bool(result.error)
    if generation_failed or critical or failed_musts or blocking_tier_b:
        overall = Verdict.FAIL.value
    elif major:
        overall = Verdict.PASS_WITH_ADVISORY.value
    else:
        overall = Verdict.PASS.value

    failure_class = None
    if overall == Verdict.FAIL.value:
        failure_class = _classify(result, tier_a)

    if overall != Verdict.PASS.value and not reasons:
        # Defensive: never report a non-PASS without saying why.
        reasons.append("verdict is not PASS but no specific reason was recorded")

    return CaseVerdict(
        case_id=str(getattr(case, "id", getattr(result, "case_id", "") if result else "")),
        repeat_idx=int(getattr(result, "repeat_idx", 0) if result else 0),
        overall=overall,
        tier_a=tier_a,
        tier_b=tier_b,
        failure_class=failure_class,
        assertion_results=assertions,
        gate_reasons=reasons,
    )


# ---------------------------------------------------------------------------
# 3. Aggregation across repeats
# ---------------------------------------------------------------------------


def _stability_grade(flake_rate: float, spread: float) -> str:
    """Grade consistency across repeats.

    A case that passes twice and fails once is a materially different signal
    from one that passes three times, and the report should say so.
    """
    if flake_rate == 0.0 and spread <= 5.0:
        return "stable"
    if flake_rate == 0.0:
        return "consistent-verdict-variable-score"
    if flake_rate <= 0.34:
        return "flaky"
    return "unstable"


def aggregate_case(case_id: str, verdicts: Sequence[CaseVerdict]) -> CaseAggregate:
    """Collapse repeats of one case into mean, spread, flake rate and grade.

    Units that failed for *infra* reasons are excluded from the quality
    statistics but still counted in ``n_repeats``, so the sample size stays
    honest while API flakes do not drag the score down.
    """
    verdicts = list(verdicts)
    quality_verdicts = [
        v for v in verdicts if v.failure_class != FailureClass.INFRA.value
    ]

    scores = [_tier_a_score(v.tier_a) for v in quality_verdicts]
    mean = float(int(statistics.fmean(scores))) if scores else 0.0
    spread = float(max(scores) - min(scores)) if len(scores) > 1 else 0.0

    if quality_verdicts:
        outcomes = [v.overall for v in quality_verdicts]
        modal = max(set(outcomes), key=outcomes.count)
        disagreeing = sum(1 for o in outcomes if o != modal)
        flake_rate = disagreeing / len(outcomes)
    else:
        flake_rate = 0.0

    return CaseAggregate(
        case_id=case_id,
        n_repeats=len(verdicts),
        verdicts=verdicts,
        score_mean=mean,
        score_spread=spread,
        flake_rate=round(flake_rate, 3),
        stability_grade=_stability_grade(flake_rate, spread),
    )


# ---------------------------------------------------------------------------
# 4. Regression detection
# ---------------------------------------------------------------------------


def _dimension_means(aggregates: Sequence[CaseAggregate]) -> Dict[str, float]:
    """Mean score per dimension across every non-infra verdict in a run."""
    buckets: Dict[str, List[float]] = {}
    for aggregate in aggregates:
        for verdict in aggregate.verdicts:
            if verdict.failure_class == FailureClass.INFRA.value:
                continue
            for score in list(verdict.tier_a) + list(verdict.tier_b):
                buckets.setdefault(score.dimension, []).append(score.score_0_100)
    return {dim: statistics.fmean(vals) for dim, vals in buckets.items() if vals}


def detect_regressions(
    current: Sequence[CaseAggregate],
    baseline: Optional[RunReport],
    tolerance: float = REGRESSION_TOLERANCE,
) -> List[Regression]:
    """Compare per-dimension means against a baseline run.

    Reports every dimension present in both runs, flagging those that dropped
    by more than ``tolerance``. Improvements are reported too -- a sudden jump
    is often as informative as a fall.
    """
    if baseline is None:
        return []

    now = _dimension_means(current)
    before = _dimension_means(baseline.aggregates)

    out: List[Regression] = []
    for dimension in sorted(set(now) & set(before)):
        current_score = round(now[dimension], 2)
        baseline_score = round(before[dimension], 2)
        delta = round(current_score - baseline_score, 2)
        out.append(
            Regression(
                dimension=dimension,
                baseline_score=baseline_score,
                current_score=current_score,
                delta=delta,
                is_regression=delta < -abs(tolerance),
            )
        )
    return out


# ---------------------------------------------------------------------------
# 5. Assembly
# ---------------------------------------------------------------------------


def build_run_report(
    run_id: str,
    aggregates: Sequence[CaseAggregate],
    *,
    suite_name: str = "",
    adapter: str = "unknown",
    live: bool = False,
    baseline: Optional[RunReport] = None,
    tolerance: float = REGRESSION_TOLERANCE,
) -> RunReport:
    """Assemble the final report, including headline totals."""
    aggregates = list(aggregates)
    verdicts = [v for a in aggregates for v in a.verdicts]

    counts = {
        Verdict.PASS.value: 0,
        Verdict.PASS_WITH_ADVISORY.value: 0,
        Verdict.FAIL.value: 0,
    }
    failure_classes: Dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict.overall] = counts.get(verdict.overall, 0) + 1
        if verdict.failure_class:
            failure_classes[verdict.failure_class] = (
                failure_classes.get(verdict.failure_class, 0) + 1
            )

    quality_units = [
        v for v in verdicts if v.failure_class != FailureClass.INFRA.value
    ]
    gate_passed = bool(quality_units) and all(
        v.overall != Verdict.FAIL.value for v in quality_units
    )

    totals: Dict[str, Any] = {
        "units": len(verdicts),
        "cases": len(aggregates),
        "verdicts": counts,
        "failure_classes": failure_classes,
        "gate_passed": gate_passed,
        "mean_score": (
            float(int(statistics.fmean([a.score_mean for a in aggregates])))
            if aggregates
            else 0.0
        ),
        "flaky_cases": [a.case_id for a in aggregates if a.flake_rate > 0.0],
        "dimension_means": {
            dim: round(value, 2) for dim, value in _dimension_means(aggregates).items()
        },
    }

    return RunReport(
        run_id=run_id,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        suite_name=suite_name,
        adapter=adapter,
        live=live,
        aggregates=aggregates,
        totals=totals,
        baseline_run_id=baseline.run_id if baseline else None,
        regressions=detect_regressions(aggregates, baseline, tolerance),
    )
