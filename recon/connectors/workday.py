from __future__ import annotations

from datetime import date, datetime
from typing import Any

from recon.connectors.base import Connector
from recon.models import Address, Hire, SystemRecord

STATUS_MAP = {"Pre-Hire": "pre_hire", "Active": "active", "Rescinded": "rescinded", "Terminated": "terminated"}


class WorkdayConnector(Connector):
    """Workday RaaS 'Onboarding Workers' report (read-only). Workday writes go through HR business processes."""

    system = "workday"
    id_path = "Worker_ID"

    def to_hire(self, raw: dict[str, Any]) -> Hire:
        addr = raw["Home_Address"]
        return Hire(
            workday_id=raw["Worker_ID"],
            first_name=raw["Legal_Name"]["First"],
            last_name=raw["Legal_Name"]["Last"],
            preferred_name=raw.get("Preferred_First_Name") or None,
            email=raw["Email"].lower(),
            title=raw["Job_Title"],
            department=raw["Supervisory_Org"],
            manager_id=raw.get("Manager_Worker_ID"),
            start_date=date.fromisoformat(raw["Hire_Date"]),
            location=raw["Location"],
            address=Address(
                line1=addr["Address_Line_1"],
                city=addr["City"],
                region=addr["Region"],
                postal_code=addr["Postal_Code"],
                country=addr["Country"],
            ),
            status=STATUS_MAP[raw["Worker_Status"]],
            ashby_candidate_id=raw.get("Ashby_Candidate_ID") or None,
            updated_at=datetime.fromisoformat(raw["Last_Updated"]),
        )

    def fetch_hires(self) -> list[Hire]:
        return [self.to_hire(r) for r in self._with_retry(self._read_raw)]

    def transform(self, raw: dict[str, Any]) -> SystemRecord:
        h = self.to_hire(raw)
        return SystemRecord(
            system=self.system,
            record_id=h.workday_id,
            workday_id=h.workday_id,
            email=h.email,
            name=h.display_name,
            fields=h.model_dump(mode="json"),
            updated_at=h.updated_at,
            raw=raw,
        )
