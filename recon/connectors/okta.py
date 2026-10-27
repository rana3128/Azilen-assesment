from __future__ import annotations

from datetime import datetime
from typing import Any

from recon.connectors.base import Connector
from recon.models import SystemRecord

LIVE = {"STAGED", "PROVISIONED", "ACTIVE", "RECOVERY", "PASSWORD_EXPIRED", "LOCKED_OUT"}


class OktaConnector(Connector):
    """Okta Users API. Profile is normally mastered by Workday via HR-driven provisioning."""

    system = "okta"
    id_path = "id"
    field_paths = {
        "first_name": "profile.firstName",
        "last_name": "profile.lastName",
        "email": "profile.email",
        "manager_id": "profile.managerId",
        "department": "profile.department",
        "title": "profile.title",
        "start_date": "profile.startDate",
        "account_status": "status",
    }
    writable = frozenset({"manager_id", "department", "title", "start_date", "first_name", "last_name"})

    def transform(self, raw: dict[str, Any]) -> SystemRecord:
        p = raw["profile"]
        return SystemRecord(
            system=self.system,
            record_id=raw["id"],
            workday_id=p.get("employeeNumber"),
            email=(p.get("email") or p.get("login", "")).lower() or None,
            name=f"{p.get('firstName', '')} {p.get('lastName', '')}".strip(),
            fields={
                "first_name": p.get("firstName"),
                "last_name": p.get("lastName"),
                "email": (p.get("email") or "").lower(),
                "manager_id": p.get("managerId"),
                "department": p.get("department"),
                "title": p.get("title"),
                "start_date": p.get("startDate"),
                "account_live": raw["status"] in LIVE,
                "account_status": raw["status"],
            },
            updated_at=datetime.fromisoformat(raw["lastUpdated"]),
            raw=raw,
        )

    def lifecycle_action(self, row: dict[str, Any], action: str) -> None:
        if action == "deactivate_account":
            row["status"] = "DEPROVISIONED"
        else:
            super().lifecycle_action(row, action)
