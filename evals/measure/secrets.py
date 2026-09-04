"""Scan for credentials and PII in narration, captions and transcripts.

This module implements one doctrine inherited from the prior art, and it is
worth stating plainly because it inverts the usual instinct:

    **A secrets finding without a literal citation does not fail the run.**

Secrets checks are the highest-consequence output of the battery and the most
hallucination-prone. Asked "are there credentials here?", a language model is
being invited to invent one. So every finding must carry the exact matched
text and where it was found. Anything that cannot produce that is recorded as
``claimed-but-unverified`` with ``passed=None`` -- visible in the report, but
never the reason a build goes red.

That is why this module is regex over real text rather than a judge pass: a
regex match *is* a literal citation by construction.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Pattern, Sequence, Tuple

from evals.schema import DimensionScore, Measurement, Severity, Tier

__all__ = ["Finding", "scan_text", "measure_secrets", "DIMENSION", "DEFAULT_ALLOWLIST"]

DIMENSION = "SECRETS"

#: Substrings that neutralise a match. Documentation placeholders are not leaks.
DEFAULT_ALLOWLIST: Tuple[str, ...] = (
    "example.com",
    "example.org",
    "test@example",
    "your-api-key",
    "your_api_key",
    "<your",
    "xxxxxxxx",
    "redacted",
    "placeholder",
    "dummy",
    "sk-xxx",
    "1234567890",
    "aki aexample",
)

# Ordered most-specific first: the first pattern to match a span wins, so a
# recognisable provider key is not also reported as a generic "long token".
_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", Severity.CRITICAL.value),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", Severity.CRITICAL.value),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", Severity.CRITICAL.value),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b", Severity.CRITICAL.value),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b", Severity.CRITICAL.value),
    ("private_key_block", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
     Severity.CRITICAL.value),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
     Severity.CRITICAL.value),
    ("bearer_token", r"\bBearer\s+[A-Za-z0-9._-]{20,}\b", Severity.CRITICAL.value),
    ("password_assignment",
     r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[\"']?[^\s\"',;]{8,}",
     Severity.CRITICAL.value),
    ("gcp_project_id", r"\b(?:projects/|--project[ =])([a-z][a-z0-9-]{5,29})\b",
     Severity.MAJOR.value),
    ("home_path", r"(?:/Users/|/home/|C:\\\\Users\\\\)[A-Za-z0-9._-]{2,}",
     Severity.MAJOR.value),
    ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
     Severity.MAJOR.value),
    ("ipv4_private", r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){1,3}\b",
     Severity.MINOR.value),
)

_COMPILED: Tuple[Tuple[str, Pattern[str], str], ...] = tuple(
    (name, re.compile(pattern), severity) for name, pattern, severity in _PATTERNS
)


@dataclass
class Finding:
    """One literal, cited match.

    Attributes:
        kind: Pattern name, e.g. ``aws_access_key``.
        matched: The literal text that matched -- the citation itself.
        source: Where it was found, e.g. ``captions.srt``.
        line_no: 1-based line number within that source.
        severity: Severity to attach if this finding stands.
    """

    kind: str
    matched: str
    source: str
    line_no: int
    severity: str

    def redacted(self) -> str:
        """The match with its middle masked, safe to print in a report."""
        text = self.matched
        if len(text) <= 12:
            return text[:2] + "*" * max(0, len(text) - 2)
        return f"{text[:6]}...{text[-4:]}"

    def citation(self) -> str:
        return f"{self.source}:{self.line_no} matched {self.kind} -> {self.redacted()}"


def _is_allowlisted(text: str, allowlist: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(entry.lower() in lowered for entry in allowlist)


def scan_text(
    text: str,
    source: str,
    *,
    allowlist: Sequence[str] = DEFAULT_ALLOWLIST,
) -> List[Finding]:
    """Scan ``text`` and return every cited finding.

    Overlapping matches are resolved most-specific-first, so a provider key is
    reported once under its own name rather than also as a generic token.
    """
    findings: List[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        claimed: List[Tuple[int, int]] = []
        for kind, pattern, severity in _COMPILED:
            for match in pattern.finditer(line):
                start, end = match.span()
                if any(start < c_end and end > c_start for c_start, c_end in claimed):
                    continue
                matched = match.group(0).strip()
                if _is_allowlisted(matched, allowlist):
                    continue
                claimed.append((start, end))
                findings.append(
                    Finding(
                        kind=kind,
                        matched=matched,
                        source=source,
                        line_no=line_no,
                        severity=severity,
                    )
                )
    return findings


def measure_secrets(
    sources: Optional[Dict[str, Path | str]] = None,
    *,
    allowlist: Sequence[str] = DEFAULT_ALLOWLIST,
    judge_claims: Optional[Sequence[str]] = None,
) -> DimensionScore:
    """Scan every supplied text source for credentials and PII.

    Args:
        sources: Mapping of label -> path, e.g.
            ``{"captions.srt": path, "narration": path}``.
        allowlist: Substrings that neutralise a match.
        judge_claims: Uncited secrets claims from a holistic judge pass. These
            are recorded as ``claimed-but-unverified`` and never fail the run.

    Returns:
        A :class:`DimensionScore`. Passing means "scanned, nothing found";
        an unreadable source is reported as unverified, not as a pass.
    """
    measurements: List[Measurement] = []
    all_findings: List[Finding] = []
    scanned: List[str] = []

    for label, path in (sources or {}).items():
        file_path = Path(path)
        if not file_path.is_file():
            measurements.append(
                Measurement(
                    dimension=f"secrets_scan[{label}]",
                    value=None,
                    threshold="source is scannable",
                    passed=None,
                    severity=Severity.MINOR.value,
                    method="regex",
                    evidence=f"unverified: {label} not present at {file_path}",
                )
            )
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            measurements.append(
                Measurement(
                    dimension=f"secrets_scan[{label}]",
                    value=None,
                    threshold="source is scannable",
                    passed=None,
                    severity=Severity.MINOR.value,
                    method="regex",
                    evidence=f"unverified: cannot read {label}: {exc}",
                )
            )
            continue

        scanned.append(label)
        all_findings.extend(scan_text(text, source=label, allowlist=allowlist))

    if not scanned:
        measurements.append(
            Measurement(
                dimension="secrets_visible",
                value=None,
                threshold="no credentials or PII in any source",
                passed=None,
                severity=Severity.CRITICAL.value,
                method="regex",
                evidence="unverified: no scannable text sources were available",
            )
        )
    else:
        by_kind: Dict[str, List[Finding]] = {}
        for finding in all_findings:
            by_kind.setdefault(finding.kind, []).append(finding)

        measurements.append(
            Measurement(
                dimension="secrets_visible",
                value=len(all_findings),
                threshold="0 cited findings",
                passed=not all_findings,
                severity=Severity.CRITICAL.value if all_findings else Severity.MINOR.value,
                method="regex",
                evidence=(
                    f"scanned {', '.join(scanned)}; no matches"
                    if not all_findings
                    else "; ".join(f.citation() for f in all_findings[:6])
                ),
            )
        )
        for kind, group in sorted(by_kind.items()):
            measurements.append(
                Measurement(
                    dimension=f"secrets[{kind}]",
                    value=len(group),
                    threshold="0",
                    passed=False,
                    severity=group[0].severity,
                    method="regex",
                    evidence="; ".join(f.citation() for f in group[:4]),
                )
            )

    # Uncited judge claims: recorded, never fatal.
    for claim in judge_claims or []:
        measurements.append(
            Measurement(
                dimension="secrets_claimed_unverified",
                value=claim,
                threshold="a literal citation is required to fail",
                passed=None,
                severity=Severity.MINOR.value,
                method="judge",
                evidence=(
                    "claimed-but-unverified: the judge reported a secret but did "
                    f"not cite literal on-screen text -- {claim}"
                ),
            )
        )

    decided = [m for m in measurements if m.passed is not None]
    failed = [m for m in decided if not m.passed]
    unverified = [m for m in measurements if m.passed is None]
    ratio = (len(decided) - len(failed)) / len(decided) if decided else 0.0

    notes = ""
    if failed:
        notes = f"{len(all_findings)} cited finding(s)"
    elif unverified:
        notes = f"{len(unverified)} item(s) unverified"

    return DimensionScore(
        dimension=DIMENSION,
        tier=Tier.A.value,
        score_0_100=float(int(ratio * 100)),
        passed=bool(decided) and not failed,
        severity=Severity.CRITICAL.value if failed else Severity.MINOR.value,
        measurements=measurements,
        notes=notes,
    )
