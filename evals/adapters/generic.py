"""Generic subprocess adapter -- the default, and the important one.

This is how a user points the harness at whatever agent runtime they actually
have. The runtime is described entirely by a command template in config::

    "adapter": {
      "name": "generic",
      "command": "my-agent run --skill video-composition --file {prompt_file} --out {output_dir}"
    }

Supported placeholders:

``{prompt_file}``
    Absolute path to the rendered prompt.
``{workdir}``
    Absolute path to this unit's isolated working directory.
``{output_dir}``
    Where artefacts should be written. Same as ``{workdir}`` unless the
    ``output_subdir`` option says otherwise.
``{case_id}`` / ``{repeat_idx}``
    Identity of the unit, for runtimes that want a run label.

Switching runtime is therefore a config edit, never a code change. That is a
deliberate constraint: we do not know which runtime any given user has, and
guessing wrong in code would be expensive to undo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from evals.adapters.base import Adapter, AdapterError, InvocationOutcome

__all__ = ["GenericAdapter"]


class GenericAdapter(Adapter):
    """Run an arbitrary command template as the agent runtime."""

    name = "generic"
    is_live = True

    #: Subclasses override this to supply a conventional command for their
    #: runtime. Empty means the user must configure one.
    default_command: str = ""

    def validate(self) -> None:
        """Fail fast on a template that cannot possibly work.

        Raises:
            AdapterError: the template is missing or lacks ``{prompt_file}``.
        """
        template = self._template()
        if not template.strip():
            raise AdapterError(
                f"adapter {self.name!r}: no command configured. Set "
                "adapter.command to a template using {prompt_file} and "
                "{output_dir}, or use the mock adapter for an offline run."
            )
        if "{prompt_file}" not in template:
            raise AdapterError(
                f"adapter {self.name!r}: command template must contain "
                "{prompt_file} so the runtime knows what to build."
            )

    def _template(self) -> str:
        """Configured command, falling back to the subclass default."""
        configured = getattr(self.adapter_config, "command", "") or ""
        return configured.strip() or self.default_command

    def _timeout(self) -> float:
        return float(getattr(self.adapter_config, "timeout_s", 1800.0))

    def _extra_env(self) -> Dict[str, str]:
        env = dict(getattr(self.adapter_config, "env", {}) or {})
        return {str(k): str(v) for k, v in env.items()}

    def _output_dir(self, workdir: Path) -> Path:
        """Resolve where the runtime should write its artefacts."""
        options = getattr(self.adapter_config, "options", {}) or {}
        subdir = options.get("output_subdir")
        if subdir:
            out = workdir / str(subdir)
            out.mkdir(parents=True, exist_ok=True)
            return out
        return workdir

    def build_command(self, prompt_file: Path, workdir: Path) -> str:
        """Substitute placeholders into the configured template.

        Raises:
            AdapterError: the template references an unknown placeholder.
        """
        output_dir = self._output_dir(workdir)
        case_id = workdir.parent.name if workdir.name.startswith("repeat-") else workdir.name
        repeat_idx = workdir.name.split("-", 1)[1] if workdir.name.startswith("repeat-") else "0"

        values = {
            "prompt_file": str(prompt_file.resolve()),
            "workdir": str(workdir.resolve()),
            "output_dir": str(output_dir.resolve()),
            "case_id": case_id,
            "repeat_idx": repeat_idx,
        }
        try:
            return self._template().format(**values)
        except KeyError as exc:
            raise AdapterError(
                f"adapter {self.name!r}: unknown placeholder {exc} in command "
                f"template. Supported: {', '.join('{' + k + '}' for k in values)}"
            ) from None
        except (IndexError, ValueError) as exc:
            raise AdapterError(
                f"adapter {self.name!r}: malformed command template ({exc}). "
                "Literal braces must be doubled as {{ and }}."
            ) from None

    def invoke(self, prompt_file: Path, workdir: Path) -> InvocationOutcome:
        """Run the configured command, capturing everything.

        A misconfigured template is reported as a non-launched outcome, which
        the scorer classifies as *infra* -- the agent never got a chance, so it
        must not count against video quality.
        """
        try:
            command = self.build_command(prompt_file, workdir)
        except AdapterError as exc:
            return InvocationOutcome(launched=False, error=str(exc))

        return self.run_command(
            command=command,
            workdir=workdir,
            timeout_s=self._timeout(),
            extra_env=self._extra_env(),
        )
