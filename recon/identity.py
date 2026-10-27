"""Cross-system identity resolution.

Strongest key wins: Workday ID carried on the record (employeeNumber / externalId / workdayWorkerId),
then exact work email, then an *alias* email learned from another record already linked by ID
(this is how a typo'd email that cascaded into ExpoIT still resolves deterministically).
Anything left gets a fuzzy name candidate - which is never auto-linked; it goes to triage.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from recon.models import Hire, SystemRecord

NICKNAMES = {
    "jon": "jonathan", "jonny": "jonathan", "alex": "alexander", "sam": "samuel", "liz": "elizabeth",
    "beth": "elizabeth", "mike": "michael", "bob": "robert", "rob": "robert", "kate": "katherine",
    "chris": "christopher", "dan": "daniel", "will": "william", "bill": "william", "matt": "matthew",
}
FUZZY_THRESHOLD = 0.75


@dataclass
class FuzzyCandidate:
    record: SystemRecord
    hire: Hire
    score: float
    reasons: list[str]


@dataclass
class Resolution:
    linked: dict[str, dict[str, list[SystemRecord]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    method: dict[tuple[str, str], str] = field(default_factory=dict)
    fuzzy: list[FuzzyCandidate] = field(default_factory=list)
    orphans: list[SystemRecord] = field(default_factory=list)

    def records(self, workday_id: str, system: str) -> list[SystemRecord]:
        return self.linked.get(workday_id, {}).get(system, [])


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z ]", "", (s or "").lower()).strip()


def name_score(record_name: str | None, hire: Hire) -> tuple[float, list[str]]:
    rn = _norm(record_name).split()
    if not rn:
        return 0.0, []
    reasons: list[str] = []
    r_first, r_last = rn[0], rn[-1]
    firsts = {_norm(hire.first_name), _norm(hire.preferred_name)} - {""}
    last = _norm(hire.last_name)
    last_sim = SequenceMatcher(None, r_last, last).ratio()
    first_ok = r_first in firsts or NICKNAMES.get(r_first) in firsts or any(NICKNAMES.get(f) == r_first for f in firsts)
    if first_ok:
        reasons.append(f"first name '{r_first}' matches {sorted(firsts)} (incl. nickname table)")
    if last_sim == 1.0:
        reasons.append("last name exact")
    elif last_sim >= 0.8:
        reasons.append(f"last name similar ({last_sim:.2f})")
    first_sim = 1.0 if first_ok else max(SequenceMatcher(None, r_first, f).ratio() for f in firsts)
    return round(0.4 * first_sim + 0.6 * last_sim, 3), reasons


def resolve(workers: list[Hire], in_scope: set[str], records: dict[str, list[SystemRecord]]) -> Resolution:
    res = Resolution()
    by_id = {w.workday_id: w for w in workers}
    by_email = {w.email: w.workday_id for w in workers}
    by_candidate = {w.ashby_candidate_id: w.workday_id for w in workers if w.ashby_candidate_id}

    def link(rec: SystemRecord, wid: str, how: str) -> None:
        res.linked[wid][rec.system].append(rec)
        res.method[(rec.system, rec.record_id)] = how

    pending: list[SystemRecord] = []
    for system, recs in records.items():
        for rec in recs:
            wid = rec.workday_id if rec.workday_id in by_id else None
            if system == "ashby" and not wid:
                wid = by_candidate.get(rec.record_id)
            if wid:
                link(rec, wid, "workday_id")
            else:
                pending.append(rec)

    aliases: dict[str, str] = {}
    for wid, systems in res.linked.items():
        for recs in systems.values():
            for r in recs:
                if r.email and r.email not in by_email:
                    aliases[r.email] = wid

    unresolved: list[SystemRecord] = []
    for rec in pending:
        if rec.email and rec.email in by_email:
            link(rec, by_email[rec.email], "email")
        elif rec.email and rec.email in aliases:
            link(rec, aliases[rec.email], "alias_email")
        else:
            unresolved.append(rec)

    for rec in unresolved:
        if rec.system == "ashby":
            continue  # candidates who never became workers are normal
        best: FuzzyCandidate | None = None
        for wid in in_scope:
            score, reasons = name_score(rec.name, by_id[wid])
            if score >= FUZZY_THRESHOLD and (best is None or score > best.score):
                best = FuzzyCandidate(rec, by_id[wid], score, reasons)
        if best:
            res.fuzzy.append(best)
        else:
            res.orphans.append(rec)
    return res
