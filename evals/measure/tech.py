"""Container and stream measurements via ``ffprobe``.

No bundled binaries. ``ffprobe`` is discovered on PATH; when it is absent every
check reports *unavailable* rather than passing, because a measurement that
did not run must never be mistaken for one that succeeded.

That distinction is load-bearing. ``passed=None`` means "not established" and
is rendered as unverified in reports; ``passed=False`` means "measured and
failed". Conflating them is how a green dashboard ends up meaning nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evals.schema import DimensionScore, Measurement, Severity, Tier

__all__ = ["ProbeResult", "probe", "measure_tech", "ffprobe_path", "DIMENSION"]

DIMENSION = "TECH"
_PROBE_TIMEOUT_S = 60.0


def ffprobe_path() -> Optional[str]:
    """Return the ``ffprobe`` executable, or ``None`` when unavailable."""
    return shutil.which("ffprobe")


@dataclass
class ProbeResult:
    """Parsed ``ffprobe`` output for one media file."""

    ok: bool
    streams: List[Dict[str, Any]]
    format: Dict[str, Any]
    error: Optional[str] = None

    def video_stream(self) -> Optional[Dict[str, Any]]:
        for stream in self.streams:
            if stream.get("codec_type") == "video":
                return stream
        return None

    def audio_stream(self) -> Optional[Dict[str, Any]]:
        for stream in self.streams:
            if stream.get("codec_type") == "audio":
                return stream
        return None

    @property
    def duration_s(self) -> Optional[float]:
        raw = self.format.get("duration")
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None


def probe(video_path: Path | str, timeout_s: float = _PROBE_TIMEOUT_S) -> ProbeResult:
    """Run ``ffprobe`` and parse its JSON output.

    Never raises for a probe failure -- the reason is carried on the result so
    callers can report it as unverified.
    """
    exe = ffprobe_path()
    if exe is None:
        return ProbeResult(False, [], {}, error="ffprobe not found on PATH")

    path = Path(video_path)
    if not path.is_file():
        return ProbeResult(False, [], {}, error=f"file does not exist: {path}")

    argv = [
        exe, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(False, [], {}, error=f"ffprobe timed out after {timeout_s:g}s")
    except OSError as exc:
        return ProbeResult(False, [], {}, error=f"could not run ffprobe: {exc}")

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        return ProbeResult(False, [], {}, error=f"ffprobe failed: {tail}")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return ProbeResult(False, [], {}, error=f"ffprobe output was not JSON: {exc}")

    return ProbeResult(
        ok=True,
        streams=list(data.get("streams") or []),
        format=dict(data.get("format") or {}),
    )


def _parse_fps(raw: Any) -> Optional[float]:
    """Parse an ffprobe rational frame rate such as ``"30000/1001"``."""
    if raw in (None, "", "0/0"):
        return None
    text = str(raw)
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            den = float(denominator)
            if den == 0:
                return None
            return float(numerator) / den
        except (TypeError, ValueError):
            return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _unavailable(name: str, threshold: Any, reason: str, severity: str) -> Measurement:
    """A measurement that could not be taken. Never a pass."""
    return Measurement(
        dimension=name,
        value=None,
        threshold=threshold,
        passed=None,
        severity=severity,
        method="ffprobe",
        evidence=f"unverified: {reason}",
    )


def measure_tech(
    video_path: Optional[Path | str],
    *,
    min_width: int = 1920,
    min_height: int = 1080,
    min_fps: float = 30.0,
    require_audio: bool = True,
    expected_duration_s: Optional[Tuple[float, float]] = None,
) -> DimensionScore:
    """Measure container/stream properties of a produced video."""
    if not video_path:
        return _dimension(
            [
                Measurement(
                    dimension="video_present",
                    value=None,
                    threshold="a video file is produced",
                    passed=False,
                    severity=Severity.CRITICAL.value,
                    method="file",
                    evidence="no video file was produced",
                )
            ],
            notes="no video file",
        )

    result = probe(video_path)
    if not result.ok:
        reason = result.error or "probe failed"
        return _dimension(
            [
                _unavailable("resolution", f">= {min_width}x{min_height}", reason,
                             Severity.CRITICAL.value),
                _unavailable("fps", f">= {min_fps:g}", reason, Severity.MAJOR.value),
                _unavailable("audio_stream_present", "yes", reason,
                             Severity.CRITICAL.value),
            ],
            notes=reason,
        )

    measurements: List[Measurement] = []
    video = result.video_stream()

    # -- resolution --------------------------------------------------------
    if video is None:
        measurements.append(
            Measurement(
                dimension="resolution",
                value=None,
                threshold=f">= {min_width}x{min_height}",
                passed=False,
                severity=Severity.CRITICAL.value,
                method="ffprobe",
                evidence="no video stream in container",
            )
        )
    else:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        measurements.append(
            Measurement(
                dimension="resolution",
                value=f"{width}x{height}",
                threshold=f">= {min_width}x{min_height}",
                passed=width >= min_width and height >= min_height,
                severity=Severity.CRITICAL.value,
                method="ffprobe",
                evidence=f"codec {video.get('codec_name', 'unknown')}, {width}x{height}",
            )
        )

        fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(video.get("r_frame_rate"))
        measurements.append(
            Measurement(
                dimension="fps",
                value=round(fps, 3) if fps is not None else None,
                threshold=f">= {min_fps:g}",
                passed=None if fps is None else fps + 1e-6 >= min_fps,
                severity=Severity.MAJOR.value,
                method="ffprobe",
                evidence=(
                    f"avg_frame_rate={video.get('avg_frame_rate')}"
                    if fps is not None
                    else "unverified: frame rate not reported"
                ),
            )
        )

    # -- audio -------------------------------------------------------------
    audio = result.audio_stream()
    if require_audio:
        measurements.append(
            Measurement(
                dimension="audio_stream_present",
                value=bool(audio),
                threshold="yes",
                passed=bool(audio),
                severity=Severity.CRITICAL.value,
                method="ffprobe",
                evidence=(
                    f"codec {audio.get('codec_name', 'unknown')}"
                    if audio
                    else "no audio stream in container"
                ),
            )
        )

    # -- duration ----------------------------------------------------------
    duration = result.duration_s
    if expected_duration_s is not None:
        lo, hi = expected_duration_s
        measurements.append(
            Measurement(
                dimension="duration_s",
                value=round(duration, 3) if duration is not None else None,
                threshold=f"[{lo:g}, {hi:g}] s",
                passed=None if duration is None else (lo <= duration <= hi),
                severity=Severity.MAJOR.value,
                method="ffprobe",
                evidence=(
                    f"container duration {duration:.2f}s"
                    if duration is not None
                    else "unverified: duration not reported"
                ),
            )
        )
    elif duration is not None:
        measurements.append(
            Measurement(
                dimension="duration_s",
                value=round(duration, 3),
                threshold="> 0",
                passed=duration > 0,
                severity=Severity.MAJOR.value,
                method="ffprobe",
                evidence=f"container duration {duration:.2f}s",
            )
        )

    # -- truncation --------------------------------------------------------
    if duration is not None and audio is not None:
        audio_duration = _stream_duration(audio) or duration
        gap = audio_duration - duration
        measurements.append(
            Measurement(
                dimension="not_truncated",
                value=round(gap, 3),
                threshold="video >= audio (no trailing cut)",
                passed=gap <= 0.5,
                severity=Severity.MAJOR.value,
                method="ffprobe",
                evidence=(
                    f"audio {audio_duration:.2f}s vs container {duration:.2f}s"
                ),
            )
        )

    return _dimension(measurements)


def _stream_duration(stream: Dict[str, Any]) -> Optional[float]:
    raw = stream.get("duration")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _dimension(measurements: Sequence[Measurement], notes: str = "") -> DimensionScore:
    """Fold measurements into a score. Unverified never counts as a pass."""
    decided = [m for m in measurements if m.passed is not None]
    failed = [m for m in decided if not m.passed]
    unverified = [m for m in measurements if m.passed is None]

    worst = Severity.MINOR.value
    for measurement in failed:
        if measurement.severity == Severity.CRITICAL.value:
            worst = Severity.CRITICAL.value
            break
        if measurement.severity == Severity.MAJOR.value:
            worst = Severity.MAJOR.value

    ratio = (len(decided) - len(failed)) / len(decided) if decided else 0.0
    note = notes
    if not note and unverified:
        note = f"{len(unverified)} check(s) could not be verified"
    elif not note and failed:
        note = f"{len(failed)} check(s) failed"

    return DimensionScore(
        dimension=DIMENSION,
        tier=Tier.A.value,
        score_0_100=float(int(ratio * 100)),
        passed=bool(decided) and not failed and not unverified,
        severity=worst if failed else Severity.MINOR.value,
        measurements=list(measurements),
        notes=note,
    )
