"""Adapter registry.

An adapter is the seam between this harness and whatever agent runtime the
user actually has installed. Look one up by name::

    from evals.adapters import get_adapter
    adapter = get_adapter("mock", config)

``mock`` is fully offline and always available; everything else shells out to a
real runtime and spends real money.
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

from evals.adapters.base import Adapter, AdapterError, InvocationOutcome
from evals.adapters.generic import GenericAdapter
from evals.adapters.claude_code import ClaudeCodeAdapter
from evals.adapters.gemini_cli import GeminiCliAdapter
from evals.adapters.antigravity import AntigravityAdapter
from evals.adapters.mock import MockAdapter

__all__ = [
    "Adapter",
    "AdapterError",
    "InvocationOutcome",
    "GenericAdapter",
    "ClaudeCodeAdapter",
    "GeminiCliAdapter",
    "AntigravityAdapter",
    "MockAdapter",
    "REGISTRY",
    "get_adapter",
    "available_adapters",
]

REGISTRY: Dict[str, Type[Adapter]] = {
    GenericAdapter.name: GenericAdapter,
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    GeminiCliAdapter.name: GeminiCliAdapter,
    AntigravityAdapter.name: AntigravityAdapter,
    MockAdapter.name: MockAdapter,
}


def available_adapters() -> List[str]:
    """Return registered adapter names, in a stable order."""
    return sorted(REGISTRY)


def get_adapter(name: str, config: Any) -> Adapter:
    """Instantiate the adapter called ``name``.

    Args:
        name: Registry key, e.g. ``"generic"`` or ``"mock"``.
        config: The resolved ``Config``.

    Returns:
        A ready, validated adapter.

    Raises:
        AdapterError: unknown name, or the adapter is misconfigured.
    """
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise AdapterError(
            f"unknown adapter {name!r}; available: {', '.join(available_adapters())}"
        ) from None
    return cls(config)
