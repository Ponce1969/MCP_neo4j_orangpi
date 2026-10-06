# Backlog — agentic-book-graph

Follow-ups that are real, evidenced and deliberately **out of** the work unit that discovered them.
Each item states what it is, the evidence that it exists, and the block it belongs to. Engram mirror:
`debt/merge-adapter-and-audit-followups` (obs 1455). The reusable diagnosis for the closed debt is
`pattern/merged-into-cycle-diagnosis` (obs 1457).

## Operational findings (infrastructure)

- **O4 `.env` (and its backups) were world-readable on that host.** **Resolved 2026-10-03**: `chmod 600 .env
  .env.bak-*` applied (all five files now `-rw-------`, owner `gonzalo`); the running service was unaffected (the
  environment was already loaded), file stays readable by its owner, and the smoke passed with the token read from
  the file. Note the residual, inherent to the unit's `EnvironmentFile`: the values also live in the process
  environment (`/proc/<pid>/environ`), so a restart is the only way to reload them and any child inherits them.
- **Block A (semantic phase) — design ready, decisions pending.** `odd/specs/cross-namespace-semantic-resolution-design.md`
  records the measured ground truth (456 candidate groups / 941 entities; **302 cross-namespace merges already
  applied, 31% of the ledger, bypassing the policy**; 64 case-only groups) and the proposed three-way model
  (identity → merge + multi-provenance; label collision → keep separate; unknown → quarantine), the quarantine
  design for §7.2, the R5 rules and four decisions (D-A1..D-A4). Two gaps found that must be closed before any new
  merge: the namespace guard lives only in `resolution_policy.decide()` (so a script can bypass it) and
  `ApproveQuarantineUseCase` has no CLI, making the quarantine queue write-only. Adjacent defect: community
  construction does not filter `merged_into`.
- **O3 The repo's systemd unit artifact was not what ran (drift).** `deploy/mcp-server.service` declares
  `EnvironmentFile=<repo>/.env` and `Environment=MCP_BIND_HOST=100.106.85.109` (R7 comment about a fail-closed
  private bind), while the **live** `/etc/systemd/system/mcp-server.service` had neither for months. **Resolved
  2026-10-03**: the second install attempt (absolute source path) landed — unit `mtime` 15:34, 734 bytes, and both
  files now share sha256 `23a13f72d59b91fed34ef6482d9b59ad89e94dd6713481ca68aed84b1c771500`; `systemctl show`
  reports `EnvironmentFiles=<repo>/.env` and `Environment=MCP_BIND_HOST=100.106.85.109`, the service came up clean
  (`NRestarts=0`, Tailscale-only bind) and the smoke matched the baseline (7111 / 1241 / 6078 / 6963, 8 tools).
  Keep the check: `ls -l --time-style=full-iso` the live file and diff it against the artifact with
  `tail -n +2` (never `grep -v '^#'`, which strips the artifact's own comments and fakes a difference).
- **O1 The MCP service is supervised by the systemd unit `mcp-server.service`**
  (`/etc/systemd/system/mcp-server.service`: `User=gonzalo`, `WorkingDirectory` = this repo,
  `ExecStart=uv run book-graph-rag-mcp serve`, `Restart=on-failure`, `RestartSec=5`, `enabled`).
  **Never launch a manual instance**: while a manual process holds `100.106.85.109:8003`, every systemd retry
  fails to bind and the unit loops every 5 seconds — that is exactly what produced the historical
  43,558-restart incident. Control it with `sudo systemctl restart mcp-server` (reading `status`/`is-active`
  needs no sudo), never with a background `uv run`.
  Diagnostic note: name-based checks (`systemctl status book-graph-rag-mcp`, `grep -i -E "book|graph"`,
  `crontab -l`, `~/.config/systemd/user/`) all miss this unit, because the unit name does not contain the
  project name. What identified the spawner was the restart cadence plus `systemctl list-units`.
- **O2 The MCP must be exercised with its token.** `scripts-ops/mcp_smoke_book4.py` reads `MCP_ACCESS_TOKEN`
  from the environment, so it needs the `.env` sourced (`set -a; . ./.env; set +a`); otherwise every request
  returns `401 Unauthorized`, which means auth is working, not that the service is broken. Post-restart
  verification on 2026-10-03: handshake `book-graph-rag 1.28.0`, `count_entities` for the four namespaces
  7111 / 1241 / 6078 / 6963 (identical to the pre-change baseline), 8 tools exposed.

## Block A — next session: semantic resolution phase

- **A1 (R5a) Cross-namespace duplicates** (same concept in two books). Must go through quarantine
  (AGENTS.md §7.2: fresh backup → dry-run → human approval → apply → scoped + global audit). The intra-namespace
  tooling is generic and reusable: `scripts-ops/resolve_intra_ns.py` (namespace, limit, backup, approval) plus
  `scripts-ops/gen_cross_groups.py` for the cross-namespace candidate set.
- **A2 (R5b) No audit rule for self-loops.** `(:Entity)-[:RELATED]->(:Entity)` with the same node on both ends:
  5 were deleted on 2026-10-01 with explicit approval, but the extractor recreates them (2 of the 5 came from LLM
  extraction in a freshly re-indexed namespace, not from a merge) and nothing flags them today. The merge path no
  longer creates them (verified after the adapter fix).
- **A3 (R5c) `duplicates_entity` is case-sensitive.** It groups by exact `n.name`, so `alucinación` and
  `Alucinación` (same concept) are never reported: "0 duplicates" means 0 *exact-name* duplicates. Belongs to the
  semantic pass, which should key on normalized names (the `normalize_key` helper already exists in
  `domain/audit_models.py`).

## Block B — hardening

- **B1 (R4) Crash/formatting debt: 16 of 381 tracked Python files carry CRLF and fail `ruff format --check`.**
  Any edit to one of them is uncommittable under the pre-commit format gate without a normalization commit, which
  pollutes the review diff (it already happened twice: `neo4j_graph_merge_adapter.py`, and
  `audit_models.py` + `neo4j_audit_adapter.py`, where reformatting `RULE_CATALOG` also orphaned its
  `# noqa: E501,SIM905`). Normalize them in one dedicated commit, then keep the gate honest.
- **B2 Retrieval warning.** The readiness gate passes but warns `low precision@k: 0.0478` (threshold 0.045,
  retrieval layer is informative/optional). Worth an analysis pass before the number drifts: is it a dataset
  artifact (8 future-corpus records skipped) or real retrieval degradation?
- **B3 Five legacy Book 1 chunks without a `:Checkpoint`** (indices 57, 1026, 1302, 1499, 1501; graph predates
  Phase 2). Cover them with `--backfill-checkpoints --dry-run` and then the approval-gated apply. The other three
  books are at 100%.

## Merge adapter debts (from the 2026-10-02 cycle-break work)

- **R1 `rollback_merge` rebuilt mirror directions.** **Resolved 2026-10-03**: the inverse map now captures the
  RELATED orientation, apply re-points only it and rollback restores exactly it. The interesting constraint: the
  ledger's chain digest is computed over the canonical dump of the parsed entry, so a plain defaulted field would
  have changed all 958 existing entries and broken verification; the field is omitted when unknown, which kept every
  old line hashing byte-identically (verified read-only: 958 entries, 0 mismatches, 0 broken links). Entries written
  before the fix keep the legacy both-ways restore, documented and pinned by test.
- **R2 The ledger does not record the canonical's prior marker.** `apply_merge` now clears the canonical's
  leftover `merged_into`/`merged_at` (it must, or round-robin merges create mutual pairs), but rollback cannot
  restore it. Declared limit (D9), pinned by test.
- **R3 `_DELETE_INTRA_GROUP_RELATED` has no reversibility test.** It deletes any group-to-group RELATED edge
  while rollback restores only what the inverse map captured; fidelity depends on capture completeness and is not
  exercised by any test.

## Closed (for reference)

- The 286 dangling edges of `knowledge:essential-graphrag` (90 MENTIONS + 196 RELATED): closed 2026-10-02 —
  two mutual merge cycles, fixed by clearing the winners' markers; audits `passed` 0/0/0; the class is now covered
  by `ENDPOINT_MENTIONS_MERGED_INVALID` and `ENDPOINT_RELATED_MERGED_INVALID`. See
  `odd/specs/tech-debt-essential-graphrag-endpoints.md` and
  `evidence-bundles/repoint-break-cycles-20261002.json`.
