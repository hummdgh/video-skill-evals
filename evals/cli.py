"""Command-line entry point.

    evals <generate|measure|score|report|calibrate|baseline> [options]

Each stage reads and writes files in the run directory, so stages can be run
separately, re-run independently, and resumed after an interruption::

    runs/<name>/
      runstate.json      unit status grid          (generate)
      suite.json         the suite actually used   (generate)
      cases/**/result.json                         (generate)
      measurements.json                            (measure)
      report.json                                  (score)
      report.html / report.md                      (report)

Stage modules are imported **lazily inside each handler**. That keeps
``--help`` working even when an optional stage is incomplete or broken, which
matters when parts of the tree are still being built.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["main", "build_parser"]

MEASUREMENTS_FILENAME = "measurements.json"
JUDGEMENTS_FILENAME = "judgements.json"
REVIEWS_FILENAME = "reviews.json"
REPORT_FILENAME = "report.json"
BASELINE_FILENAME = "baseline.json"

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2
EXIT_ERROR = 3


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser."""
    parser = argparse.ArgumentParser(
        prog="evals",
        description=(
            "Evaluate a video-composition skill: drive an agent runtime across a "
            "prompt suite, measure what it produces, and score it against a "
            "two-tier rubric."
        ),
        epilog="Mock is the default. Use --live only when you mean to spend money.",
    )
    parser.add_argument("--version", action="version", version="evals 0.1.0")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--run-dir", default="runs/latest", metavar="DIR",
                       help="run directory (default: runs/latest)")
        p.add_argument("--config", default=None, metavar="FILE",
                       help="JSON config file")

    # -- generate ----------------------------------------------------------
    gen = sub.add_parser("generate", help="run the agent across the prompt suite")
    common(gen)
    gen.add_argument("--suite", default="suites/default.json", metavar="FILE",
                     help="prompt suite (default: suites/default.json)")
    gen.add_argument("--repeats", type=int, default=None, metavar="N",
                     help="repeats per case (default: from config, 3)")
    gen.add_argument("--adapter", default=None, metavar="NAME",
                     help="generic | claude-code | gemini-cli | antigravity | mock")
    gen.add_argument("--case", action="append", dest="cases", metavar="ID",
                     help="restrict to this case id (repeatable)")
    gen.add_argument("--no-resume", action="store_true",
                     help="start fresh instead of resuming")
    mode = gen.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true",
                      help="offline stub run, no cost (default)")
    mode.add_argument("--live", action="store_true",
                      help="invoke the real agent runtime; SPENDS REAL MONEY")

    # -- measure -----------------------------------------------------------
    mea = sub.add_parser("measure", help="run the deterministic battery")
    common(mea)

    # -- judge -------------------------------------------------------------
    jud = sub.add_parser("judge", help="LLM-scored dimensions (aesthetic, sync, montage)")
    common(jud)
    jud.add_argument("--n-samples", type=int, default=None, metavar="N",
                     help="judge samples per dimension for median-of-n (default 3)")
    jud.add_argument("--backend", default=None, metavar="NAME",
                     help="mock | http (default: from config, mock)")

    # -- score -------------------------------------------------------------
    sco = sub.add_parser("score", help="apply the rubric and produce a verdict")
    common(sco)
    sco.add_argument("--baseline", default=None, metavar="FILE",
                     help="baseline report.json to detect regressions against")

    # -- report ------------------------------------------------------------
    rep = sub.add_parser("report", help="render a run report")
    common(rep)
    rep.add_argument("--html", default=None, metavar="FILE", help="write HTML here")
    rep.add_argument("--markdown", default=None, metavar="FILE", help="write Markdown here")
    rep.add_argument("--format", default="markdown",
                     choices=("markdown", "html", "json"),
                     help="format for stdout (default: markdown)")
    rep.add_argument("--quiet", action="store_true", help="do not print to stdout")

    # -- baseline ----------------------------------------------------------
    bas = sub.add_parser("baseline", help="promote this run to the baseline")
    common(bas)
    bas.add_argument("--to", default=BASELINE_FILENAME, metavar="FILE",
                     help=f"destination (default: {BASELINE_FILENAME})")

    # -- calibrate ---------------------------------------------------------
    cal = sub.add_parser("calibrate", help="judge-vs-human agreement on the golden set")
    common(cal)
    cal.add_argument("--golden", default="golden/labels.json", metavar="FILE",
                     help="human-labelled set (default: golden/labels.json)")

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config(args: argparse.Namespace) -> Any:
    """Build a Config from the file plus CLI overrides."""
    from evals.config import load_config, validate_config

    overrides: Dict[str, Any] = {}
    adapter: Dict[str, Any] = {}

    if getattr(args, "live", False):
        overrides["live"] = True
    elif getattr(args, "mock", False):
        overrides["live"] = False
        adapter["name"] = "mock"

    explicit = getattr(args, "adapter", None)
    if explicit:
        adapter["name"] = explicit
    # Default to the mock adapter unless the user asked to go live.
    if not adapter.get("name") and not getattr(args, "live", False):
        adapter["name"] = "mock"
    if adapter:
        overrides["adapter"] = adapter

    repeats = getattr(args, "repeats", None)
    if repeats is not None:
        overrides["repeats"] = repeats

    cfg = load_config(getattr(args, "config", None), overrides=overrides)
    problems = validate_config(cfg)
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return cfg


def _run_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "run_dir", "runs/latest"))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    from evals.generate import generate, load_suite

    cfg = _load_config(args)
    run_dir = _run_dir(args)

    if cfg.live:
        print("!! LIVE RUN: this invokes the real agent runtime and spends real money.",
              file=sys.stderr)

    suite = load_suite(args.suite)
    total = len(suite.cases) * int(args.repeats or cfg.repeats)
    print(f"suite '{suite.name}': {len(suite.cases)} cases x "
          f"{args.repeats or cfg.repeats} repeats = {total} units")

    done = {"n": 0}

    def progress(case_id: str, repeat_idx: int, status: str) -> None:
        done["n"] += 1
        print(f"  [{done['n']:>3}/{total}] {case_id}#{repeat_idx} -> {status}")

    summary = generate(
        suite, run_dir, cfg,
        repeats=args.repeats,
        only_cases=args.cases,
        resume=not args.no_resume,
        on_progress=progress,
    )
    print(f"\ngenerated: {summary.succeeded} ok, {summary.failed} failed "
          f"({summary.infra_failures} infra), {summary.skipped} skipped")
    return EXIT_OK


def cmd_measure(args: argparse.Namespace) -> int:
    from evals.generate import load_results, load_suite
    from evals.measure import measure_all

    cfg = _load_config(args)
    run_dir = _run_dir(args)

    suite_copy = run_dir / "suite.json"
    suite = load_suite(suite_copy) if suite_copy.is_file() else None

    results = load_results(run_dir)
    if not results:
        print(f"no generation results in {run_dir}; run 'generate' first",
              file=sys.stderr)
        return EXIT_USAGE

    print(f"measuring {len(results)} unit(s)...")
    measured = measure_all(results, suite=suite, config=cfg)

    payload = {
        key: [score.to_dict() for score in scores]
        for key, scores in measured.items()
    }
    (run_dir / MEASUREMENTS_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print(f"wrote {run_dir / MEASUREMENTS_FILENAME}")
    return EXIT_OK


def cmd_judge(args: argparse.Namespace) -> int:
    from evals.generate import load_results, load_suite
    from evals.judge import build_client, judge_all
    from evals.schema import DimensionScore

    cfg = _load_config(args)
    run_dir = _run_dir(args)

    if getattr(args, "n_samples", None):
        cfg.judge.n_samples = int(args.n_samples)
    if getattr(args, "backend", None):
        cfg.judge.backend = args.backend

    results = load_results(run_dir)
    if not results:
        print(f"no generation results in {run_dir}; run 'generate' first",
              file=sys.stderr)
        return EXIT_USAGE

    # Deterministic measurements are fed to the judge as context, so it cannot
    # contradict a measured number blind.
    measured = {}
    measurements_path = run_dir / MEASUREMENTS_FILENAME
    if measurements_path.is_file():
        raw = json.loads(measurements_path.read_text(encoding="utf-8"))
        measured = {
            key: [DimensionScore.from_dict(d) for d in scores]
            for key, scores in raw.items()
        }
    else:
        print("note: no measurements.json; judging without deterministic context",
              file=sys.stderr)

    suite_copy = run_dir / "suite.json"
    suite = load_suite(suite_copy) if suite_copy.is_file() else None

    client = build_client(cfg)
    if client is None:
        print("warning: no judge client could be built; all judged dimensions "
              "will report unverified", file=sys.stderr)

    print(f"judging {len(results)} unit(s) with backend "
          f"'{cfg.judge.backend}', median-of-{cfg.judge.n_samples}...")

    def progress(key: str, index: int, total: int) -> None:
        if index == 1 or index % 6 == 0 or index == total:
            print(f"  [{index:>3}/{total}] {key}")

    judged = judge_all(results, measured, suite=suite, config=cfg,
                       client=client, on_progress=progress)

    payload = {
        key: [score.to_dict() for score in scores]
        for key, scores in judged.items()
    }
    (run_dir / JUDGEMENTS_FILENAME).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    # Tier B: review the agent transcript for skill adherence and failure modes.
    import dataclasses as _dc

    from evals.judge import review_all

    print(f"reviewing {len(results)} transcript(s) for Tier B adherence...")
    reviews = review_all(results, config=cfg, client=client)
    (run_dir / REVIEWS_FILENAME).write_text(
        json.dumps(
            {key: _dc.asdict(review) for key, review in reviews.items()},
            indent=2,
            default=list,
        ),
        encoding="utf-8",
    )
    verified_reviews = sum(1 for r in reviews.values() if getattr(r, "verified", False))
    print(f"wrote {run_dir / REVIEWS_FILENAME} "
          f"({verified_reviews}/{len(reviews)} review(s) verified)")

    verified = sum(
        1 for scores in judged.values()
        for s in scores
        if any(m.passed is not None for m in s.measurements)
    )
    total_groups = sum(len(s) for s in judged.values())
    print(f"wrote {run_dir / JUDGEMENTS_FILENAME} "
          f"({verified}/{total_groups} group(s) produced scores)")
    return EXIT_OK


def cmd_score(args: argparse.Namespace) -> int:
    from collections import defaultdict

    from evals.execmetrics import compute_exec_metrics
    from evals.generate import load_results, load_suite
    from evals.schema import DimensionScore, RunReport
    from evals.score import aggregate_case, build_run_report, score_unit

    cfg = _load_config(args)
    run_dir = _run_dir(args)

    measurements_path = run_dir / MEASUREMENTS_FILENAME
    if not measurements_path.is_file():
        print(f"no {MEASUREMENTS_FILENAME} in {run_dir}; run 'measure' first",
              file=sys.stderr)
        return EXIT_USAGE

    raw = json.loads(measurements_path.read_text(encoding="utf-8"))
    measured = {
        key: [DimensionScore.from_dict(d) for d in scores]
        for key, scores in raw.items()
    }

    suite_copy = run_dir / "suite.json"
    suite = load_suite(suite_copy) if suite_copy.is_file() else None
    by_case = {c.id: c for c in (suite.cases if suite else [])}

    baseline: Optional[RunReport] = None
    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.is_file():
            baseline = RunReport.from_dict(
                json.loads(baseline_path.read_text(encoding="utf-8"))
            )
        else:
            print(f"baseline not found: {baseline_path}", file=sys.stderr)

    # Judged Tier A groups (aesthetic, sync, montage), when the judge stage ran.
    judged: Dict[str, List[DimensionScore]] = {}
    judgements_path = run_dir / JUDGEMENTS_FILENAME
    if judgements_path.is_file():
        raw_judged = json.loads(judgements_path.read_text(encoding="utf-8"))
        judged = {
            key: [DimensionScore.from_dict(d) for d in scores]
            for key, scores in raw_judged.items()
        }
    else:
        print("note: no judgements.json; aesthetic/sync/montage will be absent. "
              "Run 'judge' first to score them.", file=sys.stderr)

    # Tier B execution reviews (skill adherence, failure modes), when judged.
    from evals.execmetrics import execution_review_from_payload

    reviews: Dict[str, Any] = {}
    reviews_path = run_dir / REVIEWS_FILENAME
    if reviews_path.is_file():
        raw_reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
        for key, payload in raw_reviews.items():
            # Only rebuild a review that was actually verified; an unverified
            # one must stay unverified rather than becoming a zero.
            if isinstance(payload, dict) and payload.get("verified"):
                reviews[key] = execution_review_from_payload(payload)

    grouped = defaultdict(list)
    for result in load_results(run_dir):
        case = by_case.get(result.case_id)
        key = f"{result.case_id}#{result.repeat_idx}"
        tier_a = list(measured.get(key, [])) + list(judged.get(key, []))
        band = getattr(case, "band", "medium") if case else "medium"
        tier_b = compute_exec_metrics(result, band=band, review=reviews.get(key))
        grouped[result.case_id].append(
            score_unit(case, result, tier_a, tier_b,
                       tier_b_blocking=cfg.tier_b_blocking)
        )

    aggregates = [aggregate_case(cid, vs) for cid, vs in sorted(grouped.items())]
    report = build_run_report(
        run_id=run_dir.name,
        aggregates=aggregates,
        suite_name=suite.name if suite else "",
        adapter=cfg.adapter.name,
        live=cfg.live,
        baseline=baseline,
    )

    (run_dir / REPORT_FILENAME).write_text(
        json.dumps(report.to_dict(), indent=2), encoding="utf-8"
    )

    totals = report.totals
    counts = totals.get("verdicts", {})
    print(f"scored {totals.get('units', 0)} unit(s) across "
          f"{totals.get('cases', 0)} case(s)")
    print(f"  PASS {counts.get('PASS', 0)}  "
          f"ADVISORY {counts.get('PASS_WITH_ADVISORY', 0)}  "
          f"FAIL {counts.get('FAIL', 0)}")
    print(f"  mean score {totals.get('mean_score', 0):.0f}")
    regressed = [r for r in report.regressions if r.is_regression]
    if regressed:
        print(f"  {len(regressed)} regression(s) vs baseline")
    print(f"  gate: {'PASSED' if totals.get('gate_passed') else 'FAILED'}")
    print(f"wrote {run_dir / REPORT_FILENAME}")

    return EXIT_OK if totals.get("gate_passed") else EXIT_GATE_FAILED


def cmd_report(args: argparse.Namespace) -> int:
    from evals.report import render, write_report
    from evals.schema import RunReport

    run_dir = _run_dir(args)
    report_path = run_dir / REPORT_FILENAME
    if not report_path.is_file():
        print(f"no {REPORT_FILENAME} in {run_dir}; run 'score' first", file=sys.stderr)
        return EXIT_USAGE

    report = RunReport.from_dict(json.loads(report_path.read_text(encoding="utf-8")))

    if args.html:
        print(f"wrote {write_report(report, args.html, 'html')}")
    if args.markdown:
        print(f"wrote {write_report(report, args.markdown, 'markdown')}")
    if not args.quiet and not (args.html or args.markdown):
        print(render(report, args.format))

    return EXIT_OK if report.totals.get("gate_passed") else EXIT_GATE_FAILED


def cmd_baseline(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args)
    source = run_dir / REPORT_FILENAME
    if not source.is_file():
        print(f"no {REPORT_FILENAME} in {run_dir}; run 'score' first", file=sys.stderr)
        return EXIT_USAGE
    destination = Path(args.to)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"promoted {source} -> {destination}")
    return EXIT_OK


def cmd_calibrate(args: argparse.Namespace) -> int:
    try:
        from evals.calibrate import calibrate
    except ImportError as exc:
        print(f"calibration is unavailable: {exc}", file=sys.stderr)
        return EXIT_ERROR

    run_dir = _run_dir(args)
    golden = Path(args.golden)
    if not golden.is_file():
        print(f"golden set not found: {golden}", file=sys.stderr)
        return EXIT_USAGE
    return int(calibrate(run_dir, golden) or EXIT_OK)


_HANDLERS = {
    "generate": cmd_generate,
    "measure": cmd_measure,
    "judge": cmd_judge,
    "score": cmd_score,
    "report": cmd_report,
    "baseline": cmd_baseline,
    "calibrate": cmd_calibrate,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    handler = _HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE

    try:
        return handler(args)
    except SystemExit as exc:
        return int(exc.code or EXIT_OK)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\ninterrupted; run state is saved and the run can be resumed",
              file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
