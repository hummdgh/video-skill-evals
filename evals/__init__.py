"""``video-skill-evals`` -- an evaluator pipeline for a video-composition skill.

Three stages, each independently runnable and resumable::

    generate  ->  measure  ->  score / report

This package deliberately re-exports **nothing**. Import the module you need
directly::

    from evals.config import load_config
    from evals.schema import Suite, CaseVerdict

Two reasons. Eager re-exports would make ``import evals`` fail whenever any
single submodule is missing or broken, which turns one typo into a dead CLI.
And ``evals.cli`` imports its stage modules lazily inside each handler so that
``--help`` keeps working even when an optional stage is incomplete -- a
package-level import graph would defeat that.

Standard library only, throughout.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
