"""Pluggable judge backend, strict-JSON validation, and an offline mock.

Design contract for this module:

* A judge backend is anything that can turn ``(prompt, schema, context)`` into
  a **text** response. It is an abstract base class so the runtime is a config
  edit, never a code change.
* Judge output is **strict JSON validated against an explicit expected-keys
  schema** (:class:`ResponseSchema`). Missing keys, wrong value types, unknown
  keys and out-of-domain values are all rejected.
* On malformed output the client retries up to ``max_attempts`` times and then
  records an **unverified** :class:`~evals.schema.Measurement`. It never raises
  out of :meth:`JudgeClient.judge`, and it never silently reports a pass.
* :class:`MockJudgeBackend` is fully offline and deterministic: same seed plus
  same prompt plus same context always yields the same scores. No network, no
  clock, no filesystem.
* :class:`HttpJudgeBackend` builds its request with :mod:`urllib.request` only.
  It defaults to ``offline=True`` and raises :class:`JudgeOfflineError` rather
  than opening a socket, and :func:`make_backend` never constructs it in mock
  mode -- so it cannot be invoked by a mock run.

Standard library only.
"""

from __future__ import annotations

import abc
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..schema import Measurement, Severity

__all__ = [
    "JudgeError",
    "JudgeSchemaError",
    "JudgeTransportError",
    "JudgeOfflineError",
    "ResponseSchema",
    "ARTIFACT_QUALITY_SCHEMA",
    "EXECUTION_REVIEW_SCHEMA",
    "AB_COMPARE_SCHEMA",
    "AESTHETIC_DIMENSIONS",
    "AB_DIMENSIONS",
    "JudgeOutcome",
    "JudgeBackend",
    "MockJudgeBackend",
    "HttpJudgeBackend",
    "JudgeClient",
    "DEFAULT_JUDGE_CONFIG",
    "make_backend",
    "make_client",
    "unverified_measurement",
    "is_unverified",
    "load_prompt",
    "render_prompt",
    "PROMPTS_DIR",
    "UNVERIFIED_PREFIX",
]


#: Prefix stamped on the ``evidence`` of any measurement the judge could not
#: verify. :func:`is_unverified` is the supported way to test for it.
UNVERIFIED_PREFIX: str = "unverified:"

#: Directory holding the markdown prompt templates shipped with this package.
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"

#: Tier-A aesthetic dimensions the artifact-quality judge scores, 0-10.
AESTHETIC_DIMENSIONS: Tuple[str, ...] = (
    "visual_coherence",
    "color_and_light",
    "motion_quality",
    "scene_transitions",
    "narrative_arc",
    "typographic_craft",
    "audio_polish",
)

#: Dimensions tallied by a mirrored blind A/B comparison.
AB_DIMENSIONS: Tuple[str, ...] = (
    "visual_coherence",
    "color_and_light",
    "motion_quality",
    "scene_transitions",
    "narrative_arc",
    "audio_polish",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JudgeError(RuntimeError):
    """Base class for every judge-layer failure."""


class JudgeSchemaError(JudgeError):
    """Judge returned output that is not valid JSON, or violates the schema."""


class JudgeTransportError(JudgeError):
    """A backend could not reach its endpoint, or got an unusable HTTP reply."""


class JudgeOfflineError(JudgeError):
    """A network backend was invoked while running offline.

    Raised by :class:`HttpJudgeBackend` when ``offline`` is set. Its presence
    in a traceback means a mock run tried to reach the network, which is a bug
    in the caller rather than a judge failure.
    """


# ---------------------------------------------------------------------------
# Explicit expected-keys schema
# ---------------------------------------------------------------------------

#: Supported type tokens for :class:`ResponseSchema` field declarations.
_TYPE_TOKENS: Tuple[str, ...] = (
    "string",
    "number",
    "boolean",
    "object",
    "array",
    "number_map",
    "string_map",
)


def _check_type(value: Any, token: str) -> Optional[str]:
    """Return an error string when ``value`` does not match ``token``.

    Args:
        value: Parsed JSON value.
        token: One of :data:`_TYPE_TOKENS`.

    Returns:
        ``None`` when the value is acceptable, else a human-readable reason.
    """
    if token == "string":
        return None if isinstance(value, str) else "expected string"
    if token == "boolean":
        return None if isinstance(value, bool) else "expected boolean"
    if token == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "expected number"
        return None
    if token == "array":
        return None if isinstance(value, list) else "expected array"
    if token == "object":
        return None if isinstance(value, dict) else "expected object"
    if token in ("number_map", "string_map"):
        if not isinstance(value, dict):
            return "expected object"
        for key, item in value.items():
            if not isinstance(key, str):
                return f"key {key!r}: map keys must be strings"
            if token == "number_map":
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    return f"key {key!r}: expected number value"
            elif not isinstance(item, str):
                return f"key {key!r}: expected string value"
        return None
    return f"unknown type token {token!r}"


@dataclass(frozen=True)
class ResponseSchema:
    """An explicit expected-keys contract for one judge response.

    Validation is deliberately strict in both directions: a key the judge
    forgot and a key the judge invented are both errors. A judge that drifts
    off-format is a judge whose numbers we cannot trust.

    Attributes:
        name: Schema identifier, recorded on every :class:`JudgeOutcome`.
        required: Mapping of key name to a token from :data:`_TYPE_TOKENS`.
        optional: Same shape as ``required``; absent keys are not an error.
        value_domain: For ``string``/``string_map`` keys, the closed set of
            values allowed. Anything outside it is a schema violation.
    """

    name: str
    required: Mapping[str, str]
    optional: Mapping[str, str] = field(default_factory=dict)
    value_domain: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    def keys(self) -> Tuple[str, ...]:
        """Return every key this schema recognises, required then optional."""
        return tuple(self.required) + tuple(self.optional)

    def token_for(self, key: str) -> Optional[str]:
        """Return the declared type token for ``key``, or ``None`` if unknown."""
        if key in self.required:
            return self.required[key]
        return self.optional.get(key)

    def describe(self) -> str:
        """Return a compact one-line description, embeddable in a prompt."""
        parts = [f"{k}:{v}" for k, v in self.required.items()]
        parts += [f"{k}?:{v}" for k, v in self.optional.items()]
        return f"{self.name}{{{', '.join(parts)}}}"

    def validate(self, payload: Any) -> Dict[str, Any]:
        """Validate a parsed JSON payload against this schema.

        Args:
            payload: The object produced by :func:`json.loads`.

        Returns:
            The payload as a ``dict``, unchanged, when it fully conforms.

        Raises:
            JudgeSchemaError: With every violation found, not just the first.
        """
        if not isinstance(payload, dict):
            raise JudgeSchemaError(
                f"{self.name}: top-level value must be a JSON object, "
                f"got {type(payload).__name__}"
            )

        problems: List[str] = []

        for key, token in self.required.items():
            if key not in payload:
                problems.append(f"missing required key {key!r}")
                continue
            reason = _check_type(payload[key], token)
            if reason is not None:
                problems.append(f"key {key!r}: {reason}")

        for key, token in self.optional.items():
            if key not in payload:
                continue
            reason = _check_type(payload[key], token)
            if reason is not None:
                problems.append(f"key {key!r}: {reason}")

        known = set(self.keys())
        for key in payload:
            if key not in known:
                problems.append(f"unexpected key {key!r}")

        for key, domain in self.value_domain.items():
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, str):
                if value not in domain:
                    problems.append(
                        f"key {key!r}: {value!r} not in {sorted(domain)}"
                    )
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if sub_value not in domain:
                        problems.append(
                            f"key {key!r}.{sub_key}: {sub_value!r} "
                            f"not in {sorted(domain)}"
                        )

        if problems:
            raise JudgeSchemaError(f"{self.name}: " + "; ".join(sorted(problems)))
        return dict(payload)


#: Tier-A holistic artifact judge. ``scores`` is 0-10 per aesthetic dimension;
#: ``evidence`` must cite a literal timestamp, frame file or measured number.
ARTIFACT_QUALITY_SCHEMA = ResponseSchema(
    name="artifact_quality",
    required={"scores": "number_map", "evidence": "string_map"},
    optional={"notes": "string", "claimed_but_unverified": "array"},
)

#: Tier-B execution judge, reviewing the agent transcript against ``SKILL.md``.
EXECUTION_REVIEW_SCHEMA = ResponseSchema(
    name="execution_review",
    required={
        "skill_adherence": "number",
        "failure_mode_tripped": "boolean",
        "evidence": "string_map",
    },
    optional={"tripped_modes": "array", "notes": "string"},
)

#: One panel of a mirrored blind A/B. Votes name a *slot*, never a candidate,
#: so the judge cannot know which side is the reference.
AB_COMPARE_SCHEMA = ResponseSchema(
    name="ab_compare",
    required={"per_dimension": "string_map", "overall": "string"},
    optional={"evidence": "string_map", "notes": "string"},
    value_domain={
        "per_dimension": ("A", "B", "tie"),
        "overall": ("A", "B", "tie"),
    },
)


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeOutcome:
    """The result of one logical judge call, including every retry.

    Attributes:
        ok: ``True`` only when a response parsed and validated cleanly.
        schema_name: :attr:`ResponseSchema.name` the call was validated against.
        payload: The validated JSON object, or ``None`` when ``ok`` is False.
        attempts: How many backend calls were made, including the successful one.
        raw_responses: Raw text of every attempt, for the evidence trail.
        errors: One message per failed attempt, in order.
        backend: :attr:`JudgeBackend.name` of the backend used.
    """

    ok: bool
    schema_name: str
    payload: Optional[Dict[str, Any]] = None
    attempts: int = 0
    raw_responses: Tuple[str, ...] = ()
    errors: Tuple[str, ...] = ()
    backend: str = "unknown"

    @property
    def method(self) -> str:
        """Provenance string for a :class:`~evals.schema.Measurement`."""
        return f"judge:{self.backend}"

    def evidence(self) -> str:
        """Return a one-line evidence summary suitable for a measurement."""
        if self.ok:
            return f"{self.schema_name} validated on attempt {self.attempts}"
        joined = " | ".join(self.errors) if self.errors else "no attempts made"
        return (
            f"{UNVERIFIED_PREFIX} {self.schema_name} malformed after "
            f"{self.attempts} attempt(s): {joined}"
        )


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class JudgeBackend(abc.ABC):
    """Abstract judge transport.

    A backend turns a rendered prompt into raw response *text*. It performs no
    validation and no retrying -- both belong to :class:`JudgeClient`, so every
    backend behaves identically under malformed output.
    """

    #: Short identifier recorded as measurement provenance, e.g. ``"mock"``.
    name: str = "backend"

    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        schema: ResponseSchema,
        context: Mapping[str, Any],
        *,
        attempt: int = 0,
    ) -> str:
        """Return the judge's raw text response.

        Args:
            prompt: Fully rendered prompt text.
            schema: The response contract the caller will validate against.
                Backends may embed it in the request as a format hint; they
                must not validate against it themselves.
            context: Structured call context (artifact paths, dimension list,
                repeat index...). Deterministic backends key their output off
                this, so it must be JSON-serialisable.
            attempt: Zero-based retry counter, so a backend can vary a
                temperature or re-state the format demand on a retry.

        Returns:
            Raw response text, expected but not guaranteed to be JSON.

        Raises:
            JudgeError: On a transport-level problem. Malformed *content* is
                not an error here -- it is the client's business.
        """
        raise NotImplementedError


def _canonical(context: Mapping[str, Any]) -> str:
    """Return a stable JSON encoding of ``context`` for hashing.

    Values that are not JSON-serialisable fall back to :func:`repr`, so an
    exotic context can never crash a deterministic backend.
    """
    return json.dumps(context, sort_keys=True, default=repr)


class MockJudgeBackend(JudgeBackend):
    """Deterministic, fully offline judge.

    Scores are derived from a SHA-256 of ``(seed, prompt, context, key)``, so
    they are stable across processes and machines, vary when the caller varies
    the context (which is how :mod:`evals.judge.stabilise` gets a spread out of
    repeated calls), and never touch the network, the clock or the filesystem.

    Args:
        seed: Salt mixed into every digest. Change it to get a different but
            equally reproducible universe of scores.
        score_range: Inclusive ``(lo, hi)`` bounds for generated 0-10 scores.
        step: Quantisation of generated scores, e.g. ``0.5``.
        malformed_attempts: Emit this many malformed responses before the first
            well-formed one. Exercises the retry path without a fake network.
    """

    name = "mock"

    def __init__(
        self,
        seed: int = 1729,
        *,
        score_range: Tuple[float, float] = (6.0, 10.0),
        step: float = 0.5,
        malformed_attempts: int = 0,
    ) -> None:
        lo, hi = float(score_range[0]), float(score_range[1])
        if hi < lo:
            raise ValueError(f"score_range must be (lo, hi); got {score_range!r}")
        if step <= 0:
            raise ValueError(f"step must be positive; got {step!r}")
        self.seed = int(seed)
        self.score_range = (lo, hi)
        self.step = float(step)
        self.malformed_attempts = int(malformed_attempts)

    # -- deterministic primitives ------------------------------------------

    def _digest(self, *parts: str) -> int:
        """Return a stable non-negative integer for the given string parts."""
        blob = "|".join([str(self.seed), *parts]).encode("utf-8")
        return int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")

    def _score(self, *parts: str) -> float:
        """Return a quantised pseudo-score inside :attr:`score_range`."""
        lo, hi = self.score_range
        steps = int(round((hi - lo) / self.step)) + 1
        raw = lo + (self._digest(*parts) % steps) * self.step
        return round(min(hi, raw), 4)

    def _pick(self, domain: Sequence[str], *parts: str) -> str:
        """Return a stable choice from ``domain``."""
        return domain[self._digest(*parts) % len(domain)]

    # -- payload synthesis --------------------------------------------------

    def _dimensions(self, context: Mapping[str, Any]) -> Tuple[str, ...]:
        """Return the dimension list to score, falling back to the Tier-A set."""
        raw = context.get("dimensions")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(d) for d in raw)
        return AESTHETIC_DIMENSIONS

    def _value_for(
        self,
        key: str,
        token: str,
        schema: ResponseSchema,
        context: Mapping[str, Any],
        salt: str,
    ) -> Any:
        """Synthesise one schema-conformant value for ``key``."""
        domain = schema.value_domain.get(key)
        dims = self._dimensions(context)

        if token == "number_map":
            return {d: self._score(salt, key, d) for d in dims}
        if token == "string_map":
            if domain:
                return {d: self._pick(domain, salt, key, d) for d in dims}
            return {
                d: (
                    f"frame_{self._digest(salt, key, d) % 900 + 100:04d}.png "
                    f"@ 00:00:{self._digest(salt, 't', d) % 60:02d}.000 "
                    f"(mock evidence for {d})"
                )
                for d in dims
            }
        if token == "number":
            return self._score(salt, key)
        if token == "boolean":
            return bool(self._digest(salt, key) % 4 == 0)
        if token == "string":
            if domain:
                return self._pick(domain, salt, key)
            return f"mock {key} for {context.get('case_id', 'unknown-case')}"
        if token == "array":
            return []
        if token == "object":
            return {}
        raise ValueError(f"cannot synthesise unknown type token {token!r}")

    def complete(
        self,
        prompt: str,
        schema: ResponseSchema,
        context: Mapping[str, Any],
        *,
        attempt: int = 0,
    ) -> str:
        """Return deterministic JSON conforming to ``schema``.

        The first :attr:`malformed_attempts` calls return deliberately broken
        text instead, so retry handling can be tested offline.
        """
        if attempt < self.malformed_attempts:
            return f"Sure! Here is my assessment (attempt {attempt}): not JSON."

        salt = _canonical(context) + "\x00" + prompt
        payload: Dict[str, Any] = {}
        for key, token in schema.required.items():
            payload[key] = self._value_for(key, token, schema, context, salt)
        if "notes" in schema.optional:
            payload["notes"] = (
                f"deterministic mock judgement (seed={self.seed}, "
                f"schema={schema.name})"
            )
        return json.dumps(payload, sort_keys=True)


class HttpJudgeBackend(JudgeBackend):
    """Judge backed by an HTTP endpoint, built on :mod:`urllib.request`.

    This backend defaults to ``offline=True`` and refuses to open a socket in
    that state. :func:`make_backend` only constructs it when the config asks
    for ``backend="http"``, so a mock run can never reach it.

    :meth:`build_request` is pure -- it performs no I/O -- so the request shape
    is unit-testable with no network.

    Args:
        endpoint: Absolute ``http(s)://`` URL to POST to.
        model: Model identifier sent in the request body.
        api_key: Bearer token, or ``None`` for an unauthenticated endpoint.
        timeout_s: Socket timeout in seconds.
        offline: When ``True`` (the default), :meth:`complete` raises
            :class:`JudgeOfflineError` instead of making a request.
    """

    name = "http"

    def __init__(
        self,
        endpoint: str,
        *,
        model: str = "",
        api_key: Optional[str] = None,
        timeout_s: float = 60.0,
        offline: bool = True,
    ) -> None:
        if not endpoint:
            raise ValueError("HttpJudgeBackend requires a non-empty endpoint")
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"endpoint must be http(s); got scheme {parsed.scheme!r}"
            )
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout_s = float(timeout_s)
        self.offline = bool(offline)

    def build_request(
        self,
        prompt: str,
        schema: ResponseSchema,
        context: Mapping[str, Any],
        *,
        attempt: int = 0,
    ) -> urllib.request.Request:
        """Return the request that :meth:`complete` would send. No I/O.

        Args:
            prompt: Rendered prompt text.
            schema: Response contract, sent as an explicit format demand.
            context: Structured call context, forwarded verbatim.
            attempt: Retry counter; a retry restates the JSON-only demand.

        Returns:
            A configured :class:`urllib.request.Request` with a JSON body.
        """
        instruction = (
            "Reply with a single JSON object and nothing else. "
            f"Required keys: {schema.describe()}."
        )
        if attempt > 0:
            instruction += (
                " Your previous reply was not valid JSON for this schema. "
                "Emit JSON only: no prose, no markdown fence."
            )
        body = {
            "model": self.model,
            "prompt": prompt,
            "response_format": {"type": "json_object", "schema": schema.name},
            "expected_keys": sorted(schema.keys()),
            "instruction": instruction,
            "context": dict(context),
            "attempt": attempt,
        }
        data = json.dumps(body, sort_keys=True, default=repr).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return urllib.request.Request(
            self.endpoint, data=data, headers=headers, method="POST"
        )

    @staticmethod
    def extract_text(body: str) -> str:
        """Pull the response text out of a JSON envelope.

        Accepts either a bare JSON object (returned as-is) or an envelope with
        a ``text`` / ``output_text`` / ``content`` field.

        Args:
            body: Decoded HTTP response body.

        Returns:
            The judge's response text.
        """
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(parsed, dict):
            for key in ("text", "output_text", "content"):
                value = parsed.get(key)
                if isinstance(value, str):
                    return value
        return body

    def complete(
        self,
        prompt: str,
        schema: ResponseSchema,
        context: Mapping[str, Any],
        *,
        attempt: int = 0,
    ) -> str:
        """POST the prompt and return the judge's raw text.

        Raises:
            JudgeOfflineError: When this backend is configured offline.
            JudgeTransportError: On any network or decoding failure.
        """
        if self.offline:
            raise JudgeOfflineError(
                "HttpJudgeBackend invoked while offline; mock runs must use "
                "MockJudgeBackend (set judge.backend='http' and offline=False "
                "for a live run)"
            )
        request = self.build_request(prompt, schema, context, attempt=attempt)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise JudgeTransportError(
                f"judge endpoint returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise JudgeTransportError(f"judge endpoint unreachable: {exc}") from exc
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JudgeTransportError("judge response was not valid UTF-8") from exc
        return self.extract_text(body)


# ---------------------------------------------------------------------------
# Client: parsing, retry, unverified fallback
# ---------------------------------------------------------------------------


def _strip_fence(text: str) -> str:
    """Strip a single wrapping markdown code fence, if present.

    Only the fence itself is removed -- the enclosed bytes are passed to
    :func:`json.loads` untouched. Content remains strictly validated; this is
    purely a transport quirk of chat-shaped models.

    Args:
        text: Raw response text.

    Returns:
        The text with an outer ```` ```json ... ``` ```` wrapper removed.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    if not lines[-1].strip().startswith("```"):
        return stripped
    return "\n".join(lines[1:-1]).strip()


def parse_judge_response(text: str, schema: ResponseSchema) -> Dict[str, Any]:
    """Parse and validate one raw judge response.

    Args:
        text: Raw response text from a backend.
        schema: The expected-keys contract.

    Returns:
        The validated payload.

    Raises:
        JudgeSchemaError: On unparseable JSON or any schema violation.
    """
    candidate = _strip_fence(text)
    if not candidate:
        raise JudgeSchemaError(f"{schema.name}: empty response")
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise JudgeSchemaError(
            f"{schema.name}: response is not valid JSON ({exc.msg} "
            f"at line {exc.lineno} col {exc.colno})"
        ) from exc
    return schema.validate(parsed)


def is_unverified(measurement: Measurement) -> bool:
    """Return ``True`` when ``measurement`` was recorded as unverified.

    Args:
        measurement: Any measurement.

    Returns:
        Whether its evidence carries :data:`UNVERIFIED_PREFIX`.
    """
    evidence = measurement.evidence or ""
    return evidence.startswith(UNVERIFIED_PREFIX)


def unverified_measurement(
    outcome: JudgeOutcome,
    dimension: str,
    *,
    severity: str = Severity.MAJOR.value,
) -> Measurement:
    """Build the measurement recorded when the judge never returned valid JSON.

    ``passed`` is ``None``, never ``True``: an unverified judgement is not
    evidence of quality, and it must not silently satisfy a gate. Scoring
    treats a Tier-A CRITICAL dimension with no verified measurement as a gate
    failure under the default-FAIL disposition.

    Args:
        outcome: The failed outcome, used for provenance and evidence.
        dimension: Dimension the judge was asked about.
        severity: Severity to attach; defaults to ``MAJOR``.

    Returns:
        A :class:`~evals.schema.Measurement` flagged unverified.
    """
    return Measurement(
        dimension=dimension,
        value=None,
        threshold=None,
        passed=None,
        severity=severity,
        method=outcome.method,
        evidence=outcome.evidence(),
    )


@dataclass
class JudgeClient:
    """Validation and retry wrapper around a :class:`JudgeBackend`.

    Attributes:
        backend: The transport to call.
        max_attempts: Total attempts per logical call, including the first.
            Must be at least 1.
    """

    backend: JudgeBackend
    max_attempts: int = 3

    def __post_init__(self) -> None:
        """Validate configuration eagerly rather than at first call."""
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be >= 1; got {self.max_attempts!r}"
            )

    def judge(
        self,
        prompt: str,
        schema: ResponseSchema,
        context: Optional[Mapping[str, Any]] = None,
    ) -> JudgeOutcome:
        """Call the backend until it returns schema-valid JSON, or give up.

        Never raises for a malformed or unreachable judge: the failure is
        reported as ``ok=False`` with one message per attempt, so the caller
        can record an unverified measurement and carry on.

        Args:
            prompt: Rendered prompt text.
            schema: Expected-keys contract to validate against.
            context: Structured call context; defaults to empty.

        Returns:
            A :class:`JudgeOutcome`. ``ok`` is ``True`` only on clean
            validation.
        """
        ctx: Mapping[str, Any] = dict(context or {})
        raws: List[str] = []
        errors: List[str] = []

        for attempt in range(self.max_attempts):
            try:
                raw = self.backend.complete(prompt, schema, ctx, attempt=attempt)
            except JudgeError as exc:
                errors.append(f"attempt {attempt + 1}: {exc}")
                continue
            raws.append(raw)
            try:
                payload = parse_judge_response(raw, schema)
            except JudgeSchemaError as exc:
                errors.append(f"attempt {attempt + 1}: {exc}")
                continue
            return JudgeOutcome(
                ok=True,
                schema_name=schema.name,
                payload=payload,
                attempts=attempt + 1,
                raw_responses=tuple(raws),
                errors=tuple(errors),
                backend=self.backend.name,
            )

        return JudgeOutcome(
            ok=False,
            schema_name=schema.name,
            payload=None,
            attempts=self.max_attempts,
            raw_responses=tuple(raws),
            errors=tuple(errors),
            backend=self.backend.name,
        )

    def judge_measurement(
        self,
        prompt: str,
        schema: ResponseSchema,
        dimension: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        severity: str = Severity.MAJOR.value,
    ) -> Tuple[JudgeOutcome, Optional[Measurement]]:
        """Judge once, returning an unverified measurement when it fails.

        Args:
            prompt: Rendered prompt text.
            schema: Expected-keys contract.
            dimension: Dimension name for the fallback measurement.
            context: Structured call context.
            severity: Severity for the fallback measurement.

        Returns:
            ``(outcome, None)`` on success, else ``(outcome, measurement)``
            where the measurement is flagged unverified.
        """
        outcome = self.judge(prompt, schema, context)
        if outcome.ok:
            return outcome, None
        return outcome, unverified_measurement(
            outcome, dimension, severity=severity
        )


# ---------------------------------------------------------------------------
# Config-driven construction
# ---------------------------------------------------------------------------

#: Defaults for the ``judge`` config block. Mock and offline by default: real
#: spend and real sockets are both opt-in.
DEFAULT_JUDGE_CONFIG: Dict[str, Any] = {
    "backend": "mock",
    "seed": 1729,
    "max_attempts": 3,
    "endpoint": "",
    "model": "",
    "api_key": None,
    "timeout_s": 60.0,
    "offline": True,
    "score_range": [6.0, 10.0],
    "step": 0.5,
}


def make_backend(config: Optional[Mapping[str, Any]] = None) -> JudgeBackend:
    """Construct the judge backend named by ``config``.

    In mock mode :class:`HttpJudgeBackend` is never constructed, so it cannot
    be invoked -- the offline guarantee is structural, not a runtime check.

    Args:
        config: A ``judge`` config block; missing keys fall back to
            :data:`DEFAULT_JUDGE_CONFIG`.

    Returns:
        A ready :class:`JudgeBackend`.

    Raises:
        ValueError: When ``backend`` names an unknown transport.
    """
    merged: Dict[str, Any] = dict(DEFAULT_JUDGE_CONFIG)
    merged.update(dict(config or {}))
    kind = str(merged["backend"]).lower()

    if kind == "mock":
        low, high = merged["score_range"]
        return MockJudgeBackend(
            seed=int(merged["seed"]),
            score_range=(float(low), float(high)),
            step=float(merged["step"]),
        )
    if kind == "http":
        return HttpJudgeBackend(
            endpoint=str(merged["endpoint"]),
            model=str(merged["model"]),
            api_key=merged["api_key"],
            timeout_s=float(merged["timeout_s"]),
            offline=bool(merged["offline"]),
        )
    raise ValueError(
        f"unknown judge backend {kind!r}; expected 'mock' or 'http'"
    )


def make_client(config: Optional[Mapping[str, Any]] = None) -> JudgeClient:
    """Construct a :class:`JudgeClient` from a ``judge`` config block.

    Args:
        config: See :func:`make_backend`.

    Returns:
        A client wrapping the configured backend.
    """
    merged: Dict[str, Any] = dict(DEFAULT_JUDGE_CONFIG)
    merged.update(dict(config or {}))
    return JudgeClient(
        backend=make_backend(merged), max_attempts=int(merged["max_attempts"])
    )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def load_prompt(name: str, *, prompts_dir: Optional[Path] = None) -> str:
    """Load a markdown prompt template shipped with this package.

    Args:
        name: Template name, with or without the ``.md`` suffix.
        prompts_dir: Override directory, for tests.

    Returns:
        The template text.

    Raises:
        FileNotFoundError: With the searched path, when the template is absent.
    """
    directory = prompts_dir or PROMPTS_DIR
    filename = name if name.endswith(".md") else f"{name}.md"
    path = directory / filename
    if not path.is_file():
        raise FileNotFoundError(f"judge prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(
    name: str,
    variables: Optional[Mapping[str, Any]] = None,
    *,
    prompts_dir: Optional[Path] = None,
) -> str:
    """Load a template and substitute ``{{placeholder}}`` variables.

    Unknown placeholders are left intact rather than raising, so a template can
    document an optional slot without every caller having to fill it.

    Args:
        name: Template name, with or without the ``.md`` suffix.
        variables: Placeholder values; non-strings are stringified.
        prompts_dir: Override directory, for tests.

    Returns:
        The rendered prompt text.
    """
    text = load_prompt(name, prompts_dir=prompts_dir)
    for key, value in (variables or {}).items():
        text = text.replace("{{" + str(key) + "}}", str(value))
    return text
