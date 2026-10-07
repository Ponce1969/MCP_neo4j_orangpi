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

## Block A — closed 2026-10-06 (was: semantic resolution phase)

Every item below is closed; the texts that used to live here, and §3 of
`odd/specs/cross-namespace-semantic-resolution-design.md`, described the pre-T3/T6/T7 state and are kept
only as history. Verified against the tree on 2026-10-06; an independent verifier re-checked every claim in
this section. The production figures come from `odd/reports/cross-namespace-semantic-resolution-closeout.md`
and were not re-measured here.

- **A1 (R5a) Cross-namespace duplicates — closed.** The rule exists:
  `DUPLICATE_ENTITY_CROSS_NAMESPACE` (warning, category `duplicates`, `domain/audit_models.py:44`, scoped
  variant included); the closeout report recorded 456 warnings. Approved merges go through `quarantine
  approve`, which is reachable in the product (`main.py:1272`).
- **A2 (R5c) Self-loop audit rule — closed.** `ENDPOINT_SELF_LOOP_INVALID` (blocking, `endpoints` category) is
  implemented and tested (`infrastructure/neo4j_audit_adapter.py:193`,
  `tests/integration/test_audit_cross_namespace_and_self_loop.py`); the closeout report recorded zero
  self-loops. The design draft's proposed name `ENTITY_SELF_LOOP_INVALID` was not used: living in the
  `ENDPOINT_*` family is what gives the rule its blocking severity. This item was labelled A2/**R5b** before,
  and the next one A3/**R5c**, i.e. the two letters were swapped relative to the design doc and
  `docs/spec/03`; this text now follows the spec's labelling.
- **A3 (R5b) `duplicates_entity` case sensitivity — closed.** The landed grouping key is the Cypher expression
  `toLower(trim(n.name))` (`infrastructure/neo4j_audit_adapter.py:55`) — a faithful equivalent of the
  `normalize_key` helper on this corpus, but not the helper itself: the adapter's own comment records that
  NFKC and internal-whitespace cases would diverge. The 64 case-only intra-namespace groups were merged in
  three batches on 2026-10-05 with 0 failures (R5b 64 → 0), see `docs/spec/03-semantic-entity-resolution.md:96`.
- **The two "gaps before any new merge" — closed.** (1) The namespace guard lives in
  `ApplyMergeUseCase._guard_cross_namespace` (`application/apply_merge_use_case.py:72,96`) and raises
  `CrossNamespaceApprovalRequired`, so `scripts-ops/resolve_cross_namespace.py` is no longer a bypass (it is
  disabled by design). (2) `ApproveQuarantineUseCase` is exposed by `quarantine approve` (`main.py:1272`),
  which runs the §7.2 backup + approval gate before any write; `quarantine reject` (`main.py:1364`) is also
  reachable but deliberately gate-free, because it does not touch the graph. The queue is no longer write-only.
- **Adjacent defect — the community graph was built over merged entities — closed 2026-10-06.**
  `Neo4jCommunityAdapter.load_entity_graph` now applies the same live predicate the audit and command
  read paths use (`(n.merged_into IS NULL OR n.merged_into = '')`) to the entities **and** to both
  endpoints of every `:RELATED` edge, and `get_isolated_entities` (the orphan report behind
  `scripts/inspect_orphans.py`) got the same filter. Regression test `tests/integration/test_community_adapter_live_graph.py` (Neo4j testcontainer,
  red before the fix); an independent verifier additionally reproduced the pre-fix leak and confirmed no leak
  through merged-canonical chains. Only the `merged_into` half of the `_active_ids` workaround in
  `scripts-ops/run_communities_scoped.py` is now redundant — its `STARTS WITH $prefix` namespace scoping is
  still required, because the adapter reads the whole graph — and it stays until a community rebuild is
  authorized, since a rebuild rewrites `data/communities` and needs the §7.2 gate.

## Block C — soft-delete consistency in the read paths (found 2026-10-06, needs a decision)

An independent verifier audited every `:Entity` read while closing Block A. The community adapter is fixed;
several reads still return merged (soft-deleted) entities. Split by stakes:

**Feeds a pipeline or a score (fix recommended):**
- `infrastructure/neo4j_neighborhood_query_adapter.py:41-53` `related_neighbors` has no `merged_into` filter
  and is consumed by `application/resolve_entities_use_case.py:233-234` to compute the S3 neighborhood
  jaccard, so ghost neighbours can skew a resolution score.
- `scripts/resolve_entities.py:299-312` `load_entities` reads every `:Entity` and feeds
  `build_merge_plan`/`apply_merges`, so the legacy direct-merge path can plan a merge onto an already
  merged ghost.

**Visible through the MCP tools (contract change, needs an explicit decision):**
- `infrastructure/neo4j_query_adapter.py`: `count_entities` (:596-619), `list_entities` (:633-665),
  `traverse_relationships` (:432-441) and `find_path` (:495-499) do not filter `merged_into`, while the
  `find_entity` tiers in the same file **do**. Consequence: the counts documented as baselines
  (`7111 / 1241 / 6078 / 6963` in the runbook and in the MCP smoke) include ghosts, and `list_entities`
  returns ghost records. Changing this moves numbers other documents and scripts assert, so it is a
  separate, explicitly approved change; `list_entities` is also uncovered by tests.

Deliberately out of scope, with comments in the code: `scripts/migrate_namespaces.py`,
`scripts/backfill_resilience.py`, `scripts-ops/calibrate_cross_namespace_cosine.py`,
`scripts-ops/audit_applied_cross_namespace.py` (by-id lookups that must see both sides of a pair),
`scripts-ops/repoint_merged_endpoint_edges.py`, `neo4j_validation_adapter.py`,
`neo4j_rollback_plan_adapter.py`, and the `scripts-ops/probe_*` / `cleanup_*` diagnostics.

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
