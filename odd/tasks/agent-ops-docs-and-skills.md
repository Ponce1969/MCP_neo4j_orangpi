# ODD feature: agent ops runbook + portable skills

- **Branch**: `feat/agent-ops-docs-and-skills` (from `main` @ `5d17609`)
- **Engram**: `odd/agent-ops-docs-and-skills/tasks` (mirror of this document)
- **Opened**: 2026-10-03
- **Trigger**: on 2026-10-03 an agent spent ~30 minutes and triggered a restart loop because the MCP
  service's systemd unit is named `mcp-server.service`, not `book-graph-rag-mcp`; that knowledge lived in a
  backlog file and in Engram instead of where an agent reads it deterministically. Related: obs 1458 (the
  supervision finding), 1457 (the reusable diagnosis), `odd/backlog.md` (O1/O2).

## Goal

Make the operational contract of this project **unmissable and verifiable**, and give agents (pi, opencode,
and whatever client comes next) two portable skills: one for querying the graph correctly through the MCP,
and one for troubleshooting a systemd-supervised service without repeating the incident.

## Frozen decisions (maintainer, 2026-10-03)

| Id | Decision | Reason |
|----|----------|--------|
| D1 | The service runbook is **normative documentation** (`AGENTS.md` §7.5 + `docs/ops/mcp-service.md`), **not** an on-demand skill | Skills inject only name + description at startup; data an agent must always know (the exact unit name, "never launch a manual instance") must not depend on a trigger firing |
| D2 | Two skills: `book-graph-mcp-usage` lives in the **repo** at `.agents/skills/`; `service-ops-troubleshooting` lives in the **user** dir `~/.pi/agent/skills/` | The portable Agent Skills location is discovered by pi from the cwd up to the repo root, so the graph skill travels with the project; the troubleshooting one is generic and applies to every service on that host |
| D3 | Anti-drift is a **test**, not discipline: `tests/test_docs_consistency.py` gains assertions for the unit name, the verification command and the eight tool names in the usage skill | The repo already pins docs with exact-string assertions (`docs/spec/03`, roadmap); a runbook that drifts is worse than no runbook |
| D4 | **Do not index the skills in the Neo4j graph** | It would break the editorial contract (Book→Chapter→Section→Chunk with page provenance and the 21 blocking audit rules), abuse `source_id` semantics, add retrieval nondeterminism where exact text is required, create a second source of truth, and cost a subsystem for ~40 lines of text. The file-level index (`.atl/skill-registry.md`, "index, not summary") already covers discovery. MCP `resources` remain the protocol-native option if a non-skill client ever needs the same text |

## Verified fact sheet (encode these; verify each before writing)

MCP surface — evidence in `src/book_graph_rag/infrastructure/mcp/mcp_server_adapter.py` unless noted:

| Fact | Evidence |
|------|----------|
| Console script → `book_graph_rag.mcp_server_main:main`; tools + app in `infrastructure/mcp/mcp_server_adapter.py` (`create_server()` and the eight `@mcp.tool()` wrappers) | `pyproject.toml:45`, adapter `:788`, `:796-944` |
| Tools: `find_entity`, `traverse_relationships`, `search_chunks`, `list_entities`, `count_entities`, `search_rag`, `query_cypher`, `ask_global` | adapter `:796-944` |
| Scope is mandatory (`mcp_require_scope=True`); missing → `MissingScopeError`, code `missing_scope`, raised before any port call | `config.py:144`, adapter `:170-174`, `domain/mcp_security.py:56-64` |
| Scope format is exactly `corpus:source`; unknown/inactive → `InvalidScopeError`, code `invalid_scope` | `domain/namespaces.py:46-51`, `infrastructure/catalog_scope_resolver.py:28-44` |
| Valid scopes: corpus `knowledge` × sources `agentic-architectural-patterns`, `graphrag-agentic`, `essential-graphrag`, `ai-engineering-huyen` | `catalog.yaml:3-19` |
| The catalog is loaded **once at resolver construction** and cached for the process lifetime | `catalog_scope_resolver.py:20` |
| `traverse_relationships` takes two ids: positional `source_id` (node to traverse) and `scope_source_id` (the scope) | adapter `:811-814` |
| Traversal depth is clamped to 0–3 in the adapter, and `neo4j_max_traversal_depth=3` rejects out-of-range rather than silently clamping | adapter `:351`, `config.py:64-69`, `neo4j_query_adapter.py:397-401` |
| Read queries materialize at most 1000 rows → `ResourceExhaustedError` | `config.py:63`, `neo4j_query_adapter.py:96-99` |
| Empty result is not an error: `find_entity` returns `entity_not_found: true` | adapter `:330-333` |
| `query_cypher` is disabled by default → structured `policy_violation` payload, no graph/LLM call; when enabled it has a read-only keyword prefilter, a structural allowlist, an `EXPLAIN` gate with 2 retries and a 10 s timeout | `config.py:140`, adapter `:690-706`, `text2cypher_adapter.py:34-37,134-136,208-247` |
| `ask_global` `detail_level` must be 0–3 | adapter `:775` |
| Tier budgets (not exposure gates): LOW 8 concurrent / 60 calls per 60 s, MEDIUM 4 / 30, HIGH 1 / 5 | `domain/tool_tier_registry.py:16-27`, `domain/mcp_security.py:211-236` |
| Every HTTP path needs `Authorization: Bearer <MCP_ACCESS_TOKEN>` when the token is configured; failure is `401` with `{"error":"unauthorized",...}` | `infrastructure/mcp/bearer_auth.py:19-21,42-59` |
| Bind is fail-closed to `127.0.0.1` by default and a wildcard bind is rejected in production | `config.py:125,478-482` |
| Query log `logs/mcp_queries.jsonl` is metadata-only (raw text becomes HMAC fingerprints), 7-day retention, and `tool_name` makes it usable as evidence | `infrastructure/logging/json_query_logger_adapter.py:30,71`, `domain/models.py:403-416` |

Service operations — evidence gathered on the OrangePi on 2026-10-03:

| Fact | Evidence |
|------|----------|
| The service is a systemd unit named **`mcp-server.service`** ("Book Graph RAG MCP Server"): `User=gonzalo`, `WorkingDirectory=<repo>`, `ExecStart=uv run book-graph-rag-mcp serve`, `Restart=on-failure`, `RestartSec=5`, `enabled` | `/etc/systemd/system/mcp-server.service` |
| A manual instance holding port 8003 makes every unit retry fail to bind, looping every 5 s — the mechanism behind the historical 43,558 restarts | live observation 2026-10-03 + obs 1458 |
| Control: `sudo systemctl restart mcp-server` (sudo needs a password on that host); reading `status`/`is-active`/`cat` needs no sudo | live |
| Verification: source the env (`set -a; . ./.env; set +a`) then `uv run --no-sync python scripts-ops/mcp_smoke_book4.py`; without the token every request is `401` (auth working, not a broken service) | `scripts-ops/mcp_smoke_book4.py` |
| Post-restart baseline: handshake `book-graph-rag 1.28.0`, `count_entities` 7111 / 1241 / 6078 / 6963, 8 tools | smoke run 2026-10-03 |
| Name-based checks that MISS the unit: `systemctl status book-graph-rag-mcp`, `list-unit-files \| grep -i -E "book\|graph"`, `crontab -l`, `~/.config/systemd/user/`, greps for the command in `~/.config` | obs 1458 |

## Tasks

- [x] **T1** Branch + this document + Engram mirror + visible todo list (before the first source write)
- [x] **T2** `docs/ops/mcp-service.md` (158 lines) + `AGENTS.md` §7.5 (18 lines, CRLF preserved so the diff is
      additions only) + the anti-drift assertions. Commits `f30e11c`, `759bf44`
- [x] **T3** `.agents/skills/book-graph-mcp-usage/SKILL.md` (project skill, 176 lines). Commit `983cdcf`
- [x] **T4** `~/.pi/agent/skills/service-ops-troubleshooting/SKILL.md` (global, 115 lines)
- [x] **T5** `.atl/skill-registry.md` refreshed itself and lists both skills, with `.agents\skills` now among the
      scanned sources; mirrored to Engram as `skill-registry` (obs 568)
- [x] **T6** Gates and close-out. Review fixes in commit `b716b8a`

## Review findings (fixes applied after the first pass)

1. **The repo's unit artifact is not what runs** — the worker wrote `EnvironmentFile` and
   `Environment=MCP_BIND_HOST=100.106.85.109` as live-unit facts, but those lines exist only in
   `deploy/mcp-server.service`. Verified with `systemctl cat mcp-server` on the host: the live unit has neither
   (the service reads `.env` from its `WorkingDirectory`, and the Tailscale bind still happens because
   `MCP_BIND_HOST` is in `.env`). The runbook now states what is live, adds a repo-vs-live comparison table and
   the deliberate install command; the drift is registered as **O3** in `odd/backlog.md`.
2. **The new assertion tripped an existing guard** — `tests/test_deploy_artifacts.py` scans `deploy/`, `tests/`
   and `scripts/` for live-deployment tokens, and the literal restart command inside the docs test matched
   `systemctl`. Fixed by holding the command in a local variable annotated with the guard's own
   `no-live-deployment-allow` marker, so the assertion stays strong and the guard is not weakened.
3. **Invalid YAML in the skill frontmatter** — the description was unquoted and contained a colon, so a strict
   reader would not load the skill. Now quoted; both skills are checked with `yaml.safe_load`.
4. **Fact-sheet line numbers drifted** (e.g. `create_server` is at `:786`, not `:788`) while every claim held;
   the sheet is evidence-oriented, so the claims matter and the reviewer confirmed them at the current lines.
5. **The skill no longer invents entity ids**: the traversal recipe and §3 use a placeholder and say to take ids
   from tool output, and the depth rule now describes the clamp and the possible `TraversalDepthExceededError`
   without implying they cannot happen together.

## Evidence log

| Task | Commit | Evidence |
|------|--------|----------|
| T1 | — | branch `feat/agent-ops-docs-and-skills`; this document; Engram mirror |
| T2 | `f30e11c`, `759bf44` | runbook + §7.5 + 4 anti-drift tests; `AGENTS.md` diff 18 insertions / 0 deletions after restoring its CRLF endings |
| T3 | `983cdcf` | project skill in the portable `.agents/skills/` location |
| T4 | — | global skill, `~/.pi/agent/skills/service-ops-troubleshooting/SKILL.md` (115 lines, valid frontmatter) |
| T5 | — | `.atl/skill-registry.md` (local, gitignored) lists both skills |
| T6 | `b716b8a` | 34 tests green (`test_docs_consistency.py` 10 + `test_deploy_artifacts.py` 24); ruff, mypy (383 files), architecture validator green. The full suite was last green at `1fc1cea` (1689 passed); this feature touches no source or runtime file, and the focused suites cover every artifact it changes |
