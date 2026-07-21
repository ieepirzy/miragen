"""Managed schedule bindings (issue #33 Phase F).

A ScheduleBinding is a named, durable, API-owned schedule — the control
plane's counterpart to a hand-authored profile trigger. Bindings live as one
JSON file each under the agent volume (`/agent/schedules/`), survive
restarts, and are reconciled with compare-and-swap versioning so concurrent
reconcilers converge instead of flapping.

Design record: docs/design/managed-schedules.md. Settled decisions: managed
fires dispatch the profile's on_complete hooks exactly like profile cron
fires (uniform behavior), respect the daily token budget, and are rejected
on interactive-mode agents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from apscheduler.triggers.cron import CronTrigger as _APCronTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from miragen.models import RunProvenance

BINDING_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"


class ScheduleSpec(BaseModel):
    """Exactly one of `cron` (5-field, evaluated in UTC) or `every_s`."""

    model_config = ConfigDict(extra="forbid")

    cron: Optional[str] = Field(default=None, min_length=1)
    every_s: Optional[int] = Field(
        default=None,
        ge=10,
        description="Fire every N seconds; minimum 10s, same hot-loop guard as profile triggers.",
    )

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            _APCronTrigger.from_crontab(v)
        except ValueError as e:
            raise ValueError(
                f"invalid cron expression '{v}': {e}. "
                "Expected 5 fields (minute hour day month day_of_week), e.g. '0 9 * * 1-5'."
            )
        return v

    @model_validator(mode="after")
    def validate_exactly_one(self) -> "ScheduleSpec":
        if (self.cron is None) == (self.every_s is None):
            raise ValueError("schedule requires exactly one of `cron` or `every_s`")
        return self


class ScheduleBinding(BaseModel):
    """The durable binding. `version` is server-assigned and monotonic —
    callers never invent it; it is the compare-and-swap token."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=BINDING_NAME_PATTERN)
    schedule: ScheduleSpec
    prompt: str = Field(
        min_length=1,
        description="COMPLETED prompt, dispatched verbatim — rendering is a control-plane concern.",
    )
    enabled: bool = True
    provenance: Optional[RunProvenance] = None
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Typed-parameter escape hatch: recorded on fired runs, never interpreted.",
    )
    version: int = Field(default=1, ge=1)


class BindingConflictError(Exception):
    """CAS failure: the caller's expectation doesn't match stored state.
    Carries the current binding (None if the conflict is 'already exists'
    semantics inverted — see message) so callers can re-read-and-retry."""

    def __init__(self, message: str, current: ScheduleBinding | None):
        self.current = current
        super().__init__(message)


class ScheduleStore:
    """File-per-binding store with atomic writes and CAS upsert/delete.

    Same durability posture as run records: the volume is the source of
    truth; unparsable files are skipped loudly by callers, never deleted.
    """

    def __init__(self, root: Path = Path("/agent/schedules")):
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def list(self) -> list[ScheduleBinding]:
        if not self.root.exists():
            return []
        bindings = []
        for path in sorted(self.root.glob("*.json")):
            binding = self._read(path)
            if binding is not None:
                bindings.append(binding)
        return bindings

    def get(self, name: str) -> ScheduleBinding | None:
        return self._read(self._path(name))

    def upsert(
        self,
        name: str,
        *,
        schedule: ScheduleSpec,
        prompt: str,
        enabled: bool = True,
        provenance: RunProvenance | None = None,
        metadata: dict[str, str] | None = None,
        expected_version: int | None = None,
    ) -> ScheduleBinding:
        """Create-or-update with compare-and-swap semantics:

        - expected_version omitted → CREATE-ONLY. Raises BindingConflictError
          if the binding exists (a blind upsert from a reconciler that never
          read is a bug, not a convenience).
        - expected_version given → UPDATE. Raises BindingConflictError if the
          binding is missing or the stored version differs.
        """
        current = self.get(name)
        if expected_version is None:
            if current is not None:
                raise BindingConflictError(
                    f"schedule binding '{name}' already exists (version {current.version}); "
                    "pass expected_version to update it",
                    current,
                )
            version = 1
        else:
            if current is None:
                raise BindingConflictError(
                    f"schedule binding '{name}' does not exist; omit expected_version to create it",
                    None,
                )
            if current.version != expected_version:
                raise BindingConflictError(
                    f"schedule binding '{name}' is at version {current.version}, "
                    f"not {expected_version} — re-read and retry",
                    current,
                )
            version = current.version + 1

        binding = ScheduleBinding(
            name=name,
            schedule=schedule,
            prompt=prompt,
            enabled=enabled,
            provenance=provenance,
            metadata=dict(metadata or {}),
            version=version,
        )
        self.save(binding)
        return binding

    def delete(self, name: str, *, expected_version: int | None = None) -> ScheduleBinding:
        """Remove a binding; returns what was removed. Optional CAS check."""
        current = self.get(name)
        if current is None:
            raise KeyError(name)
        if expected_version is not None and current.version != expected_version:
            raise BindingConflictError(
                f"schedule binding '{name}' is at version {current.version}, "
                f"not {expected_version} — re-read and retry",
                current,
            )
        self._path(name).unlink(missing_ok=True)
        return current

    def save(self, binding: ScheduleBinding) -> None:
        """Atomic write (tmp + replace) — crash between file and scheduler
        reconciliation self-heals from disk at next startup."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(binding.name)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(binding.model_dump_json())
        os.replace(tmp, path)

    def _read(self, path: Path) -> ScheduleBinding | None:
        try:
            return ScheduleBinding.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return None
