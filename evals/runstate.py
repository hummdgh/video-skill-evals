"""Resumable run state for ``video-skill-evals``.

A run is a grid of ``(case_id, repeat_idx)`` units. This module persists the
status of every unit to ``<run_dir>/runstate.json`` so an interrupted run can
be resumed without repeating completed work -- which matters a great deal when
a single live unit can cost real money and take twenty minutes.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write cannot
corrupt the manifest.

Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

__all__ = ["UnitStatus", "Unit", "RunState", "RUNSTATE_FILENAME"]

RUNSTATE_FILENAME = "runstate.json"


class UnitStatus:
    """Status constants for a single (case, repeat) unit."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    ALL = (PENDING, RUNNING, DONE, FAILED)
    TERMINAL = (DONE, FAILED)


@dataclass
class Unit:
    """One (case, repeat) cell of the run grid.

    Attributes:
        case_id: Suite case identifier.
        repeat_idx: Zero-based repeat index.
        status: One of :class:`UnitStatus`.
        workdir: Isolated working directory for this unit, if allocated.
        started_at: Epoch seconds when the unit last entered ``running``.
        finished_at: Epoch seconds when the unit reached a terminal status.
        attempts: How many times this unit has been started.
        error: Human-readable failure description, or ``None``.
        failure_class: ``infra`` | ``agent`` | ``quality`` | ``None``.
            Infra failures are excluded from quality aggregates so that API
            flakes cannot poison the metrics.
    """

    case_id: str
    repeat_idx: int
    status: str = UnitStatus.PENDING
    workdir: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    attempts: int = 0
    error: Optional[str] = None
    failure_class: Optional[str] = None

    @property
    def key(self) -> Tuple[str, int]:
        return (self.case_id, self.repeat_idx)

    @property
    def is_terminal(self) -> bool:
        return self.status in UnitStatus.TERMINAL

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Unit":
        return cls(
            case_id=str(data.get("case_id", "")),
            repeat_idx=int(data.get("repeat_idx", 0)),
            status=str(data.get("status", UnitStatus.PENDING)),
            workdir=data.get("workdir"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            attempts=int(data.get("attempts", 0)),
            error=data.get("error"),
            failure_class=data.get("failure_class"),
        )


@dataclass
class RunState:
    """Persistent manifest for one evaluation run."""

    run_dir: Path
    run_id: str
    suite_name: str
    repeats: int
    created_at: float
    units: Dict[Tuple[str, int], Unit] = field(default_factory=dict)

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        run_dir: Path | str,
        case_ids: Sequence[str],
        repeats: int,
        suite_name: str = "",
        run_id: Optional[str] = None,
    ) -> "RunState":
        """Create a fresh run state and write it to disk.

        Args:
            run_dir: Directory for this run; created if absent.
            case_ids: Suite case identifiers, in suite order.
            repeats: Repeats per case. Must be at least 1.
            suite_name: Human-readable suite name, recorded for reporting.
            run_id: Explicit run id; defaults to a UTC timestamp.

        Raises:
            ValueError: ``repeats`` < 1, or ``case_ids`` is empty, or duplicate
                case ids were supplied.
        """
        if repeats < 1:
            raise ValueError(f"repeats must be at least 1, got {repeats}")
        if not case_ids:
            raise ValueError("case_ids must not be empty")
        duplicates = {c for c in case_ids if list(case_ids).count(c) > 1}
        if duplicates:
            raise ValueError(f"duplicate case ids: {', '.join(sorted(duplicates))}")

        path = Path(run_dir)
        path.mkdir(parents=True, exist_ok=True)
        now = time.time()
        state = cls(
            run_dir=path,
            run_id=run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
            suite_name=suite_name,
            repeats=repeats,
            created_at=now,
            units={},
        )
        for case_id in case_ids:
            for idx in range(repeats):
                unit = Unit(case_id=case_id, repeat_idx=idx)
                state.units[unit.key] = unit
        state.save()
        return state

    @classmethod
    def load(cls, run_dir: Path | str) -> "RunState":
        """Load an existing run state.

        Raises:
            FileNotFoundError: no manifest in ``run_dir``.
            ValueError: the manifest is malformed.
        """
        path = Path(run_dir)
        manifest = path / RUNSTATE_FILENAME
        if not manifest.is_file():
            raise FileNotFoundError(f"no {RUNSTATE_FILENAME} in {path}")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{manifest} is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise ValueError(f"cannot read {manifest}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{manifest} must contain a JSON object")

        units: Dict[Tuple[str, int], Unit] = {}
        for raw in data.get("units", []):
            if not isinstance(raw, Mapping):
                raise ValueError(f"{manifest}: every unit must be an object")
            unit = Unit.from_dict(raw)
            units[unit.key] = unit

        return cls(
            run_dir=path,
            run_id=str(data.get("run_id", "")),
            suite_name=str(data.get("suite_name", "")),
            repeats=int(data.get("repeats", 1)),
            created_at=float(data.get("created_at", 0.0)),
            units=units,
        )

    @classmethod
    def load_or_create(
        cls,
        run_dir: Path | str,
        case_ids: Sequence[str],
        repeats: int,
        suite_name: str = "",
    ) -> "RunState":
        """Resume an existing run, or start one if none exists."""
        try:
            return cls.load(run_dir)
        except FileNotFoundError:
            return cls.create(run_dir, case_ids, repeats, suite_name=suite_name)

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        ordered = sorted(self.units.values(), key=lambda u: (u.case_id, u.repeat_idx))
        return {
            "run_id": self.run_id,
            "suite_name": self.suite_name,
            "repeats": self.repeats,
            "created_at": self.created_at,
            "units": [u.to_dict() for u in ordered],
        }

    def save(self) -> None:
        """Atomically persist the manifest.

        Writes to a temporary file in the same directory then ``os.replace``s
        it into position, so a crash mid-write leaves the previous manifest
        intact rather than a truncated one.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        target = self.run_dir / RUNSTATE_FILENAME
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=False)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self.run_dir), prefix=".runstate-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except OSError:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    # -- mutation ----------------------------------------------------------

    def get(self, case_id: str, repeat_idx: int) -> Unit:
        """Return one unit.

        Raises:
            KeyError: the unit is not part of this run.
        """
        try:
            return self.units[(case_id, repeat_idx)]
        except KeyError:
            raise KeyError(f"no unit {case_id}[{repeat_idx}] in run {self.run_id}") from None

    def mark(
        self,
        case_id: str,
        repeat_idx: int,
        status: str,
        *,
        workdir: Optional[str] = None,
        error: Optional[str] = None,
        failure_class: Optional[str] = None,
        save: bool = True,
    ) -> Unit:
        """Update a unit's status and persist.

        Entering ``running`` stamps ``started_at`` and increments ``attempts``.
        Entering a terminal status stamps ``finished_at``.

        Raises:
            KeyError: the unit is not part of this run.
            ValueError: ``status`` is not a known status.
        """
        if status not in UnitStatus.ALL:
            raise ValueError(
                f"unknown status {status!r}; expected one of {', '.join(UnitStatus.ALL)}"
            )
        unit = self.get(case_id, repeat_idx)
        now = time.time()

        if status == UnitStatus.RUNNING:
            unit.started_at = now
            unit.finished_at = None
            unit.attempts += 1
            unit.error = None
            unit.failure_class = None
        elif status in UnitStatus.TERMINAL:
            unit.finished_at = now

        unit.status = status
        if workdir is not None:
            unit.workdir = workdir
        if error is not None:
            unit.error = error
        if failure_class is not None:
            unit.failure_class = failure_class

        if save:
            self.save()
        return unit

    def reset_stale_running(self, save: bool = True) -> List[Unit]:
        """Return ``running`` units to ``pending``.

        A unit left in ``running`` means the process died mid-flight. On resume
        those must be retried, not skipped.
        """
        reset: List[Unit] = []
        for unit in self.units.values():
            if unit.status == UnitStatus.RUNNING:
                unit.status = UnitStatus.PENDING
                unit.started_at = None
                unit.finished_at = None
                reset.append(unit)
        if reset and save:
            self.save()
        return reset

    # -- queries -----------------------------------------------------------

    def all_units(self) -> List[Unit]:
        return sorted(self.units.values(), key=lambda u: (u.case_id, u.repeat_idx))

    def pending(self) -> List[Unit]:
        """Units still to run, in stable order. This is what drives resume."""
        return [u for u in self.all_units() if u.status == UnitStatus.PENDING]

    def failed(self) -> List[Unit]:
        return [u for u in self.all_units() if u.status == UnitStatus.FAILED]

    def done(self) -> List[Unit]:
        return [u for u in self.all_units() if u.status == UnitStatus.DONE]

    def units_for_case(self, case_id: str) -> List[Unit]:
        return [u for u in self.all_units() if u.case_id == case_id]

    def case_ids(self) -> List[str]:
        seen: List[str] = []
        for unit in self.all_units():
            if unit.case_id not in seen:
                seen.append(unit.case_id)
        return seen

    def is_complete(self) -> bool:
        return all(u.is_terminal for u in self.units.values())

    def __iter__(self) -> Iterator[Unit]:
        return iter(self.all_units())

    def __len__(self) -> int:
        return len(self.units)

    def summary(self) -> Dict[str, Any]:
        """Counts by status, plus a per-failure-class breakdown."""
        counts = {status: 0 for status in UnitStatus.ALL}
        classes: Dict[str, int] = {}
        for unit in self.units.values():
            counts[unit.status] = counts.get(unit.status, 0) + 1
            if unit.status == UnitStatus.FAILED:
                key = unit.failure_class or "unclassified"
                classes[key] = classes.get(key, 0) + 1
        return {
            "run_id": self.run_id,
            "suite_name": self.suite_name,
            "repeats": self.repeats,
            "total": len(self.units),
            "counts": counts,
            "failure_classes": classes,
            "complete": self.is_complete(),
        }
