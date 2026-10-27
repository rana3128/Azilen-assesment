from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


def stable_hash(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class Address(BaseModel):
    line1: str
    city: str
    region: str
    postal_code: str
    country: str

    def one_line(self) -> str:
        return f"{self.line1}, {self.city}, {self.region} {self.postal_code}, {self.country}"


HireStatus = Literal["pre_hire", "active", "rescinded", "terminated"]


class Hire(BaseModel):
    """Canonical worker record. Workday is the system of record for every field here."""

    workday_id: str
    first_name: str
    last_name: str
    preferred_name: str | None = None
    email: str
    title: str
    department: str
    manager_id: str | None
    start_date: date
    location: str
    address: Address
    status: HireStatus
    ashby_candidate_id: str | None = None
    updated_at: datetime

    @property
    def display_first(self) -> str:
        return self.preferred_name or self.first_name

    @property
    def display_name(self) -> str:
        return f"{self.display_first} {self.last_name}"

    @property
    def is_live(self) -> bool:
        return self.status in ("pre_hire", "active")


class SystemRecord(BaseModel):
    """A downstream system's record, already transformed into canonical field names by its connector."""

    system: str
    record_id: str
    workday_id: str | None = None
    email: str | None = None
    name: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return ["critical", "high", "medium", "low", "info"].index(self.value)


class FindingType(str, Enum):
    FIELD_DRIFT = "field_drift"
    MISSING_RECORD = "missing_record"
    ACCESS_AFTER_RESCIND = "access_after_rescind"
    SOURCE_CONFLICT = "source_conflict"
    DATA_QUALITY = "data_quality"
    GATE_BREACH = "gate_breach"
    GATE_AT_RISK = "gate_at_risk"
    OFF_CYCLE_HIRE = "off_cycle_hire"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    ORPHAN_ACCOUNT = "orphan_account"
    SYSTEM_UNAVAILABLE = "system_unavailable"


class Finding(BaseModel):
    type: FindingType
    severity: Severity
    system: str
    message: str
    workday_id: str | None = None
    record_id: str | None = None
    field: str | None = None
    expected: Any = None
    actual: Any = None
    ambiguous: bool = False
    context: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key(self) -> str:
        """Dedup key for the exception queue: same problem on the same record with the same bad value."""
        return stable_hash(self.type.value, self.workday_id, self.system, self.record_id, self.field, self.actual)


Decision = Literal[
    "same_person",
    "different_person",
    "trust_workday",
    "trust_ashby",
    "non_employee_account",
    "needs_human",
]


class Verdict(BaseModel):
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    suggested_action: str


class TriageResult(BaseModel):
    finding_key: str
    verdict: Verdict
    actor: str
    requires_human: bool
    guardrail_note: str | None = None


Action = Literal["update_field", "deactivate_account", "cancel_order", "link_identity", "escalate"]
Risk = Literal["low", "high"]
RemediationStatus = Literal[
    "planned",
    "escalated",
    "awaiting_approval",
    "applied",
    "already_applied",
    "blocked_system_unavailable",
    "failed",
]


class Remediation(BaseModel):
    finding_key: str
    system: str
    action: Action
    record_id: str | None
    field: str | None = None
    from_value: Any = None
    to_value: Any = None
    risk: Risk
    reason: str
    owner: str | None = None
    status: RemediationStatus = "planned"
    error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def idempotency_key(self) -> str:
        """Identifies the *intended end state*, not the attempt: retries and re-runs map to the same key."""
        return stable_hash(self.system, self.action, self.record_id, self.field, self.to_value)


class SystemStatus(BaseModel):
    system: str
    ok: bool
    records: int = 0
    attempts: int = 0
    error: str | None = None


class RunResult(BaseModel):
    run_id: str
    as_of: date
    started_at: datetime
    systems: list[SystemStatus]
    hires: list[Hire]
    records: dict[str, list[SystemRecord]]
    findings: list[Finding]
    triage: dict[str, TriageResult]
    remediations: list[Remediation]
    queue_stats: dict[str, int] = Field(default_factory=dict)
