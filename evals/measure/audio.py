"""Loudness measurement via ffmpeg's ``ebur128`` filter.

Broadcast loudness is one of the few things about a video that is genuinely
objective, which is why it is measured here rather than asked of a judge. The
filter is run in analysis-only mode (``-f null``) so nothing is re-encoded.

As elsewhere in the battery, an unavailable measurement is reported as
``passed=None`` (unverified) and never as a pass.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from evals.schema import DimensionScore, Measurement, Severity, Tier

__all__ = ["LoudnessResult", "analyse_loudness", "measure_audio", "DIMENSION"]

DIMENSION = "AUDIO"
_ANALYSIS_TIMEOUT_S = 300.0

# ebur128 prints a trailing summary block:
#   Integrated loudness:
#     I:         -14.2 LUFS
#     Threshold: -24.9 LUFS
#   True peak:
#     Peak:       -1.4 dBFS
_RE_INTEGRATED = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)
_RE_RANGE = re.compile(r"^\s*LRA:\s*(-?\d+(?:\.\d+)?)\s*LU", re.MULTILINE)
_RE_PEAK = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS", re.MULTILINE)


@dataclass
class LoudnessResult:
    """Parsed ebur128 summary."""

    ok: bool
    integrated_lufs: Optional[float] = None
    loudness_range_lu: Optional[float] = None
    true_peak_dbtp: Optional[float] = None
    error: Optional[str] = None

    @property
    def is_silent(self) -> bool:
        """True when the track is silent or effectively so."""
        return self.integrated_lufs is not None and self.integrated_lufs <= -70.0


def analyse_loudness(
    video_path: Path | str, timeout_s: float = _ANALYSIS_TIMEOUT_S
) -> LoudnessResult:
    """Run ``ffmpeg -af ebur128`` and parse the summary.

    Never raises for an analysis failure; the reason is carried on the result.
    """
    exe = shutil.which("ffmpeg")
    if exe is None:
        return LoudnessResult(False, error="ffmpeg not found on PATH")

    path = Path(video_path)
    if not path.is_file():
        return LoudnessResult(False, error=f"file does not exist: {path}")

    argv = [
        exe, "-nostdin", "-hide_banner", "-i", str(path),
        "-af", "ebur128=peak=true", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return LoudnessResult(False, error=f"ebur128 timed out after {timeout_s:g}s")
    except OSError as exc:
        return LoudnessResult(False, error=f"could not run ffmpeg: {exc}")

    # ebur128 writes its report to stderr.
    blob = proc.stderr or ""
    if "Audio: " not in blob and "Stream #" in blob and "ebur128" not in blob:
        return LoudnessResult(False, error="no audio stream to analyse")

    integrated = _last_float(_RE_INTEGRATED, blob)
    if integrated is None:
        detail = (blob.strip().splitlines() or ["no ebur128 summary"])[-1]
        return LoudnessResult(False, error=f"could not parse ebur128 output: {detail[:120]}")

    return LoudnessResult(
        ok=True,
        integrated_lufs=integrated,
        loudness_range_lu=_last_float(_RE_RANGE, blob),
        true_peak_dbtp=_last_float(_RE_PEAK, blob),
    )


def _last_float(pattern: re.Pattern[str], text: str) -> Optional[float]:
    """Return the last match, which is the final summary rather than a tick."""
    matches = pattern.findall(text)
    if not matches:
        return None
    raw = matches[-1]
    if raw == "-inf":
        return float("-inf")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _unverified(name: str, threshold: str, reason: str, severity: str) -> Measurement:
    return Measurement(
        dimension=name,
        value=None,
        threshold=threshold,
        passed=None,
        severity=severity,
        method="ebur128",
        evidence=f"unverified: {reason}",
    )


def measure_audio(
    video_path: Optional[Path | str],
    *,
    target_lufs: float = -14.0,
    tolerance_lufs: float = 1.5,
    true_peak_max_dbtp: float = -1.0,
    intelligibility_floor_lufs: float = -50.0,
) -> DimensionScore:
    """Measure integrated loudness, true peak and a voice-intelligibility floor."""
    lo = target_lufs - tolerance_lufs
    hi = target_lufs + tolerance_lufs
    window = f"{target_lufs:g} +/- {tolerance_lufs:g} LUFS"
    peak_threshold = f"<= {true_peak_max_dbtp:g} dBTP"
    floor_threshold = f"> {intelligibility_floor_lufs:g} LUFS"

    if not video_path:
        return _dimension(
            [
                _unverified("integrated_loudness_lufs", window, "no video file", Severity.MAJOR.value),
                _unverified("true_peak_dbtp", peak_threshold, "no video file", Severity.MAJOR.value),
                _unverified("vo_intelligibility", floor_threshold, "no video file",
                            Severity.CRITICAL.value),
            ],
            notes="no video file",
        )

    result = analyse_loudness(video_path)
    if not result.ok:
        reason = result.error or "analysis failed"
        return _dimension(
            [
                _unverified("integrated_loudness_lufs", window, reason, Severity.MAJOR.value),
                _unverified("true_peak_dbtp", peak_threshold, reason, Severity.MAJOR.value),
                _unverified("vo_intelligibility", floor_threshold, reason,
                            Severity.CRITICAL.value),
            ],
            notes=reason,
        )

    measurements: List[Measurement] = []
    integrated = result.integrated_lufs

    measurements.append(
        Measurement(
            dimension="integrated_loudness_lufs",
            value=None if integrated is None else round(integrated, 2),
            threshold=window,
            passed=None if integrated is None else (lo <= integrated <= hi),
            severity=Severity.MAJOR.value,
            method="ebur128",
            evidence=(
                f"integrated {integrated:.2f} LUFS, target window [{lo:g}, {hi:g}]"
                if integrated is not None
                else "unverified: integrated loudness not reported"
            ),
        )
    )

    peak = result.true_peak_dbtp
    measurements.append(
        Measurement(
            dimension="true_peak_dbtp",
            value=None if peak is None else round(peak, 2),
            threshold=peak_threshold,
            passed=None if peak is None else peak <= true_peak_max_dbtp,
            severity=Severity.MAJOR.value,
            method="ebur128",
            evidence=(
                f"true peak {peak:.2f} dBFS"
                if peak is not None
                else "unverified: true peak not reported"
            ),
        )
    )

    # Intelligibility floor: a track this quiet cannot carry a voiceover.
    measurements.append(
        Measurement(
            dimension="vo_intelligibility",
            value=None if integrated is None else round(integrated, 2),
            threshold=floor_threshold,
            passed=None if integrated is None else integrated > intelligibility_floor_lufs,
            severity=Severity.CRITICAL.value,
            method="ebur128",
            evidence=(
                "silent or near-silent track"
                if result.is_silent
                else f"integrated {integrated:.2f} LUFS is above the intelligibility floor"
                if integrated is not None
                else "unverified: loudness not reported"
            ),
        )
    )

    if result.loudness_range_lu is not None:
        measurements.append(
            Measurement(
                dimension="loudness_range_lu",
                value=round(result.loudness_range_lu, 2),
                threshold="informational",
                passed=None,
                severity=Severity.MINOR.value,
                method="ebur128",
                evidence=f"loudness range {result.loudness_range_lu:.2f} LU",
            )
        )

    return _dimension(measurements)


def _dimension(measurements: Sequence[Measurement], notes: str = "") -> DimensionScore:
    """Fold measurements into a score. Unverified never counts as a pass."""
    decided = [m for m in measurements if m.passed is not None]
    failed = [m for m in decided if not m.passed]
    unverified = [
        m for m in measurements
        if m.passed is None and m.threshold != "informational"
    ]

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
