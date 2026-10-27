from __future__ import annotations

from datetime import datetime
from typing import Any

from recon.connectors.base import Connector
from recon.models import SystemRecord


class AshbyConnector(Connector):
    """Ashby candidate + accepted offer. Owns offer terms until the Workday hire event, then Workday wins."""

    system = "ashby"
    id_path = "id"
    field_paths = {"start_date": "offer.startDate", "location": "offer.location", "title": "offer.jobTitle"}

    def transform(self, raw: dict[str, Any]) -> SystemRecord:
        offer = raw.get("offer", {})
        bg = raw.get("backgroundCheck") or {}
        first, _, last = raw["name"].partition(" ")
        return SystemRecord(
            system=self.system,
            record_id=raw["id"],
            workday_id=raw.get("workdayWorkerId"),
            email=None,  # Ashby holds the personal email; never used as an identity key against corp systems
            name=raw["name"],
            fields={
                "first_name": first,
                "last_name": last,
                "start_date": offer.get("startDate"),
                "title": offer.get("jobTitle"),
                "location": offer.get("location"),
                "offer_status": raw.get("applicationStatus"),
                "background_check_status": bg.get("status", "not_started"),
                "notes": raw.get("notes", []),
            },
            updated_at=datetime.fromisoformat(raw["updatedAt"]),
            raw=raw,
        )
