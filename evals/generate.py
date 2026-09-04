"""Stage 1: drive the agent runtime across the prompt suite.

Lays out one isolated working directory per ``(case, repeat)`` unit::

    <run_dir>/
      runstate.json
      suite.json                     copy of the suite actually used
      cases/<case_id>/repeat-<n>/
        prompt.txt
        transcript.txt
        result.json                  the GenerationResult
        composition.mp4 ...          whatever the agent produced

Two properties matter more than speed here:

*Resumability* -- a live run can cost real money and take hours, so an
interrupted run must resume rather than start over.

*Failure isolation* -- one broken case must never abort the run, and a failure
that is the environment's fault (runtime missing, timeout, crash before any
artefact) is classified ``infra`` so it can be excluded from quality
aggregates. An API flake is not a bad video.

Standard library only.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from evals.adapters import AdapterError, get_adapter
from evals.runstate import RunState, UnitStatus
from evals.schema import GenerationResult, Suite, SuiteCase, validate_suite

__all__ = ["GenerateSummary", "load_suite", "generate", "RESULT_FILENAME"]

RESULT_FILENAME = "result.json"
SUITE_COPY_FILENAME = "suite.json"


@dataclass
class GenerateSummary:
    """Outcome of a generate stage."""

    run_dir: Path
    run_id: str
    total: int
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    infra_failures: int
    results: List[GenerationResult]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "run_id": self.run_id,
            "total": self.total,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "infra_failures": self.infra_failures,
        }


def load_suite(path: Path | str) -> Suite:
    """Load and validate a prompt suite.

    Raises:
        FileNotFoundError: no such file.
        ValueError: malformed JSON, or the suite fails schema validation.
    """
    suite_path = Path(path)
    if not suite_path.is_file():
        raise FileNotFoundError(f"suite not found: {suite_path}")
    try:
        raw = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{suite_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {suite_path}: {exc}") from exc

    errors = validate_suite(raw)
    if errors:
        joined = "\n  - ".join(errors)
        raise ValueError(f"{suite_path} failed validation:\n  - {joined}")
    return Suite.from_dict(raw)


def _classify_failure(result: GenerationResult, outcome_infra: bool) -> str:
    """Return ``infra`` | ``agent`` for a failed unit.

    ``quality`` is not decided here -- that is the scorer's job, and only for
    units that actually produced a video.
    """
    if outcome_infra:
        return "infra"
    if result.video_path is None:
        return "agent"
    return "agent"


def generate(
    suite: Suite,
    run_dir: Path | str,
    config: Any,
    *,
    repeats: Optional[int] = None,
    only_cases: Optional[Sequence[str]] = None,
    resume: bool = True,
    on_progress: Optional[Callable[[str, int, str], None]] = None,
) -> GenerateSummary:
    """Run every case in ``suite`` ``repeats`` times.

    Args:
        suite: The validated prompt suite.
        run_dir: Directory to write the run into; created if absent.
        config: Resolved ``Config``.
        repeats: Override ``config.repeats``.
        only_cases: Restrict to these case ids.
        resume: Reuse an existing run state and skip completed units.
        on_progress: Called as ``(case_id, repeat_idx, status)`` per unit.

    Returns:
        A :class:`GenerateSummary`.

    Raises:
        AdapterError: the adapter is unknown or misconfigured. Raised up front,
            before any unit runs, so a typo does not burn half a suite.
        ValueError: ``only_cases`` names a case that is not in the suite.
    """
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)

    cases: List[SuiteCase] = list(suite.cases)
    if only_cases:
        wanted = list(only_cases)
        known = {c.id for c in cases}
        missing = [c for c in wanted if c not in known]
        if missing:
            raise ValueError(
                f"case(s) not in suite: {', '.join(missing)}. "
                f"Available: {', '.join(sorted(known))}"
            )
        cases = [c for c in cases if c.id in set(wanted)]

    n_repeats = int(repeats if repeats is not None else getattr(config, "repeats", 1))
    if n_repeats < 1:
        raise ValueError(f"repeats must be at least 1, got {n_repeats}")

    # Build the adapter before touching anything: fail fast on misconfiguration.
    adapter_name = getattr(getattr(config, "adapter", None), "name", "mock")
    adapter = get_adapter(adapter_name, config)

    # Persist the exact suite used, so a report is reproducible later.
    (root / SUITE_COPY_FILENAME).write_text(
        json.dumps(suite.to_dict(), indent=2), encoding="utf-8"
    )

    case_ids = [c.id for c in cases]
    if resume:
        state = RunState.load_or_create(root, case_ids, n_repeats, suite_name=suite.name)
        state.reset_stale_running()
    else:
        state = RunState.create(root, case_ids, n_repeats, suite_name=suite.name)

    by_id = {c.id: c for c in cases}
    results: List[GenerationResult] = []
    attempted = succeeded = failed = infra = 0

    total_units = len(state)
    skipped = len([u for u in state.all_units() if u.is_terminal])

    for unit in state.pending():
        case = by_id.get(unit.case_id)
        if case is None:
            state.mark(unit.case_id, unit.repeat_idx, UnitStatus.FAILED,
                       error="case not present in suite", failure_class="infra")
            failed += 1
            infra += 1
            continue

        workdir = root / "cases" / unit.case_id / f"repeat-{unit.repeat_idx}"
        workdir.mkdir(parents=True, exist_ok=True)
        state.mark(unit.case_id, unit.repeat_idx, UnitStatus.RUNNING, workdir=str(workdir))
        attempted += 1

        # The adapter reads the repeat index off the case object.
        setattr(case, "_repeat_idx", unit.repeat_idx)

        try:
            prompt_file = adapter.prepare(case, workdir)
            outcome = adapter.invoke(prompt_file, workdir)
            result = adapter.collect(case, workdir, outcome)
        except AdapterError as exc:
            result = GenerationResult(
                case_id=unit.case_id,
                repeat_idx=unit.repeat_idx,
                workdir=str(workdir),
                adapter=adapter.name,
                live=adapter.is_live,
                error=f"adapter error: {exc}",
            )
            outcome_infra = True
        except OSError as exc:
            result = GenerationResult(
                case_id=unit.case_id,
                repeat_idx=unit.repeat_idx,
                workdir=str(workdir),
                adapter=adapter.name,
                live=adapter.is_live,
                error=f"filesystem error: {exc}",
            )
            outcome_infra = True
        else:
            outcome_infra = outcome.is_infra_failure

        result.repeat_idx = unit.repeat_idx
        _write_result(workdir, result)
        results.append(result)

        if result.error is None:
            state.mark(unit.case_id, unit.repeat_idx, UnitStatus.DONE, workdir=str(workdir))
            succeeded += 1
            status = UnitStatus.DONE
        else:
            failure_class = _classify_failure(result, outcome_infra)
            state.mark(
                unit.case_id, unit.repeat_idx, UnitStatus.FAILED,
                workdir=str(workdir), error=result.error, failure_class=failure_class,
            )
            failed += 1
            if failure_class == "infra":
                infra += 1
            status = UnitStatus.FAILED

        if on_progress is not None:
            on_progress(unit.case_id, unit.repeat_idx, status)

    return GenerateSummary(
        run_dir=root,
        run_id=state.run_id,
        total=total_units,
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        infra_failures=infra,
        results=results,
    )


def _write_result(workdir: Path, result: GenerationResult) -> None:
    """Persist a unit's result next to its artefacts."""
    path = workdir / RESULT_FILENAME
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")


def load_results(run_dir: Path | str) -> List[GenerationResult]:
    """Read back every ``result.json`` under ``run_dir``, in stable order.

    Malformed individual results are skipped rather than aborting the stage --
    a later stage reports them as missing, which is more useful than a crash.
    """
    root = Path(run_dir)
    out: List[GenerationResult] = []
    for path in sorted((root / "cases").rglob(RESULT_FILENAME)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            out.append(GenerationResult.from_dict(data))
    return out
