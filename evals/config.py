"""Layered configuration for ``video-skill-evals``.

Precedence, lowest to highest::

    built-in defaults  <-  JSON config file  <-  environment  <-  CLI overrides

Everything here is Python standard library only. No third-party imports.

The single most important block is ``adapter``: it is how a user points this
harness at whatever agent runtime they actually have installed. Switching
runtime must always be a config edit, never a code change.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "AdapterConfig",
    "Thresholds",
    "Budget",
    "JudgeConfig",
    "Config",
    "default_config",
    "load_config",
    "validate_config",
]

ENV_PREFIX = "EVALS_"

BANDS = ("simple", "medium", "hard", "adversarial")


# --------------------------------------------------------------------------
# Sub-blocks
# --------------------------------------------------------------------------


@dataclass
class AdapterConfig:
    """How to invoke the agent runtime under test.

    Attributes:
        name: Adapter key -- ``generic`` (default), ``claude-code``,
            ``gemini-cli``, ``antigravity`` or ``mock``.
        command: Command template for the ``generic`` adapter. Supports the
            placeholders ``{prompt_file}``, ``{workdir}`` and ``{output_dir}``.
            Ignored by ``mock``.
        timeout_s: Hard wall-clock ceiling for a single invocation. On expiry
            the run is recorded as an *infra* failure, not a quality failure.
        env: Extra environment variables to inject into the child process.
        options: Free-form adapter-specific settings.
    """

    name: str = "generic"
    command: str = ""
    timeout_s: float = 1800.0
    env: Dict[str, str] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "AdapterConfig":
        data = data or {}
        base = cls()
        return cls(
            name=str(data.get("name", base.name)),
            command=str(data.get("command", base.command)),
            timeout_s=float(data.get("timeout_s", base.timeout_s)),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            options=dict(data.get("options") or {}),
        )


@dataclass
class Thresholds:
    """Tier A measurement thresholds.

    Defaults mirror the prior-art rubric. Do not soften these without
    recording the reason in ``RUBRIC.md``.
    """

    min_width: int = 1920
    min_height: int = 1080
    min_fps: float = 30.0
    require_audio_stream: bool = True

    loudness_target_lufs: float = -14.0
    loudness_tolerance_lufs: float = 1.5
    true_peak_max_dbtp: float = -1.0
    vo_intelligibility_floor_lufs: float = -50.0

    caption_center_x: List[float] = field(default_factory=lambda: [0.45, 0.55])
    caption_center_y_spread_max: float = 0.02
    caption_cap_height: List[float] = field(default_factory=lambda: [0.02, 0.05])
    caption_contrast_min: float = 4.5
    caption_bottom_band_min_y: float = 0.70
    caption_min_cue_duration_s: float = 0.6

    aesthetic_min_per_dimension: float = 8.0
    aesthetic_mean_min: float = 8.7

    sync_max_drift_s: float = 1.5

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Thresholds":
        base = cls()
        if not data:
            return base
        merged = base.to_dict()
        for key, value in data.items():
            if key in merged:
                merged[key] = value
        return cls(**merged)  # type: ignore[arg-type]


@dataclass
class Budget:
    """Per-band cost and latency ceilings (rubric improvement #7)."""

    cost_usd: float
    wall_time_s: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _default_budgets() -> Dict[str, Budget]:
    return {
        "simple": Budget(cost_usd=2.00, wall_time_s=600.0),
        "medium": Budget(cost_usd=4.00, wall_time_s=1200.0),
        "hard": Budget(cost_usd=8.00, wall_time_s=1800.0),
        "adversarial": Budget(cost_usd=8.00, wall_time_s=1800.0),
    }


@dataclass
class JudgeConfig:
    """LLM judge settings.

    ``n_samples`` implements median-of-n stabilisation: a single judge pass on
    aesthetic dimensions swings run to run, so we take the median of several.
    """

    backend: str = "mock"
    model: str = ""
    n_samples: int = 3
    max_retries: int = 3
    temperature: float = 0.0
    endpoint: str = ""
    api_key_env: str = "EVALS_JUDGE_API_KEY"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "JudgeConfig":
        base = cls()
        if not data:
            return base
        merged = base.to_dict()
        for key, value in data.items():
            if key in merged:
                merged[key] = value
        return cls(**merged)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Top-level config
# --------------------------------------------------------------------------


@dataclass
class Config:
    """Fully resolved run configuration."""

    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    budgets: Dict[str, Budget] = field(default_factory=_default_budgets)
    judge: JudgeConfig = field(default_factory=JudgeConfig)

    repeats: int = 3
    live: bool = False
    tier_b_blocking: bool = False
    keep_artifacts: bool = True

    def budget_for(self, band: str) -> Budget:
        """Return the budget for ``band``, falling back to the hardest band."""
        if band in self.budgets:
            return self.budgets[band]
        return self.budgets.get("hard", Budget(cost_usd=8.0, wall_time_s=1800.0))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "adapter": self.adapter.to_dict(),
            "thresholds": self.thresholds.to_dict(),
            "budgets": {k: v.to_dict() for k, v in self.budgets.items()},
            "judge": self.judge.to_dict(),
            "repeats": self.repeats,
            "live": self.live,
            "tier_b_blocking": self.tier_b_blocking,
            "keep_artifacts": self.keep_artifacts,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "Config":
        data = data or {}
        budgets = _default_budgets()
        for band, raw in (data.get("budgets") or {}).items():
            if isinstance(raw, Mapping):
                budgets[str(band)] = Budget(
                    cost_usd=float(raw.get("cost_usd", 8.0)),
                    wall_time_s=float(raw.get("wall_time_s", 1800.0)),
                )
        return cls(
            adapter=AdapterConfig.from_dict(data.get("adapter")),
            thresholds=Thresholds.from_dict(data.get("thresholds")),
            budgets=budgets,
            judge=JudgeConfig.from_dict(data.get("judge")),
            repeats=int(data.get("repeats", 3)),
            live=bool(data.get("live", False)),
            tier_b_blocking=bool(data.get("tier_b_blocking", False)),
            keep_artifacts=bool(data.get("keep_artifacts", True)),
        )


def default_config() -> Config:
    """Return the built-in defaults: mock adapter, mock judge, 3 repeats."""
    cfg = Config()
    cfg.adapter.name = "generic"
    cfg.adapter.command = ""
    return cfg


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _deep_merge(base: Dict[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``, returning a new dict."""
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce_env_value(raw: str) -> Any:
    """Interpret an environment string as JSON when possible, else as text."""
    lowered = raw.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _env_overrides(environ: Mapping[str, str]) -> Dict[str, Any]:
    """Build an override mapping from ``EVALS_*`` environment variables.

    ``EVALS_REPEATS=5`` sets ``repeats``. Nested keys use a double
    underscore: ``EVALS_ADAPTER__NAME=mock`` sets ``adapter.name``.
    """
    overrides: Dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX) or key == "EVALS_JUDGE_API_KEY":
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")
        cursor = overrides
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):
                break
        else:
            cursor[path[-1]] = _coerce_env_value(raw)
    return overrides


def load_config(
    path: Optional[Path | str] = None,
    overrides: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Config:
    """Resolve configuration from all layers.

    Args:
        path: Optional JSON config file. Missing file raises ``FileNotFoundError``;
            an unreadable or malformed one raises ``ValueError``.
        overrides: Highest-precedence mapping, normally built from CLI flags.
        environ: Environment mapping; defaults to ``os.environ``.

    Returns:
        A fully resolved :class:`Config`.

    Raises:
        FileNotFoundError: ``path`` was given but does not exist.
        ValueError: the config file is not valid JSON or not an object.
    """
    merged: Dict[str, Any] = default_config().to_dict()

    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.is_file():
            raise FileNotFoundError(f"config file not found: {cfg_path}")
        try:
            text = cfg_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read config file {cfg_path}: {exc}") from exc
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"config file {cfg_path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"config file {cfg_path} must contain a JSON object")
        merged = _deep_merge(merged, parsed)

    merged = _deep_merge(merged, _env_overrides(os.environ if environ is None else environ))

    if overrides:
        merged = _deep_merge(merged, {k: v for k, v in overrides.items() if v is not None})

    return Config.from_dict(merged)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

KNOWN_ADAPTERS = ("generic", "claude-code", "gemini-cli", "antigravity", "mock")


def validate_config(cfg: Config) -> List[str]:
    """Return a list of human-readable configuration errors.

    An empty list means the configuration is usable. This never raises --
    callers decide whether to warn or abort.
    """
    errors: List[str] = []

    if cfg.adapter.name not in KNOWN_ADAPTERS:
        errors.append(
            f"adapter.name: {cfg.adapter.name!r} is not one of {', '.join(KNOWN_ADAPTERS)}"
        )
    if cfg.adapter.timeout_s <= 0:
        errors.append("adapter.timeout_s: must be greater than zero")

    # A live run through the generic adapter is meaningless without a command.
    if cfg.live and cfg.adapter.name == "generic" and not cfg.adapter.command.strip():
        errors.append(
            "adapter.command: required for a --live run with the generic adapter "
            "(set a command template using {prompt_file}, {workdir}, {output_dir})"
        )
    if cfg.adapter.command:
        if "{prompt_file}" not in cfg.adapter.command:
            errors.append("adapter.command: must contain the {prompt_file} placeholder")
        if "{workdir}" not in cfg.adapter.command and "{output_dir}" not in cfg.adapter.command:
            errors.append(
                "adapter.command: must contain {workdir} or {output_dir} so the "
                "runtime knows where to write its artefacts"
            )

    if cfg.repeats < 1:
        errors.append("repeats: must be at least 1")

    if cfg.judge.n_samples < 1:
        errors.append("judge.n_samples: must be at least 1")
    if cfg.judge.n_samples % 2 == 0:
        errors.append(
            f"judge.n_samples: {cfg.judge.n_samples} is even; an odd number gives "
            "an unambiguous median"
        )
    if cfg.judge.max_retries < 0:
        errors.append("judge.max_retries: cannot be negative")
    if cfg.judge.backend not in ("mock", "http"):
        errors.append(f"judge.backend: {cfg.judge.backend!r} is not one of mock, http")
    if cfg.judge.backend == "http" and not cfg.judge.endpoint.strip():
        errors.append("judge.endpoint: required when judge.backend is 'http'")

    t = cfg.thresholds
    if len(t.caption_center_x) != 2 or t.caption_center_x[0] >= t.caption_center_x[1]:
        errors.append("thresholds.caption_center_x: must be [lo, hi] with lo < hi")
    if len(t.caption_cap_height) != 2 or t.caption_cap_height[0] >= t.caption_cap_height[1]:
        errors.append("thresholds.caption_cap_height: must be [lo, hi] with lo < hi")
    if not 0.0 <= t.caption_bottom_band_min_y < 1.0:
        errors.append("thresholds.caption_bottom_band_min_y: must be in [0, 1)")
    if t.loudness_tolerance_lufs <= 0:
        errors.append("thresholds.loudness_tolerance_lufs: must be greater than zero")
    if t.min_fps <= 0:
        errors.append("thresholds.min_fps: must be greater than zero")

    for band, budget in cfg.budgets.items():
        if budget.cost_usd < 0:
            errors.append(f"budgets.{band}.cost_usd: cannot be negative")
        if budget.wall_time_s <= 0:
            errors.append(f"budgets.{band}.wall_time_s: must be greater than zero")
    for band in BANDS:
        if band not in cfg.budgets:
            errors.append(f"budgets.{band}: missing budget for a standard band")

    return errors
