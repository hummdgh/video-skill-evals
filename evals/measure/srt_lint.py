"""Deterministic subtitle linting.

Pure Python -- no ffmpeg, no third-party parser, no network. Every check here
is exact, which is what earns it authority over a holistic judge opinion: if
this module says a cue lasts 0.4s, that is arithmetic, not an impression.

The orphan-cue check is the one worth understanding. A naive
"is every cue a substring of the script?" fidelity test passes happily when a
caption is split as::

    cue 7:  "and that is why we cache the"
    cue 8:  "response"

Both fragments *are* in the script, so substring fidelity sees nothing wrong.
A viewer sees a one-word flash. This module catches it by looking at cue
duration and at one-word cues that continue the previous line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence

from evals.schema import DimensionScore, Measurement, Severity, Tier

__all__ = ["Cue", "parse_srt", "SrtParseError", "lint_srt", "measure_captions"]

DIMENSION = "CAPTION_CUES"

_TIMECODE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
    r"\s*-->\s*"
    r"(?P<h2>\d{1,2}):(?P<m2>\d{2}):(?P<s2>\d{2})[,.](?P<ms2>\d{1,3})"
)


class SrtParseError(ValueError):
    """Raised when a subtitle file cannot be parsed at all."""


@dataclass
class Cue:
    """One subtitle cue."""

    index: int
    start_s: float
    end_s: float
    text: str

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    @property
    def words(self) -> List[str]:
        return [w for w in re.split(r"\s+", self.text.strip()) if w]

    def normalised(self) -> str:
        """Lowercased, punctuation-stripped text for comparison."""
        return re.sub(r"[^\w\s]", "", self.text.lower()).strip()


def _to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    ms = millis.ljust(3, "0")[:3]
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(ms) / 1000.0


def parse_srt(text: str) -> List[Cue]:
    """Parse SRT (and the common WebVTT shape) into cues.

    Tolerant of blank-line variation, CRLF, a leading BOM and a ``WEBVTT``
    header, because a strict parser that dies on formatting noise would let a
    real subtitle defect escape unmeasured.

    Raises:
        SrtParseError: no cue with a valid timecode could be found.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")

    cues: List[Cue] = []
    blocks = re.split(r"\n\s*\n", normalised.strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if lines[0].strip().upper().startswith("WEBVTT"):
            lines = lines[1:]
            if not lines:
                continue

        timecode_at = None
        for position, line in enumerate(lines):
            if _TIMECODE.search(line):
                timecode_at = position
                break
        if timecode_at is None:
            continue

        match = _TIMECODE.search(lines[timecode_at])
        if match is None:
            continue
        gd = match.groupdict()
        start = _to_seconds(gd["h"], gd["m"], gd["s"], gd["ms"])
        end = _to_seconds(gd["h2"], gd["m2"], gd["s2"], gd["ms2"])

        index = len(cues) + 1
        if timecode_at > 0:
            raw_index = lines[timecode_at - 1].strip()
            if raw_index.isdigit():
                index = int(raw_index)

        body = " ".join(ln.strip() for ln in lines[timecode_at + 1:]).strip()
        cues.append(Cue(index=index, start_s=start, end_s=end, text=body))

    if not cues:
        raise SrtParseError("no cues with a valid timecode were found")
    return cues


def _is_continuation_fragment(cue: Cue, previous: Optional[Cue]) -> bool:
    """True when ``cue`` is a one-word tail of ``previous``.

    This is the orphan pattern: the previous cue was truncated mid-sentence and
    its final word was pushed into a cue of its own.
    """
    if previous is None:
        return False
    if len(cue.words) != 1:
        return False
    word = cue.normalised()
    if not word:
        return False
    # A single short word directly abutting the previous cue, where the
    # previous cue does not end on sentence-final punctuation.
    abuts = (cue.start_s - previous.end_s) <= 0.35
    unterminated = not previous.text.strip().endswith((".", "!", "?", ":", "\u2026"))
    return abuts and unterminated


def lint_srt(
    cues: Sequence[Cue],
    *,
    min_cue_duration_s: float = 0.6,
    script: Optional[str] = None,
) -> List[Measurement]:
    """Run every deterministic cue check and return measurements."""
    measurements: List[Measurement] = []

    # -- short cues --------------------------------------------------------
    short = [c for c in cues if c.duration_s < min_cue_duration_s]
    measurements.append(
        Measurement(
            dimension="caption_cue_min_duration",
            value=round(min((c.duration_s for c in cues), default=0.0), 3),
            threshold=f">= {min_cue_duration_s}s",
            passed=not short,
            severity=Severity.MAJOR.value,
            method="srt-lint",
            evidence=(
                None
                if not short
                else "; ".join(
                    f"cue {c.index} at {c.start_s:.2f}s lasts {c.duration_s:.2f}s: {c.text!r}"
                    for c in short[:5]
                )
            ),
        )
    )

    # -- orphan cues -------------------------------------------------------
    orphans: List[Cue] = []
    for position, cue in enumerate(cues):
        previous = cues[position - 1] if position > 0 else None
        if _is_continuation_fragment(cue, previous):
            orphans.append(cue)
    measurements.append(
        Measurement(
            dimension="caption_orphan_cues",
            value=len(orphans),
            threshold="0",
            passed=not orphans,
            severity=Severity.MAJOR.value,
            method="srt-lint",
            evidence=(
                None
                if not orphans
                else "; ".join(
                    f"cue {c.index} at {c.start_s:.2f}s is the lone word {c.text!r}"
                    for c in orphans[:5]
                )
            ),
        )
    )

    # -- overlaps ----------------------------------------------------------
    overlaps = [
        (a, b)
        for a, b in zip(cues, cues[1:])
        if b.start_s < a.end_s - 1e-6
    ]
    measurements.append(
        Measurement(
            dimension="caption_overlaps",
            value=len(overlaps),
            threshold="0",
            passed=not overlaps,
            severity=Severity.MAJOR.value,
            method="srt-lint",
            evidence=(
                None
                if not overlaps
                else "; ".join(
                    f"cue {a.index} ends {a.end_s:.2f}s but cue {b.index} starts {b.start_s:.2f}s"
                    for a, b in overlaps[:5]
                )
            ),
        )
    )

    # -- monotonic / well-formed ------------------------------------------
    inverted = [c for c in cues if c.end_s < c.start_s]
    measurements.append(
        Measurement(
            dimension="caption_timecodes_valid",
            value=len(inverted),
            threshold="0 inverted cues",
            passed=not inverted,
            severity=Severity.CRITICAL.value,
            method="srt-lint",
            evidence=(
                None
                if not inverted
                else "; ".join(
                    f"cue {c.index}: end {c.end_s:.2f}s precedes start {c.start_s:.2f}s"
                    for c in inverted[:5]
                )
            ),
        )
    )

    # -- verbatim fidelity -------------------------------------------------
    if script is not None:
        haystack = re.sub(r"[^\w\s]", "", script.lower())
        haystack = re.sub(r"\s+", " ", haystack).strip()
        missing: List[Cue] = []
        for cue in cues:
            needle = re.sub(r"\s+", " ", cue.normalised()).strip()
            if needle and needle not in haystack:
                missing.append(cue)
        measurements.append(
            Measurement(
                dimension="caption_verbatim_fidelity",
                value=f"{len(cues) - len(missing)}/{len(cues)} cues found in script",
                threshold="every cue is a substring of the spoken script",
                passed=not missing,
                severity=Severity.MAJOR.value,
                method="substring",
                evidence=(
                    None
                    if not missing
                    else "; ".join(f"cue {c.index}: {c.text!r}" for c in missing[:5])
                ),
            )
        )

    return measurements


def measure_captions(
    srt_path: Optional[Path | str],
    *,
    script_path: Optional[Path | str] = None,
    min_cue_duration_s: float = 0.6,
) -> DimensionScore:
    """Measure subtitle quality for one generation result.

    A missing or unparseable subtitle file is reported as a failure with a
    clear reason -- never as a silent pass.
    """
    if not srt_path:
        return DimensionScore(
            dimension=DIMENSION,
            tier=Tier.A.value,
            score_0_100=0.0,
            passed=False,
            severity=Severity.CRITICAL.value,
            measurements=[
                Measurement(
                    dimension="caption_present",
                    value=None,
                    threshold="a subtitle file is produced",
                    passed=False,
                    severity=Severity.CRITICAL.value,
                    method="file",
                    evidence="no subtitle file was produced",
                )
            ],
            notes="no subtitle file",
        )

    path = Path(srt_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _failed_dimension(f"cannot read {path.name}: {exc}")

    try:
        cues = parse_srt(text)
    except SrtParseError as exc:
        return _failed_dimension(f"{path.name} could not be parsed: {exc}")

    script: Optional[str] = None
    if script_path:
        try:
            script = Path(script_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            script = None

    measurements = lint_srt(
        cues, min_cue_duration_s=min_cue_duration_s, script=script
    )
    measurements.insert(
        0,
        Measurement(
            dimension="caption_cue_count",
            value=len(cues),
            threshold=">= 1",
            passed=len(cues) >= 1,
            severity=Severity.MAJOR.value,
            method="srt-lint",
            evidence=f"parsed {len(cues)} cues from {path.name}",
        ),
    )
    return _score_from(measurements)


def _score_from(measurements: Sequence[Measurement]) -> DimensionScore:
    """Fold measurements into a dimension score. Round DOWN; default FAIL."""
    checked = [m for m in measurements if m.passed is not None]
    passed_all = all(m.passed for m in checked) if checked else False
    failed = [m for m in checked if not m.passed]
    worst = Severity.MINOR.value
    for measurement in failed:
        if measurement.severity == Severity.CRITICAL.value:
            worst = Severity.CRITICAL.value
            break
        if measurement.severity == Severity.MAJOR.value:
            worst = Severity.MAJOR.value
    ratio = (len(checked) - len(failed)) / len(checked) if checked else 0.0
    return DimensionScore(
        dimension=DIMENSION,
        tier=Tier.A.value,
        score_0_100=float(int(ratio * 100)),
        passed=passed_all,
        severity=worst if failed else Severity.MINOR.value,
        measurements=list(measurements),
        notes="" if passed_all else f"{len(failed)} caption check(s) failed",
    )


def _failed_dimension(reason: str) -> DimensionScore:
    return DimensionScore(
        dimension=DIMENSION,
        tier=Tier.A.value,
        score_0_100=0.0,
        passed=False,
        severity=Severity.CRITICAL.value,
        measurements=[
            Measurement(
                dimension="caption_parseable",
                value=None,
                threshold="subtitle file parses",
                passed=False,
                severity=Severity.CRITICAL.value,
                method="srt-lint",
                evidence=reason,
            )
        ],
        notes=reason,
    )
