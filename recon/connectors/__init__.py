from __future__ import annotations

from recon.connectors.ashby import AshbyConnector
from recon.connectors.base import Connector, FailureSpec, MockStore
from recon.connectors.cohort import CohortTrackerConnector
from recon.connectors.expoit import ExpoITConnector
from recon.connectors.google import GoogleConnector
from recon.connectors.okta import OktaConnector
from recon.connectors.slack import SlackConnector
from recon.connectors.workday import WorkdayConnector

DOWNSTREAM: dict[str, type[Connector]] = {
    c.system: c
    for c in (AshbyConnector, OktaConnector, GoogleConnector, SlackConnector, ExpoITConnector, CohortTrackerConnector)
}


def build_connectors(
    store: MockStore, failures: dict[str, FailureSpec] | None = None, **kw
) -> tuple[WorkdayConnector, dict[str, Connector]]:
    failures = failures or {}
    workday = WorkdayConnector(store, failures.get("workday"), **kw)
    return workday, {name: cls(store, failures.get(name), **kw) for name, cls in DOWNSTREAM.items()}
