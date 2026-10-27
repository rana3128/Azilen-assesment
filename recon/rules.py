"""Deterministic reconciliation rules. Everything here is pure: (workers, records, as_of) -> findings.

Claude never decides what is *wrong*; these rules do. Claude only gets findings marked ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from recon.identity import Resolution
from recon.models import Finding, FindingType, Hire, Severity, SystemRecord

ACCOUNT_SYSTEMS = ("okta", "google", "slack")
SCOPE_LOOKBACK_DAYS = 30
SHIP_LEAD_DAYS = 5
AT_RISK_DAYS = 3

POSTAL_PATTERNS = {
    "US": r"^\d{5}(-\d{4})?$",
    "CA": r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$",
    "GB": r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$",
}
LOCATION_COUNTRY = {"Remote - US": "US", "New York, NY": "US", "San Francisco, CA": "US", "London, UK": "GB", "Toronto, ON": "CA"}

FIELD_SEVERITY = {
    "start_date": Severity.HIGH,
    "ship_address": Severity.HIGH,
    "email": Severity.HIGH,
    "ship_by": Severity.MEDIUM,
    "manager_id": Severity.MEDIUM,
    "manager_name": Severity.MEDIUM,
    "first_name": Severity.MEDIUM,
    "last_name": Severity.MEDIUM,
    "display_name": Severity.MEDIUM,
    "recipient_name": Severity.MEDIUM,
    "name": Severity.LOW,
    "cohort": Severity.LOW,
    "title": Severity.LOW,
    "department": Severity.LOW,
}


def cohort_week(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def expected_fields(system: str, h: Hire, workers: dict[str, Hire]) -> dict[str, Any]:
    """Field ownership as code: what each downstream system *should* hold, derived from the Workday record."""
    mgr = workers.get(h.manager_id or "")
    if system == "okta":
        return {"first_name": h.display_first, "last_name": h.last_name, "email": h.email, "manager_id": h.manager_id,
                "department": h.department, "title": h.title, "start_date": h.start_date.isoformat()}
    if system == "google":
        return {"first_name": h.display_first, "last_name": h.last_name, "email": h.email,
                "department": h.department, "title": h.title}
    if system == "slack":
        return {"display_name": h.display_name, "email": h.email, "title": h.title, "manager_id": h.manager_id}
    if system == "expoit":
        a = h.address
        return {"recipient_name": h.display_name,
                "ship_address": {"line1": a.line1, "city": a.city, "region": a.region, "postal_code": a.postal_code, "country": a.country},
                "start_date": h.start_date.isoformat(),
                "ship_by": (h.start_date - timedelta(days=SHIP_LEAD_DAYS)).isoformat()}
    if system == "cohort":
        return {"name": h.display_name, "start_date": h.start_date.isoformat(), "cohort": cohort_week(h.start_date),
                "manager_name": mgr.display_name if mgr else None}
    return {}


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return a == b


@dataclass
class Gate:
    name: str
    days_before_start: int
    owner: str
    check: Callable[[Hire, Resolution], tuple[bool, str]]


def _okta_gate(h: Hire, res: Resolution) -> tuple[bool, str]:
    live = [r for r in res.records(h.workday_id, "okta") if r.fields.get("account_live")]
    return bool(live), "Okta account provisioned" if live else "no Okta account"


def _hardware_gate(h: Hire, res: Resolution) -> tuple[bool, str]:
    orders = [r for r in res.records(h.workday_id, "expoit") if r.fields.get("order_status") != "cancelled"]
    return bool(orders), "hardware order on file" if orders else "IT hardware form not submitted"


def _bg_gate(h: Hire, res: Resolution) -> tuple[bool, str]:
    ash = res.records(h.workday_id, "ashby")
    if not ash:
        return False, "no Ashby record - background check is not tracked anywhere"
    status = ash[0].fields.get("background_check_status")
    return status == "clear", f"background check status: {status}"


GATES = [
    Gate("okta_provisioned", 7, "IT", _okta_gate),
    Gate("hardware_order", 10, "IT / ExpoIT", _hardware_gate),
    Gate("background_check", 7, "Recruiting Ops", _bg_gate),
]


@dataclass
class RuleContext:
    as_of: date
    workers: dict[str, Hire]
    res: Resolution
    unavailable: set[str] = field(default_factory=set)
    dq_failed: dict[str, set[str]] = field(default_factory=dict)

    def in_scope(self) -> list[Hire]:
        cutoff = self.as_of - timedelta(days=SCOPE_LOOKBACK_DAYS)
        return [w for w in self.workers.values()
                if w.status in ("pre_hire", "rescinded") or (w.status == "active" and w.start_date >= cutoff)]


def check_data_quality(h: Hire, ctx: RuleContext) -> list[Finding]:
    """Validate the *source* record. Catching bad data here stops it cascading to email, labels, tracker."""
    out: list[Finding] = []
    failed = ctx.dq_failed.setdefault(h.workday_id, set())

    def dq(field_: str, sev: Severity, msg: str, actual: Any) -> None:
        failed.add(field_)
        out.append(Finding(type=FindingType.DATA_QUALITY, severity=sev, system="workday", workday_id=h.workday_id,
                           record_id=h.workday_id, field=field_, actual=actual, message=msg))

    pat = POSTAL_PATTERNS.get(h.address.country)
    if pat and not re.match(pat, h.address.postal_code.upper()):
        dq("ship_address", Severity.HIGH,
           f"Postal code '{h.address.postal_code}' is invalid for {h.address.country}; shipping label will fail", h.address.postal_code)
    loc_country = LOCATION_COUNTRY.get(h.location)
    if loc_country and loc_country != h.address.country:
        dq("location", Severity.HIGH, f"Work location '{h.location}' ({loc_country}) disagrees with home country {h.address.country}", h.location)
    local = h.email.split("@")[0]
    allowed = {f"{_slug(h.first_name)}.{_slug(h.last_name)}", f"{_slug(h.display_first)}.{_slug(h.last_name)}"}
    if local not in allowed:
        dq("email", Severity.MEDIUM, f"Email '{h.email}' doesn't follow first.last convention for {h.display_name}", h.email)
    if h.status == "pre_hire" and h.start_date < ctx.as_of:
        dq("start_date", Severity.HIGH, f"Start date {h.start_date} has passed but worker is still Pre-Hire", h.start_date.isoformat())
    if h.manager_id and h.manager_id not in ctx.workers:
        dq("manager_id", Severity.HIGH, f"Manager {h.manager_id} does not exist in Workday", h.manager_id)
    for part in (h.first_name, h.last_name):
        if part != part.strip() or part[:1].islower():
            dq("name", Severity.LOW, f"Name '{part}' has formatting issues (case/whitespace)", part)
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def check_source_conflict(h: Hire, ctx: RuleContext) -> list[Finding]:
    out: list[Finding] = []
    for ash in ctx.res.records(h.workday_id, "ashby"):
        if ash.fields.get("offer_status") == "Rescinded" and h.is_live:
            out.append(Finding(type=FindingType.SOURCE_CONFLICT, severity=Severity.CRITICAL, system="ashby",
                               workday_id=h.workday_id, record_id=ash.record_id, field="status",
                               expected="Rescinded", actual=h.status,
                               message="Offer rescinded in Ashby but Workday still shows a live worker"))
        canonical = {"start_date": h.start_date.isoformat(), "title": h.title, "location": h.location}
        for f, wd_val in canonical.items():
            a_val = ash.fields.get(f)
            if a_val is None or _eq(a_val, wd_val):
                continue
            if ash.updated_at and ash.updated_at > h.updated_at:
                out.append(Finding(
                    type=FindingType.SOURCE_CONFLICT, severity=Severity.HIGH, system="ashby", workday_id=h.workday_id,
                    record_id=ash.record_id, field=f, expected=wd_val, actual=a_val, ambiguous=True,
                    message=f"Ashby {f} ({a_val}) changed after Workday ({wd_val}); change never reached the system of record",
                    context={"workday_value": wd_val, "workday_updated_at": h.updated_at.isoformat(),
                             "ashby_value": a_val, "ashby_updated_at": ash.updated_at.isoformat(),
                             "ashby_notes": ash.fields.get("notes", [])}))
    return out


def check_rescinded(h: Hire, ctx: RuleContext) -> list[Finding]:
    out: list[Finding] = []
    for system in ACCOUNT_SYSTEMS:
        for r in ctx.res.records(h.workday_id, system):
            if r.fields.get("account_live"):
                out.append(Finding(type=FindingType.ACCESS_AFTER_RESCIND, severity=Severity.CRITICAL, system=system,
                                   workday_id=h.workday_id, record_id=r.record_id, field="account_live",
                                   expected=False, actual=True,
                                   message=f"{system} account still live for {h.status} hire {h.display_name}"))
    for r in ctx.res.records(h.workday_id, "expoit"):
        if r.fields.get("order_open"):
            out.append(Finding(type=FindingType.ACCESS_AFTER_RESCIND, severity=Severity.HIGH, system="expoit",
                               workday_id=h.workday_id, record_id=r.record_id, field="order_status",
                               expected="cancelled", actual=r.fields.get("order_status"),
                               message=f"Hardware order {r.record_id} still open for {h.status} hire"))
    return out


def check_drift(h: Hire, ctx: RuleContext) -> list[Finding]:
    out: list[Finding] = []
    for system in ("okta", "google", "slack", "expoit", "cohort"):
        expected = expected_fields(system, h, ctx.workers)
        for rec in ctx.res.records(h.workday_id, system):
            for f, exp in expected.items():
                act = rec.fields.get(f)
                if exp is None or _eq(act, exp):
                    continue
                out.append(Finding(
                    type=FindingType.FIELD_DRIFT, severity=FIELD_SEVERITY.get(f, Severity.LOW), system=system,
                    workday_id=h.workday_id, record_id=rec.record_id, field=f, expected=exp, actual=act,
                    message=f"{system}.{f} is {act!r}, Workday says {exp!r}",
                    context={"source_failed_validation": f in ctx.dq_failed.get(h.workday_id, set())}))
    return out


def check_missing(h: Hire, ctx: RuleContext) -> list[Finding]:
    out: list[Finding] = []
    okta_live = any(r.fields.get("account_live") for r in ctx.res.records(h.workday_id, "okta"))
    # Google/Slack are pushed from Okta; if Okta itself is missing that's one root cause (the Okta gate), not three.
    expected_systems = (["google", "slack"] if okta_live else []) + ["cohort"]
    # A record pending identity triage exists; it just isn't linked yet. Reporting it as missing double-counts.
    pending_identity = {c.record.system for c in ctx.res.fuzzy if c.hire.workday_id == h.workday_id}
    for system in expected_systems:
        if system in ctx.unavailable or system in pending_identity or ctx.res.records(h.workday_id, system):
            continue
        out.append(Finding(type=FindingType.MISSING_RECORD, severity=Severity.MEDIUM, system=system,
                           workday_id=h.workday_id,
                           message=f"{h.display_name} has no {system} record" +
                                   (" although Okta is provisioned (push failed?)" if system != "cohort" else "")))
    return out


def check_gates(h: Hire, ctx: RuleContext) -> list[Finding]:
    out: list[Finding] = []
    for g in GATES:
        dep = {"okta_provisioned": "okta", "hardware_order": "expoit", "background_check": "ashby"}[g.name]
        if dep in ctx.unavailable:
            continue
        ok, detail = g.check(h, ctx.res)
        if ok:
            continue
        due = h.start_date - timedelta(days=g.days_before_start)
        days_left = (due - ctx.as_of).days
        if days_left < 0:
            ftype, sev = FindingType.GATE_BREACH, Severity.CRITICAL if (h.start_date - ctx.as_of).days <= 3 else Severity.HIGH
            msg = f"{g.name} overdue by {-days_left}d (due {due}, start {h.start_date}): {detail}"
        elif days_left <= AT_RISK_DAYS:
            ftype, sev = FindingType.GATE_AT_RISK, Severity.MEDIUM
            msg = f"{g.name} due in {days_left}d ({due}): {detail}"
        else:
            continue
        out.append(Finding(type=ftype, severity=sev, system=dep, workday_id=h.workday_id, field=g.name,
                           expected=f"done by {due}", actual=detail, message=msg,
                           context={"owner": g.owner, "due": due.isoformat(), "start_date": h.start_date.isoformat()}))
    return out


def check_off_cycle(h: Hire, ctx: RuleContext) -> list[Finding]:
    if h.ashby_candidate_id or ctx.res.records(h.workday_id, "ashby") or "ashby" in ctx.unavailable:
        return []
    return [Finding(type=FindingType.OFF_CYCLE_HIRE, severity=Severity.HIGH, system="ashby", workday_id=h.workday_id,
                    message=f"{h.display_name} was created directly in Workday (no Ashby offer): no offer record, "
                            f"no background check tracked, not auto-added to cohort tracker")]


def check_identity(ctx: RuleContext) -> list[Finding]:
    out: list[Finding] = []
    for c in ctx.res.fuzzy:
        notes = [n for a in ctx.res.records(c.hire.workday_id, "ashby") for n in a.fields.get("notes", [])]
        out.append(Finding(
            type=FindingType.IDENTITY_AMBIGUOUS, severity=Severity.MEDIUM, system=c.record.system,
            workday_id=c.hire.workday_id, record_id=c.record.record_id, field="workday_id",
            expected=c.hire.workday_id, actual=c.record.email, ambiguous=True,
            message=f"{c.record.system} account '{c.record.name}' <{c.record.email}> has no employee ID; "
                    f"fuzzy match to {c.hire.display_name} ({c.score:.2f})",
            context={"record": {"name": c.record.name, "email": c.record.email, **_safe(c.record.fields)},
                     "candidate": {"workday_id": c.hire.workday_id, "legal_name": f"{c.hire.first_name} {c.hire.last_name}",
                                   "preferred_name": c.hire.preferred_name, "email": c.hire.email,
                                   "title": c.hire.title, "start_date": c.hire.start_date.isoformat()},
                     "score": c.score, "match_reasons": c.reasons, "recruiter_notes": notes}))
    for r in ctx.res.orphans:
        if r.system not in ACCOUNT_SYSTEMS or not r.fields.get("account_live", True):
            continue
        out.append(Finding(
            type=FindingType.ORPHAN_ACCOUNT, severity=Severity.MEDIUM, system=r.system, record_id=r.record_id,
            actual=r.email, ambiguous=True,
            message=f"Live {r.system} account '{r.name}' <{r.email}> matches no Workday worker",
            context={"record": {"name": r.name, "email": r.email, **_safe(r.fields)}}))
    return out


def _safe(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if not isinstance(v, (datetime,))}


def evaluate(ctx: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    for h in sorted(ctx.in_scope(), key=lambda w: w.workday_id):
        findings += check_data_quality(h, ctx)
        if h.status in ("rescinded", "terminated"):
            findings += check_rescinded(h, ctx)
            continue
        findings += check_source_conflict(h, ctx)
        findings += check_off_cycle(h, ctx)
        findings += check_drift(h, ctx)
        findings += check_missing(h, ctx)
        findings += check_gates(h, ctx)
    findings += check_identity(ctx)
    findings.sort(key=lambda f: (f.severity.rank, f.workday_id or "~", f.system))
    return findings
