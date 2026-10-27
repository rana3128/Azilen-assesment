from __future__ import annotations

from typing import Any

from recon.connectors.base import Connector
from recon.models import SystemRecord


class CohortTrackerConnector(Connector):
    """Onboarding cohort tracker (a Google Sheet today). Rows keyed by work email; free-text names."""

    system = "cohort"
    id_path = "row_id"
    field_paths = {
        "name": "Name",
        "start_date": "Start Date",
        "cohort": "Cohort",
        "manager_name": "Manager",
    }
    writable = frozenset({"name", "start_date", "cohort", "manager_name"})

    def transform(self, raw: dict[str, Any]) -> SystemRecord:
        return SystemRecord(
            system=self.system,
            record_id=raw["row_id"],
            email=(raw.get("Work Email") or "").lower() or None,
            name=raw.get("Name"),
            fields={
                "name": raw.get("Name"),
                "start_date": raw.get("Start Date"),
                "cohort": raw.get("Cohort"),
                "manager_name": raw.get("Manager"),
            },
            raw=raw,
        )
