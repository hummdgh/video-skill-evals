"""Mirrored blind A/B comparison against a reference video.

Two things make a pairwise judge untrustworthy, and this module defends
against both.

**Position bias.** A judge shown two videos favours a slot, not a video. So
every comparison is run **twice** -- once with the candidate in slot A, once
with it in slot B -- and a dimension only counts as better when *both* orders
agree. Where the two orders disagree the judge was voting for a position
rather than for a video, and the dimension is recorded as a tie. That
disagreement is kept in :attr:`ABResult.disagreements` as measured evidence of
bias, not thrown away.

**The overall vote.** The judge's holistic "which is better overall" answer is
read, recorded, and then **discarded**. It correlates with recency and with
whichever video had the louder opening far more than with quality. Only the
per-dimension tally decides anything.

Win rule: at least :data:`MIN_DIMENSIONS_BETTER` dimensions better, with
**zero** worse. A candidate that wins four dimensions and loses one has not
won; it has traded. Anything unverified is not a win -- default disposition is
FAIL.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..schema import DimensionScore, Measurement, Severity, Tier
from .client import (
    AB_COMPARE_SCHEMA,
    AB_DIMENSIONS,
    JudgeClient,
    JudgeOutcome,
    ResponseSchema,
    UNVERIFIED_PREFIX,
    render_prompt,
)

__all__ = [
    "MIN_DIMENSIONS_BETTER",
    "BETTER",
    "WORSE",
    "TIE",
    "PanelResult",
    "ABResult",
    "run_ab",
]

#: A win needs this many dimensions strictly better, with none worse.
MIN_DIMENSIONS_BETTER: int = 3

#: Candidate-relative verdicts, once slot votes have been de-mirrored.
BETTER: str = "better"
WORSE: str = "worse"
TIE: str = "tie"

#: Panel orders. The judge never sees these labels -- it sees slots A and B.
_ORDER_CANDIDATE_FIRST: str = "candidate_first"
_ORDER_REFERENCE_FIRST: str = "reference_first"


@dataclass(frozen=True)
class PanelResult:
    """One half of a mirrored pair: a single blind comparison.

    Attributes:
        order: :data:`_ORDER_CANDIDATE_FIRST` or
            :data:`_ORDER_REFERENCE_FIRST`.
        candidate_slot: Which slot (``"A"`` or ``"B"``) held the candidate.
        votes: Candidate-relative verdict per dimension, already translated
            out of slot space into :data:`BETTER` / :data:`WORSE` / :data:`TIE`.
        raw_overall: The judge's holistic slot vote. Recorded for the audit
            trail and **never** used in the decision.
        outcome: The underlying judge outcome.
    """

    order: str
    candidate_slot: str
    votes: Mapping[str, str]
    raw_overall: Optional[str]
    outcome: JudgeOutcome

    @property
    def ok(self) -> bool:
        """``True`` when this panel returned schema-valid JSON."""
        return self.outcome.ok


def _translate(slot_vote: str, candidate_slot: str) -> str:
    """Convert a slot vote into a candidate-relative verdict.

    Args:
        slot_vote: ``"A"``, ``"B"`` or ``"tie"`` as returned by the judge.
        candidate_slot: The slot the candidate occupied in this panel.

    Returns:
        :data:`BETTER`, :data:`WORSE` or :data:`TIE`.
    """
    if slot_vote == "tie":
        return TIE
    return BETTER if slot_vote == candidate_slot else WORSE


@dataclass(frozen=True)
class ABResult:
    """The de-mirrored outcome of one candidate-versus-reference comparison.

    Attributes:
        dimensions: Dimensions compared, in order.
        per_dimension: Final candidate-relative verdict per dimension, after
            both orders have been reconciled.
        better: Dimensions where both orders agreed the candidate is better.
        worse: Dimensions where both orders agreed it is worse.
        tied: Everything else, including bias-cancelled disagreements.
        disagreements: Dimensions where the two orders contradicted each
            other. Direct evidence of position bias in this judge.
        win: Whether the candidate beat the reference under the win rule.
        unverified: ``True`` when either panel failed validation.
        panels: Both panels, in call order.
        discarded_overall: The raw holistic votes, recorded and unused.
        reasons: Human-readable explanation of the outcome. Always populated
            when :attr:`win` is ``False``.
    """

    dimensions: Tuple[str, ...]
    per_dimension: Mapping[str, str]
    better: Tuple[str, ...]
    worse: Tuple[str, ...]
    tied: Tuple[str, ...]
    disagreements: Tuple[str, ...]
    win: bool
    unverified: bool
    panels: Tuple[PanelResult, ...]
    discarded_overall: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()

    def evidence(self) -> str:
        """Return a one-line evidence summary of the tally."""
        if self.unverified:
            return (
                f"{UNVERIFIED_PREFIX} mirrored A/B incomplete: "
                + "; ".join(self.reasons)
            )
        line = (
            f"mirrored A/B over {len(self.panels)} panel(s): "
            f"{len(self.better)} better, {len(self.worse)} worse, "
            f"{len(self.tied)} tied"
        )
        if self.disagreements:
            line += (
                f"; position bias cancelled on {len(self.disagreements)} "
                f"dimension(s): {', '.join(self.disagreements)}"
            )
        if self.discarded_overall:
            line += (
                f"; raw overall vote(s) {list(self.discarded_overall)} discarded "
                "as unreliable"
            )
        return line

    def to_measurement(
        self,
        dimension: str = "AB_PREFERENCE",
        *,
        method: str = "judge:ab",
        severity: str = Severity.MAJOR.value,
    ) -> Measurement:
        """Render the tally as a :class:`~evals.schema.Measurement`.

        Args:
            dimension: Measurement dimension key.
            method: Provenance string.
            severity: Severity attached when the candidate did not win.

        Returns:
            A measurement whose ``value`` is the net dimension tally and whose
            ``passed`` is the win flag -- ``None`` when unverified, so an
            incomplete comparison never reads as a pass.
        """
        return Measurement(
            dimension=dimension,
            value=len(self.better) - len(self.worse),
            threshold=MIN_DIMENSIONS_BETTER,
            passed=None if self.unverified else self.win,
            severity=severity,
            method=method,
            evidence=self.evidence(),
        )

    def to_dimension_score(
        self,
        dimension: str = "AB_PREFERENCE",
        *,
        method: str = "judge:ab",
        severity: str = Severity.MAJOR.value,
        tier: str = Tier.A.value,
    ) -> DimensionScore:
        """Render the tally as a :class:`~evals.schema.DimensionScore`.

        The score is the share of compared dimensions the candidate won
        outright, on the contract's 0-100 scale. An unverified comparison
        scores ``0.0`` and fails, per the default-FAIL disposition.

        Args:
            dimension: Dimension key for the rolled-up score.
            method: Provenance string for the embedded measurement.
            severity: Severity attached to a loss.
            tier: Tier this score belongs to.

        Returns:
            A populated :class:`~evals.schema.DimensionScore`.
        """
        if self.unverified or not self.dimensions:
            score = 0.0
        else:
            score = round(100.0 * len(self.better) / len(self.dimensions), 4)
        return DimensionScore(
            dimension=dimension,
            tier=tier,
            score_0_100=score,
            passed=bool(self.win),
            severity=severity,
            measurements=[
                self.to_measurement(dimension, method=method, severity=severity)
            ],
            notes=self.evidence(),
        )


def _run_panel(
    client: JudgeClient,
    prompt_template: str,
    schema: ResponseSchema,
    *,
    order: str,
    candidate: str,
    reference: str,
    dimensions: Sequence[str],
    base_context: Mapping[str, Any],
) -> PanelResult:
    """Run one blind panel and translate its slot votes.

    The judge is told only about slots A and B. Nothing in the prompt or the
    context names which side is the candidate -- that is what makes the panel
    blind, and it is why the caller must not stuff a candidate path into the
    shared context.

    Args:
        client: Configured judge client.
        prompt_template: Template text with ``{{slot_a}}`` / ``{{slot_b}}``.
        schema: Expected-keys contract for a comparison response.
        order: Which mirroring this panel represents.
        candidate: Candidate artifact reference (path or id).
        reference: Reference artifact reference.
        dimensions: Dimensions to compare.
        base_context: Extra blind context, e.g. ``case_id``.

    Returns:
        A :class:`PanelResult` with candidate-relative votes.
    """
    candidate_first = order == _ORDER_CANDIDATE_FIRST
    slot_a = candidate if candidate_first else reference
    slot_b = reference if candidate_first else candidate
    candidate_slot = "A" if candidate_first else "B"

    prompt = prompt_template
    for key, value in (
        ("slot_a", slot_a),
        ("slot_b", slot_b),
        ("dimensions", ", ".join(dimensions)),
    ):
        prompt = prompt.replace("{{" + key + "}}", str(value))

    # Blindness: the context names the slot *contents* and nothing else. It
    # must never carry the panel order or the candidate's slot, or the judge
    # could infer which artifact is under test and the mirroring would be
    # worthless.
    context: Dict[str, Any] = dict(base_context)
    context.update(
        {
            "slot_a": slot_a,
            "slot_b": slot_b,
            "dimensions": list(dimensions),
        }
    )

    outcome = client.judge(prompt, schema, context)

    votes: Dict[str, str] = {}
    raw_overall: Optional[str] = None
    if outcome.ok and outcome.payload is not None:
        payload_votes = outcome.payload.get("per_dimension")
        if isinstance(payload_votes, dict):
            for dimension in dimensions:
                slot_vote = payload_votes.get(dimension)
                if isinstance(slot_vote, str):
                    votes[dimension] = _translate(slot_vote, candidate_slot)
        overall = outcome.payload.get("overall")
        if isinstance(overall, str):
            raw_overall = overall

    return PanelResult(
        order=order,
        candidate_slot=candidate_slot,
        votes=votes,
        raw_overall=raw_overall,
        outcome=outcome,
    )


def _reconcile(first: str, second: str) -> Tuple[str, bool]:
    """Combine the same dimension's verdict from both mirrored orders.

    Args:
        first: Candidate-relative verdict from the candidate-first panel.
        second: Candidate-relative verdict from the reference-first panel.

    Returns:
        ``(verdict, disagreed)``. The verdict is :data:`BETTER` or
        :data:`WORSE` only when both orders agree; any other combination is a
        :data:`TIE`. ``disagreed`` marks a direct contradiction, which is
        position bias rather than a genuine tie.
    """
    if first == second:
        return first, False
    contradiction = {first, second} == {BETTER, WORSE}
    return TIE, contradiction


def run_ab(
    client: JudgeClient,
    *,
    candidate: str,
    reference: str,
    dimensions: Sequence[str] = AB_DIMENSIONS,
    schema: ResponseSchema = AB_COMPARE_SCHEMA,
    prompt_template: Optional[str] = None,
    context: Optional[Mapping[str, Any]] = None,
    min_better: int = MIN_DIMENSIONS_BETTER,
) -> ABResult:
    """Compare a candidate against a reference with mirrored blind panels.

    Runs exactly two panels -- candidate in slot A, then candidate in slot B --
    and keeps only verdicts both orders agree on. The holistic overall vote is
    recorded and discarded.

    Args:
        client: Configured judge client.
        candidate: Candidate artifact reference (path or id).
        reference: Reference artifact reference to beat.
        dimensions: Dimensions to tally. Defaults to
            :data:`~evals.judge.client.AB_DIMENSIONS`.
        schema: Expected-keys contract for a comparison response.
        prompt_template: Template text. Defaults to the packaged
            ``ab_compare.md``.
        context: Extra blind context, e.g. ``{"case_id": "hard-03"}``. Must
            not identify which artifact is the candidate.
        min_better: Dimensions that must be better for a win.

    Returns:
        An :class:`ABResult`.

    Raises:
        ValueError: When ``dimensions`` is empty.
    """
    compared = tuple(str(d) for d in dimensions)
    if not compared:
        raise ValueError("run_ab requires at least one dimension")

    template = (
        prompt_template
        if prompt_template is not None
        else render_prompt("ab_compare")
    )
    base_context = dict(context or {})

    panels = tuple(
        _run_panel(
            client,
            template,
            schema,
            order=order,
            candidate=candidate,
            reference=reference,
            dimensions=compared,
            base_context=base_context,
        )
        for order in (_ORDER_CANDIDATE_FIRST, _ORDER_REFERENCE_FIRST)
    )

    reasons: List[str] = []
    unverified = any(not panel.ok for panel in panels)
    for panel in panels:
        if not panel.ok:
            reasons.append(f"panel {panel.order}: {panel.outcome.evidence()}")

    per_dimension: Dict[str, str] = {}
    disagreements: List[str] = []
    for dimension in compared:
        first = panels[0].votes.get(dimension)
        second = panels[1].votes.get(dimension)
        if first is None or second is None:
            per_dimension[dimension] = TIE
            if not unverified:
                reasons.append(
                    f"dimension {dimension!r} missing from one panel; "
                    "recorded as tie"
                )
            continue
        verdict, disagreed = _reconcile(first, second)
        per_dimension[dimension] = verdict
        if disagreed:
            disagreements.append(dimension)

    better = tuple(d for d in compared if per_dimension[d] == BETTER)
    worse = tuple(d for d in compared if per_dimension[d] == WORSE)
    tied = tuple(d for d in compared if per_dimension[d] == TIE)

    win = (not unverified) and len(better) >= int(min_better) and not worse
    if not win:
        if unverified:
            reasons.append("unverified comparison cannot be a win (default FAIL)")
        elif worse:
            reasons.append(
                f"{len(worse)} dimension(s) worse than reference: "
                f"{', '.join(worse)}; a win requires zero worse"
            )
        elif len(better) < int(min_better):
            reasons.append(
                f"only {len(better)} dimension(s) better; "
                f"{int(min_better)} required for a win"
            )

    discarded = tuple(
        panel.raw_overall for panel in panels if panel.raw_overall is not None
    )

    return ABResult(
        dimensions=compared,
        per_dimension=per_dimension,
        better=better,
        worse=worse,
        tied=tied,
        disagreements=tuple(disagreements),
        win=win,
        unverified=unverified,
        panels=panels,
        discarded_overall=discarded,
        reasons=tuple(reasons),
    )
