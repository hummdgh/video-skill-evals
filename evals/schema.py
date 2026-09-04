"""Shared data contract for ``video-skill-evals`` -- **FROZEN**.

This module is the single source of truth for every structure that crosses a
stage boundary in the pipeline::

    generate  ->  GenerationResult
    measure   ->  Measurement
    judge     ->  Measurement (tier="B" style, judge-sourced evidence)
    score     ->  DimensionScore, CaseVerdict, CaseAggregate, Regression
    report    ->  RunReport

Every other module (``evals.measure.*``, ``evals.judge.*``, ``evals.score``,
``evals.report.*``, ``evals.execmetrics``, ``evals.calibrate``) imports from
here and **must not modify this file**. It is deliberately dependency-free:
Python standard library only, no imports from anywhere else in the package, so
it can never introduce an import cycle.

Design rules honoured by every dataclass below:

* ``to_dict()`` returns a plain JSON-serialisable ``dict`` (no enums, no
  tuples, no dataclasses left inside).
* ``from_dict()`` is total and forgiving: unknown keys are ignored, missing
  optional keys fall back to documented defaults. This lets newer readers load
  older run artefacts.
* Enums are declared as ``str`` enums so ``json.dumps`` and plain ``==``
  comparisons against literals both behave.
* Nothing here performs I/O, spawns processes, or touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "SCHEMA_VERSION",
    "SEVERITY_ORDER",
    "Severity",
    "Tier",
    "FailureClass",
    "Verdict",
    "AssertionResult",
    "Assertions",
    "SuiteCase",
    "Suite",
    "GenerationResult",
    "Measurement",
    "DimensionScore",
    "CaseVerdict",
    "CaseAggregate",
    "Regression",
    "RunReport",
    "validate_suite",
    "max_severity",
    "dumps",
    "loads",
]

SCHEMA_VERSION: str = "1.0.0"

#: Canonical band names used by ``suites/default.json``.
BANDS: Tuple[str, ...] = ("simple", "medium", "hard", "adversarial")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """How much a failed measurement matters.

    ``CRITICAL`` failures in Tier A break the gate. ``MAJOR`` and ``MINOR``
    reduce the continuous score and may downgrade a ``PASS`` to
    ``PASS_WITH_ADVISORY``, but never fail a run on their own.
    """

    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


#: Ascending order of seriousness. ``SEVERITY_ORDER.index(x)`` is a sort key;
#: a larger index means a more serious problem.
SEVERITY_ORDER: Tuple[str, ...] = (
    Severity.MINOR.value,
    Severity.MAJOR.value,
    Severity.CRITICAL.value,
)


class Tier(str, Enum):
    """Rubric tier. ``A`` = artifact quality, ``B`` = skill execution."""

    A = "A"
    B = "B"


class FailureClass(str, Enum):
    """Failure taxonomy (rubric improvement #6).

    ``INFRA`` failures are retried and excluded from quality aggregates so
    that API flakes cannot poison the metrics. ``AGENT`` means the agent
    misused the skill. ``QUALITY`` means the artifact itself is bad.
    """

    INFRA = "infra"
    AGENT = "agent"
    QUALITY = "quality"


class Verdict(str, Enum):
    """Overall per-case outcome."""

    PASS = "PASS"
    PASS_WITH_ADVISORY = "PASS_WITH_ADVISORY"
    FAIL = "FAIL"


def max_severity(severities: Iterable[str]) -> Optional[str]:
    """Return the most serious severity in ``severities``.

    Args:
        severities: Iterable of severity strings or :class:`Severity` members.

    Returns:
        The most serious severity as a plain string, or ``None`` when the
        iterable is empty. Unknown values are ignored rather than raising, so
        a forward-compatible reader never crashes on an unfamiliar label.
    """
    best: Optional[str] = None
    best_rank = -1
    for raw in severities:
        value = raw.value if isinstance(raw, Severity) else str(raw)
        if value not in SEVERITY_ORDER:
            continue
        rank = SEVERITY_ORDER.index(value)
        if rank > best_rank:
            best_rank = rank
            best = value
    return best


# ---------------------------------------------------------------------------
# Internal coercion helpers (module-private, but stable)
# ---------------------------------------------------------------------------


def _enum_value(value: Any, default: Optional[str] = None) -> Optional[str]:
    """Normalise an enum member / string into its plain string value."""
    if value is None:
        return default
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _opt_float(value: Any) -> Optional[float]:
    """Coerce to ``float``, mapping ``None`` and blank strings to ``None``."""
    if value is None or value == "":
        return None
    return float(value)


def _opt_int(value: Any) -> Optional[int]:
    """Coerce to ``int``, mapping ``None`` and blank strings to ``None``."""
    if value is None or value == "":
        return None
    return int(value)


def _str_list(value: Any) -> List[str]:
    """Coerce ``value`` into a list of strings; ``None`` becomes ``[]``."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value if isinstance(value, str) else value.decode("utf-8")]
    return [str(item) for item in value]


def _duration_pair(value: Any) -> Optional[Tuple[float, float]]:
    """Coerce a ``[lo, hi]`` sequence into a ``(lo, hi)`` float tuple."""
    if value is None:
        return None
    items = list(value)
    if len(items) != 2:
        raise ValueError(
            f"duration_s must have exactly 2 elements, got {len(items)}"
        )
    return (float(items[0]), float(items[1]))


# ---------------------------------------------------------------------------
# Suite definition
# ---------------------------------------------------------------------------


@dataclass
class Assertions:
    """Declarative, machine-checkable expectations for one suite case.

    Rubric improvement #5: this replaces hand-written per-run job specs. Every
    field is optional -- ``None`` (or an empty list) means "do not assert".

    Attributes:
        duration_s: Inclusive ``(lo, hi)`` bounds on the final video duration
            in seconds.
        min_scenes: Minimum number of distinct scenes/shots required.
        aspect_ratio: Required aspect ratio, e.g. ``"16:9"``.
        required_concepts: Concepts that must be evidenced in the video,
            transcript or script.
        language: Expected BCP-47-ish language tag of the narration, e.g.
            ``"en"``.
    """

    duration_s: Optional[Tuple[float, float]] = None
    min_scenes: Optional[int] = None
    aspect_ratio: Optional[str] = None
    required_concepts: List[str] = field(default_factory=list)
    language: Optional[str] = None

    def is_empty(self) -> bool:
        """Return ``True`` when this block asserts nothing at all."""
        return (
            self.duration_s is None
            and self.min_scenes is None
            and self.aspect_ratio is None
            and not self.required_concepts
            and self.language is None
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict (``duration_s`` becomes a list)."""
        return {
            "duration_s": list(self.duration_s) if self.duration_s else None,
            "min_scenes": self.min_scenes,
            "aspect_ratio": self.aspect_ratio,
            "required_concepts": list(self.required_concepts),
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Assertions":
        """Build from a (possibly ``None`` or partial) mapping."""
        data = data or {}
        return cls(
            duration_s=_duration_pair(data.get("duration_s")),
            min_scenes=_opt_int(data.get("min_scenes")),
            aspect_ratio=data.get("aspect_ratio"),
            required_concepts=_str_list(data.get("required_concepts")),
            language=data.get("language"),
        )


@dataclass
class SuiteCase:
    """One prompt in the suite, plus what we expect back from it.

    Attributes:
        id: Stable identifier, e.g. ``"simple-01"``. Used as a directory name,
            so it must be filesystem-safe.
        band: Difficulty band -- one of :data:`BANDS`.
        prompt: The natural-language task handed to the agent under test.
        assertions: Deterministic expectations (see :class:`Assertions`).
        probes: Names of documented failure modes this case deliberately
            targets, e.g. ``"fresh-shell-per-scene"``.
        notes: Free text explaining why the case exists.
    """

    id: str
    band: str
    prompt: str
    assertions: Assertions = field(default_factory=Assertions)
    probes: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "id": self.id,
            "band": self.band,
            "prompt": self.prompt,
            "assertions": self.assertions.to_dict(),
            "probes": list(self.probes),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SuiteCase":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            id=str(data["id"]),
            band=str(data.get("band", "simple")),
            prompt=str(data.get("prompt", "")),
            assertions=Assertions.from_dict(data.get("assertions")),
            probes=_str_list(data.get("probes")),
            notes=str(data.get("notes", "")),
        )


@dataclass
class Suite:
    """A named, versioned collection of :class:`SuiteCase` objects."""

    name: str
    version: str
    cases: List[SuiteCase] = field(default_factory=list)

    def case_ids(self) -> List[str]:
        """Return every case id, in declaration order."""
        return [case.id for case in self.cases]

    def get(self, case_id: str) -> Optional[SuiteCase]:
        """Return the case with ``case_id``, or ``None`` if absent."""
        for case in self.cases:
            if case.id == case_id:
                return case
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "name": self.name,
            "version": self.version,
            "cases": [case.to_dict() for case in self.cases],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Suite":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            name=str(data.get("name", "unnamed")),
            version=str(data.get("version", "0")),
            cases=[SuiteCase.from_dict(c) for c in data.get("cases", [])],
        )


# ---------------------------------------------------------------------------
# Stage 1: generate
# ---------------------------------------------------------------------------


@dataclass
class GenerationResult:
    """Everything ``generate`` learned from one agent invocation.

    One instance per ``(case_id, repeat_idx)`` pair. Paths are stored as
    strings (not ``Path``) so the record is trivially JSON round-trippable;
    they are relative to the run root wherever possible.

    Attributes:
        case_id: The :class:`SuiteCase` id this result belongs to.
        repeat_idx: 0-based repeat index within the case.
        workdir: Isolated working directory the agent ran in.
        video_path: Produced video, or ``None`` if nothing was produced.
        srt_path: Produced subtitle file, if any.
        script_path: Narration/script artefact, if any.
        transcript_path: Full agent transcript (stdout+stderr capture).
        exit_code: Process exit status; ``None`` if the agent never launched.
        wall_time_s: Wall-clock seconds for the invocation.
        turns: Agent turns consumed, when the adapter can report it.
        retries: Number of infra-classified retries spent on this result.
        cost_usd: Reported cost in US dollars, when available.
        adapter: Name of the adapter that produced this result.
        live: ``True`` when a real agent/network was used, ``False`` for mock
            or replayed runs. Reports must surface this prominently.
        error: Human-readable failure description, or ``None`` on success.
    """

    case_id: str
    repeat_idx: int = 0
    workdir: str = ""
    video_path: Optional[str] = None
    srt_path: Optional[str] = None
    script_path: Optional[str] = None
    transcript_path: Optional[str] = None
    exit_code: Optional[int] = None
    wall_time_s: float = 0.0
    turns: Optional[int] = None
    retries: int = 0
    cost_usd: Optional[float] = None
    adapter: str = "unknown"
    live: bool = False
    error: Optional[str] = None

    @property
    def key(self) -> str:
        """Stable ``"<case_id>#<repeat_idx>"`` identifier."""
        return f"{self.case_id}#{self.repeat_idx}"

    def succeeded(self) -> bool:
        """Return ``True`` when the agent exited cleanly and left a video."""
        return self.error is None and self.exit_code == 0 and bool(self.video_path)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "case_id": self.case_id,
            "repeat_idx": self.repeat_idx,
            "workdir": self.workdir,
            "video_path": self.video_path,
            "srt_path": self.srt_path,
            "script_path": self.script_path,
            "transcript_path": self.transcript_path,
            "exit_code": self.exit_code,
            "wall_time_s": self.wall_time_s,
            "turns": self.turns,
            "retries": self.retries,
            "cost_usd": self.cost_usd,
            "adapter": self.adapter,
            "live": bool(self.live),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerationResult":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            case_id=str(data["case_id"]),
            repeat_idx=int(data.get("repeat_idx", 0)),
            workdir=str(data.get("workdir", "")),
            video_path=data.get("video_path"),
            srt_path=data.get("srt_path"),
            script_path=data.get("script_path"),
            transcript_path=data.get("transcript_path"),
            exit_code=_opt_int(data.get("exit_code")),
            wall_time_s=float(data.get("wall_time_s", 0.0)),
            turns=_opt_int(data.get("turns")),
            retries=int(data.get("retries", 0)),
            cost_usd=_opt_float(data.get("cost_usd")),
            adapter=str(data.get("adapter", "unknown")),
            live=bool(data.get("live", False)),
            error=data.get("error"),
        )


# ---------------------------------------------------------------------------
# Stage 2: measure / judge
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """A single observation about one artefact.

    Deterministic measurements (ffprobe, scene counts, SRT parsing) and
    judge-sourced observations use the same shape so that ``score`` can treat
    them uniformly. ``method`` records the provenance so the
    "deterministic beats holistic" rule can be applied downstream.

    Attributes:
        dimension: Rubric dimension key, e.g. ``"duration"`` or
            ``"skill_adherence"``.
        value: Observed value. Any JSON-safe scalar or structure.
        threshold: The expectation ``value`` was compared against, if any.
        passed: ``True``/``False`` outcome, or ``None`` for informational
            measurements that do not gate anything.
        severity: One of :class:`Severity`; how much a failure here matters.
        method: Provenance, e.g. ``"ffprobe"``, ``"srt-parse"``,
            ``"judge:gemini"``, ``"mock"``.
        evidence: Free-form supporting detail -- frame citations, command
            output snippets, judge rationale. A secrets/PII claim without a
            literal citation here must be recorded as
            ``claimed-but-unverified`` by the caller, never as a failure.
    """

    dimension: str
    value: Any = None
    threshold: Any = None
    passed: Optional[bool] = None
    severity: str = Severity.MAJOR.value
    method: str = "unknown"
    evidence: Optional[str] = None

    def failed(self) -> bool:
        """Return ``True`` only for an explicit ``passed is False``."""
        return self.passed is False

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "dimension": self.dimension,
            "value": self.value,
            "threshold": self.threshold,
            "passed": self.passed,
            "severity": _enum_value(self.severity, Severity.MAJOR.value),
            "method": self.method,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Measurement":
        """Build from a mapping; unknown keys are ignored."""
        passed = data.get("passed")
        return cls(
            dimension=str(data["dimension"]),
            value=data.get("value"),
            threshold=data.get("threshold"),
            passed=None if passed is None else bool(passed),
            severity=_enum_value(data.get("severity"), Severity.MAJOR.value)
            or Severity.MAJOR.value,
            method=str(data.get("method", "unknown")),
            evidence=data.get("evidence"),
        )


@dataclass
class DimensionScore:
    """Rolled-up result for one rubric dimension of one case repeat.

    Attributes:
        dimension: Rubric dimension key.
        tier: ``"A"`` (artifact quality) or ``"B"`` (skill execution).
        score_0_100: Continuous score, rounded DOWN by the scorer so that a
            borderline result never flatters the agent.
        passed: Whether this dimension met its gate.
        severity: Severity attached to a failure here.
        measurements: The raw observations this score was derived from.
        notes: Human-readable explanation of the score.
    """

    dimension: str
    tier: str = Tier.A.value
    score_0_100: float = 0.0
    passed: bool = False
    severity: str = Severity.MAJOR.value
    measurements: List[Measurement] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "dimension": self.dimension,
            "tier": _enum_value(self.tier, Tier.A.value),
            "score_0_100": self.score_0_100,
            "passed": bool(self.passed),
            "severity": _enum_value(self.severity, Severity.MAJOR.value),
            "measurements": [m.to_dict() for m in self.measurements],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DimensionScore":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            dimension=str(data["dimension"]),
            tier=_enum_value(data.get("tier"), Tier.A.value) or Tier.A.value,
            score_0_100=float(data.get("score_0_100", 0.0)),
            passed=bool(data.get("passed", False)),
            severity=_enum_value(data.get("severity"), Severity.MAJOR.value)
            or Severity.MAJOR.value,
            measurements=[
                Measurement.from_dict(m) for m in data.get("measurements", [])
            ],
            notes=str(data.get("notes", "")),
        )


@dataclass
class AssertionResult:
    """Outcome of one declarative assertion from :class:`Assertions`.

    Attributes:
        name: Assertion key, e.g. ``"duration_s"`` or ``"min_scenes"``.
        expected: The declared expectation.
        actual: What was actually observed.
        passed: Whether the assertion held.
        must: ``True`` for MUST assertions, which gate the overall verdict.
        detail: Optional human-readable explanation.
    """

    name: str
    expected: Any = None
    actual: Any = None
    passed: bool = False
    must: bool = True
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "passed": bool(self.passed),
            "must": bool(self.must),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssertionResult":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            name=str(data["name"]),
            expected=data.get("expected"),
            actual=data.get("actual"),
            passed=bool(data.get("passed", False)),
            must=bool(data.get("must", True)),
            detail=str(data.get("detail", "")),
        )


# ---------------------------------------------------------------------------
# Stage 3: score
# ---------------------------------------------------------------------------


@dataclass
class CaseVerdict:
    """The gated outcome for one ``(case_id, repeat_idx)`` pair.

    Gate rule (from the spec)::

        gate = all Tier-A CRITICAL pass
               AND all MUST assertions pass
               AND cost/latency within budget

    Attributes:
        case_id: Case this verdict belongs to.
        repeat_idx: 0-based repeat index.
        overall: ``PASS`` | ``PASS_WITH_ADVISORY`` | ``FAIL``.
        tier_a: Artifact-quality dimension scores.
        tier_b: Skill-execution dimension scores.
        failure_class: ``infra`` | ``agent`` | ``quality``, or ``None`` when
            the case did not fail.
        assertion_results: Per-assertion outcomes.
        gate_reasons: Human-readable reasons the gate produced this verdict.
            Always populated for a ``FAIL`` -- never fail silently.
    """

    case_id: str
    repeat_idx: int = 0
    overall: str = Verdict.FAIL.value
    tier_a: List[DimensionScore] = field(default_factory=list)
    tier_b: List[DimensionScore] = field(default_factory=list)
    failure_class: Optional[str] = None
    assertion_results: List[AssertionResult] = field(default_factory=list)
    gate_reasons: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable ``"<case_id>#<repeat_idx>"`` identifier."""
        return f"{self.case_id}#{self.repeat_idx}"

    def all_scores(self) -> List[DimensionScore]:
        """Return Tier-A and Tier-B dimension scores together."""
        return list(self.tier_a) + list(self.tier_b)

    def is_pass(self) -> bool:
        """Return ``True`` for ``PASS`` or ``PASS_WITH_ADVISORY``."""
        return self.overall in (
            Verdict.PASS.value,
            Verdict.PASS_WITH_ADVISORY.value,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "case_id": self.case_id,
            "repeat_idx": self.repeat_idx,
            "overall": _enum_value(self.overall, Verdict.FAIL.value),
            "tier_a": [d.to_dict() for d in self.tier_a],
            "tier_b": [d.to_dict() for d in self.tier_b],
            "failure_class": _enum_value(self.failure_class, None),
            "assertion_results": [a.to_dict() for a in self.assertion_results],
            "gate_reasons": list(self.gate_reasons),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CaseVerdict":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            case_id=str(data["case_id"]),
            repeat_idx=int(data.get("repeat_idx", 0)),
            overall=_enum_value(data.get("overall"), Verdict.FAIL.value)
            or Verdict.FAIL.value,
            tier_a=[DimensionScore.from_dict(d) for d in data.get("tier_a", [])],
            tier_b=[DimensionScore.from_dict(d) for d in data.get("tier_b", [])],
            failure_class=_enum_value(data.get("failure_class"), None),
            assertion_results=[
                AssertionResult.from_dict(a)
                for a in data.get("assertion_results", [])
            ],
            gate_reasons=_str_list(data.get("gate_reasons")),
        )


@dataclass
class CaseAggregate:
    """All repeats of one case, collapsed into stability statistics.

    Rubric improvement #4: a single run is an anecdote. ``flake_rate`` and
    ``score_spread`` expose the variance a pass/fail gate alone hides.

    Attributes:
        case_id: The case these repeats belong to.
        n_repeats: How many repeats were attempted.
        verdicts: Per-repeat verdicts.
        score_mean: Mean overall score across repeats.
        score_spread: Max minus min score across repeats.
        flake_rate: Fraction of repeats that disagreed with the majority
            verdict, in ``[0.0, 1.0]``.
        stability_grade: Coarse label, e.g. ``"stable"``, ``"flaky"``,
            ``"unstable"``.
    """

    case_id: str
    n_repeats: int = 0
    verdicts: List[CaseVerdict] = field(default_factory=list)
    score_mean: float = 0.0
    score_spread: float = 0.0
    flake_rate: float = 0.0
    stability_grade: str = "unknown"

    def pass_count(self) -> int:
        """Number of repeats whose verdict was a pass of either kind."""
        return sum(1 for v in self.verdicts if v.is_pass())

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "case_id": self.case_id,
            "n_repeats": self.n_repeats,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "score_mean": self.score_mean,
            "score_spread": self.score_spread,
            "flake_rate": self.flake_rate,
            "stability_grade": self.stability_grade,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CaseAggregate":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            case_id=str(data["case_id"]),
            n_repeats=int(data.get("n_repeats", 0)),
            verdicts=[CaseVerdict.from_dict(v) for v in data.get("verdicts", [])],
            score_mean=float(data.get("score_mean", 0.0)),
            score_spread=float(data.get("score_spread", 0.0)),
            flake_rate=float(data.get("flake_rate", 0.0)),
            stability_grade=str(data.get("stability_grade", "unknown")),
        )


@dataclass
class Regression:
    """One dimension compared against a stored baseline run.

    Attributes:
        dimension: Rubric dimension key.
        baseline_score: Score recorded in the baseline run.
        current_score: Score in the run being reported.
        delta: ``current_score - baseline_score`` (negative = worse).
        is_regression: Whether ``delta`` breached the regression threshold.
    """

    dimension: str
    baseline_score: float = 0.0
    current_score: float = 0.0
    delta: float = 0.0
    is_regression: bool = False

    @classmethod
    def compute(
        cls,
        dimension: str,
        baseline_score: float,
        current_score: float,
        threshold: float = 0.0,
    ) -> "Regression":
        """Build a :class:`Regression`, deriving ``delta`` and the flag.

        Args:
            dimension: Rubric dimension key.
            baseline_score: Baseline value.
            current_score: Current value.
            threshold: Maximum tolerated drop, as a positive number. A drop
                strictly greater than ``threshold`` is a regression.

        Returns:
            A populated :class:`Regression`.
        """
        delta = float(current_score) - float(baseline_score)
        return cls(
            dimension=dimension,
            baseline_score=float(baseline_score),
            current_score=float(current_score),
            delta=delta,
            is_regression=delta < -abs(float(threshold)),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "dimension": self.dimension,
            "baseline_score": self.baseline_score,
            "current_score": self.current_score,
            "delta": self.delta,
            "is_regression": bool(self.is_regression),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Regression":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            dimension=str(data["dimension"]),
            baseline_score=float(data.get("baseline_score", 0.0)),
            current_score=float(data.get("current_score", 0.0)),
            delta=float(data.get("delta", 0.0)),
            is_regression=bool(data.get("is_regression", False)),
        )


# ---------------------------------------------------------------------------
# Stage 4: report
# ---------------------------------------------------------------------------


@dataclass
class RunReport:
    """Top-level artefact written by ``report`` -- one per run.

    Attributes:
        run_id: Unique run identifier (also the run directory name).
        created_at: ISO-8601 UTC timestamp.
        suite_name: Name of the suite that was executed.
        adapter: Adapter used for generation.
        live: ``True`` only when a real agent ran. Reports must state this
            loudly so mock output is never mistaken for a live result.
        aggregates: Per-case aggregates.
        totals: Roll-up counters and headline scores.
        baseline_run_id: Run compared against, if any.
        regressions: Per-dimension baseline comparison.
        schema_version: Version of this contract the report was written with.
    """

    run_id: str
    created_at: str = ""
    suite_name: str = ""
    adapter: str = "unknown"
    live: bool = False
    aggregates: List[CaseAggregate] = field(default_factory=list)
    totals: Dict[str, Any] = field(default_factory=dict)
    baseline_run_id: Optional[str] = None
    regressions: List[Regression] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def case_count(self) -> int:
        """Number of distinct cases in this report."""
        return len(self.aggregates)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "suite_name": self.suite_name,
            "adapter": self.adapter,
            "live": bool(self.live),
            "aggregates": [a.to_dict() for a in self.aggregates],
            "totals": dict(self.totals),
            "baseline_run_id": self.baseline_run_id,
            "regressions": [r.to_dict() for r in self.regressions],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunReport":
        """Build from a mapping; unknown keys are ignored."""
        return cls(
            run_id=str(data["run_id"]),
            created_at=str(data.get("created_at", "")),
            suite_name=str(data.get("suite_name", "")),
            adapter=str(data.get("adapter", "unknown")),
            live=bool(data.get("live", False)),
            aggregates=[
                CaseAggregate.from_dict(a) for a in data.get("aggregates", [])
            ],
            totals=dict(data.get("totals", {})),
            baseline_run_id=data.get("baseline_run_id"),
            regressions=[
                Regression.from_dict(r) for r in data.get("regressions", [])
            ],
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# Suite validation
# ---------------------------------------------------------------------------


def validate_suite(data: Any) -> List[str]:
    """Validate a raw suite mapping and return human-readable errors.

    This never raises and never mutates ``data``. An empty returned list means
    the suite is structurally sound and safe to pass to :meth:`Suite.from_dict`.

    Args:
        data: The decoded suite JSON -- expected to be a ``dict``.

    Returns:
        A list of error strings, each naming the offending path, e.g.
        ``"cases[2].assertions.duration_s: lo (9.0) must be <= hi (4.0)"``.
        Ordered outermost-first so the first line is the most useful.
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        return [f"suite: expected an object, got {type(data).__name__}"]

    for key in ("name", "version"):
        value = data.get(key)
        if value is None:
            errors.append(f"suite.{key}: missing required field")
        elif not isinstance(value, str) or not value.strip():
            errors.append(f"suite.{key}: must be a non-empty string")

    raw_cases = data.get("cases")
    if raw_cases is None:
        errors.append("suite.cases: missing required field")
        return errors
    if not isinstance(raw_cases, list):
        errors.append(
            f"suite.cases: expected a list, got {type(raw_cases).__name__}"
        )
        return errors
    if not raw_cases:
        errors.append("suite.cases: must contain at least one case")

    seen_ids: Dict[str, int] = {}
    for index, case in enumerate(raw_cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(
                f"{where}: expected an object, got {type(case).__name__}"
            )
            continue

        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"{where}.id: must be a non-empty string")
        else:
            if case_id in seen_ids:
                errors.append(
                    f"{where}.id: duplicate id {case_id!r} "
                    f"(first seen at cases[{seen_ids[case_id]}])"
                )
            else:
                seen_ids[case_id] = index
            if any(ch in case_id for ch in "/\\ "):
                errors.append(
                    f"{where}.id: {case_id!r} must not contain '/', '\\\\' "
                    "or spaces -- it is used as a directory name"
                )

        band = case.get("band")
        if band is not None and band not in BANDS:
            errors.append(
                f"{where}.band: {band!r} is not one of {list(BANDS)}"
            )

        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{where}.prompt: must be a non-empty string")

        probes = case.get("probes")
        if probes is not None and not isinstance(probes, list):
            errors.append(
                f"{where}.probes: expected a list, got {type(probes).__name__}"
            )

        errors.extend(_validate_assertions(case.get("assertions"), where))

    return errors


def _validate_assertions(raw: Any, where: str) -> List[str]:
    """Validate one ``assertions`` block. Helper for :func:`validate_suite`."""
    errors: List[str] = []
    if raw is None:
        return errors
    if not isinstance(raw, dict):
        return [
            f"{where}.assertions: expected an object, got {type(raw).__name__}"
        ]

    duration = raw.get("duration_s")
    if duration is not None:
        if not isinstance(duration, (list, tuple)) or len(duration) != 2:
            errors.append(
                f"{where}.assertions.duration_s: expected [lo, hi], "
                f"got {duration!r}"
            )
        else:
            try:
                lo, hi = float(duration[0]), float(duration[1])
            except (TypeError, ValueError):
                errors.append(
                    f"{where}.assertions.duration_s: "
                    f"both bounds must be numbers, got {duration!r}"
                )
            else:
                if lo < 0 or hi < 0:
                    errors.append(
                        f"{where}.assertions.duration_s: "
                        "bounds must be non-negative"
                    )
                if lo > hi:
                    errors.append(
                        f"{where}.assertions.duration_s: "
                        f"lo ({lo}) must be <= hi ({hi})"
                    )

    min_scenes = raw.get("min_scenes")
    if min_scenes is not None:
        if not isinstance(min_scenes, int) or isinstance(min_scenes, bool):
            errors.append(
                f"{where}.assertions.min_scenes: expected an integer, "
                f"got {min_scenes!r}"
            )
        elif min_scenes < 1:
            errors.append(
                f"{where}.assertions.min_scenes: must be >= 1, "
                f"got {min_scenes}"
            )

    aspect = raw.get("aspect_ratio")
    if aspect is not None:
        if not isinstance(aspect, str) or ":" not in aspect:
            errors.append(
                f"{where}.assertions.aspect_ratio: expected 'W:H', "
                f"got {aspect!r}"
            )

    concepts = raw.get("required_concepts")
    if concepts is not None:
        if not isinstance(concepts, list):
            errors.append(
                f"{where}.assertions.required_concepts: expected a list, "
                f"got {type(concepts).__name__}"
            )
        elif not all(isinstance(c, str) for c in concepts):
            errors.append(
                f"{where}.assertions.required_concepts: "
                "every entry must be a string"
            )

    language = raw.get("language")
    if language is not None and (
        not isinstance(language, str) or not language.strip()
    ):
        errors.append(
            f"{where}.assertions.language: expected a language tag string, "
            f"got {language!r}"
        )

    return errors


# ---------------------------------------------------------------------------
# JSON convenience
# ---------------------------------------------------------------------------


def dumps(obj: Any, indent: int = 2) -> str:
    """Serialise any schema dataclass (or list of them) to a JSON string.

    Args:
        obj: A dataclass exposing ``to_dict()``, a sequence of such, or any
            plain JSON-safe value.
        indent: ``json.dumps`` indentation.

    Returns:
        A JSON string with sorted keys, safe to diff between runs.
    """
    return json.dumps(_jsonable(obj), indent=indent, sort_keys=True)


def loads(text: str) -> Any:
    """Parse a JSON string. Thin wrapper so callers need not import ``json``."""
    return json.loads(text)


def _jsonable(obj: Any) -> Any:
    """Recursively convert schema objects / enums into JSON-safe values."""
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj
