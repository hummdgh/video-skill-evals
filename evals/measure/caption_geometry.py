"""Pixel measurement of burned-in captions.

The rubric places four CRITICAL/MAJOR thresholds on caption placement, and all
four have to be settled by looking at actual pixels. A judge asked "are the
subtitles centred?" will confidently say yes about a video where they are not.

No numpy, no PIL. ffmpeg converts frames to **PGM** (`P5`), a format whose
entire specification is a short ASCII header followed by raw bytes, so it can
be parsed in a dozen lines of standard library.

## The bottom-band filter

Measurement is restricted to the lower part of the frame (`center_y > 0.7` by
default). This is inherited from the prior art and it matters: intro and outro
title cards sit mid-frame, and including them inflates the vertical spread so
that a video with perfectly placed subtitles fails. The filter is the
difference between measuring subtitles and measuring "any text anywhere".

## What "no captions found" means

If no caption-like pixels are found in the band, that is reported as
**unverified**, not as a pass and not as a failure. The video may legitimately
have no burned-in subtitles, or the detector may have missed them; either way
the honest answer is that geometry was not established.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from evals.schema import DimensionScore, Measurement, Severity, Tier

__all__ = [
    "GrayFrame",
    "parse_pgm",
    "extract_gray_frames",
    "CaptionBox",
    "find_caption_box",
    "measure_caption_geometry",
    "DIMENSION",
]

DIMENSION = "CAPTION_GEOMETRY"

_EXTRACT_TIMEOUT_S = 180.0

#: Where to put the bright/dark cut within the band's dynamic range. Keyed off
#: the range rather than a percentile: a subtitle line covers a tiny fraction
#: of the band, so any percentile cut sits below the text on a dark background.
_BRIGHT_FRACTION = 0.55

#: A row qualifies as a text row when at least this fraction of its pixels are
#: bright. Tuned low because a subtitle line is mostly background.
_MIN_ROW_COVERAGE = 0.004

#: Upper bound on a plausible caption line height, as a fraction of the frame.
#: Used to reject contiguous bright runs that are background rather than text.
#: Deliberately generous -- roughly twice the rubric's 5% MAJOR threshold -- so
#: an oversized caption is still *measured* and fails on its merits, rather
#: than being discarded here and reported as "not found".
_MAX_PLAUSIBLE_CAP_HEIGHT = 0.11


@dataclass
class GrayFrame:
    """One 8-bit greyscale frame."""

    width: int
    height: int
    pixels: bytes

    def at(self, x: int, y: int) -> int:
        return self.pixels[y * self.width + x]

    def row(self, y: int) -> memoryview:
        start = y * self.width
        return memoryview(self.pixels)[start:start + self.width]


class PgmError(ValueError):
    """Raised when a PGM file cannot be parsed."""


def parse_pgm(data: bytes) -> GrayFrame:
    """Parse a binary PGM (``P5``).

    Handles comment lines and arbitrary whitespace between header fields, per
    the format specification.

    Raises:
        PgmError: not a P5 file, or the payload is short.
    """
    if not data.startswith(b"P5"):
        raise PgmError("not a binary PGM (expected magic 'P5')")

    fields: List[int] = []
    index = 2
    while len(fields) < 3:
        while index < len(data) and data[index:index + 1].isspace():
            index += 1
        if index < len(data) and data[index:index + 1] == b"#":
            while index < len(data) and data[index] != 0x0A:
                index += 1
            continue
        start = index
        while index < len(data) and not data[index:index + 1].isspace():
            index += 1
        token = data[start:index]
        if not token.isdigit():
            raise PgmError(f"malformed PGM header near byte {start}")
        fields.append(int(token))
    index += 1  # single whitespace byte after maxval

    width, height, maxval = fields
    if maxval > 255:
        raise PgmError("16-bit PGM is not supported")
    expected = width * height
    payload = data[index:index + expected]
    if len(payload) < expected:
        raise PgmError(
            f"PGM payload truncated: expected {expected} bytes, got {len(payload)}"
        )
    return GrayFrame(width=width, height=height, pixels=payload)


def extract_gray_frames(
    video_path: Path | str,
    *,
    count: int = 12,
    out_dir: Optional[Path] = None,
    timeout_s: float = _EXTRACT_TIMEOUT_S,
) -> List[GrayFrame]:
    """Sample greyscale frames evenly across the video.

    Returns an empty list when ffmpeg is unavailable or the video is unreadable;
    the caller reports that as unverified.
    """
    source = Path(video_path)
    if not source.is_file():
        return []
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return []

    created = out_dir is None
    target = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="capgeo-"))
    target.mkdir(parents=True, exist_ok=True)

    pattern = str(target / "g-%04d.pgm")
    argv = [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error",
        "-i", str(source),
        "-vf", f"select='not(mod(n\\,{max(1, count)}))',format=gray",
        "-vsync", "0", "-frames:v", str(count),
        "-f", "image2", pattern,
    ]
    try:
        subprocess.run(argv, capture_output=True, text=True,
                       timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []

    frames: List[GrayFrame] = []
    for path in sorted(target.glob("g-*.pgm")):
        try:
            frames.append(parse_pgm(path.read_bytes()))
        except (PgmError, OSError):
            continue
    return frames


@dataclass
class CaptionBox:
    """Bounding box of caption pixels within one frame, in absolute pixels."""

    x0: int
    y0: int
    x1: int
    y1: int
    frame_width: int
    frame_height: int
    text_luminance: float
    background_luminance: float

    @property
    def center_x(self) -> float:
        return ((self.x0 + self.x1) / 2.0) / self.frame_width

    @property
    def center_y(self) -> float:
        return ((self.y0 + self.y1) / 2.0) / self.frame_height

    @property
    def cap_height(self) -> float:
        """Box height as a fraction of the full frame height."""
        return (self.y1 - self.y0 + 1) / self.frame_height

    @property
    def contrast_ratio(self) -> float:
        """WCAG contrast ratio between text and its local background."""
        lighter = max(self.text_luminance, self.background_luminance)
        darker = min(self.text_luminance, self.background_luminance)
        return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(value8: float) -> float:
    """sRGB 8-bit grey to WCAG relative luminance."""
    c = max(0.0, min(1.0, value8 / 255.0))
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def find_caption_box(
    frame: GrayFrame, *, band_min_y: float = 0.70
) -> Optional[CaptionBox]:
    """Locate caption pixels in the lower band of one frame.

    Captions are burned in as bright glyphs, usually over a darker plate or a
    scrim. The detector finds rows in the band whose bright-pixel coverage
    stands out, then bounds the bright pixels within those rows.

    Returns ``None`` when nothing caption-like is present.
    """
    height, width = frame.height, frame.width
    if height < 10 or width < 10:
        return None

    y_start = int(height * band_min_y)
    if y_start >= height - 1:
        return None

    band_rows = range(y_start, height)

    # Brightness threshold from the band's own distribution, so the detector
    # adapts to a dark scrim or a bright background rather than assuming one.
    sample: List[int] = []
    step = max(1, width // 160)
    for y in band_rows:
        row = frame.row(y)
        sample.extend(row[x] for x in range(0, width, step))
    if not sample:
        return None
    sample.sort()
    background = sample[len(sample) // 2]
    peak = sample[-1]

    # Key the cut off the band's dynamic range, not a fixed quantile. A
    # subtitle line covers a very small fraction of the band, so a percentile
    # cut sits *below* the text on any dark background and finds nothing.
    if peak - background < 25:
        # No meaningful bright/dark separation: nothing that looks like text.
        return None
    bright_cut = background + int((peak - background) * _BRIGHT_FRACTION)

    text_rows: List[int] = []
    for y in band_rows:
        row = frame.row(y)
        bright = sum(1 for x in range(0, width, step) if row[x] >= bright_cut)
        coverage = bright / max(1, len(range(0, width, step)))
        if coverage >= _MIN_ROW_COVERAGE:
            text_rows.append(y)
    if not text_rows:
        return None

    # Take the largest *contiguous* run of qualifying rows, not min/max across
    # all of them. On a busy background, scattered bright pattern rows also
    # qualify, and spanning them inflates the box from a caption line to most
    # of the band. A subtitle is one contiguous horizontal strip.
    runs: List[List[int]] = []
    current: List[int] = [text_rows[0]]
    for row_y in text_rows[1:]:
        if row_y - current[-1] <= 2:  # tolerate 1-row gaps within a glyph
            current.append(row_y)
        else:
            runs.append(current)
            current = [row_y]
    runs.append(current)

    # Among contiguous runs, prefer ones of plausible caption height. A
    # subtitle line is a few percent of the frame; a run spanning a third of it
    # is background that happens to be bright, not text. Falling back to the
    # longest run keeps a result when nothing is plausible, and the height
    # threshold then reports the failure honestly.
    max_plausible = max(2, int(height * _MAX_PLAUSIBLE_CAP_HEIGHT))
    plausible = [r for r in runs if (r[-1] - r[0] + 1) <= max_plausible]
    chosen = max(plausible or runs, key=len)
    y0, y1 = chosen[0], chosen[-1]
    text_rows = chosen

    x0, x1 = width, -1
    text_values: List[int] = []
    for y in text_rows:
        row = frame.row(y)
        for x in range(width):
            if row[x] >= bright_cut:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                text_values.append(row[x])
    if x1 < x0 or not text_values:
        return None

    text_mean = sum(text_values) / len(text_values)
    return CaptionBox(
        x0=x0, y0=y0, x1=x1, y1=y1,
        frame_width=width, frame_height=height,
        text_luminance=_relative_luminance(text_mean),
        background_luminance=_relative_luminance(background),
    )


def _unverified(name: str, threshold: str, reason: str, severity: str) -> Measurement:
    return Measurement(
        dimension=name,
        value=None,
        threshold=threshold,
        passed=None,
        severity=severity,
        method="pixel",
        evidence=f"unverified: {reason}",
    )


def measure_caption_geometry(
    video_path: Optional[Path | str],
    *,
    center_x_range: Sequence[float] = (0.45, 0.55),
    center_y_spread_max: float = 0.02,
    cap_height_range: Sequence[float] = (0.02, 0.05),
    contrast_min: float = 4.5,
    band_min_y: float = 0.70,
    frame_count: int = 12,
) -> DimensionScore:
    """Measure caption placement, size and contrast across sampled frames."""
    thresholds = {
        "caption_center_x": f"in [{center_x_range[0]:g}, {center_x_range[1]:g}]",
        "caption_center_y_spread": f"<= {center_y_spread_max:g}",
        "caption_cap_height": f"in [{cap_height_range[0]:g}, {cap_height_range[1]:g}]",
        "caption_contrast": f">= {contrast_min:g}:1",
    }
    severities = {
        "caption_center_x": Severity.CRITICAL.value,
        "caption_center_y_spread": Severity.CRITICAL.value,
        "caption_cap_height": Severity.MAJOR.value,
        "caption_contrast": Severity.CRITICAL.value,
    }

    def all_unverified(reason: str) -> DimensionScore:
        return _fold(
            [
                _unverified(name, thresholds[name], reason, severities[name])
                for name in thresholds
            ],
            notes=reason,
        )

    if not video_path:
        return all_unverified("no video file was produced")
    if shutil.which("ffmpeg") is None:
        return all_unverified("ffmpeg not found on PATH")

    frames = extract_gray_frames(video_path, count=frame_count)
    if not frames:
        return all_unverified("no frames could be extracted from the video")

    boxes = [
        box for box in (find_caption_box(f, band_min_y=band_min_y) for f in frames)
        if box is not None
    ]
    if not boxes:
        return all_unverified(
            f"no caption-like pixels found in the bottom band "
            f"(y > {band_min_y:g}) across {len(frames)} sampled frame(s); "
            "the video may have no burned-in subtitles"
        )

    centers_x = [b.center_x for b in boxes]
    centers_y = [b.center_y for b in boxes]
    heights = [b.cap_height for b in boxes]
    contrasts = [b.contrast_ratio for b in boxes]

    mean_x = sum(centers_x) / len(centers_x)
    spread_y = max(centers_y) - min(centers_y)
    mean_h = sum(heights) / len(heights)
    min_contrast = min(contrasts)

    detected = f"{len(boxes)}/{len(frames)} frame(s) had captions"

    measurements = [
        Measurement(
            dimension="caption_center_x",
            value=round(mean_x, 4),
            threshold=thresholds["caption_center_x"],
            passed=center_x_range[0] <= mean_x <= center_x_range[1],
            severity=severities["caption_center_x"],
            method="pixel",
            evidence=f"mean horizontal centre {mean_x:.3f}; {detected}",
        ),
        Measurement(
            dimension="caption_center_y_spread",
            value=round(spread_y, 4),
            threshold=thresholds["caption_center_y_spread"],
            passed=spread_y <= center_y_spread_max,
            severity=severities["caption_center_y_spread"],
            method="pixel",
            evidence=(
                f"vertical centre ranged {min(centers_y):.3f}-{max(centers_y):.3f} "
                f"(spread {spread_y:.3f}); {detected}"
            ),
        ),
        Measurement(
            dimension="caption_cap_height",
            value=round(mean_h, 4),
            threshold=thresholds["caption_cap_height"],
            passed=cap_height_range[0] <= mean_h <= cap_height_range[1],
            severity=severities["caption_cap_height"],
            method="pixel",
            evidence=f"mean caption height {mean_h * 100:.2f}% of frame; {detected}",
        ),
        Measurement(
            dimension="caption_contrast",
            value=round(min_contrast, 2),
            threshold=thresholds["caption_contrast"],
            passed=min_contrast >= contrast_min,
            severity=severities["caption_contrast"],
            method="pixel",
            evidence=(
                f"worst-frame contrast {min_contrast:.2f}:1 "
                f"(best {max(contrasts):.2f}:1); {detected}"
            ),
        ),
    ]
    return _fold(measurements)


def _fold(measurements: Sequence[Measurement], notes: str = "") -> DimensionScore:
    """Fold measurements into a dimension score. Unverified is never a pass."""
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
