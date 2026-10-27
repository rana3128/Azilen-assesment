from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from recon.models import RunResult
from recon.store import Store

TEMPLATES = Path(__file__).parent / "templates"


def write_report(result: RunResult, store: Store, path: Path) -> Path:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    hires = {h.workday_id: h for h in result.hires}
    by_hire: dict[str, list] = defaultdict(list)
    for f in result.findings:
        by_hire[f.workday_id or "_unlinked"].append(f)
    rem_by_finding = defaultdict(list)
    for r in result.remediations:
        rem_by_finding[r.finding_key].append(r)
    html = env.get_template("report.html.j2").render(
        r=result,
        hires=hires,
        by_hire=dict(sorted(by_hire.items(), key=lambda kv: min(f.severity.rank for f in kv[1]))),
        sev=Counter(f.severity.value for f in result.findings),
        rem_status=Counter(r.status for r in result.remediations),
        rem_by_finding=rem_by_finding,
        audit=list(reversed(store.audit_tail(60))),
        queue=store.list_exceptions("open"),
    )
    path.write_text(html)
    return path
