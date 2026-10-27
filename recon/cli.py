from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from recon.connectors.base import FailureSpec, MockStore
from recon.pipeline import RunAborted, run_pipeline
from recon.report import write_report
from recon.store import Store
from recon.triage import Triage

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "data" / "fixtures"
OUT = Path(os.environ.get("RECON_OUT", ROOT / "out"))
DEFAULT_AS_OF = "2026-10-27"

SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def _parse_failures(specs: list[str]) -> dict[str, FailureSpec]:
    out = {}
    for s in specs:
        system, _, mode = s.partition("=")
        out[system] = FailureSpec.parse(mode)
    return out


def cmd_run(args: argparse.Namespace) -> int:
    triage = Triage(use_llm=not args.no_llm and bool(os.environ.get("ANTHROPIC_API_KEY") or args.force_llm))
    try:
        result = run_pipeline(as_of=date.fromisoformat(args.as_of), fixtures_dir=FIXTURES, out_dir=OUT,
                              triage=triage, failures=_parse_failures(args.fail), apply=args.apply)
    except RunAborted as e:
        print(f"ABORTED: {e}", file=sys.stderr)
        return 2

    print(f"run {result.run_id}  as_of={result.as_of}  triage={triage.mode}  apply={args.apply}\n")
    print("systems:")
    for s in result.systems:
        state = f"ok ({s.records} records, {s.attempts} attempt{'s' if s.attempts != 1 else ''})" if s.ok else f"UNAVAILABLE - {s.error}"
        print(f"  {s.system:<8} {state}")

    sev = Counter(f.severity.value for f in result.findings)
    print("\nfindings: " + "  ".join(f"{k}={sev[k]}" for k in SEV_ORDER if sev[k]) + f"   (total {len(result.findings)})")
    for f in result.findings:
        tag = "  [triaged]" if f.key in result.triage else ""
        print(f"  {f.severity.value:<8} {f.type.value:<20} {(f.workday_id or '-'):<6} {f.system:<7} {f.message}{tag}")

    if result.triage:
        print("\ntriage:")
        for k, t in result.triage.items():
            gate = f"  -> HUMAN ({t.guardrail_note})" if t.requires_human else "  -> auto"
            print(f"  {k} {t.actor}: {t.verdict.decision} ({t.verdict.confidence:.2f}){gate}")

    st = Counter(r.status for r in result.remediations)
    print("\nremediations: " + "  ".join(f"{k}={v}" for k, v in sorted(st.items())))
    for r in result.remediations:
        if r.status != "escalated":
            print(f"  {r.status:<18} {r.idempotency_key} {r.system}.{r.action} {r.record_id or ''} "
                  f"{r.field or ''} {r.from_value!r} -> {r.to_value!r}" + (f"  ERROR {r.error}" if r.error else ""))
    print(f"\nexception queue: {result.queue_stats}")

    report = write_report(result, Store(OUT), OUT / "report.html")
    print(f"report: {report}\naudit:  {OUT / 'audit.jsonl'}")
    return 0


def cmd_reset(_: argparse.Namespace) -> int:
    MockStore(OUT / "state", FIXTURES).reset()
    for f in ("recon.db", "audit.jsonl", "report.html"):
        (OUT / f).unlink(missing_ok=True)
    print(f"reset mock systems from {FIXTURES} and cleared {OUT}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    store = Store(OUT)
    store.approve(args.key, args.by)
    store.audit("-", f"human:{args.by}", "remediation_approved", idempotency_key=args.key)
    print(f"approved {args.key} by {args.by}; it will execute on the next `run --apply`")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    for e in Store(OUT).list_exceptions(None if args.all else "open"):
        print(f"{e['status']:<8} {e['severity']:<8} {e['type']:<20} seen={e['times_seen']} {e['message']}")
    return 0


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="recon", description="Cross-system onboarding reconciliation")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="reconcile all systems against Workday")
    r.add_argument("--apply", action="store_true", help="execute low-risk + approved remediations (default: dry run)")
    r.add_argument("--no-llm", action="store_true", help="use the deterministic triage stub instead of Claude")
    r.add_argument("--force-llm", action="store_true", help="try Claude even if ANTHROPIC_API_KEY is unset (ant auth profile)")
    r.add_argument("--as-of", default=DEFAULT_AS_OF, help="evaluation date (default %(default)s)")
    r.add_argument("--fail", action="append", default=[], metavar="SYSTEM=MODE",
                   help="fault injection, e.g. okta=transient:2, slack=down, expoit=auth")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("reset", help="restore mock systems to fixture state").set_defaults(fn=cmd_reset)

    a = sub.add_parser("approve", help="approve a high-risk remediation by idempotency key")
    a.add_argument("key")
    a.add_argument("--by", required=True)
    a.set_defaults(fn=cmd_approve)

    q = sub.add_parser("queue", help="show the exception queue")
    q.add_argument("--all", action="store_true")
    q.set_defaults(fn=cmd_queue)

    args = p.parse_args(argv)
    sys.exit(args.fn(args))
