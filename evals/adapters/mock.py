"""Fully offline mock adapter.

This is what CI and the acceptance test run on. It must therefore work with no
network, no API keys, no agent runtime and -- if necessary -- no ffmpeg.

Results are *deterministically seeded* from ``(case_id, repeat_idx)``, so the
same suite always yields the same numbers. That determinism is what makes the
mock useful for testing scoring, regression detection and reporting: you can
assert on exact outputs. Repeats vary slightly from one another so that the
variance and flake-rate machinery has something real to chew on.

When ffmpeg happens to be installed the mock renders a genuine (tiny) video, so
the measurement battery gets exercised end to end. When it is not, it writes a
placeholder and the battery degrades gracefully to "unavailable". Either way
the pipeline completes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

from evals.adapters.base import Adapter, InvocationOutcome

__all__ = ["MockAdapter"]

# A case id containing one of these is deliberately made to fail, so the suite
# exercises the failure paths as well as the happy one.
_FAIL_HINTS = ("adversarial", "fail")


class MockAdapter(Adapter):
    """Deterministic offline stand-in for a real agent runtime."""

    name = "mock"
    is_live = False

    def validate(self) -> None:
        """Nothing to validate: the mock works in any environment."""
        return None

    # -- seeding -----------------------------------------------------------

    @staticmethod
    def _seed(case_id: str, repeat_idx: int) -> int:
        """Stable integer seed derived from the unit identity."""
        digest = hashlib.sha256(f"{case_id}#{repeat_idx}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")

    @classmethod
    def _jitter(cls, case_id: str, repeat_idx: int, salt: str, lo: float, hi: float) -> float:
        """Deterministic pseudo-random float in ``[lo, hi)`` for this unit."""
        digest = hashlib.sha256(f"{case_id}#{repeat_idx}#{salt}".encode("utf-8")).digest()
        fraction = int.from_bytes(digest[:6], "big") / float(1 << 48)
        return lo + (hi - lo) * fraction

    # -- step 2 ------------------------------------------------------------

    def invoke(self, prompt_file: Path, workdir: Path) -> InvocationOutcome:
        """Fabricate a plausible agent run without touching the network."""
        case_id, repeat_idx = self._identity(workdir)

        should_fail = (
            any(hint in case_id.lower() for hint in _FAIL_HINTS)
            and repeat_idx % 3 == 2
        )

        wall_time = round(self._jitter(case_id, repeat_idx, "wall", 45.0, 240.0), 2)
        turns = int(self._jitter(case_id, repeat_idx, "turns", 4, 22))
        retries = int(self._jitter(case_id, repeat_idx, "retries", 0, 3))
        cost = round(self._jitter(case_id, repeat_idx, "cost", 0.35, 5.5), 4)

        self._write_artifacts(workdir, case_id, repeat_idx, cost, degraded=should_fail)

        lines = [
            f"[mock] case={case_id} repeat={repeat_idx}",
            f"[mock] prompt file: {prompt_file.name}",
            f"[mock] turns: {turns}",
            f"[mock] retries: {retries}",
            f"[mock] total_cost_usd: {cost}",
        ]
        if should_fail:
            lines.append("[mock] renderer aborted before muxing audio")
            return InvocationOutcome(
                exit_code=1,
                wall_time_s=wall_time,
                stdout="\n".join(lines),
                stderr="mock: simulated render failure",
                error="agent exited 1",
            )

        lines.append("[mock] wrote composition and captions")
        return InvocationOutcome(
            exit_code=0,
            wall_time_s=wall_time,
            stdout="\n".join(lines),
            stderr="",
        )

    # -- artefacts ---------------------------------------------------------

    @staticmethod
    def _identity(workdir: Path) -> Tuple[str, int]:
        """Recover ``(case_id, repeat_idx)`` from the workdir naming convention.

        ``generate`` lays out ``<run>/cases/<case_id>/repeat-<n>/``.
        """
        repeat_idx = 0
        name = workdir.name
        if name.startswith("repeat-"):
            try:
                repeat_idx = int(name.split("-", 1)[1])
            except ValueError:
                repeat_idx = 0
            case_id = workdir.parent.name
        else:
            case_id = name
        return case_id, repeat_idx

    def _write_artifacts(
        self,
        workdir: Path,
        case_id: str,
        repeat_idx: int,
        cost: float,
        degraded: bool,
    ) -> None:
        """Write the stub video, captions, script and spend ledger."""
        workdir.mkdir(parents=True, exist_ok=True)

        duration = round(self._jitter(case_id, repeat_idx, "dur", 22.0, 55.0), 2)
        script_text = self._script_text(case_id)

        (workdir / "narration-script.txt").write_text(script_text, encoding="utf-8")
        (workdir / "captions.srt").write_text(
            self._srt_text(script_text, duration, orphan=degraded), encoding="utf-8"
        )
        (workdir / "ledger.json").write_text(
            json.dumps({"total_usd": cost, "currency": "USD", "mock": True}, indent=2),
            encoding="utf-8",
        )

        if not degraded:
            self._write_video(workdir / "composition.mp4", duration, case_id, repeat_idx)

    def _write_video(self, path: Path, duration: float, case_id: str, repeat_idx: int) -> None:
        """Render a real tiny video when ffmpeg exists, else a placeholder."""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            # Keep it genuinely short; the duration we *claim* lives in the
            # ledger and captions, and measurement reads the real file.
            secs = 2
            width, height = (1920, 1080)
            fps = 30

            # Burn in a subtitle line so the caption-geometry measurement has
            # something real to measure. Without this the mock exercises only
            # half the battery. Placed centre-bottom, inside the geometry
            # thresholds, over a dark scrim for contrast.
            vf = f"testsrc=duration={secs}:size={width}x{height}:rate={fps}"
            font = self._find_font()
            if font:
                caption = "mock caption line for geometry measurement"
                drawtext = (
                    f"drawbox=x=0:y=ih-170:w=iw:h=90:color=black@0.75:t=fill,"
                    f"drawtext=fontfile='{font}':text='{caption}'"
                    f":fontcolor=white:fontsize=44"
                    f":x=(w-text_w)/2:y=h-140"
                )
                vf = f"{vf},{drawtext}"

            argv = [
                ffmpeg, "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", vf,
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
                "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                "-c:a", "aac",
                str(path),
            ]
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=120, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                proc = None
            if proc is not None and proc.returncode == 0 and path.is_file():
                return

        # Placeholder: the battery will report "unavailable", not a false pass.
        path.write_text(
            f"MOCK VIDEO PLACEHOLDER case={case_id} repeat={repeat_idx} "
            f"duration={duration}s (ffmpeg unavailable)\n",
            encoding="utf-8",
        )

    @staticmethod
    def _find_font() -> Optional[str]:
        """Locate a TrueType font for burning in captions.

        Returns ``None`` when none is found, in which case the mock renders
        without captions and the geometry dimension honestly reports that it
        found nothing to measure.
        """
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]
        for path in candidates:
            if Path(path).is_file():
                return path

        fc_match = shutil.which("fc-match")
        if fc_match:
            try:
                proc = subprocess.run(
                    [fc_match, "-f", "%{file}", "sans"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            found = (proc.stdout or "").strip()
            if found and Path(found).is_file():
                return found
        return None

    @staticmethod
    def _script_text(case_id: str) -> str:
        return (
            f"This is a mock narration for {case_id}. "
            "It introduces the subject, walks through the main steps, "
            "and closes with a short summary of what was covered."
        )

    @staticmethod
    def _srt_text(script: str, duration: float, orphan: bool) -> str:
        """Build a valid SRT, optionally with a deliberate orphan cue.

        The orphan variant reproduces the known weakness a naive fidelity
        check misses: a one-word trailing fragment of the previous cue.
        """
        words = script.split()
        chunks: List[str] = []
        size = 8
        for start in range(0, len(words), size):
            chunks.append(" ".join(words[start:start + size]))
        if not chunks:
            chunks = ["(silence)"]

        if orphan and chunks:
            tail = chunks[-1].split()
            if len(tail) > 1:
                chunks[-1] = " ".join(tail[:-1])
                chunks.append(tail[-1])

        per = max(0.7, duration / max(1, len(chunks)))
        lines: List[str] = []
        clock = 0.0
        for index, chunk in enumerate(chunks, start=1):
            end = clock + (0.4 if (orphan and index == len(chunks)) else per)
            lines.append(str(index))
            lines.append(f"{_ts(clock)} --> {_ts(end)}")
            lines.append(chunk)
            lines.append("")
            clock = end
        return "\n".join(lines)


def _ts(seconds: float) -> str:
    """Format seconds as an SRT timestamp ``HH:MM:SS,mmm``."""
    if seconds < 0:
        seconds = 0.0
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
