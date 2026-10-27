from __future__ import annotations

from typing import Any

from recon.connectors.base import Connector
from recon.models import SystemRecord


class GoogleConnector(Connector):
    """Google Workspace Admin SDK Directory users."""

    system = "google"
    id_path = "id"
    field_paths = {
        "first_name": "name.givenName",
        "last_name": "name.familyName",
        "department": "organizations.department",
        "title": "organizations.title",
    }
    writable = frozenset({"department", "title"})

    def transform(self, raw: dict[str, Any]) -> SystemRecord:
        ext = next((e["value"] for e in raw.get("externalIds", []) if e.get("type") == "organization"), None)
        org = raw.get("organizations") or {}
        return SystemRecord(
            system=self.system,
            record_id=raw["id"],
            workday_id=ext,
            email=raw["primaryEmail"].lower(),
            name=f"{raw['name']['givenName']} {raw['name']['familyName']}",
            fields={
                "first_name": raw["name"]["givenName"],
                "last_name": raw["name"]["familyName"],
                "email": raw["primaryEmail"].lower(),
                "department": org.get("department"),
                "title": org.get("title"),
                "account_live": not raw.get("suspended", False),
            },
            raw=raw,
        )

    def lifecycle_action(self, row: dict[str, Any], action: str) -> None:
        if action == "deactivate_account":
            row["suspended"] = True
        else:
            super().lifecycle_action(row, action)
