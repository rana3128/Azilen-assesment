# Build note

## Why this slice
I built cross-system validation (reconciliation against Workday), not change propagation or a standalone monitor. The full argument is in [systems_map.md](systems_map.md). In short:
- Propagation is largely covered by integrations BetterUp already owns, and it only fixes paths you build.
- A monitor needs a definition of "wrong", and reconciliation provides one.
- One engine covers all three problems in the brief:
  - **changes that don't propagate** show up as drift and source conflicts
  - **bad data that cascades** is caught by validation at the source plus drift checks downstream
  - **unwatched gates** become deadline checks

## Key design decisions

**Data model**
- Canonical `Hire` keyed on the Workday worker ID.
- Each connector *transforms* its vendor shape into canonical field names. Examples: Okta `profile.employeeNumber`, the Slack SCIM enterprise extension, ExpoIT orders keyed only by email.
- `expected_fields()` encodes field ownership as code: what each system *should* hold, derived from Workday.
  - Okta's first name is the preferred name.
  - ExpoIT ship-by is start date minus 5 days.
  - The cohort is the Monday of the start week.
- Ashby is authoritative only for offer terms and the background check, and only until the hire event.

**Identity resolution** ([identity.py](../recon/identity.py))
- Strongest key wins: Workday ID on the record, then exact email, then an *alias email* learned from a record already linked by ID.
- The alias step is what lets an order placed under a typo'd email still resolve deterministically.
- Fuzzy name matches are **never auto-linked**. They become `identity_ambiguous` findings.

**Where Claude fits** ([triage.py](../recon/triage.py))
Claude is the exception handler for exactly three ambiguous cases. In each one the answer depends on judgment or unstructured text:
1. "Is this Slack account the same person?" (nicknames, recruiter notes)
2. "Is this orphaned account a contractor?"
3. "Ashby changed after Workday. Which is right?" (reads a free-text recruiter note such as "hiring manager approved by email")

Claude's role is bounded on every side:
- The deterministic rules decide what is wrong. Claude only rules on ambiguity.
- The verdict is schema-validated (`messages.parse` → a Pydantic `Verdict`).
- A decision outside the allowed set for that finding type, a confidence below 0.8, or `needs_human` all route the case to a person.
- Claude never writes to a system.
- A refusal, an API error or unparseable output degrades to `needs_human`, so a run never blocks on the model.
- Verdicts are cached by finding key, so recurring findings don't re-bill.
- Only the evidence needed for the decision is sent: names, emails, dates, notes. No home address, comp or background-check detail.

**Failure handling**
- Typed errors: `TransientError` (429/5xx) is retried with exponential backoff. `PermanentError` (401/4xx) is not retried. When retries are exhausted, the connector raises `SystemUnavailable`.
- Downstream fetches run in parallel and fail in isolation. A down system produces a `system_unavailable` finding, and its checks and writes are skipped.
- Its open exceptions **stay open** rather than auto-resolving. "Couldn't read Slack" is not the same as "Slack is fixed".
- An outage also suppresses root-cause-adjacent findings. If Okta is missing, Google and Slack aren't reported as three separate "missing" problems.
- If Workday is unreadable, the run **aborts**. Reconciling against no source of truth would generate garbage.
- Values that fail source validation (for example, an invalid ZIP) are **never propagated**. They're escalated to People Ops to fix in Workday first.

**Idempotency**
- *Detection* is idempotent by construction: it compares desired state to actual state, and finding keys hash (type, worker, system, record, field, bad value). That gives the queue dedupe, recurrence counts and auto-resolve.
- *Writes* are keyed by the **intended end state** `(system, action, record, field, target value)`, not by the attempt. A ledger records `pending` → `applied`.
- On a retry after a crash or lost ack, the executor does a read-after-write check and marks the write applied *without re-writing*.
- Every write is verified by reading it back.
- Second run, verified: 0 of the run-1 writes repeat, and the fixed findings auto-resolve.

**Safety policy**
- The default is a dry run.
- `--apply` executes only low-risk field fixes on systems where the integration has write scope.
- Destructive actions (deactivating an account, cancelling an order) need a recorded human approval, from the CLI or the MCP tool.
- Identity-bearing fields such as email renames are escalated, not written.
- Workday is never written. HR business processes own it.

**Audit trail**
- Append-only JSONL: run start/end, every Claude verdict (actor, decision, confidence, guardrail outcome), and every approval and write with before/after values and the idempotency key.
- The actor is always one of `recon`, `claude:<model>` or `human:<email>`.

## Stack fit: what I'd add and what I wouldn't
- **Runs on n8n, which BetterUp already has.** A Schedule trigger (hourly) plus a webhook trigger (Workday/Ashby change events) calls this service or runs it as a Code node. A Slack node posts escalations to owners with Approve buttons, and those call `approve_remediation`.
- Connectors map 1:1 to APIs we already pay for: Workday RaaS, Okta Users, Ashby, Google Admin SDK, Slack SCIM, and the Sheets API for the tracker.
- **Claude** is used through the API (triage) and MCP (ops Q&A: "what's blocking Monday's cohort?").
- **Not adding:**
  - an iPaaS / MDM tool: n8n plus this code covers it
  - a data warehouse: SQLite/Postgres for the queue is enough at our hire volume
  - a separate ticketing system: Slack plus the queue is enough
  - Zapier for this flow: keep Zapier for simple zaps, because branching, retries and idempotency are painful there
- **Auth (production):** an OAuth2 client-credentials app per vendor with the narrowest scopes (Okta `okta.users.manage` only, a Google domain-wide-delegation service account limited to the Directory scope). Secrets live in the existing secret manager and n8n credentials store. The MCP server sits behind Okta SSO (OIDC) so approvals carry a real identity.

## What I'd productionize next
1. Real connectors with pagination, rate-limit-aware backoff (`Retry-After`) and incremental fetch (`updated_since`).
2. Postgres for the queue and ledger. Run `--apply` per system behind feature flags, starting with the cohort tracker, then ExpoIT, then Okta.
3. Slack approvals and owner routing with SLAs. Escalate again if a gate breach stays open for 24h.
4. Metrics: time-to-detect, time-to-resolve, the share of findings auto-fixed, and the Claude override rate (how often a human disagrees with a verdict). Use that override rate to tune the 0.8 threshold.
5. A small eval set of past ambiguous cases to regression-test the triage prompt before model upgrades.
6. Lumos and Highspot connectors (app entitlements for rescinded hires).

## AI tools used
- **Claude Code (Claude Opus)** for analysing the brief, planning, writing the code, fixtures and docs, and running the smoke tests. I reviewed and directed the slice choice, the guardrail policy and the failure semantics.
- **The Claude API (`claude-opus-5-5`)** inside the product as the triage component.

**Things the AI got wrong, and how they were caught:**
1. **It worked from an incomplete brief.** The first analysis came from `task.txt`, which was a truncated copy-paste. It missed the PDF's three concrete pain points ("changes don't propagate", "bad data cascades", "nobody's watching the gates") and the 7-day deadlines. I caught this by checking the original PDF before building. That added deadline gates, off-cycle hires and source validation to the scope.
2. **It double-counted a root cause.** In the first smoke run, Jon Okafor's Slack account was reported both as `identity_ambiguous` and as `missing_record` ("no Slack record although Okta is provisioned"). The missing-record rule didn't know a candidate record was waiting on identity triage. I caught it by reading the run output line by line against the seeded scenarios. The fix is in `check_missing`, and there's a regression assertion in `test_seeded_scenarios_detected`.
3. **It used a stale SDK API.** It reached for the MCP SDK's `FastMCP` class from memory, but the installed `mcp` 2.x renamed it to `MCPServer`. The import check failed, and I rewrote the code against the installed API.
