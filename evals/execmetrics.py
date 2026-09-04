"""Tier B -- skill execution metrics.

Tier A grades the artifact. Tier B grades **how the agent got there**, which is
the blind spot in artifact-only judging: a great video after 40 flailing turns
and $12 is a worse outcome than the same video in 8 turns for $2. Artifact
grading scores those two runs identically. This module does not.

Six dimensions, all ``tier=Tier.B``:

===========================  ===========================================
``turns``                    Conversation turns the agent consumed.
``retries``                  Retries it needed.
``wall_time_s``              End-to-end latency.
``cost_usd``                 Real API spend from the ledger.
``skill_adherence``          Did it follow ``SKILL.md``? (judge, 0-10)
``failure_mode_tripped``     Did it hit a documented failure mode? (judge)
===========================  ===========================================

**Tier B is non-blocking by default** (``tier_b_blocking: false``) but always
reported. A team that cannot yet afford to fail a run for costing too much can
still watch the number move. Flip the flag when the budget is real.

Cost defaults, and where they come from
---------------------------------------
``jaime-pricing.md`` documents the real per-call economics rather than a rate
table (rates live in ``assets/config/models.mjs`` precisely so a prose copy
cannot drift). Two load-bearing figures from it:

* building and validating the whole skill end to end -- five providers, two
  full narrated renders, plus a content-filtered attempt that billed nothing --
  came to *a little over two dollars*, of which **video was roughly 85%**;
* a typical short piece -- a handful of stills, one voiceover, one music bed
  and a few seconds of video -- lands in the *low single-digit dollars*, and
  composition and verification are free.

So a single simple case should cost around a dollar, and the per-band budgets
below are set just above that, widening for bands that legitimately render
more video. They are defaults, not truths: every one is overridable in config,
and the honest way to set them is to measure a few real runs and tighten.

Standard library only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .schema import (
    BANDS,
    DimensionScore,
    GenerationResult,
    Measurement,
    Severity,
    Tier,
)

__all__ = [
    "DEFAULT_EXECMETRICS_CONFIG",
    "TIER_B_DIMENSIONS",
    "ExecutionReview",
    "band_budget",
    "tier_b_blocking",
    "compute_exec_metrics",
    "execution_review_from_payload",
    "review_execution",
]


#: Dimension keys emitted by :func:`compute_exec_metrics`, in report order.
TIER_B_DIMENSIONS: Tuple[str, ...] = (
    "turns",
    "retries",
    "wall_time_s",
    "cost_usd",
    "skill_adherence",
    "failure_mode_tripped",
)

#: Defaults for the ``execmetrics`` config block. Every budget is per band.
DEFAULT_EXECMETRICS_CONFIG: Dict[str, Any] = {
    # Tier B reports always; it only gates when this is switched on.
    "tier_b_blocking": False,
    # Dollars. Anchored on "a typical short piece lands in the low
    # single-digit dollars" from jaime-pricing.md, widened per band.
    "cost_budget_usd": {
        "simple": 1.50,
        "medium": 3.00,
        "hard": 6.00,
        "adversarial": 8.00,
    },
    # Seconds. Veo renders dominate; these are generous by design because a
    # latency budget that fires on every run teaches people to ignore it.
    "wall_time_budget_s": {
        "simple": 600.0,
        "medium": 1200.0,
        "hard": 2400.0,
        "adversarial": 3000.0,
    },
    "max_turns": {
        "simple": 8,
        "medium": 14,
        "hard": 22,
        "adversarial": 30,
    },
    "max_retries": {
        "simple": 1,
        "medium": 2,
        "hard": 3,
        "adversarial": 4,
    },
    # Judge-scored, 0-10.
    "skill_adherence_min": 7.0,
    # Band used when a case names one we have no budget for.
    "default_band": "medium",
}

#: Severity per Tier-B dimension. Cost and latency are first-class scored
#: dimensions, so they carry MAJOR; turn and retry counts are efficiency
#: signals and carry MINOR.
_SEVERITY: Dict[str, str] = {
    "turns": Severity.MINOR.value,
    "retries": Severity.MINOR.value,
    "wall_time_s": Severity.MAJOR.value,
    "cost_usd": Severity.MAJOR.value,
    "skill_adherence": Severity.MAJOR.value,
    "failure_mode_tripped": Severity.MAJOR.value,
}

#: Provenance recorded on deterministic Tier-B measurements.
_METHOD: str = "execmetrics"

#: Marker for a Tier-B dimension we could not measure. Mirrors the judge
#: layer's convention so one predicate finds every unverified record.
_UNVERIFIED: str = "unverified:"


def _round_down(value: float, places: int = 2) -> float:
    """Truncate ``value`` towards zero at ``places`` decimals.

    The rubric rounds DOWN, so a score is floored rather than rounded to
    nearest -- 89.999 must not become 90.0 and clear a threshold of 90.

    Args:
        value: Number to truncate.
        places: Decimal places to keep.

    Returns:
        The truncated value.
    """
    factor = 10 ** places
    if value >= 0:
        return math.floor(value * factor) / factor
    return math.ceil(value * factor) / factor


def _budget_score(value: float, budget: float) -> float:
    """Score a lower-is-better measurement against its budget, 0-100.

    At or under budget scores 100. The score then falls linearly to 0 at twice
    the budget, so overspending degrades gracefully instead of cliff-edging --
    a run 5% over budget should not look identical to one 300% over.

    Args:
        value: Measured value; negatives are treated as zero.
        budget: Budget for this band. Zero or negative means "must not exceed
            zero", scored as a hard 100/0.

    Returns:
        A score in ``[0, 100]``, rounded down.
    """
    measured = max(0.0, float(value))
    limit = float(budget)
    if limit <= 0:
        return 100.0 if measured <= 0 else 0.0
    if measured <= limit:
        return 100.0
    return _round_down(max(0.0, 100.0 * (2.0 - measured / limit)))


def band_budget(
    config: Mapping[str, Any],
    key: str,
    band: str,
) -> Any:
    """Look up one per-band budget, falling back to the default band.

    Args:
        config: A merged ``execmetrics`` config block.
        key: Budget key, e.g. ``"cost_budget_usd"``.
        band: Case band, e.g. ``"hard"``.

    Returns:
        The budget for ``band``, or for ``config["default_band"]`` when the
        band is unknown.

    Raises:
        KeyError: When ``key`` names no budget at all, which is a config bug
            rather than a runtime condition and must not be swallowed.
    """
    if key not in config:
        raise KeyError(f"execmetrics config has no budget {key!r}")
    budgets = config[key]
    if not isinstance(budgets, Mapping):
        return budgets
    if band in budgets:
        return budgets[band]
    fallback = config.get("default_band", "medium")
    if fallback in budgets:
        return budgets[fallback]
    raise KeyError(
        f"execmetrics budget {key!r} covers neither band {band!r} nor "
        f"fallback {fallback!r}"
    )


def tier_b_blocking(config: Optional[Mapping[str, Any]] = None) -> bool:
    """Return whether Tier B is allowed to block the gate.

    Args:
        config: An ``execmetrics`` config block; missing keys use defaults.

    Returns:
        ``True`` only when the config explicitly opts in.
    """
    merged = _merge(config)
    return bool(merged["tier_b_blocking"])


def _merge(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Merge a partial config over :data:`DEFAULT_EXECMETRICS_CONFIG`.

    Nested budget maps are merged per band, so overriding one band does not
    silently delete the others.

    Args:
        config: Partial config block, or ``None``.

    Returns:
        A fully populated config dict.
    """
    merged: Dict[str, Any] = {}
    for key, value in DEFAULT_EXECMETRICS_CONFIG.items():
        merged[key] = dict(value) if isinstance(value, dict) else value
    for key, value in dict(config or {}).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key].update(dict(value))
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Judge-sourced half of Tier B
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionReview:
    """The Tier-B judge's reading of an agent transcript.

    Produced by the ``execution_review.md`` prompt. Kept separate from
    :class:`~evals.schema.GenerationResult` because it is an opinion about a
    run, not a fact about it.

    Attributes:
        skill_adherence: 0-10; how closely the agent followed ``SKILL.md``.
        failure_mode_tripped: Whether it hit a documented failure mode.
        tripped_modes: Documented failure-mode names the judge identified.
        evidence: Claim key to literal transcript citation.
        notes: Free-form judge commentary.
        verified: ``False`` when the judge never returned valid JSON. An
            unverified review is recorded, never treated as a clean run.
    """

    skill_adherence: Optional[float] = None
    failure_mode_tripped: Optional[bool] = None
    tripped_modes: Tuple[str, ...] = ()
    evidence: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""
    verified: bool = True

    def citation(self) -> str:
        """Return a short literal citation for the evidence trail."""
        for value in self.evidence.values():
            if isinstance(value, str) and value:
                return value
        return self.notes or "no citation supplied by judge"


def execution_review_from_payload(
    payload: Optional[Mapping[str, Any]],
) -> ExecutionReview:
    """Build an :class:`ExecutionReview` from a validated judge payload.

    Args:
        payload: A payload already validated against
            :data:`~evals.judge.client.EXECUTION_REVIEW_SCHEMA`, or ``None``
            when the judge failed.

    Returns:
        A populated review, or an unverified one when ``payload`` is ``None``.
    """
    if not payload:
        return ExecutionReview(verified=False)

    adherence = payload.get("skill_adherence")
    score = (
        float(adherence)
        if isinstance(adherence, (int, float)) and not isinstance(adherence, bool)
        else None
    )
    tripped = payload.get("failure_mode_tripped")
    modes = payload.get("tripped_modes")
    evidence = payload.get("evidence")
    notes = payload.get("notes")

    return ExecutionReview(
        skill_adherence=score,
        failure_mode_tripped=tripped if isinstance(tripped, bool) else None,
        tripped_modes=tuple(str(m) for m in modes) if isinstance(modes, list) else (),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
        notes=str(notes) if isinstance(notes, str) else "",
        verified=score is not None and isinstance(tripped, bool),
    )


def review_execution(
    client: Any,
    result: GenerationResult,
    *,
    transcript: str = "",
    prompt_template: Optional[str] = None,
) -> ExecutionReview:
    """Run the Tier-B execution judge over one generation result.

    Imports the judge layer lazily so :mod:`evals.execmetrics` stays usable --
    and importable -- with no judge configured at all.

    Args:
        client: A :class:`~evals.judge.client.JudgeClient`.
        result: The run to review.
        transcript: Agent transcript text, or a path to it.
        prompt_template: Override template text; defaults to the packaged
            ``execution_review.md``.

    Returns:
        An :class:`ExecutionReview`. Unverified when the judge never returned
        schema-valid JSON -- this function does not raise for a bad judge.
    """
    from .judge.client import EXECUTION_REVIEW_SCHEMA, render_prompt

    variables = {
        "case_id": result.case_id,
        "adapter": result.adapter,
        "turns": result.turns,
        "retries": result.retries,
        "wall_time_s": result.wall_time_s,
        "cost_usd": result.cost_usd,
        "exit_code": result.exit_code,
        "transcript": transcript or (result.transcript_path or "(none)"),
    }
    if prompt_template is None:
        prompt = render_prompt("execution_review", variables)
    else:
        prompt = prompt_template
        for key, value in variables.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))

    outcome = client.judge(
        prompt,
        EXECUTION_REVIEW_SCHEMA,
        {"case_id": result.case_id, "repeat_idx": result.repeat_idx},
    )
    review = execution_review_from_payload(outcome.payload if outcome.ok else None)
    if outcome.ok:
        return review
    return ExecutionReview(verified=False, notes=outcome.evidence())


# ---------------------------------------------------------------------------
# Dimension construction
# ---------------------------------------------------------------------------


def _measured_dimension(
    dimension: str,
    value: Optional[float],
    budget: float,
    *,
    unit: str,
) -> DimensionScore:
    """Build one lower-is-better Tier-B dimension from a measured value.

    Args:
        dimension: Dimension key.
        value: Measured value, or ``None`` when the adapter did not report it.
        budget: Per-band budget for this dimension.
        unit: Unit shown in the evidence string, e.g. ``"USD"``.

    Returns:
        A Tier-B :class:`~evals.schema.DimensionScore`. An unreported value
        scores 0 and fails, under the default-FAIL disposition -- an adapter
        that hides its cost does not thereby pass the cost budget.
    """
    severity = _SEVERITY[dimension]

    if value is None:
        evidence = (
            f"{_UNVERIFIED} adapter reported no {dimension}; "
            f"cannot check against budget {budget:g} {unit}"
        )
        measurement = Measurement(
            dimension=dimension,
            value=None,
            threshold=budget,
            passed=None,
            severity=severity,
            method=_METHOD,
            evidence=evidence,
        )
        return DimensionScore(
            dimension=dimension,
            tier=Tier.B.value,
            score_0_100=0.0,
            passed=False,
            severity=severity,
            measurements=[measurement],
            notes=evidence,
        )

    numeric = float(value)
    within = numeric <= float(budget)
    score = _budget_score(numeric, budget)
    evidence = (
        f"measured {numeric:g} {unit} against budget {float(budget):g} {unit} "
        f"({'within' if within else 'over'} budget; score {score:g}/100)"
    )
    measurement = Measurement(
        dimension=dimension,
        value=numeric,
        threshold=float(budget),
        passed=within,
        severity=severity,
        method=_METHOD,
        evidence=evidence,
    )
    return DimensionScore(
        dimension=dimension,
        tier=Tier.B.value,
        score_0_100=score,
        passed=within,
        severity=severity,
        measurements=[measurement],
        notes=evidence,
    )


def _adherence_dimension(
    review: Optional[ExecutionReview],
    minimum: float,
) -> DimensionScore:
    """Build the ``skill_adherence`` dimension from the Tier-B judge.

    Args:
        review: The judge's review, or ``None`` when none was run.
        minimum: Minimum acceptable adherence on the judge's 0-10 scale.

    Returns:
        A Tier-B :class:`~evals.schema.DimensionScore`.
    """
    severity = _SEVERITY["skill_adherence"]

    if review is None or not review.verified or review.skill_adherence is None:
        detail = (
            review.notes
            if review is not None and review.notes
            else "no execution review supplied"
        )
        evidence = f"{_UNVERIFIED} skill adherence unknown ({detail})"
        measurement = Measurement(
            dimension="skill_adherence",
            value=None,
            threshold=minimum,
            passed=None,
            severity=severity,
            method="judge:execution_review",
            evidence=evidence,
        )
        return DimensionScore(
            dimension="skill_adherence",
            tier=Tier.B.value,
            score_0_100=0.0,
            passed=False,
            severity=severity,
            measurements=[measurement],
            notes=evidence,
        )

    value = float(review.skill_adherence)
    passed = value >= float(minimum)
    evidence = (
        f"judge scored skill adherence {value:g}/10 "
        f"(minimum {float(minimum):g}); cited: {review.citation()}"
    )
    measurement = Measurement(
        dimension="skill_adherence",
        value=value,
        threshold=float(minimum),
        passed=passed,
        severity=severity,
        method="judge:execution_review",
        evidence=evidence,
    )
    return DimensionScore(
        dimension="skill_adherence",
        tier=Tier.B.value,
        score_0_100=_round_down(value * 10.0),
        passed=passed,
        severity=severity,
        measurements=[measurement],
        notes=evidence,
    )


def _failure_mode_dimension(review: Optional[ExecutionReview]) -> DimensionScore:
    """Build the ``failure_mode_tripped`` dimension from the Tier-B judge.

    Args:
        review: The judge's review, or ``None`` when none was run.

    Returns:
        A Tier-B :class:`~evals.schema.DimensionScore`. Tripping a documented
        failure mode scores 0; not tripping one scores 100.
    """
    severity = _SEVERITY["failure_mode_tripped"]

    if review is None or not review.verified or review.failure_mode_tripped is None:
        detail = (
            review.notes
            if review is not None and review.notes
            else "no execution review supplied"
        )
        evidence = f"{_UNVERIFIED} failure-mode status unknown ({detail})"
        measurement = Measurement(
            dimension="failure_mode_tripped",
            value=None,
            threshold=False,
            passed=None,
            severity=severity,
            method="judge:execution_review",
            evidence=evidence,
        )
        return DimensionScore(
            dimension="failure_mode_tripped",
            tier=Tier.B.value,
            score_0_100=0.0,
            passed=False,
            severity=severity,
            measurements=[measurement],
            notes=evidence,
        )

    tripped = bool(review.failure_mode_tripped)
    modes = ", ".join(review.tripped_modes) if review.tripped_modes else "unnamed"
    evidence = (
        f"documented failure mode tripped ({modes}); cited: {review.citation()}"
        if tripped
        else "no documented failure mode tripped"
    )
    measurement = Measurement(
        dimension="failure_mode_tripped",
        value=tripped,
        threshold=False,
        passed=not tripped,
        severity=severity,
        method="judge:execution_review",
        evidence=evidence,
    )
    return DimensionScore(
        dimension="failure_mode_tripped",
        tier=Tier.B.value,
        score_0_100=0.0 if tripped else 100.0,
        passed=not tripped,
        severity=severity,
        measurements=[measurement],
        notes=evidence,
    )


def compute_exec_metrics(
    result: GenerationResult,
    *,
    band: str = "medium",
    config: Optional[Mapping[str, Any]] = None,
    review: Optional[ExecutionReview] = None,
) -> List[DimensionScore]:
    """Compute every Tier-B dimension for one generation result.

    Always returns all six dimensions in :data:`TIER_B_DIMENSIONS` order, even
    when the underlying value is missing. A dimension that quietly disappears
    is a dimension that silently passes, which the rubric forbids: anything
    unmeasured is recorded ``unverified``, scored 0 and marked failed.

    Whether any of this can block the gate is decided by
    :func:`tier_b_blocking`, not here. Tier B always reports.

    Args:
        result: The generation result to measure.
        band: Case band, selecting which per-band budgets apply.
        config: Partial ``execmetrics`` config; missing keys use defaults.
        review: Tier-B judge review supplying ``skill_adherence`` and
            ``failure_mode_tripped``. ``None`` records both as unverified.

    Returns:
        Six Tier-B :class:`~evals.schema.DimensionScore` objects.
    """
    merged = _merge(config)
    resolved_band = band if band in BANDS else str(merged["default_band"])

    turns_budget = band_budget(merged, "max_turns", resolved_band)
    retries_budget = band_budget(merged, "max_retries", resolved_band)
    time_budget = band_budget(merged, "wall_time_budget_s", resolved_band)
    cost_budget = band_budget(merged, "cost_budget_usd", resolved_band)

    return [
        _measured_dimension("turns", result.turns, turns_budget, unit="turns"),
        _measured_dimension(
            "retries", float(result.retries), retries_budget, unit="retries"
        ),
        _measured_dimension(
            "wall_time_s", result.wall_time_s, time_budget, unit="s"
        ),
        _measured_dimension("cost_usd", result.cost_usd, cost_budget, unit="USD"),
        _adherence_dimension(review, float(merged["skill_adherence_min"])),
        _failure_mode_dimension(review),
    ]
