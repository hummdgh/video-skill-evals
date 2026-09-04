"""Allow ``python -m evals``."""

from __future__ import annotations

from evals.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
