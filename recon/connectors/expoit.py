from __future__ import annotations

from typing import Any

from recon.connectors.base import Connector
from recon.models import SystemRecord

OPEN = {"submitted", "ordered", "shipped"}


class ExpoITConnector(Connector):
    """ExpoIT hardware orders. Keyed by work email only - no employee ID on the order."""

    system = "expoit"
    id_path = "order_id"
    field_paths = {
        "ship_address": "ship_to",
        "ship_by": "ship_by",
        "start_date": "start_date",
        "recipient_name": "recipient_name",
    }
    writable = frozenset({"ship_address", "ship_by", "start_date", "recipient_name"})

    def transform(self, raw: dict[str, Any]) -> SystemRecord:
        return SystemRecord(
            system=self.system,
            record_id=raw["order_id"],
            email=raw["employee_email"].lower(),
            name=raw.get("recipient_name"),
            fields={
                "recipient_name": raw.get("recipient_name"),
                "ship_address": raw.get("ship_to"),
                "ship_by": raw.get("ship_by"),
                "start_date": raw.get("start_date"),
                "order_status": raw.get("status"),
                "order_open": raw.get("status") in OPEN,
                "form_submitted_at": raw.get("form_submitted_at"),
            },
            raw=raw,
        )

    def lifecycle_action(self, row: dict[str, Any], action: str) -> None:
        if action == "cancel_order":
            row["status"] = "cancelled"
        else:
            super().lifecycle_action(row, action)
