"""Adapter for Claude Code running non-interactively.

The default template below is a *sensible starting point*, not a guarantee.
CLI flags change between versions, so verify it against your installed
version and override ``adapter.command`` in config if it differs. Nothing in
the harness depends on this default being correct -- it exists so that a user
on this runtime has less to write.
"""

from __future__ import annotations

from evals.adapters.generic import GenericAdapter

__all__ = ["ClaudeCodeAdapter"]


class ClaudeCodeAdapter(GenericAdapter):
    """Drive Claude Code in headless mode."""

    name = "claude-code"
    is_live = True

    #: ``-p`` runs a single non-interactive prompt and exits.
    default_command = (
        'claude -p "$(cat {prompt_file})" '
        "--add-dir {output_dir} "
        "--permission-mode acceptEdits"
    )
