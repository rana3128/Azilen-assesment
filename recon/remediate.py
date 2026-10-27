"""Turn findings into remediations, then execute them safely.

Policy:
- Low-risk field corrections on systems we have write scope for are auto-applied (with --apply).
- Anything destructive (deactivate, cancel) needs a recorded human approval.
- Never propagate a value that failed source validation - fix it in Workday first.
- Workday itself is never written by this service (HR business processes own it).
- Every write is keyed by the intended end state; the ledger + read-after-write make retries safe.
"""

from __future__ import annotations

from recon.connectors.base import Connector, ConnectorError
from recon.models import Finding, FindingType, Remediation, TriageResult
from recon.store import Store

OWNERS = {
    FindingType.DATA_QUALITY: "People Ops (Workday)",
    FindingType.SOURCE_CONFLICT: "People Ops (Workday)",
    FindingType.OFF_CYCLE_HIRE: "Recruiting Ops",
    FindingType.IDENTITY_AMBIGUOUS: "IT",
    FindingType.ORPHAN_ACCOUNT: "IT",
    FindingType.SYSTEM_UNAVAILABLE: "People Systems on-call",
}
SYSTEM_OWNERS = {"okta": "IT", "google": "IT", "slack": "IT", "expoit": "IT", "cohort": "People Ops onboarding",
                 "ashby": "Recruiting Ops", "workday": "People Ops (Workday)"}


def _escalate(f: Finding, reason: str, owner: str | None = None) -> Remediation:
    return Remediation(finding_key=f.key, system=f.system, action="escalate", record_id=f.record_id, field=f.field,
                       risk="low", reason=reason,
                       owner=owner or f.context.get("owner") or OWNERS.get(f.type) or SYSTEM_OWNERS.get(f.system))


def plan(findings: list[Finding], triage: dict[str, TriageResult], connectors: dict[str, Connector]) -> list[Remediation]:
    out: list[Remediation] = []
    for f in findings:
        t = triage.get(f.key)
        if f.type == FindingType.FIELD_DRIFT:
            conn = connectors.get(f.system)
            if f.context.get("source_failed_validation"):
                out.append(_escalate(f, f"Workday value for {f.field} failed validation; fix at source before propagating",
                                     "People Ops (Workday)"))
            elif conn is None or f.field not in conn.writable:
                out.append(_escalate(f, f"No write scope for {f.system}.{f.field} (identity-bearing or not exposed); "
                                        f"set {f.expected!r} manually"))
            else:
                out.append(Remediation(finding_key=f.key, system=f.system, action="update_field", record_id=f.record_id,
                                       field=f.field, from_value=f.actual, to_value=f.expected, risk="low",
                                       reason=f"Align with Workday ({f.message})", owner=SYSTEM_OWNERS.get(f.system)))
        elif f.type == FindingType.ACCESS_AFTER_RESCIND:
            action = "cancel_order" if f.system == "expoit" else "deactivate_account"
            out.append(Remediation(finding_key=f.key, system=f.system, action=action, record_id=f.record_id,
                                   field=f.field, from_value=f.actual, to_value=f.expected, risk="high",
                                   reason=f.message, owner=SYSTEM_OWNERS.get(f.system)))
        elif f.type == FindingType.IDENTITY_AMBIGUOUS and t and not t.requires_human and t.verdict.decision == "same_person":
            out.append(Remediation(finding_key=f.key, system=f.system, action="link_identity", record_id=f.record_id,
                                   field="workday_id", from_value=None, to_value=f.workday_id, risk="low",
                                   reason=f"{t.actor}: {t.verdict.rationale}", owner="IT"))
        elif t:
            out.append(_escalate(f, f"{t.verdict.suggested_action} [{t.actor}: {t.verdict.decision}, "
                                    f"conf {t.verdict.confidence:.2f}{'; ' + t.guardrail_note if t.guardrail_note else ''}]"))
        else:
            out.append(_escalate(f, f.message))
    return out


def execute(rems: list[Remediation], connectors: dict[str, Connector], store: Store, run_id: str, *,
            apply: bool, unavailable: set[str]) -> None:
    approved = store.approved_keys()
    for r in rems:
        key = r.idempotency_key
        if r.action == "escalate":
            r.status = "escalated"
            continue
        if r.system in unavailable:
            r.status = "blocked_system_unavailable"
            continue
        prior = store.ledger_status(key)
        if prior == "applied":
            r.status = "already_applied"
            continue
        if r.risk == "high" and key not in approved:
            r.status = "awaiting_approval"
            continue
        if not apply:
            r.status = "planned"
            continue

        conn = connectors[r.system]
        cols = dict(system=r.system, action=r.action, record_id=r.record_id, field=r.field, to_value=r.to_value)
        try:
            if prior == "pending" and _already_in_target_state(conn, r):
                # Previous attempt's write landed but we crashed / lost the ack before recording it.
                store.ledger_set(key, "applied", run_id, **cols)
                r.status = "already_applied"
                store.audit(run_id, "recon", "remediation_recovered", idempotency_key=key, system=r.system,
                            record_id=r.record_id, field=r.field)
                continue
            store.ledger_set(key, "pending", run_id, **cols)
            conn.apply(r)
            if r.action == "update_field" and not _already_in_target_state(conn, r):
                raise ConnectorError("read-after-write verification failed")
            store.ledger_set(key, "applied", run_id, **cols)
            r.status = "applied"
            store.audit(run_id, "human:approved" if r.risk == "high" else "recon", "remediation_applied",
                        idempotency_key=key, system=r.system, action=r.action, record_id=r.record_id,
                        field=r.field, before=r.from_value, after=r.to_value)
        except ConnectorError as e:
            store.ledger_set(key, "failed", run_id, **cols)
            r.status, r.error = "failed", str(e)
            store.audit(run_id, "recon", "remediation_failed", idempotency_key=key, system=r.system, error=str(e))


def _already_in_target_state(conn: Connector, r: Remediation) -> bool:
    if r.action != "update_field" or r.record_id is None or r.field is None:
        return False
    return conn.current_value(r.record_id, r.field) == conn.to_raw(r.field, r.to_value)
