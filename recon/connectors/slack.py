from __future__ import annotations

from typing import Any

from recon.connectors.base import Connector
from recon.models import SystemRecord


class SlackConnector(Connector):
    """Slack SCIM v2 Users (enterprise extension carries employeeNumber/manager)."""

    system = "slack"
    id_path = "id"
    field_paths = {
        "title": "title",
        "manager_id": "enterprise.managerId",
        "display_name": "displayName",
        "workday_id": "enterprise.employeeNumber",
    }
    writable = frozenset({"title", "manager_id"})

    def transform(self, raw: dict[str, Any]) -> SystemRecord:
        ent = raw.get("enterprise") or {}
        email = next((e["value"] for e in raw.get("emails", []) if e.get("primary")), None)
        return SystemRecord(
            system=self.system,
            record_id=raw["id"],
            workday_id=ent.get("employeeNumber"),
            email=email.lower() if email else None,
            name=raw.get("displayName"),
            fields={
                "display_name": raw.get("displayName"),
                "email": email.lower() if email else None,
                "title": raw.get("title"),
                "manager_id": ent.get("managerId"),
                "account_live": raw.get("active", True),
            },
            raw=raw,
        )

    def lifecycle_action(self, row: dict[str, Any], action: str) -> None:
        if action == "deactivate_account":
            row["active"] = False
        else:
            super().lifecycle_action(row, action)
