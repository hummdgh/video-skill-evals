"""Median-of-n stabilisation for judge scores.

The prior art's hardest-won lesson: a single aesthetic judgement swings run to
run. The same artifact scored twice by the same model can differ by a point or
more, which is enough to flip a threshold and turn a stable pipeline into a
coin toss. So every holistic score is the **median of n independent calls**,
never one call.

What this module returns, per dimension:

* ``median`` -- the stabilised score, the only number anything gates on;
* ``spread`` -- ``max - min`` across the calls, the honesty metric; a wide
  spread means the judge does not actually know, and a narrow median hides it;
* ``raw`` -- every per-call score, kept for the evidence trail so a reviewer
  can see what the median was computed from.

Rounding doctrine: with an even ``n`` the median is the **lower** of the two
middle values, not their mean. The rubric rounds DOWN, and an averaged median
can invent a score no judge actually gave.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..schema import DimensionScore, Measurement, Severity, Tier
from .client import (
    ARTIFACT_QUALITY_SCHEMA,
    JudgeClient,
    JudgeOutcome,
    ResponseSchema,
    UNVERIFIED_PREFIX,
)

__all__ = [
    "DEFAULT_N",
    "JUDGE_SCORE_MAX",
    "median_of_n",
    "score_spread",
    "StabilisedScore",
    "StabilisationResult",
    "stabilise",
]

#: Default number of judge calls per dimension.
DEFAULT_N: int = 3

#: Judges score 0-10; :attr:`~evals.schema.DimensionScore.score_0_100` is 0-100.
JUDGE_SCORE_MAX: float = 10.0


def median_of_n(values: Sequence[float]) -> float:
    """Return the median, taking the lower middle value for an even count.

    Unlike :func:`statistics.median` this never averages the two central
    samples. Averaging fabricates a score no judge gave and rounds *up* half
    the time, both of which the rubric forbids.

    Args:
        values: One or more numeric scores.

    Returns:
        The median score as a float.

    Raises:
        ValueError: When ``values`` is empty -- there is no honest median of
            nothing, and returning 0.0 would read as a real judgement.
    """
    if not values:
        raise ValueError("median_of_n requires at least one value")
    ordered = sorted(float(v) for v in values)
    return ordered[(len(ordered) - 1) // 2]


def score_spread(values: Sequence[float]) -> float:
    """Return ``max - min`` across ``values``, or ``0.0`` for a single sample.

    Args:
        values: One or more numeric scores.

    Returns:
        The spread; ``0.0`` when fewer than two samples were collected.
    """
    if len(values) < 2:
        return 0.0
    floats = [float(v) for v in values]
    return round(max(floats) - min(floats), 6)


@dataclass(frozen=True)
class StabilisedScore:
    """One dimension's stabilised judgement.

    Attributes:
        dimension: Rubric dimension key, e.g. ``"visual_coherence"``.
        median: Median across :attr:`raw`, on the judge's own 0-10 scale.
        spread: ``max - min`` across :attr:`raw`.
        raw: Every per-call score, in call order, for the evidence trail.
        n: Number of verified calls that contributed a score.
        evidence_by_call: Per-call literal citation the judge gave, if any.
    """

    dimension: str
    median: float
    spread: float
    raw: Tuple[float, ...]
    n: int
    evidence_by_call: Tuple[str, ...] = ()

    @property
    def score_0_100(self) -> float:
        """Return :attr:`median` rescaled from 0-10 to the contract's 0-100."""
        return round(self.median * (100.0 / JUDGE_SCORE_MAX), 4)

    def evidence(self) -> str:
        """Return a one-line evidence trail naming every raw score."""
        raw = ", ".join(f"{v:g}" for v in self.raw)
        line = f"median-of-{self.n} = {self.median:g} (raw: {raw}; spread {self.spread:g})"
        citations = [c for c in self.evidence_by_call if c]
        if citations:
            line += f"; cited: {citations[0]}"
        return line

    def to_measurement(
        self,
        *,
        method: str,
        threshold: Optional[float] = None,
        severity: str = Severity.MAJOR.value,
    ) -> Measurement:
        """Render this stabilised score as a :class:`~evals.schema.Measurement`.

        Args:
            method: Provenance string, e.g. ``"judge:mock"``.
            threshold: Minimum acceptable median on the 0-10 scale. ``None``
                records the score as informational (``passed=None``).
            severity: Severity to attach when the threshold is missed.

        Returns:
            A measurement carrying the median, its threshold and its evidence.
        """
        passed: Optional[bool] = None
        if threshold is not None:
            passed = self.median >= float(threshold)
        return Measurement(
            dimension=self.dimension,
            value=self.median,
            threshold=threshold,
            passed=passed,
            severity=severity,
            method=method,
            evidence=self.evidence(),
        )


@dataclass(frozen=True)
class StabilisationResult:
    """The outcome of stabilising every dimension over ``n`` judge calls.

    Attributes:
        dimensions: Dimensions that were requested, in order.
        scores: Stabilised score per dimension. A dimension the judge never
            returned is absent here and listed in :attr:`missing`.
        outcomes: Every :class:`~evals.judge.client.JudgeOutcome`, in order.
        n_requested: How many calls were asked for.
        n_verified: How many returned schema-valid JSON.
        method: Provenance string for the measurements produced.
        missing: Requested dimensions with no verified sample at all.
    """

    dimensions: Tuple[str, ...]
    scores: Mapping[str, StabilisedScore]
    outcomes: Tuple[JudgeOutcome, ...]
    n_requested: int
    n_verified: int
    method: str
    missing: Tuple[str, ...] = ()

    @property
    def unverified(self) -> bool:
        """``True`` when no call produced a usable score for any dimension."""
        return self.n_verified == 0 or not self.scores

    def failure_notes(self) -> str:
        """Return a joined summary of every failed attempt, for the trail."""
        problems: List[str] = []
        for index, outcome in enumerate(self.outcomes):
            if not outcome.ok:
                problems.append(f"call {index + 1}: {outcome.evidence()}")
        return " | ".join(problems)

    def measurements(
        self,
        thresholds: Optional[Mapping[str, float]] = None,
        *,
        severity: str = Severity.MAJOR.value,
    ) -> List[Measurement]:
        """Render one measurement per requested dimension.

        Dimensions with no verified sample get an unverified measurement --
        ``passed=None`` and evidence prefixed ``unverified:`` -- rather than
        being dropped. A silently missing dimension is a silent pass.

        Args:
            thresholds: Optional per-dimension minimum median (0-10 scale).
            severity: Severity attached to each measurement.

        Returns:
            One measurement per entry in :attr:`dimensions`, in order.
        """
        limits = dict(thresholds or {})
        out: List[Measurement] = []
        for dimension in self.dimensions:
            stabilised = self.scores.get(dimension)
            if stabilised is None:
                out.append(
                    Measurement(
                        dimension=dimension,
                        value=None,
                        threshold=limits.get(dimension),
                        passed=None,
                        severity=severity,
                        method=self.method,
                        evidence=(
                            f"{UNVERIFIED_PREFIX} no verified judge sample after "
                            f"{self.n_requested} call(s). {self.failure_notes()}"
                        ).strip(),
                    )
                )
                continue
            out.append(
                stabilised.to_measurement(
                    method=self.method,
                    threshold=limits.get(dimension),
                    severity=severity,
                )
            )
        return out

    def mean_0_100(self) -> float:
        """Return the mean of every stabilised median, rescaled to 0-100.

        Returns:
            The mean, or ``0.0`` when nothing was verified. Zero is the
            default-FAIL disposition: no evidence is not a good score.
        """
        if not self.scores:
            return 0.0
        values = [s.score_0_100 for s in self.scores.values()]
        return round(sum(values) / len(values), 4)

    def to_dimension_score(
        self,
        dimension: str = "AESTHETIC",
        *,
        threshold_0_100: float = 87.0,
        severity: str = Severity.MAJOR.value,
        tier: str = Tier.A.value,
        per_dimension_threshold: Optional[float] = None,
    ) -> DimensionScore:
        """Fold the stabilised scores into one contract dimension score.

        Args:
            dimension: Name for the rolled-up score, e.g. ``"AESTHETIC"``.
            threshold_0_100: Minimum acceptable mean on the 0-100 scale.
            severity: Severity attached when the threshold is missed.
            tier: Tier this score belongs to; aesthetics are Tier A.
            per_dimension_threshold: Optional per-sub-dimension floor on the
                0-10 scale, applied to the individual measurements.

        Returns:
            A :class:`~evals.schema.DimensionScore`. An unverified result
            scores ``0.0`` and fails: default disposition is FAIL.
        """
        thresholds = (
            {d: per_dimension_threshold for d in self.dimensions}
            if per_dimension_threshold is not None
            else None
        )
        measurements = self.measurements(thresholds, severity=severity)
        mean = self.mean_0_100()

        if self.unverified:
            notes = (
                f"{UNVERIFIED_PREFIX} judge produced no usable score in "
                f"{self.n_requested} call(s); scored 0 under default-FAIL. "
                f"{self.failure_notes()}"
            ).strip()
            return DimensionScore(
                dimension=dimension,
                tier=tier,
                score_0_100=0.0,
                passed=False,
                severity=severity,
                measurements=measurements,
                notes=notes,
            )

        spreads = {d: s.spread for d, s in self.scores.items()}
        widest = max(spreads.values()) if spreads else 0.0
        notes = (
            f"median-of-{self.n_verified}/{self.n_requested} verified calls; "
            f"mean {mean:g}/100; widest per-dimension spread {widest:g}"
        )
        if self.missing:
            notes += f"; unverified dimensions: {', '.join(self.missing)}"

        passed = mean >= float(threshold_0_100) and not self.missing
        return DimensionScore(
            dimension=dimension,
            tier=tier,
            score_0_100=mean,
            passed=passed,
            severity=severity,
            measurements=measurements,
            notes=notes,
        )


def stabilise(
    client: JudgeClient,
    prompt: str,
    *,
    dimensions: Sequence[str],
    schema: ResponseSchema = ARTIFACT_QUALITY_SCHEMA,
    context: Optional[Mapping[str, Any]] = None,
    n: int = DEFAULT_N,
    scores_key: str = "scores",
    evidence_key: str = "evidence",
) -> StabilisationResult:
    """Call the judge ``n`` times and reduce each dimension to its median.

    Each call receives ``call_idx`` in its context so a deterministic backend
    varies between calls the way a real judge does, and so a caching layer
    cannot collapse the n calls into one.

    A call that fails validation is skipped, not fatal: with ``n=3`` a single
    malformed response still leaves a median of two. Only when *no* call
    verifies does the result become unverified.

    Args:
        client: Configured judge client.
        prompt: Rendered prompt text, identical for every call.
        dimensions: Dimensions to extract from each response.
        schema: Expected-keys contract for the response.
        context: Base context, copied and extended with ``call_idx``.
        n: Number of calls; must be at least 1. Default :data:`DEFAULT_N`.
        scores_key: Payload key holding the ``dimension -> score`` map.
        evidence_key: Payload key holding the ``dimension -> citation`` map.

    Returns:
        A :class:`StabilisationResult`.

    Raises:
        ValueError: When ``n`` is below 1 or ``dimensions`` is empty.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1; got {n!r}")
    requested = tuple(str(d) for d in dimensions)
    if not requested:
        raise ValueError("stabilise requires at least one dimension")

    base: Dict[str, Any] = dict(context or {})
    base.setdefault("dimensions", list(requested))

    outcomes: List[JudgeOutcome] = []
    raw_scores: Dict[str, List[float]] = {d: [] for d in requested}
    raw_evidence: Dict[str, List[str]] = {d: [] for d in requested}

    for call_idx in range(n):
        call_context = dict(base)
        call_context["call_idx"] = call_idx
        outcome = client.judge(prompt, schema, call_context)
        outcomes.append(outcome)
        if not outcome.ok or outcome.payload is None:
            continue

        payload_scores = outcome.payload.get(scores_key)
        if not isinstance(payload_scores, dict):
            continue
        payload_evidence = outcome.payload.get(evidence_key)
        evidence_map = payload_evidence if isinstance(payload_evidence, dict) else {}

        for dimension in requested:
            value = payload_scores.get(dimension)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            raw_scores[dimension].append(float(value))
            citation = evidence_map.get(dimension)
            raw_evidence[dimension].append(
                citation if isinstance(citation, str) else ""
            )

    scores: Dict[str, StabilisedScore] = {}
    missing: List[str] = []
    for dimension in requested:
        samples = raw_scores[dimension]
        if not samples:
            missing.append(dimension)
            continue
        scores[dimension] = StabilisedScore(
            dimension=dimension,
            median=median_of_n(samples),
            spread=score_spread(samples),
            raw=tuple(samples),
            n=len(samples),
            evidence_by_call=tuple(raw_evidence[dimension]),
        )

    backend_name = outcomes[0].backend if outcomes else client.backend.name
    return StabilisationResult(
        dimensions=requested,
        scores=scores,
        outcomes=tuple(outcomes),
        n_requested=n,
        n_verified=sum(1 for o in outcomes if o.ok),
        method=f"judge:{backend_name}",
        missing=tuple(missing),
    )
