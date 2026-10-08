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

## Block C — soft-delete consistency in the read paths (found 2026-10-06)

An independent verifier audited every `:Entity` read while closing Block A. The community adapter was fixed
first; the two reads that feed a pipeline or a score are now fixed too, and the MCP-visible ones remain open
by decision (they move documented baselines).

**Feeds a pipeline or a score — closed 2026-10-06:**
- `infrastructure/neo4j_neighborhood_query_adapter.py::related_neighbors` now filters both endpoints, so it no
  longer hands ghost neighbours to the S3 neighborhood jaccard in
  `application/resolve_entities_use_case.py:233-234`. Both ids the only real consumer passes are already
  live, so filtering is a no-op there and removes exactly the ghosts (verified).
- `scripts/resolve_entities.py::load_entities` now filters, so the legacy `build_merge_plan`/`apply_merges`
  path can no longer plan a merge onto an already merged ghost.

  Evidence: `tests/integration/test_merged_entities_invisible_to_pipeline.py` (two testcontainer tests, both
  red before the fix). An independent verifier reproduced the pre-fix leak from `git show HEAD:<path>`, traced
  every caller of both reads, and confirmed no consumer needed the merged entities and no test asserted the
  old behaviour.

**Visible through the MCP tools — closed 2026-10-07 (was: needs an explicit decision):**
- `infrastructure/neo4j_query_adapter.py`: `count_entities`, `list_entities`, `traverse_relationships` and
  `find_path` now apply the same live predicate the `find_entity` tiers in that file already used,
  `(n.merged_into IS NULL OR n.merged_into = '')`. The two path reads apply it to **every** node of the path
  (`ALL(n IN nodes(p) WHERE …)`), not only the endpoints, and the depth-0 branch of
  `traverse_relationships` got the same guard — a ghost in the middle of a chain no longer makes its two ends
  look connected.

  Evidence: `tests/integration/test_merged_entities_invisible_to_mcp_reads.py`, five testcontainer cases,
  **all five red before the fix** (the failure output showed the returned path crossing the ghost) and green
  after: `count_entities`, `list_entities` (previously uncovered — it is covered now), traversal from a live
  node, traversal from a ghost at depth 0 and depth 1 (both empty), and `find_path` whose only route crosses a
  ghost. One existing unit test asserted the exact depth-0 query text; its assertion was split into two
  intent-level ones (targets the start id, returns the start), since the query gained a WHERE — the "no
  relationship expansion" assertion was already separate. Its behavioural expectations did not change.
- **A fifth read the original list missed**, found by an independent verifier's sweep of every `:Entity` read:
  `mcp_server_main.py::_CATALOG_STATS_CYPHER`, which backs the `bookgraph://catalog` resource. It had the same
  leak, so the same graph answered `entities: 5` through the resource and `3` through the `count_entities` tool.
  It now carries the same predicate, with its own red-then-green case (`assert 4 == 3` before the fix) in the
  same integration file.

  **Baselines re-measured on production — closed 2026-10-07.** Deployed and verified with an A/B on the same
  graph: with the pre-fix process still in memory the smoke returned the old baseline
  (`7111 / 1241 / 6078 / 6963`, MainPID 3085521), and after the restart that loaded the fix it returned
  `6899 / 1103 / 5900 / 6526` (MainPID 2632211) — **965 ghosts left the counts** (−212 / −138 / −178 / −437).
  `search_rag` was unchanged, so the retrieval path was not touched. The runbook table now carries the new
  numbers and the delta. The raw escape hatches remain documented limits, not leaks: `query_cypher` (off by
  default) and `execute_read` run caller-supplied Cypher and cannot filter it.

Deliberately out of scope, with comments in the code: `scripts/migrate_namespaces.py`,
`scripts/backfill_resilience.py`, `scripts-ops/calibrate_cross_namespace_cosine.py`,
`scripts-ops/audit_applied_cross_namespace.py` (by-id lookups that must see both sides of a pair),
`scripts-ops/repoint_merged_endpoint_edges.py`, `neo4j_validation_adapter.py`,
`neo4j_rollback_plan_adapter.py`, and the `scripts-ops/probe_*` / `cleanup_*` diagnostics.

## Block B — hardening

- **B1 (R4) Formatting + line-ending debt — closed 2026-10-06.** The figures this entry used to carry ("16 of
  381 tracked Python files carry CRLF") were wrong; measured before the fix: **151** tracked `.py` files failed
  `ruff format --check` (all Python, 0 Markdown, `ruff 0.15.18`), and **48** tracked files carried CRLF *in the
  index* (33 `.py` + 8 `.md` + 7 `.json`, one of them mixed), **10** of them in both sets. Fixed in two dedicated
  commits — **format first, then EOL + `.gitattributes`** — which is the order the gate requires: with the EOL
  commit first, the `ruff-format-check` hook fails on the 10 files that are in both sets.

  Evidence, none of it taken from the tools' own word:
  - **Semantic equivalence against a frozen pre-image of the 189 touched files: 0 problems.** Every `.py` is
    `ast.dump`-identical, every `.json` is `json.loads`-equal, every other file differs only in end-of-line bytes.
    This is what proves the reflow — and the manual string splits below — changed no behaviour. An independent
    verifier strengthened it: CPython normalises CRLF inside literals at tokenise time, so all 173 changed `.py`
    files are `ast.dump`-identical with *no* normalisation applied at all.
  - **The gates caught three classes of collateral the formatter itself introduced, fixed in the format commit.**
    5 `E501` (`ruff format` parenthesised nested `await`s, adding an indent level that pushed three Cypher string
    literals past 100 — split into implicit concatenations, which the parser folds into the same constant);
    9 `# noqa: E501` left dead by the reflow; and 4 mypy `unused-ignore` (the reflow separated
    `# type: ignore[union-attr]` from the line mypy flags in `tests/test_run_full_pipeline.py`; the comments moved
    to the flagged line). A fourth instance was self-inflicted while repairing those: one file kept **mixed** line
    endings because a byte-level edit stripped the `\r` along with the comment.
  - **An independent verifier then found 8 more dead `# noqa: E501`** (2 in `application/audit_graph_use_case.py`,
    6 in `tests/test_audit_models.py`), left by the same reflow and fixed in a follow-up commit. My own `RUF100`
    check missed them **because of method, not luck**: it compared findings as a *set* of `(file, rule)`, and those
    two files already had dead `E501` directives on other lines, so the set was unchanged and the per-line
    additions were invisible. Compared as a **multiset** at `7947e84` vs HEAD, the repo is identical at
    **104 findings**. The 106 this entry first quoted was my own `wc -l` counting ruff's two summary lines.
    Method note: aggregate lint findings by rule *and count*, never by set membership.
  - **`data/evaluation/MANIFEST.json` digests untouched:** all four sha256 payloads (`pairs.yaml` and the three
    `.jsonl`) are LF and still verify. None of them was CRLF, so normalising the EOL of the manifests and of
    `resolution_baseline.json` is safe — they *contain* hashes, they are not hashed themselves.
  - **Docstrings — the change is inert, and my first claim about it was wrong.** 17 files carried CRLF *inside*
    string literals, all of them docstrings. I wrote that `scripts-ops/cleanup_namespace.py --help` would stop
    printing carriage returns; the verifier disproved it: CPython applies universal-newline translation when it
    tokenises source, so CRLF inside a source string literal never reaches the value, and `ast.get_docstring` of
    that file hashes identically at `7947e84` and at HEAD. There is no observable change at all.
  - **Gates after, all green:** `ruff check .` 0 findings, `ruff format --check .` 0 functional files (only the
    untracked `scripts-ops/probe_cross_risk.py` loose tail is left alone), `mypy .` 423 files success, architecture
    validator OK, fast suite (unit + property + root) 1913 passed / 3 skipped, integration suite **153 passed** in
    24:42, `tools/mcp-oranpi` 502 passed with its own three gates. Full suite: **2066 passed, 3 skipped**.
    Caveat, recorded on purpose: the integration run overlapped the commits being built and the 8-directive fix, so
    it attests the *semantics* — every one of those edits is `ast.dump`-identical to the pre-image — rather than one
    frozen revision. The fast suite was re-run after the fix, against the frozen tree.
  - **Recurrence guard:** new `.gitattributes` with `* text=auto eol=lf`. This repo tracks no `.bat/.cmd/.ps1`, the
    only kinds that require CRLF. It changes what every clone materialises on its next checkout, the production
    clone included — that is the point, and it is why the change is a separate commit.
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
- **R3 `_DELETE_INTRA_GROUP_RELATED` reversibility — closed 2026-10-07 (the entry was stale).** The claim that
  fidelity "is not exercised by any test" stopped being true on 2026-10-02: `45d96c3` extended
  `tests/integration/test_neo4j_graph_merge_adapter.py::test_apply_merge_does_not_leave_intra_group_related_edges`
  to capture, apply, **roll back** and assert that the intra-group edges come back once each
  (`duplicate -> canonical` and `duplicate -> duplicate-2`), with no mirror. What was still uncovered was the third
  shape of the same class — an intra-group edge with the **canonical as the source** — so it is seeded and asserted
  now, and the old "no mirror" assertion for that pair moved to the restored list because it is a real edge, not a
  mirror. It passes: the capture reads `(dup)-[r:RELATED]-(other)` undirected and unfiltered, so that edge is
  captured from the duplicate's side as an `in` entry and rebuilt by `_ROLLBACK_RESTORE_RELATED_IN`. The file's 10
  testcontainer tests pass, which is what makes this a proof instead of a reading of the Cypher.

## Closed (for reference)

- The 286 dangling edges of `knowledge:essential-graphrag` (90 MENTIONS + 196 RELATED): closed 2026-10-02 —
  two mutual merge cycles, fixed by clearing the winners' markers; audits `passed` 0/0/0; the class is now covered
  by `ENDPOINT_MENTIONS_MERGED_INVALID` and `ENDPOINT_RELATED_MERGED_INVALID`. See
  `odd/specs/tech-debt-essential-graphrag-endpoints.md` and
  `evidence-bundles/repoint-break-cycles-20261002.json`.
