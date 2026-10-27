"""Claude as a bounded exception handler.

Only findings the rules marked `ambiguous` reach here: identity matches the rules can't prove, orphaned
accounts, and Ashby-vs-Workday conflicts where the answer lives in free-text recruiter notes.
Claude returns a schema-validated Verdict; code-side guardrails decide whether that verdict may drive
an automated action or must go to a human. Claude never writes to any system.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from recon.models import Finding, FindingType, TriageResult, Verdict

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
MIN_CONFIDENCE = 0.8

ALLOWED: dict[FindingType, set[str]] = {
    FindingType.IDENTITY_AMBIGUOUS: {"same_person", "different_person", "needs_human"},
    FindingType.ORPHAN_ACCOUNT: {"non_employee_account", "needs_human"},
    FindingType.SOURCE_CONFLICT: {"trust_workday", "trust_ashby", "needs_human"},
}

SYSTEM_PROMPT = """You are the exception handler inside BetterUp's onboarding reconciliation pipeline.
Deterministic rules have already compared Workday (system of record) against Ashby, Okta, Google Workspace,
Slack, ExpoIT and the cohort tracker. You only see the cases the rules could not settle on their own.

Your job: read the evidence for ONE finding and return a verdict.
- identity_ambiguous: decide whether the downstream account belongs to the candidate worker
  (same_person / different_person). Nicknames, preferred names and recruiter notes are valid evidence;
  a similar name alone is not.
- orphan_account: decide whether a live account with no Workday worker looks like a legitimate
  non-employee account (contractor, vendor, shared/service) -> non_employee_account, or is unexplained.
- source_conflict: Ashby and Workday disagree and Ashby changed more recently. Decide which value is
  authoritative (trust_ashby / trust_workday) using timestamps and the recruiter notes.

Return needs_human whenever the evidence is thin or conflicting. Calibrate confidence honestly:
a downstream system may be changed based on your answer. suggested_action should name the concrete next
step and the owning team. Keep rationale to 1-3 sentences that cite the specific evidence."""


def _user_prompt(f: Finding) -> str:
    payload = {"finding_type": f.type.value, "system": f.system, "summary": f.message,
               "allowed_decisions": sorted(ALLOWED.get(f.type, {"needs_human"})), "evidence": f.context}
    return "Triage this reconciliation finding.\n\n" + json.dumps(payload, indent=2, default=str)


class Triage:
    def __init__(self, use_llm: bool = True, model: str | None = None, min_confidence: float = MIN_CONFIDENCE,
                 client_factory: Callable[[], object] | None = None):
        self.model = model or os.environ.get("RECON_CLAUDE_MODEL", DEFAULT_MODEL)
        self.min_confidence = min_confidence
        self.client = None
        if use_llm:
            try:
                if client_factory:
                    self.client = client_factory()
                else:
                    import anthropic

                    self.client = anthropic.Anthropic(max_retries=3, timeout=120)
            except Exception as e:  # missing credentials etc. -> offline stub, never block the run
                log.warning("Claude unavailable (%s); using deterministic stub", e)

    @property
    def mode(self) -> str:
        return f"claude:{self.model}" if self.client else "stub"

    # ---- public ------------------------------------------------------------------
    def run(self, f: Finding) -> TriageResult:
        if self.client:
            verdict, actor = self._ask_claude(f)
        else:
            verdict, actor = stub_verdict(f), "stub"
        return self._guardrails(f, verdict, actor)

    # ---- Claude ------------------------------------------------------------------------
    def _ask_claude(self, f: Finding) -> tuple[Verdict, str]:
        import anthropic

        actor = f"claude:{self.model}"
        try:
            resp = self.client.beta.messages.parse(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_prompt(f)}],
                output_format=Verdict,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            return _human(f"Claude transiently unavailable after retries: {type(e).__name__}"), f"{actor}:error"
        except anthropic.APIStatusError as e:
            return _human(f"Claude request rejected ({e.status_code}): {e.message}"), f"{actor}:error"
        if resp.stop_reason == "refusal":
            return _human("Claude declined to answer"), f"{actor}:refusal"
        if resp.parsed_output is None:
            return _human(f"No parseable verdict (stop_reason={resp.stop_reason})"), f"{actor}:invalid"
        return resp.parsed_output, f"claude:{resp.model}"

    # ---- guardrails --------------------------------------------------------------------
    def _guardrails(self, f: Finding, v: Verdict, actor: str) -> TriageResult:
        note = None
        allowed = ALLOWED.get(f.type, {"needs_human"})
        if v.decision not in allowed:
            note = f"decision '{v.decision}' not valid for {f.type.value}; routed to human"
        elif v.decision == "needs_human":
            note = "model asked for human review"
        elif v.confidence < self.min_confidence:
            note = f"confidence {v.confidence:.2f} < {self.min_confidence}; routed to human"
        return TriageResult(finding_key=f.key, verdict=v, actor=actor, requires_human=note is not None, guardrail_note=note)


def _human(reason: str) -> Verdict:
    return Verdict(decision="needs_human", confidence=0.0, rationale=reason, suggested_action="Route to owning team for manual review")


def stub_verdict(f: Finding) -> Verdict:
    """Offline stand-in with the same contract, so the pipeline is demoable without an API key.

    Deliberately conservative: it only ever proposes what the deterministic evidence already supports.
    """
    ctx = f.context
    if f.type == FindingType.IDENTITY_AMBIGUOUS:
        score = float(ctx.get("score", 0))
        reasons = " ".join(ctx.get("match_reasons", []))
        if score >= 0.95 and "nickname" in reasons and "last name exact" in reasons:
            return Verdict(decision="same_person", confidence=0.85,
                           rationale=f"[stub] Name match {score:.2f}: {reasons}.",
                           suggested_action="Link account to Workday ID; IT to reconcile the duplicate email alias")
        return _human("[stub] Name similarity alone is insufficient")
    if f.type == FindingType.ORPHAN_ACCOUNT:
        email = str(ctx.get("record", {}).get("email", ""))
        if any(k in email for k in ("contractor", "vendor", "temp", "svc", "service")):
            return Verdict(decision="non_employee_account", confidence=0.6,
                           rationale="[stub] Email naming suggests a non-employee account.",
                           suggested_action="IT to confirm sponsor and expiry, or deactivate")
        return _human("[stub] Unexplained account")
    if f.type == FindingType.SOURCE_CONFLICT:
        notes = " ".join(n.get("text", "") for n in ctx.get("ashby_notes", [])).lower()
        if "approved" in notes and ctx.get("ashby_updated_at", "") > ctx.get("workday_updated_at", ""):
            return Verdict(decision="trust_ashby", confidence=0.7,
                           rationale="[stub] Newer Ashby change with an approval note.",
                           suggested_action="People Ops to update Workday, then downstream systems re-sync")
        return _human("[stub] Cannot determine authoritative source")
    return _human("[stub] Unsupported finding type")
