"""Self-contained HTML rendering of a run report.

Everything is inlined: CSS, layout, score bars. There are no ``<script>`` tags,
no external stylesheets, no web fonts and no images fetched over the network.
A report must render identically on a locked-down CI runner, from a
``file://`` URL, and from a zip opened a year later.

All interpolated content passes through :func:`html.escape`.
"""

from __future__ import annotations

from html import escape
from typing import Iterable, List, Optional, Sequence

from evals.schema import (
    CaseAggregate,
    CaseVerdict,
    RunReport,
    Severity,
    Verdict,
)

__all__ = ["render_html"]

_CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#272b34;--fg:#e6e9ef;--dim:#9aa3b2;
--pass:#3fb950;--warn:#d29922;--fail:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:1.6rem;margin:0 0 .35rem}
h2{font-size:1.15rem;margin:2rem 0 .75rem;padding-bottom:.35rem;
border-bottom:1px solid var(--line)}
h3{font-size:.98rem;margin:1.25rem 0 .4rem}
.sub{color:var(--dim);margin:0 0 1.25rem}
.banner{padding:.85rem 1rem;border-radius:8px;font-weight:600;margin:0 0 1.25rem}
.banner.pass{background:rgba(63,185,80,.12);border:1px solid var(--pass);color:var(--pass)}
.banner.fail{background:rgba(248,81,73,.12);border:1px solid var(--fail);color:var(--fail)}
.note{background:rgba(88,166,255,.10);border:1px solid var(--accent);color:var(--accent);
padding:.7rem .9rem;border-radius:8px;margin:0 0 1.25rem;font-size:.9rem}
.cards{display:flex;gap:.75rem;flex-wrap:wrap;margin:0 0 1rem}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:.7rem 1rem;min-width:118px}
.card .n{font-size:1.5rem;font-weight:700;line-height:1.1}
.card .l{color:var(--dim);font-size:.76rem;text-transform:uppercase;letter-spacing:.04em}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin:0 0 1rem}
th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:.78rem;text-transform:uppercase;
letter-spacing:.04em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
code{background:#0b0d11;border:1px solid var(--line);border-radius:4px;
padding:.08rem .34rem;font-size:.86em}
.tag{display:inline-block;padding:.1rem .5rem;border-radius:999px;
font-size:.74rem;font-weight:700;letter-spacing:.02em}
.tag.pass{background:rgba(63,185,80,.15);color:var(--pass)}
.tag.warn{background:rgba(210,153,34,.15);color:var(--warn)}
.tag.fail{background:rgba(248,81,73,.15);color:var(--fail)}
.tag.muted{background:rgba(154,163,178,.15);color:var(--dim)}
.bar{position:relative;height:7px;background:#0b0d11;border-radius:4px;
overflow:hidden;min-width:90px}
.bar>i{display:block;height:100%;border-radius:4px}
.fail-block{background:var(--panel);border:1px solid var(--line);
border-left:3px solid var(--fail);border-radius:6px;padding:.75rem 1rem;margin:0 0 .85rem}
.fail-block ul{margin:.4rem 0 0;padding-left:1.1rem}
.fail-block li{margin:.15rem 0}
details{margin:.5rem 0 0}
summary{cursor:pointer;color:var(--dim);font-size:.85rem}
.ev{color:var(--dim);font-size:.83rem;margin:.35rem 0 0;padding-left:1.1rem}
footer{color:var(--dim);font-size:.8rem;margin-top:2.5rem;
border-top:1px solid var(--line);padding-top:.9rem}
"""


def _bar(score: float) -> str:
    """An inline score bar, coloured by band."""
    pct = max(0.0, min(100.0, float(score)))
    colour = "var(--pass)" if pct >= 90 else "var(--warn)" if pct >= 70 else "var(--fail)"
    return f'<div class="bar"><i style="width:{pct:.0f}%;background:{colour}"></i></div>'


def _tag(verdict: str) -> str:
    cls = {
        Verdict.PASS.value: "pass",
        Verdict.PASS_WITH_ADVISORY.value: "warn",
        Verdict.FAIL.value: "fail",
    }.get(verdict, "muted")
    label = {
        Verdict.PASS.value: "PASS",
        Verdict.PASS_WITH_ADVISORY.value: "ADVISORY",
        Verdict.FAIL.value: "FAIL",
    }.get(verdict, verdict)
    return f'<span class="tag {cls}">{escape(label)}</span>'


def _modal(aggregate: CaseAggregate) -> str:
    outcomes = [v.overall for v in aggregate.verdicts]
    if not outcomes:
        return Verdict.FAIL.value
    return max(set(outcomes), key=outcomes.count)


def render_html(report: RunReport) -> str:
    """Render the report as one self-contained HTML document."""
    totals = report.totals or {}
    counts = totals.get("verdicts", {}) or {}
    gate = bool(totals.get("gate_passed"))

    out: List[str] = []
    a = out.append

    a("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    a("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    a(f"<title>Evaluation run {escape(report.run_id)}</title>")
    a(f"<style>{_CSS}</style></head><body><div class='wrap'>")

    a(f"<h1>Evaluation run <code>{escape(report.run_id)}</code></h1>")
    a(
        f"<p class='sub'>suite <code>{escape(report.suite_name or 'unknown')}</code> "
        f"&middot; adapter <code>{escape(report.adapter)}</code> "
        f"&middot; {escape(report.created_at or '')}</p>"
    )

    a(
        f"<div class='banner {'pass' if gate else 'fail'}'>"
        f"Gate {'PASSED' if gate else 'FAILED'}</div>"
    )

    if not report.live:
        a(
            "<div class='note'><strong>Mock run.</strong> No agent runtime was "
            "invoked and no real video was generated. This exercises the harness, "
            "not the skill.</div>"
        )

    # -- summary cards -----------------------------------------------------
    a("<div class='cards'>")
    for label, value in (
        ("Cases", totals.get("cases", 0)),
        ("Units", totals.get("units", 0)),
        ("Pass", counts.get(Verdict.PASS.value, 0)),
        ("Advisory", counts.get(Verdict.PASS_WITH_ADVISORY.value, 0)),
        ("Fail", counts.get(Verdict.FAIL.value, 0)),
        ("Mean score", f"{totals.get('mean_score', 0):.0f}"),
    ):
        a(f"<div class='card'><div class='n'>{escape(str(value))}</div>"
          f"<div class='l'>{escape(label)}</div></div>")
    a("</div>")

    # -- failure taxonomy --------------------------------------------------
    classes = totals.get("failure_classes", {}) or {}
    if classes:
        meanings = {
            "infra": "environment failed; excluded from quality aggregates",
            "agent": "agent ran but produced no usable artefact",
            "quality": "artefact produced, did not meet the bar",
        }
        a("<h2>Failure taxonomy</h2><table><tr><th>Class</th><th class='num'>Units</th>"
          "<th>Meaning</th></tr>")
        for name in ("infra", "agent", "quality"):
            if name in classes:
                a(f"<tr><td><code>{escape(name)}</code></td>"
                  f"<td class='num'>{classes[name]}</td>"
                  f"<td>{escape(meanings[name])}</td></tr>")
        a("</table>")

    # -- regressions -------------------------------------------------------
    if report.regressions:
        regressed = [r for r in report.regressions if r.is_regression]
        a("<h2>Regressions vs baseline</h2>")
        a(f"<p class='sub'>Baseline: <code>"
          f"{escape(report.baseline_run_id or 'none')}</code></p>")
        if regressed:
            a("<table><tr><th>Dimension</th><th class='num'>Baseline</th>"
              "<th class='num'>Current</th><th class='num'>Delta</th></tr>")
            for reg in regressed:
                a(f"<tr><td>{escape(reg.dimension)}</td>"
                  f"<td class='num'>{reg.baseline_score:.1f}</td>"
                  f"<td class='num'>{reg.current_score:.1f}</td>"
                  f"<td class='num' style='color:var(--fail)'>{reg.delta:+.1f}</td></tr>")
            a("</table>")
        else:
            a("<p>No dimension regressed beyond tolerance.</p>")

    # -- cases -------------------------------------------------------------
    a("<h2>Cases</h2><table><tr><th>Case</th><th>Verdict</th><th class='num'>Mean</th>"
      "<th>Score</th><th class='num'>Spread</th><th class='num'>Flake</th>"
      "<th>Stability</th></tr>")
    for aggregate in report.aggregates:
        a(
            f"<tr><td><code>{escape(aggregate.case_id)}</code></td>"
            f"<td>{_tag(_modal(aggregate))}</td>"
            f"<td class='num'>{aggregate.score_mean:.0f}</td>"
            f"<td>{_bar(aggregate.score_mean)}</td>"
            f"<td class='num'>{aggregate.score_spread:.1f}</td>"
            f"<td class='num'>{aggregate.flake_rate:.0%}</td>"
            f"<td>{escape(aggregate.stability_grade)}</td></tr>"
        )
    a("</table>")

    # -- dimension means ---------------------------------------------------
    means = totals.get("dimension_means") or {}
    if means:
        a("<h2>Dimension means</h2><table><tr><th>Dimension</th>"
          "<th class='num'>Mean</th><th>Score</th></tr>")
        for dimension in sorted(means):
            value = float(means[dimension])
            a(f"<tr><td>{escape(dimension)}</td>"
              f"<td class='num'>{value:.1f}</td><td>{_bar(value)}</td></tr>")
        a("</table>")

    # -- failure detail ----------------------------------------------------
    a("<h2>Failure detail</h2>")
    any_failures = False
    for aggregate in report.aggregates:
        for verdict in aggregate.verdicts:
            if verdict.overall == Verdict.PASS.value:
                continue
            any_failures = True
            klass = (
                f" &middot; <code>{escape(verdict.failure_class)}</code>"
                if verdict.failure_class
                else ""
            )
            a("<div class='fail-block'>")
            a(f"<h3><code>{escape(verdict.case_id)}</code> repeat "
              f"{verdict.repeat_idx} {_tag(verdict.overall)}{klass}</h3>")
            a("<ul>")
            for reason in verdict.gate_reasons:
                a(f"<li>{escape(reason)}</li>")
            a("</ul>")
            evidence = list(_failed_evidence(verdict))
            if evidence:
                a("<details><summary>Measured evidence "
                  f"({len(evidence)})</summary>")
                for line in evidence:
                    a(f"<p class='ev'>{line}</p>")
                a("</details>")
            a("</div>")
    if not any_failures:
        a("<p>No failures.</p>")

    a(
        f"<footer>Generated {escape(report.created_at or 'unknown')} "
        f"&middot; schema <code>{escape(report.schema_version)}</code> "
        f"&middot; self-contained: no external resources</footer>"
    )
    a("</div></body></html>")
    return "".join(out)


def _failed_evidence(verdict: CaseVerdict) -> Iterable[str]:
    """Escaped, cited evidence for each failed or unverified measurement."""
    for score in list(verdict.tier_a) + list(verdict.tier_b):
        for measurement in score.measurements:
            if measurement.passed is True:
                continue
            state = "unverified" if measurement.passed is None else "failed"
            detail = measurement.evidence or f"value={measurement.value}"
            yield (
                f"<code>{escape(measurement.dimension)}</code> "
                f"({escape(measurement.severity)}, {state}, via "
                f"{escape(measurement.method)}): {escape(str(detail))}"
            )
