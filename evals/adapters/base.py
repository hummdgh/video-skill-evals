"""Adapter interface for driving an agent runtime.

The harness does not know which agent runtime a user has installed, and it must
never need to. An adapter is the seam: it turns a :class:`SuiteCase` into a
prompt, runs *something* that produces a video, and reports back a
:class:`GenerationResult`.

Three steps, deliberately separable so a failure can be attributed precisely::

    prepare(case, workdir)            -> prompt file on disk
    invoke(prompt_file, workdir)      -> InvocationOutcome (raw process result)
    collect(case, workdir, outcome)   -> GenerationResult (artefacts + metrics)

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from evals.schema import GenerationResult

__all__ = [
    "InvocationOutcome",
    "Adapter",
    "AdapterError",
    "VIDEO_SUFFIXES",
    "SRT_SUFFIXES",
]

VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")
SRT_SUFFIXES = (".srt", ".vtt")
SCRIPT_NAME_HINTS = ("script", "narration", "voiceover", "vo")


class AdapterError(RuntimeError):
    """Raised when an adapter is misconfigured, before any process runs."""


@dataclass
class InvocationOutcome:
    """Raw result of running the agent runtime once.

    Attributes:
        exit_code: Process exit status, or ``None`` if it never launched.
        wall_time_s: Wall-clock seconds spent.
        stdout: Captured standard output.
        stderr: Captured standard error.
        timed_out: Whether the runtime was killed for exceeding its timeout.
        launched: Whether the process actually started. A false value means an
            *infra* failure -- the agent never got a chance to try.
        error: Human-readable description when something went wrong.
    """

    exit_code: Optional[int] = None
    wall_time_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    launched: bool = True
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.launched and not self.timed_out and self.exit_code == 0

    @property
    def is_infra_failure(self) -> bool:
        """True when the failure is the harness's or the environment's fault.

        Infra failures are excluded from quality aggregates so that a flaky
        API or a missing binary cannot be mistaken for a bad video.
        """
        return (not self.launched) or self.timed_out


class Adapter(ABC):
    """Base class for all runtime adapters."""

    #: Registry key, e.g. ``"generic"``.
    name: str = "base"

    #: Whether this adapter spends real money / touches the network.
    is_live: bool = True

    def __init__(self, config: Any) -> None:
        """Store config and validate eagerly.

        Args:
            config: A ``Config`` instance (duck-typed to keep this module free
                of a circular import).

        Raises:
            AdapterError: the adapter cannot possibly work as configured.
        """
        self.config = config
        self.adapter_config = getattr(config, "adapter", None)
        self.validate()

    def validate(self) -> None:
        """Check configuration. Override to enforce adapter-specific needs."""
        return None

    # -- step 1 ------------------------------------------------------------

    def prepare(self, case: Any, workdir: Path) -> Path:
        """Write the prompt for ``case`` into ``workdir`` and return its path.

        The default renders the case prompt plus its declarative assertions as
        plain instructions, so the agent is told what it will be judged on.
        """
        workdir.mkdir(parents=True, exist_ok=True)
        prompt_file = workdir / "prompt.txt"
        prompt_file.write_text(self.render_prompt(case), encoding="utf-8")
        return prompt_file

    def render_prompt(self, case: Any) -> str:
        """Render the full instruction text handed to the agent."""
        lines: List[str] = [str(getattr(case, "prompt", "")).strip(), ""]
        assertions = getattr(case, "assertions", None)
        if assertions is not None:
            requirements = self._format_assertions(assertions)
            if requirements:
                lines.append("Requirements:")
                lines.extend(f"- {r}" for r in requirements)
                lines.append("")
        lines.append(
            "Produce a finished video file, a subtitle file (.srt) and the "
            "narration script in the output directory."
        )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _format_assertions(assertions: Any) -> List[str]:
        """Turn an ``Assertions`` object into human-readable requirements."""
        out: List[str] = []
        duration = getattr(assertions, "duration_s", None)
        if duration:
            out.append(f"Duration between {duration[0]:g} and {duration[1]:g} seconds.")
        min_scenes = getattr(assertions, "min_scenes", None)
        if min_scenes:
            out.append(f"At least {min_scenes} distinct scenes.")
        aspect = getattr(assertions, "aspect_ratio", None)
        if aspect:
            out.append(f"Aspect ratio {aspect}.")
        concepts = getattr(assertions, "required_concepts", None) or []
        if concepts:
            out.append("The narration must cover: " + ", ".join(str(c) for c in concepts) + ".")
        language = getattr(assertions, "language", None)
        if language:
            out.append(f"Narration language: {language}.")
        return out

    # -- step 2 ------------------------------------------------------------

    @abstractmethod
    def invoke(self, prompt_file: Path, workdir: Path) -> InvocationOutcome:
        """Run the agent runtime once. Must never raise for a runtime failure."""
        raise NotImplementedError

    # -- step 3 ------------------------------------------------------------

    def collect(
        self, case: Any, workdir: Path, outcome: InvocationOutcome
    ) -> GenerationResult:
        """Assemble a :class:`GenerationResult` from what landed in ``workdir``."""
        transcript_path = self._write_transcript(workdir, outcome)
        video = self.find_artifact(workdir, VIDEO_SUFFIXES)
        srt = self.find_artifact(workdir, SRT_SUFFIXES)
        script = self.find_script(workdir)
        metrics = self.parse_metrics(outcome, workdir)

        error = outcome.error
        if error is None and outcome.succeeded and video is None:
            error = "agent exited cleanly but produced no video file"

        return GenerationResult(
            case_id=str(getattr(case, "id", "")),
            repeat_idx=int(getattr(case, "_repeat_idx", 0)),
            workdir=str(workdir),
            video_path=str(video) if video else None,
            srt_path=str(srt) if srt else None,
            script_path=str(script) if script else None,
            transcript_path=str(transcript_path),
            exit_code=outcome.exit_code,
            wall_time_s=outcome.wall_time_s,
            turns=metrics.get("turns"),
            retries=int(metrics.get("retries") or 0),
            cost_usd=metrics.get("cost_usd"),
            adapter=self.name,
            live=self.is_live,
            error=error,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _write_transcript(workdir: Path, outcome: InvocationOutcome) -> Path:
        """Persist stdout/stderr so every claim in a report has a source."""
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / "transcript.txt"
        parts = ["=== stdout ===", outcome.stdout or "", "", "=== stderr ===", outcome.stderr or ""]
        path.write_text("\n".join(parts), encoding="utf-8")
        return path

    @staticmethod
    def find_artifact(workdir: Path, suffixes: Sequence[str]) -> Optional[Path]:
        """Return the largest matching artefact in ``workdir``.

        Largest rather than newest: intermediate renders are usually small
        fragments, and the finished piece is the substantial one.
        """
        candidates: List[Path] = []
        for suffix in suffixes:
            candidates.extend(p for p in workdir.rglob(f"*{suffix}") if p.is_file())
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_size)

    @staticmethod
    def find_script(workdir: Path) -> Optional[Path]:
        """Locate a narration script by conventional filename."""
        for path in sorted(workdir.rglob("*.txt")):
            if path.name in ("prompt.txt", "transcript.txt"):
                continue
            stem = path.stem.lower()
            if any(hint in stem for hint in SCRIPT_NAME_HINTS):
                return path
        return None

    def parse_metrics(
        self, outcome: InvocationOutcome, workdir: Path
    ) -> Dict[str, Optional[float]]:
        """Extract turns / retries / cost from the transcript and any ledger.

        Two sources, in order of trust:

        1. A ``ledger.json`` written by the skill under test, which records
           real API spend.
        2. Regex over the transcript, as a fallback.
        """
        metrics: Dict[str, Optional[float]] = {
            "turns": None,
            "retries": None,
            "cost_usd": None,
        }

        ledger = self._read_ledger(workdir)
        if ledger is not None:
            metrics["cost_usd"] = ledger

        blob = f"{outcome.stdout}\n{outcome.stderr}"
        if metrics["cost_usd"] is None:
            cost = self._search_float(blob, r"(?:total[_ ]?cost|cost[_ ]?usd)\D{0,12}([0-9]+\.?[0-9]*)")
            metrics["cost_usd"] = cost
        turns = self._search_float(blob, r"(?:turns?|steps?)\D{0,12}(\d+)")
        if turns is not None:
            metrics["turns"] = int(turns)
        retries = self._search_float(blob, r"retr(?:y|ies)\D{0,12}(\d+)")
        if retries is not None:
            metrics["retries"] = int(retries)
        return metrics

    @staticmethod
    def _read_ledger(workdir: Path) -> Optional[float]:
        """Sum spend from a ``ledger.json`` if the skill wrote one."""
        for path in workdir.rglob("ledger.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, Mapping):
                for key in ("total_usd", "total_cost_usd", "cost_usd", "total"):
                    value = data.get(key)
                    if isinstance(value, (int, float)):
                        return float(value)
                entries = data.get("entries")
                if isinstance(entries, list):
                    total = 0.0
                    found = False
                    for entry in entries:
                        if isinstance(entry, Mapping):
                            value = entry.get("cost_usd", entry.get("cost"))
                            if isinstance(value, (int, float)):
                                total += float(value)
                                found = True
                    if found:
                        return total
        return None

    @staticmethod
    def _search_float(text: str, pattern: str) -> Optional[float]:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return None
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            return None

    # -- process running ---------------------------------------------------

    def run_command(
        self,
        command: str,
        workdir: Path,
        timeout_s: float,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> InvocationOutcome:
        """Run ``command`` in ``workdir``, capturing everything.

        Never raises for a runtime failure -- the failure is reported in the
        returned :class:`InvocationOutcome` so the caller can classify it.
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return InvocationOutcome(
                launched=False, error=f"could not parse command: {exc}"
            )
        if not argv:
            return InvocationOutcome(launched=False, error="empty command")

        env = dict(os.environ)
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})

        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(workdir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError:
            return InvocationOutcome(
                launched=False,
                wall_time_s=time.monotonic() - started,
                error=f"executable not found: {argv[0]!r}",
            )
        except PermissionError as exc:
            return InvocationOutcome(
                launched=False,
                wall_time_s=time.monotonic() - started,
                error=f"permission denied running {argv[0]!r}: {exc}",
            )
        except subprocess.TimeoutExpired as exc:
            return InvocationOutcome(
                exit_code=None,
                wall_time_s=time.monotonic() - started,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                timed_out=True,
                error=f"timed out after {timeout_s:g}s",
            )
        except OSError as exc:
            return InvocationOutcome(
                launched=False,
                wall_time_s=time.monotonic() - started,
                error=f"could not start {argv[0]!r}: {exc}",
            )

        elapsed = time.monotonic() - started
        error = None if proc.returncode == 0 else f"agent exited {proc.returncode}"
        return InvocationOutcome(
            exit_code=proc.returncode,
            wall_time_s=elapsed,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            error=error,
        )


def _as_text(value: Any) -> str:
    """Coerce subprocess output (bytes or str or None) to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
