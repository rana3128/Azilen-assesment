"""MCP server exposing the reconciliation engine to Claude (Claude Code / Claude Desktop / an n8n AI node).

Lets an ops person ask "what's blocking next Monday's cohort?" and have Claude answer from live
cross-system state instead of someone opening seven tabs. Read tools are side-effect free;
the one write tool records a human approval - it never touches a downstream system directly.

Run:  .venv/bin/python mcp_server.py        (stdio transport)
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer

from recon.cli import DEFAULT_AS_OF, FIXTURES, OUT
from recon.connectors import build_connectors
from recon.connectors.base import ConnectorError, MockStore
from recon.identity import resolve
from recon.rules import RuleContext, evaluate, expected_fields
from recon.store import Store

mcp = MCPServer(
    name="betterup-onboarding-recon",
    instructions="Cross-system onboarding state for BetterUp new hires. Workday is the system of record. "
                 "Use list_open_exceptions for the queue, get_hire_view for one person across all systems, "
                 "and approve_remediation only when the user explicitly approves a specific idempotency key.",
)


def _snapshot(as_of: str):
    mock = MockStore(OUT / "state", FIXTURES)
    mock.ensure()
    workday, conns = build_connectors(mock)
    workers = {w.workday_id: w for w in workday.fetch_hires()}
    records, unavailable = {}, set()
    for name, c in conns.items():
        try:
            records[name] = c.fetch_all()
        except ConnectorError:
            unavailable.add(name)
    ctx = RuleContext(as_of=date.fromisoformat(as_of), workers=workers, res=None, unavailable=unavailable)  # type: ignore[arg-type]
    ctx.res = resolve(list(workers.values()), {h.workday_id for h in ctx.in_scope()}, records)
    return ctx, evaluate(ctx)


@mcp.tool()
def list_open_exceptions(severity: str | None = None, workday_id: str | None = None) -> list[dict[str, Any]]:
    """Open items in the onboarding exception queue (from the last reconciliation run).

    severity: optional filter - critical | high | medium | low.
    workday_id: optional filter to one worker, e.g. W1005.
    """
    rows = Store(OUT).list_exceptions("open")
    return [r for r in rows if (not severity or r["severity"] == severity) and (not workday_id or r["workday_id"] == workday_id)]


@mcp.tool()
def get_hire_view(workday_id: str, as_of: str = DEFAULT_AS_OF) -> dict[str, Any]:
    """Live 360° view of one hire: Workday record, what every downstream system holds vs. what it should hold,
    and current findings. Read-only - does not write the queue or any system."""
    ctx, findings = _snapshot(as_of)
    h = ctx.workers.get(workday_id)
    if not h:
        return {"error": f"unknown workday_id {workday_id}"}
    systems = {}
    for system in ("ashby", "okta", "google", "slack", "expoit", "cohort"):
        recs = ctx.res.records(workday_id, system)
        systems[system] = {
            "unavailable": system in ctx.unavailable,
            "linked_by": [ctx.res.method.get((system, r.record_id)) for r in recs],
            "records": [{"record_id": r.record_id, **{k: v for k, v in r.fields.items() if k != "notes"}} for r in recs],
            "expected": expected_fields(system, h, ctx.workers),
        }
    return {
        "workday": h.model_dump(mode="json"),
        "systems": systems,
        "findings": [f.model_dump(mode="json", include={"key", "type", "severity", "system", "field", "message"})
                     for f in findings if f.workday_id == workday_id],
    }


@mcp.tool()
def approve_remediation(idempotency_key: str, approver_email: str) -> str:
    """Record a human approval for a high-risk remediation (deactivate account, cancel hardware order).

    It executes on the next `recon run --apply`. Only call this when the user explicitly approves this key.
    """
    store = Store(OUT)
    store.approve(idempotency_key, approver_email)
    store.audit("-", f"human:{approver_email}", "remediation_approved", idempotency_key=idempotency_key, via="mcp")
    return f"Approved {idempotency_key} by {approver_email}; will execute on the next apply run."


if __name__ == "__main__":
    mcp.run()
