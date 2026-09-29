"""Normalized alert contracts independent of webhook formats."""

from __future__ import annotations

import hashlib
from datetime import UTC
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from tau_incident.models import Model, Scope, Text


class AlertEvent(Model):
    source: Text
    fingerprint: Text
    rule: Text
    severity: Text
    scope: Scope
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    observed_at: AwareDatetime | None = None
    status: Literal["firing", "resolved"]
    summary: Text
    external_incident_id: str | None = None
    raw_reference: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.scope.entities:
            raise ValueError("normalized alerts require at least one entity")
        if self.status == "resolved" and self.ends_at is None:
            raise ValueError("resolved alert requires ends_at")
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("alert ends before it starts")
        return self

    @property
    def occurrence_key(self) -> str:
        value = "\0".join(
            (
                self.source,
                self.scope.environment,
                self.fingerprint,
                self.starts_at.astimezone(UTC).isoformat(),
            )
        )
        return hashlib.sha256(value.encode()).hexdigest()
