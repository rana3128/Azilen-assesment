"""Durable state: exception queue, idempotency ledger, approvals, cached Claude verdicts, audit log."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recon.models import Finding, TriageResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS exceptions (
  finding_key TEXT PRIMARY KEY, type TEXT, severity TEXT, workday_id TEXT, system TEXT, message TEXT,
  first_seen TEXT, last_seen TEXT, times_seen INTEGER, status TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  idem_key TEXT PRIMARY KEY, status TEXT, system TEXT, action TEXT, record_id TEXT, field TEXT,
  to_value TEXT, run_id TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (idem_key TEXT PRIMARY KEY, approver TEXT, approved_at TEXT);
CREATE TABLE IF NOT EXISTS verdicts (finding_key TEXT PRIMARY KEY, payload TEXT, created_at TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(out_dir / "recon.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.audit_path = out_dir / "audit.jsonl"

    # ---- audit -----------------------------------------------------------------
    def audit(self, run_id: str, actor: str, event: str, **details: Any) -> None:
        line = {"ts": now(), "run_id": run_id, "actor": actor, "event": event, **details}
        with self.audit_path.open("a") as f:
            f.write(json.dumps(line, default=str) + "\n")

    def audit_tail(self, n: int = 200) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(l) for l in self.audit_path.read_text().splitlines()[-n:]]

    # ---- exception queue ---------------------------------------------------------
    def sync_queue(self, findings: list[Finding], unavailable: set[str]) -> dict[str, int]:
        """Upsert this run's findings; auto-resolve open ones that no longer reproduce.

        A finding on a system we couldn't read this run is *unknown*, not resolved - leave it open.
        """
        ts = now()
        stats = {"new": 0, "recurring": 0, "resolved": 0}
        seen = set()
        with self.db:
            for f in findings:
                seen.add(f.key)
                row = self.db.execute("SELECT status FROM exceptions WHERE finding_key=?", (f.key,)).fetchone()
                if row is None:
                    stats["new"] += 1
                    self.db.execute(
                        "INSERT INTO exceptions VALUES (?,?,?,?,?,?,?,?,1,'open',NULL)",
                        (f.key, f.type.value, f.severity.value, f.workday_id, f.system, f.message, ts, ts))
                else:
                    stats["recurring"] += 1
                    self.db.execute(
                        "UPDATE exceptions SET last_seen=?, times_seen=times_seen+1, status='open', resolved_at=NULL "
                        "WHERE finding_key=?", (ts, f.key))
            for row in self.db.execute("SELECT finding_key, system FROM exceptions WHERE status='open'").fetchall():
                if row["finding_key"] not in seen and row["system"] not in unavailable:
                    stats["resolved"] += 1
                    self.db.execute("UPDATE exceptions SET status='resolved', resolved_at=? WHERE finding_key=?",
                                    (ts, row["finding_key"]))
        return stats

    def list_exceptions(self, status: str | None = "open") -> list[dict[str, Any]]:
        q, args = "SELECT * FROM exceptions", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY last_seen DESC", args)]

    # ---- idempotency ledger --------------------------------------------------------
    def ledger_status(self, key: str) -> str | None:
        row = self.db.execute("SELECT status FROM ledger WHERE idem_key=?", (key,)).fetchone()
        return row["status"] if row else None

    def ledger_set(self, key: str, status: str, run_id: str, **cols: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO ledger (idem_key,status,system,action,record_id,field,to_value,run_id,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(idem_key) DO UPDATE SET status=excluded.status, "
                "run_id=excluded.run_id, updated_at=excluded.updated_at",
                (key, status, cols.get("system"), cols.get("action"), cols.get("record_id"), cols.get("field"),
                 json.dumps(cols.get("to_value"), default=str), run_id, now()))

    # ---- approvals -------------------------------------------------------------------
    def approve(self, key: str, approver: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?)", (key, approver, now()))

    def approved_keys(self) -> set[str]:
        return {r["idem_key"] for r in self.db.execute("SELECT idem_key FROM approvals")}

    # ---- verdict cache -------------------------------------------------------------------
    def get_verdict(self, finding_key: str) -> TriageResult | None:
        row = self.db.execute("SELECT payload FROM verdicts WHERE finding_key=?", (finding_key,)).fetchone()
        return TriageResult.model_validate_json(row["payload"]) if row else None

    def save_verdict(self, result: TriageResult) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?)",
                            (result.finding_key, result.model_dump_json(), now()))
