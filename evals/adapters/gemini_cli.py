"""Adapter for the Gemini CLI running non-interactively.

The default template below is a *sensible starting point*, not a guarantee.
CLI flags change between versions, so verify it against your installed
version and override ``adapter.command`` in config if it differs.
"""

from __future__ import annotations

from evals.adapters.generic import GenericAdapter

__all__ = ["GeminiCliAdapter"]


class GeminiCliAdapter(GenericAdapter):
    """Drive the Gemini CLI in single-prompt mode."""

    name = "gemini-cli"
    is_live = True

    #: ``-p`` supplies the prompt; ``-y`` auto-approves tool calls so the run
    #: does not block waiting for a human.
    default_command = 'gemini -p "$(cat {prompt_file})" -y'
