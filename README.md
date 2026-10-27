# Onboarding reconciliation prototype

This is a prototype for BetterUp's People-systems take-home. It reads Ashby, Workday, Okta, Google Workspace, Slack, ExpoIT and the cohort tracker, and reconciles all of them against Workday. The deterministic rules decide what is wrong. Claude triages only the ambiguous cases. Fixes are idempotent, approval-gated where they're risky, and recorded in an audit log.

- **Systems map, and why this slice:** [docs/systems_map.md](docs/systems_map.md)
- **Build note** (design decisions, failure handling, idempotency, what the AI got wrong): [docs/BUILD_NOTE.md](docs/BUILD_NOTE.md)

## Run it
```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
alias recon='.venv/bin/python -m recon'

recon reset                       # seed mock systems from data/fixtures
recon run                         # dry run → console + out/report.html + out/audit.jsonl
recon run --apply                 # execute low-risk fixes
recon run --apply                 # again: nothing re-applied, fixed findings auto-resolve
recon approve <idempotency_key> --by you@betterup.co   # approve a deactivation / order cancel
recon run --apply --fail okta=transient:2 --fail slack=down   # retries + outage isolation
recon queue --all                 # exception queue lifecycle
.venv/bin/python -m pytest -q
```
`recon run` uses Claude (`claude-opus-5-5`) when `ANTHROPIC_API_KEY` is set. Otherwise it falls back to a deterministic stub with the same contract. Use `--no-llm` to force the stub.

**MCP server:** run `.venv/bin/python mcp_server.py` (stdio). It exposes three tools:
- `list_open_exceptions`
- `get_hire_view`, a live 360° view of one hire
- `approve_remediation`

## Seeded scenarios (as of 2026-10-27)
| Hire | Problem |
|---|---|
| W1001 Priya | Start date moved in Workday; Okta, ExpoIT ship-by and the tracker are stale |
| W1002 Marcus | Offer rescinded; Okta, Google and Slack are still live and the laptop is still ordered (**critical**) |
| W1003 Jon | Slack account hand-made as `jonathan.okafor@` with no employee ID, so Claude decides whether it's the same person |
| W1004 Aisha | Manager changed; Okta, Slack and the tracker still have the old manager |
| W1005 Diego | 6 days out: no Okta account, no hardware form, background check pending (gate breaches) |
| W1006 Sam | Off-cycle hire created directly in Workday: no Ashby record, no background check, not in the tracker |
| W1007 Elena | Name typo fixed in Workday but cascaded to Okta, Google, Slack, ExpoIT and the tracker; invalid ZIP at source |
| W1008 Hannah | Recruiter moved the start date in Ashby *after* Workday, so Claude reads the note and decides which source is right |
| — | `design.contractor@` live in Google and Slack with no worker (orphan, so Claude triages it) |

## Layout
```
recon/connectors/   one adapter per system: vendor shape → canonical fields, retries, typed errors, fault injection
recon/identity.py   cross-system identity resolution (ID → email → alias email → fuzzy candidate)
recon/rules.py      field ownership + validation, drift, rescind, gates, off-cycle, identity rules
recon/triage.py     Claude exception handler + guardrails + offline stub
recon/remediate.py  remediation policy + idempotent executor (ledger, read-after-write, approvals)
recon/store.py      exception queue, ledger, approvals, verdict cache (SQLite) + JSONL audit
recon/pipeline.py   orchestration: fetch → resolve → rules → triage → plan → execute → queue
mcp_server.py       MCP tools for Claude
```
