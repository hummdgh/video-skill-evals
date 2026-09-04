"""Markdown rendering of a run report.

Ordered so the most decision-relevant information is first: the verdict, then
what regressed, then what failed and why, then the detail. Someone skimming
the top three lines should learn whether to ship.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

from evals.schema import (
    CaseAggregate,
    CaseVerdict,
    DimensionScore,
    RunReport,
    Severity,
    Verdict,
)

__all__ = ["render_markdown"]

_BADGE = {
    Verdict.PASS.value: "PASS",
    Verdict.PASS_WITH_ADVISORY.value: "ADVISORY",
    Verdict.FAIL.value: "FAIL",
}


def render_markdown(report: RunReport) -> str:
    """Render the whole report as Markdown."""
    lines: List[str] = []
    totals = report.totals or {}

    # -- headline ----------------------------------------------------------
    gate = totals.get("gate_passed")
    headline = "PASSED" if gate else "FAILED"
    lines.append(f"# Evaluation run `{report.run_id}` -- gate {headline}")
    lines.append("")

    if not report.live:
        lines.append(
            "> **Mock run.** No agent runtime was invoked and no real video was "
            "generated. This exercises the harness, not the skill."
        )
        lines.append("")

    counts = totals.get("verdicts", {}) or {}
    lines.append(
        f"**{totals.get('cases', 0)} cases / {totals.get('units', 0)} units** "
        f"| suite `{report.suite_name or 'unknown'}` "
        f"| adapter `{report.adapter}` "
        f"| mean score **{totals.get('mean_score', 0):.0f}**"
    )
    lines.append("")
    lines.append(
        f"| PASS | ADVISORY | FAIL |\n|---:|---:|---:|\n"
        f"| {counts.get(Verdict.PASS.value, 0)} "
        f"| {counts.get(Verdict.PASS_WITH_ADVISORY.value, 0)} "
        f"| {counts.get(Verdict.FAIL.value, 0)} |"
    )
    lines.append("")

    # -- failure taxonomy --------------------------------------------------
    classes = totals.get("failure_classes", {}) or {}
    if classes:
        lines.append("## Failure taxonomy")
        lines.append("")
        lines.append("| Class | Units | Meaning |")
        lines.append("|---|---:|---|")
        meanings = {
            "infra": "environment failed; excluded from quality aggregates",
            "agent": "agent ran but produced no usable artefact",
            "quality": "artefact produced, did not meet the bar",
        }
        for name in ("infra", "agent", "quality"):
            if name in classes:
                lines.append(f"| `{name}` | {classes[name]} | {meanings[name]} |")
        lines.append("")

    # -- regressions -------------------------------------------------------
    if report.regressions:
        regressed = [r for r in report.regressions if r.is_regression]
        lines.append("## Regressions vs baseline")
        lines.append("")
        lines.append(f"Baseline: `{report.baseline_run_id or 'none'}`")
        lines.append("")
        if regressed:
            lines.append("| Dimension | Baseline | Current | Delta |")
            lines.append("|---|---:|---:|---:|")
            for reg in regressed:
                lines.append(
                    f"| {reg.dimension} | {reg.baseline_score:.1f} "
                    f"| {reg.current_score:.1f} | **{reg.delta:+.1f}** |"
                )
        else:
            lines.append("No dimension regressed beyond tolerance.")
        lines.append("")

    # -- per case ----------------------------------------------------------
    lines.append("## Cases")
    lines.append("")
    lines.append("| Case | Verdict | Mean | Spread | Flake | Stability |")
    lines.append("|---|---|---:|---:|---:|---|")
    for aggregate in report.aggregates:
        verdict = _modal_verdict(aggregate)
        lines.append(
            f"| `{aggregate.case_id}` | {_BADGE.get(verdict, verdict)} "
            f"| {aggregate.score_mean:.0f} | {aggregate.score_spread:.1f} "
            f"| {aggregate.flake_rate:.0%} | {aggregate.stability_grade} |"
        )
    lines.append("")

    flaky = totals.get("flaky_cases") or []
    if flaky:
        lines.append(
            f"**{len(flaky)} flaky case(s):** "
            + ", ".join(f"`{c}`" for c in flaky)
            + " -- repeats disagreed on the verdict."
        )
        lines.append("")

    # -- dimension means ---------------------------------------------------
    means = totals.get("dimension_means") or {}
    if means:
        lines.append("## Dimension means")
        lines.append("")
        lines.append("| Dimension | Mean score |")
        lines.append("|---|---:|")
        for dimension in sorted(means):
            lines.append(f"| {dimension} | {means[dimension]:.1f} |")
        lines.append("")

    # -- failure detail ----------------------------------------------------
    lines.append("## Failure detail")
    lines.append("")
    any_failures = False
    for aggregate in report.aggregates:
        for verdict in aggregate.verdicts:
            if verdict.overall == Verdict.PASS.value:
                continue
            any_failures = True
            lines.append(
                f"### `{verdict.case_id}` repeat {verdict.repeat_idx} -- "
                f"{_BADGE.get(verdict.overall, verdict.overall)}"
                + (f" (`{verdict.failure_class}`)" if verdict.failure_class else "")
            )
            lines.append("")
            for reason in verdict.gate_reasons:
                lines.append(f"- {reason}")
            lines.append("")
            evidence = list(_failed_evidence(verdict))
            if evidence:
                lines.append("<details><summary>Measured evidence</summary>")
                lines.append("")
                for line in evidence:
                    lines.append(f"- {line}")
                lines.append("")
                lines.append("</details>")
                lines.append("")
    if not any_failures:
        lines.append("No failures.")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"Generated {report.created_at or 'unknown'} "
        f"| schema `{report.schema_version}`"
    )
    return "\n".join(lines) + "\n"


def _modal_verdict(aggregate: CaseAggregate) -> str:
    outcomes = [v.overall for v in aggregate.verdicts]
    if not outcomes:
        return Verdict.FAIL.value
    return max(set(outcomes), key=outcomes.count)


def _failed_evidence(verdict: CaseVerdict) -> Iterable[str]:
    """Yield cited evidence for every failed or unverified measurement."""
    for score in list(verdict.tier_a) + list(verdict.tier_b):
        for measurement in score.measurements:
            if measurement.passed is True:
                continue
            state = "unverified" if measurement.passed is None else "failed"
            detail = measurement.evidence or f"value={measurement.value}"
            yield (
                f"`{measurement.dimension}` ({measurement.severity}, {state}, "
                f"via {measurement.method}): {detail}"
            )
