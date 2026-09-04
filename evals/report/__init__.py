"""Stage 4: turn a :class:`RunReport` into something a human reads.

Three renderers, one entry point::

    render(report, "markdown")  -> str
    render(report, "html")      -> str   (self-contained; no CDN, no fetches)
    render(report, "json")      -> str

The HTML renderer deliberately inlines everything. A report that needs the
network to display is useless in exactly the situation you most want it --
a locked-down CI box, or an artefact opened from ``file://`` six months later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from evals.schema import RunReport, dumps

__all__ = ["render", "write_report", "FORMATS"]

FORMATS = ("markdown", "html", "json")


def render(report: RunReport, fmt: str = "markdown") -> str:
    """Render ``report`` in the requested format.

    Raises:
        ValueError: unknown format.
    """
    normalised = (fmt or "").strip().lower()
    if normalised in ("md", "markdown"):
        from evals.report.markdown import render_markdown

        return render_markdown(report)
    if normalised == "html":
        from evals.report.html import render_html

        return render_html(report)
    if normalised == "json":
        return dumps(report.to_dict())
    raise ValueError(f"unknown report format {fmt!r}; expected one of {', '.join(FORMATS)}")


def write_report(report: RunReport, path: Path | str, fmt: Optional[str] = None) -> Path:
    """Render and write to ``path``, inferring the format from its suffix.

    Raises:
        ValueError: the format is unknown.
        OSError: the file could not be written.
    """
    target = Path(path)
    if fmt is None:
        suffix = target.suffix.lower()
        fmt = {".md": "markdown", ".html": "html", ".htm": "html", ".json": "json"}.get(
            suffix, "markdown"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(report, fmt), encoding="utf-8")
    return target
