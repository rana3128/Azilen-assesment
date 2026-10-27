from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

from recon.connectors import build_connectors
from recon.connectors.base import ConnectorError, FailureSpec, MockStore
from recon.identity import resolve
from recon.models import Finding, FindingType, RunResult, Severity, SystemStatus, TriageResult
from recon.remediate import execute, plan
from recon.rules import RuleContext, evaluate
from recon.store import Store
from recon.triage import Triage


class RunAborted(Exception):
    """Workday (system of record) unreadable: without the source of truth every comparison would be wrong."""


def run_pipeline(*, as_of: date, fixtures_dir: Path, out_dir: Path, triage: Triage,
                 failures: dict[str, FailureSpec] | None = None, apply: bool = False,
                 connector_kw: dict | None = None) -> RunResult:
    run_id = f"{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    started = datetime.now()
    mock = MockStore(out_dir / "state", fixtures_dir)
    mock.ensure()
    store = Store(out_dir)
    workday, conns = build_connectors(mock, failures, **(connector_kw or {}))
    store.audit(run_id, "recon", "run_started", as_of=as_of, apply=apply, triage=triage.mode)

    try:
        workers = workday.fetch_hires()
    except ConnectorError as e:
        store.audit(run_id, "recon", "run_aborted", reason=str(e))
        raise RunAborted(f"Workday unavailable, aborting run: {e}") from e
    statuses = [SystemStatus(system="workday", ok=True, records=len(workers), attempts=workday.last_attempts)]

    records, unavailable, findings = {}, set(), []

    def fetch(name: str):
        try:
            return name, conns[name].fetch_all(), None
        except ConnectorError as e:
            return name, [], e

    with ThreadPoolExecutor(max_workers=len(conns)) as pool:
        for name, recs, err in pool.map(fetch, conns):
            c = conns[name]
            if err:
                unavailable.add(name)
                statuses.append(SystemStatus(system=name, ok=False, attempts=c.last_attempts, error=str(err)))
                findings.append(Finding(type=FindingType.SYSTEM_UNAVAILABLE, severity=Severity.HIGH, system=name,
                                        message=f"{name} unreadable this run ({err}); its checks were skipped, "
                                                f"its open exceptions stay open, no writes attempted",
                                        actual=type(err).__name__))
            else:
                records[name] = recs
                statuses.append(SystemStatus(system=name, ok=True, records=len(recs), attempts=c.last_attempts))

    by_id = {w.workday_id: w for w in workers}
    ctx = RuleContext(as_of=as_of, workers=by_id, res=None, unavailable=unavailable)  # type: ignore[arg-type]
    ctx.res = resolve(workers, {h.workday_id for h in ctx.in_scope()}, records)
    findings += evaluate(ctx)

    verdicts: dict[str, TriageResult] = {}
    for f in findings:
        if not f.ambiguous:
            continue
        cached = store.get_verdict(f.key)
        if cached:
            verdicts[f.key] = cached
            continue
        result = triage.run(f)
        store.save_verdict(result)
        verdicts[f.key] = result
        store.audit(run_id, result.actor, "triage_verdict", finding_key=f.key, finding_type=f.type.value,
                    decision=result.verdict.decision, confidence=result.verdict.confidence,
                    requires_human=result.requires_human, guardrail=result.guardrail_note,
                    rationale=result.verdict.rationale)

    rems = plan(findings, verdicts, conns)
    execute(rems, conns, store, run_id, apply=apply, unavailable=unavailable)
    queue_stats = store.sync_queue(findings, unavailable)

    counts: dict[str, int] = {}
    for r in rems:
        counts[r.status] = counts.get(r.status, 0) + 1
    store.audit(run_id, "recon", "run_completed", findings=len(findings), queue=queue_stats, remediations=counts,
                unavailable=sorted(unavailable))

    return RunResult(run_id=run_id, as_of=as_of, started_at=started, systems=statuses, hires=ctx.in_scope(),
                     records=records, findings=findings, triage=verdicts, remediations=rems, queue_stats=queue_stats)
