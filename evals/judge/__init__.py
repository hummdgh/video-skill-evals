"""Judge layer: the half of the rubric a machine cannot settle.

``evals/measure/`` handles everything objective -- resolution, loudness, cue
duration, credentials. What is left needs judgement: whether the narration
describes what is actually on screen, whether the transitions work, whether
the thing is any good.

This package supplies three Tier A groups (AESTHETIC, SYNC, MONTAGE) and one
Tier B group (skill adherence), each stabilised by median-of-n sampling and
each degrading to *unverified* rather than to a pass when the backend fails.

Entry point::

    from evals.judge import judge_all
    judged = judge_all(results, measured, suite=suite, config=cfg)

Standard library only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from evals.judge.artifact import GROUPS, judge_artifact
from evals.judge.client import JudgeClient, make_client
from evals.schema import DimensionScore, GenerationResult

__all__ = [
    "judge_all",
    "judge_one",
    "build_client",
    "review_all",
    "review_one",
    "GROUPS",
]


def build_client(config: Any) -> Optional[JudgeClient]:
    """Construct a judge client from config, or ``None`` if unavailable.

    Never raises: a judge that cannot be built means the judged dimensions
    report unverified, which is a reportable state rather than a crash.
    """
    judge_config = getattr(config, "judge", None)
    if judge_config is None:
        return None
    payload = {
        "backend": getattr(judge_config, "backend", "mock"),
        "model": getattr(judge_config, "model", ""),
        "n_samples": getattr(judge_config, "n_samples", 3),
        "max_retries": getattr(judge_config, "max_retries", 3),
        "temperature": getattr(judge_config, "temperature", 0.0),
        "endpoint": getattr(judge_config, "endpoint", ""),
        "api_key_env": getattr(judge_config, "api_key_env", ""),
    }
    try:
        return make_client(payload)
    except Exception:
        return None


def judge_one(
    result: GenerationResult,
    measured: Sequence[DimensionScore] = (),
    *,
    case: Any = None,
    client: Optional[JudgeClient] = None,
    config: Any = None,
) -> List[DimensionScore]:
    """Judge a single generation result."""
    n_samples = 3
    judge_config = getattr(config, "judge", None)
    if judge_config is not None:
        n_samples = int(getattr(judge_config, "n_samples", 3) or 3)

    frames_dir = None
    if result.workdir:
        frames_dir = Path(result.workdir) / "frames"

    return judge_artifact(
        result,
        case=case,
        measured=measured,
        client=client,
        n_samples=n_samples,
        frames_dir=frames_dir,
    )


def judge_all(
    results: Sequence[GenerationResult],
    measured: Optional[Mapping[str, Sequence[DimensionScore]]] = None,
    *,
    suite: Any = None,
    config: Any = None,
    client: Optional[JudgeClient] = None,
    on_progress: Optional[Any] = None,
) -> Dict[str, List[DimensionScore]]:
    """Judge every result, keyed by ``"<case_id>#<repeat_idx>"``.

    A failure judging one unit never aborts the batch -- that unit's groups are
    recorded unverified with the reason, and the run continues.
    """
    if client is None:
        client = build_client(config)

    by_case: Dict[str, Any] = {}
    if suite is not None:
        for case in getattr(suite, "cases", []) or []:
            by_case[getattr(case, "id", None)] = case

    out: Dict[str, List[DimensionScore]] = {}
    for index, result in enumerate(results, start=1):
        key = f"{result.case_id}#{result.repeat_idx}"
        out[key] = judge_one(
            result,
            (measured or {}).get(key, ()),
            case=by_case.get(result.case_id),
            client=client,
            config=config,
        )
        if on_progress is not None:
            on_progress(key, index, len(results))
    return out


# ---------------------------------------------------------------------------
# Tier B: execution review
# ---------------------------------------------------------------------------


def _read_transcript(result: GenerationResult, limit: int = 20000) -> str:
    """Read the agent transcript, truncated from the end.

    The tail matters more than the head: failures and final state live there.
    """
    if not result.transcript_path:
        return ""
    try:
        text = Path(result.transcript_path).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def review_one(
    result: GenerationResult,
    *,
    client: Optional[JudgeClient] = None,
) -> Any:
    """Review one agent transcript for skill adherence and failure modes.

    Returns an ``ExecutionReview``. When no client is available, or the judge
    fails, the review comes back with ``verified=False`` so the Tier B
    dimensions report unverified rather than scoring zero.
    """
    from evals.execmetrics import execution_review_from_payload, review_execution

    if client is None:
        return execution_review_from_payload(None)

    try:
        return review_execution(
            client, result, transcript=_read_transcript(result)
        )
    except Exception:
        # A judge failure must never abort scoring; report it as unverified.
        return execution_review_from_payload(None)


def review_all(
    results: Sequence[GenerationResult],
    *,
    config: Any = None,
    client: Optional[JudgeClient] = None,
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Review every result, keyed by ``"<case_id>#<repeat_idx>"``."""
    if client is None:
        client = build_client(config)

    out: Dict[str, Any] = {}
    for index, result in enumerate(results, start=1):
        key = f"{result.case_id}#{result.repeat_idx}"
        out[key] = review_one(result, client=client)
        if on_progress is not None:
            on_progress(key, index, len(results))
    return out
