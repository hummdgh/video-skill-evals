"""Adapter for an Antigravity-style orchestrator.

The default template below is a *sensible starting point*, not a guarantee.
Verify it against your installed version and override ``adapter.command`` in
config if it differs.
"""

from __future__ import annotations

from evals.adapters.generic import GenericAdapter

__all__ = ["AntigravityAdapter"]


class AntigravityAdapter(GenericAdapter):
    """Drive an Antigravity-style agent orchestrator."""

    name = "antigravity"
    is_live = True

    default_command = (
        "agy run --prompt-file {prompt_file} --workdir {workdir} "
        "--output {output_dir} --non-interactive"
    )
