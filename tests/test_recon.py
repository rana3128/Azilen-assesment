from datetime import date
from pathlib import Path

import pytest

from recon.connectors.base import FailureSpec
from recon.models import FindingType
from recon.pipeline import RunAborted, run_pipeline
from recon.triage import Triage

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures"
AS_OF = date(2026, 10, 27)


def run(tmp_path, **kw):
    return run_pipeline(as_of=AS_OF, fixtures_dir=FIXTURES, out_dir=tmp_path, triage=Triage(use_llm=False),
                        connector_kw={"sleep": lambda _: None}, **kw)


def types_for(result, wid):
    return {f.type for f in result.findings if f.workday_id == wid}


def test_seeded_scenarios_detected(tmp_path):
    r = run(tmp_path)
    assert FindingType.ACCESS_AFTER_RESCIND in types_for(r, "W1002")
    assert FindingType.GATE_BREACH in types_for(r, "W1005")
    assert FindingType.OFF_CYCLE_HIRE in types_for(r, "W1006")
    assert FindingType.DATA_QUALITY in types_for(r, "W1007")
    assert FindingType.SOURCE_CONFLICT in types_for(r, "W1008")
    assert {f.system for f in r.findings if f.type == FindingType.ORPHAN_ACCOUNT} == {"google", "slack"}
    # Jon's unlinked Slack account goes to identity triage - not double-reported as missing
    jon = [f for f in r.findings if f.workday_id == "W1003"]
    assert [f.type for f in jon] == [FindingType.IDENTITY_AMBIGUOUS]


def test_destructive_actions_need_approval_and_bad_source_data_not_propagated(tmp_path):
    r = run(tmp_path, apply=True)
    for rem in r.remediations:
        if rem.action in ("deactivate_account", "cancel_order"):
            assert rem.status == "awaiting_approval"
    assert not any(rem.field == "ship_address" and rem.action == "update_field" for rem in r.remediations)


def test_apply_is_idempotent_and_converges(tmp_path):
    first = run(tmp_path, apply=True)
    applied = {r.idempotency_key for r in first.remediations if r.status == "applied"}
    assert applied
    second = run(tmp_path, apply=True)
    # nothing applied in run 1 is re-applied; fixed findings are auto-resolved in the queue
    assert not applied & {r.idempotency_key for r in second.remediations if r.status == "applied"}
    assert second.queue_stats["resolved"] >= len(applied) - 1


def test_downstream_outage_is_isolated(tmp_path):
    r = run(tmp_path, failures={"slack": FailureSpec("down"), "okta": FailureSpec("transient", 2)})
    assert any(f.type == FindingType.SYSTEM_UNAVAILABLE and f.system == "slack" for f in r.findings)
    assert not any(f.system == "slack" and f.type == FindingType.MISSING_RECORD for f in r.findings)
    assert next(s for s in r.systems if s.system == "okta").ok  # recovered via retry


def test_workday_outage_aborts(tmp_path):
    with pytest.raises(RunAborted):
        run(tmp_path, failures={"workday": FailureSpec("auth")})


def test_low_confidence_verdict_routed_to_human(tmp_path):
    r = run(tmp_path)
    orphan_verdicts = [t for k, t in r.triage.items()
                       if any(f.key == k and f.type == FindingType.ORPHAN_ACCOUNT for f in r.findings)]
    assert orphan_verdicts and all(t.requires_human for t in orphan_verdicts)
