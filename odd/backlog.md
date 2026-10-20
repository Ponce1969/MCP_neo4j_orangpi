# Backlog — agentic-book-graph

Follow-ups that are real, evidenced and deliberately **out of** the work unit that discovered them.
Each item states what it is, the evidence that it exists, and the block it belongs to. Engram mirror:
`debt/merge-adapter-and-audit-followups` (obs 1455). The reusable diagnosis for the closed debt is
`pattern/merged-into-cycle-diagnosis` (obs 1457).

## Operational findings (infrastructure)

- **O5 A fresh clone's example config and its auth posture both differ from production.** Found
  2026-10-10 by an agent working on the separate `agentic-memory` repository, and verified here
  against the code rather than assumed:
  (a) **`.env.example` steers a fresh clone at NVIDIA with a Llama model**, so `ask_global` does
  not behave as it does in production, where the host runs `deepseek-v4-flash`. The code's own
  defaults are empty strings and `validate_llm_provider_settings` fails fast without a base URL
  and a model name, so the drift lives in the example file and not in the defaults. That file is
  the maintainer's and was neither read nor edited here.
  (b) **`MCP_ACCESS_TOKEN` is absent from `.env.example`**, while `docs/ops/mcp-service.md` §7
  states that every path requires `Authorization: Bearer <MCP_ACCESS_TOKEN>` and that the token
  lives in `<repo>/.env`. The question asked was whether a missing variable leaves the service
  fail-open or fail-closed. **Verified answer: fail-open on authentication.** `mcp_access_token`
  defaults to `None` (`config.py`) and `mcp_server_adapter.py` installs
  `BearerTokenAuthMiddleware` only when the value is set and non-empty, so a clone without the
  variable serves every tool with no Authorization header at all. The blast radius is bounded by
  two other defaults: `mcp_bind_host` defaults to `127.0.0.1`, and a production environment
  refuses a wildcard bind. A fresh clone is therefore unauthenticated but reachable only from its
  own machine — unless someone points it at a non-loopback host outside production, which is
  precisely the case the documentation says cannot happen.
  **Decision needed (maintainer):** either make the service refuse to start without a token
  (fail-closed, matching §7) or state plainly that a token-less service is loopback-only and
  unauthenticated. Today the two documents disagree with the code.
  **Resolved 2026-10-11 as fail-closed** (`9f09772`): with `app_env=production` and no token the
  server refuses to start (exit 2) and outside production it starts with a loud warning, scoped
  exactly like the wildcard-bind gate. An **independent verifier** challenged it and confirmed:
  `serve` is the only production entrypoint, the refusal is the first executable statement of
  `_run_server` (before any adapter is constructed and before anything binds), the warning is
  development/test only, and an empty-string token is refused. Two residuals it found, neither
  reachable through the shipped CLI:
  (i) **a whitespace-only token is accepted as a credential** — the value is truthy, so the gate
  passes and the middleware wraps the app expecting `Bearer  `; the server is still
  authenticated, but the value is never sanitized;
  (ii) **`run_sse` itself has no production gate** — enforcement lives in the composition root,
  so a future programmatic caller of the adapter's `run_sse` would bypass it. Defense in depth
  would put the check where serving happens; the adapter already knows `app_env`.
  Also confirmed: **a fresh clone following `.env.example` now exits 2 at startup**, because the
  template has no `MCP_ACCESS_TOKEN`. The example must gain it — and should point its LLM
  provider at production's shape instead of NVIDIA/Llama — for the fail-closed default to be
  usable by anyone but this host, which already sets the token (verified: 401 without it).
  **Both residuals closed 2026-10-11** (`96ba64e`): a token of spaces now counts as absent in
  both read sites, so it can never become a credential, and `run_sse` refuses in production
  before building the app or binding a port (defense in depth). A second independent verifier
  confirmed the ordering, the absence of disagreement between the two layers, constant-time
  comparison, fail-fast `APP_ENV` parsing, and that an empty or space-padded `.env` value is
  treated as absent. It found **one narrower residual of the same class**: `str.strip()` removes
  whitespace but not invisible **non**-whitespace characters (zero-width space U+200B, BOM
  U+FEFF), so a token consisting only of those would still pass the gate and become a
  trivially-guessable credential. A one-line guard would be `any(char.isprintable() ...)` on the
  stripped value. It also noted the inherent policy gap that a low-entropy token like `"a"`
  passes: the code does not assess guessability, and that is a decision rather than a bug.
  **Closed 2026-10-18** (the template by #37 and the read-only-credential work; the guard by R2):
  (a) the example now points **both** roles at production's provider — `GRAPH_LLM_MODEL_NAME=deepseek-v4-pro`
  and `QUERY_LLM_MODEL_NAME=deepseek-v4-flash`, the latter with the comment that production runs the same —
  so a fresh clone no longer lands on NVIDIA/Llama; (b) the template **carries `MCP_ACCESS_TOKEN=`** and its
  header states that a production server refuses to start without it; and the narrower residual this entry
  recorded is closed: `domain/mcp_security.py::is_effective_secret_shape` applies exactly the
  `any(character.isprintable() ...)` guard it asked for, at both read sites, so a token of invisible
  non-whitespace characters can no longer become a credential. Left standing as this entry itself declared:
  the low-entropy-token gap is a decision, not a bug. **Verified 2026-10-18 on the host**: the names-only diff
  (whitespace-tolerant on both sides) leaves exactly one variable in production's `.env` absent from the
  template — `NEO4J_BIND_HOST`, which the compose defaults to `127.0.0.1` — and six template-only variables
  (`APP_ENV`, `ASK_GLOBAL_*`, `COMMUNITY_*`, `ROUTER_*`) that the code defaults supply. The entry's opening
  claim is settled, not inherited. *(A first attempt at this diff used a line-anchored pattern, which reads
  indented keys as empty strings and reported 46 phantom absences; the tolerant form is the one to reuse.)*
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
  **Stale as written 2026-10-18, kept for the record.** This is pre-decision narration: the two gaps it lists are
  closed in the "two gaps before any new merge" entry below, the adjacent `merged_into` defect is closed under
  the same block header, and the block is declared closed 2026-10-06. Nothing here is pending work; the only
  live remainder of Block A is the community rebuild noted there.
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
- **B5 RAGAS drop warning — closed 2026-10-08: it was a dead, mislabelled branch.** `drop_warning` is
  computed on **faithfulness** (`stub_ragas_runner.py` and `subprocess_ragas_runner.py`,
  `_DROP_THRESHOLD = 0.05`), yet the retrieval layer's message said "RAGAS context_precision drop
  detected" — and that layer calls the runner with `previous_metrics=None`
  (`evaluate_retrieval_layer_use_case.py`), so the flag is always `False` there and the branch could
  never fire. The last gate report (2026-10-01) confirms it: `overall_state: passed`, exit 0, and
  **no warnings in any layer**. The branch and the test that pinned its message are gone; the
  generation layer keeps its own warning, which is reachable and correctly worded ("RAGAS drop
  detected vs baseline"). If a real `context_precision` drop check is ever wanted, that is a feature
  — pass a previous snapshot in the retrieval layer and label it right — not a fix.
- **B2 Retrieval warning — closed 2026-10-07.** The warning was noise by construction, not a signal:
  `_PRECISION_WARNING_THRESHOLD` was a hardcoded `0.5` while the metric is `matched / len(contexts)` with `k=10`,
  so the ceiling with *perfect* retrieval is `mean(min(len(refs), k)/k) = 0.1917` over the 12 `current` records —
  it could never be satisfied. Both hypotheses this entry carried were wrong: the 8 future-corpus records are
  filtered correctly (they carry 0 reference ids and `corpus: "future"`), and the drop from the `0.0549` of the
  2026-09-23 Phase-5 note to the committed `0.0478` is the expected effect of `80f68f1` ("exclude merged_into
  entities in find_entity and batch lookup", 2026-09-26), an ancestor of the baseline's `code_commit` `3605e33` —
  the old number was inflated by ghosts, so the drop is a correctness fix, not degradation.

  Fix: the arbitrary threshold is gone and the committed baseline (`precision_at_k_min`, the criterion
  `docs/spec/06-evaluation-and-readiness.md` already names: ">= baseline") is the only alert mechanism; the
  measured value stays visible in the rationale. The module docstring now carries the ceiling **formula** and the
  dated value, so the next reader recomputes it instead of hardcoding another absolute number. RED first: the new
  test asserts that precision `0.1` against a committed `0.045` is silent (it warned before), and the two tests
  that pinned the old warning were rewritten to the new contract — the `chunk_id` one keeps its `0.0` assertion,
  which is its real point. Residual, noted for whoever finalizes thresholds: the committed `0.045` sits ~6 % below
  the measured `0.0478`, so that check is the fragile one now, and it is the one that matters.
  **Superseded 2026-10-18**: nobody has to finalize these numbers any more. As of 2026-10-15 the committed
  retrieval threshold is `precision_at_k_min = 0.1175` against a measured `precision_at_k = 0.1250`, with
  `thresholds_finalized: true` in `data/evaluation/retrieval_baseline.json`. Both the `0.045` and the `0.0478`
  this residual worried about are gone from the gate's inputs.
- **B3 Legacy checkpoints — closed 2026-10-07 as "accepted, nothing to repair".** A read-only census of
  production (`MATCH (c:Checkpoint)` per source, run through the project's own container) settled what this entry
  had guessed:

  | source | chunks | `PROCESSED` | gap |
  |---|---|---|---|
  | `knowledge:agentic-architectural-patterns` | 1520 | 1515 | **5** |
  | `knowledge:ai-engineering-huyen` | 989 | 989 | 0 |
  | `knowledge:essential-graphrag` | 302 | 302 | 0 |
  | `knowledge:graphrag-agentic` | 657 | 657 | 0 |

  So the graph is 3463/3468 backfilled, "graph predates Phase 2" was the wrong frame, and the 5 indices this entry
  named (57, 1026, 1302, 1499, 1501) really are the only chunks without a `PROCESSED` checkpoint. **The proposed
  remedy could not have worked**: `fetch_backfill_candidates` (`neo4j_command_adapter.py:725`) selects chunks with
  at least one outgoing `MENTIONS` edge and never looks at checkpoints — its own docstring says "Libro 1 has 16.200
  mentions for 1.515 chunks" — so an `--apply` would have rewritten the 1515 checkpoints that already exist and
  left the 5 untouched. Those 5 are exactly the chunks with **no mentions**, i.e. chunks that produced no entities:
  the state is legitimate and a resume run is the only tool that could touch them. Accepted as is.

  Method note for whoever revisits: the dry-run reports *candidates to mark*, not *missing checkpoints*, and the
  two sets differ (`ai-engineering-huyen` has 987 chunks with mentions against 989 with checkpoints). A first pass
  of this analysis got that backwards and claimed the other three books were not at 100 %; the census disproved it,
  and the correction is recorded here on purpose.
- **B4 `catalog.yaml` and the host's `data/` disagreed — resolved 2026-10-07.** `catalog.yaml` points
  `knowledge:essential-graphrag` at `data/Essential_GraphRAG.pdf`, which was missing on the host. A checkpoint stores
  `source_version = sha256(pdf_bytes)[:16]` (`infrastructure/version_dimensions.py:38`), so a source can be matched to
  its PDF by hash instead of by name — and three of the four matched a host PDF exactly (`425c9533…` Book 1,
  `e5dfe834…` ai-engineering-huyen, `0f31856f…` graphrag-agentic) while `knowledge:essential-graphrag` matched
  nothing there and `163660ba…` existed only on the development machine. Root cause: `*.pdf` is in `.gitignore`, so
  the PDFs are local artifacts that never travel with the repo; four reached the host by other means and this one did
  not. Fixed by copying it (13 841 182 bytes): it verifies as the same hash on both sides, and the backfill dry-run
  for that source now runs and reports `source_version: 163660ba80115472`, matching the 302 checkpoints already
  stored — the exact bytes, so a future re-index stays consistent instead of looking like a new source version.

  A first pass of this analysis also called the required `PDF_PATH` of `index` a defect, on the claim that a backfill
  "never reads the PDF". `main.py:293` disproves it: the checkpoint version dimensions are computed from
  `pdf_path.read_bytes()`. The requirement is correct.

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

## MCP consumer surface (implemented 2026-10-10; found from the agentic-memory work)

Both items below are **implemented**; the evidence that motivated them is kept as written.

- **C1 (implemented `42cbd2e`) `ask_global` returns citations but not the contexts it used.** The answer carries
  `[Data: CommunitySummary(<id>)]` markers, so a consumer can confirm that an id exists but
  cannot audit whether that summary actually supports the sentence. The summaries were
  already fetched and scored inside the server, so returning their text costs **no extra LLM
  call** — only payload bytes. Evidence: (a) the real `ask_global` payload read on 2026-10-10
  is `{"answer": "- Logs for flat event coverage ... [Data: CommunitySummary(007873583f96e774)] ..."}`
  — one answer string and ids, no summary text; (b) a memory service that must decide whether an
  answer has "sufficient evidence" cannot do it from ids alone; (c) a RAGAS faithfulness run over
  answers obtained through the MCP is impossible for the same reason, which is what blocked the
  level-0 vs level-1 quality A/B. Proposed shape (additive, so no existing consumer breaks): keep
  `answer` short and add sibling fields — `citations` (ids), `contexts` (`[{id, level, score, text}]`),
  `graph_version`, and `usage` (`{llm_calls, detail_level}`); allow trimming the contexts with a
  parameter for callers that only want the answer. A `usage.llm_calls` counter would also remove the
  need to scrape `journalctl` to count calls.
- **C2 (implemented `e3e5053` + `9650d93`) The graph does not expose a data version identifier.** A caller that caches or cites an
  answer needs to name the graph state it came from, and today there is nothing to name:
  `agentic-memory` was fed a date by hand (`2026-10-10`). Proposed: derive a cheap, reproducible
  `graph_version` from a fixed census (counts by label + newest `indexed_at` + the catalog revision)
  rather than from a counter that needs its own state, and expose it both as a field in every read
  response and as an MCP resource (`bookgraph://version`). Consumers then pass it back verbatim,
  which is what keeps the memory from ever querying the graph (see §8 of AGENTS.md).

## Evaluation instrument findings (found 2026-10-15, out of the retrieval-reranker-port work unit)

Both were found while running the layer-5 gate for `retrieval-reranker-port` (T8) and both predate that
branch. The owner's routing decision (2026-10-15) was to record them here as separate work rather than
extend that feature. Neither one invalidates T8's number: the gate's `faithfulness` threshold is applied
to the project-owned claims metric, which is what T8 measured.

- **E1 The RAGAS secondary metric of layers 4 and 5 is unmeasurable by construction, and the subprocess
  failure that hides it reads like a provider problem.** Chain, with code:
  `evaluate_generation_layer_use_case.py:246` builds
  `generation_results = tuple((r.question_id, "", ()))`, discarding the answer and the contexts the use
  case already holds (the retrieval layer does the same at `evaluate_retrieval_layer_use_case.py:184`);
  `subprocess_ragas_runner._write_generation_jsonl` writes `"question": question_id`, so the *id*
  travels as the question; and `scripts/run_ragas_evaluation.py` ignores the payload and re-retrieves
  from that string, which is what makes the value noise. Measured evidence: the runner's input is
  **98 bytes** (`answer: ""`, `contexts: []`, `question: "gen-001"`), and running the script directly on
  that same input returned `faithfulness` **1.0** once and **0.0** another time. The subprocess inside
  the gate still exits 1 and the runner swallows its stderr into a one-line warning. It also rewrites the untracked `docs/benchmarks/gr3_after.json` on every gate run, so the repo's own `test_gr3_after_removed_or_distinct` guard fails until that file is deleted by hand. Consequences: the
  `0.6825` that `generation_baseline.json` recorded as `metrics.faithfulness` is a standalone-benchmark
  number, not this path's; and the `0.792` of the 2026-10-09 reference run was never RAGAS (the empty
  feed dates from `b442705`, 2026-09-10) but the project-owned claims metric. Fix shape if it is taken:
  pass the real question, answer and contexts through `RAGASRunnerPort.run` (the port signature already
  carries them), stop writing the id as the question, and let the use case keep the per-question answer
  so the secondary signal becomes a real measurement.
  **Closed 2026-10-18 by the E3b merge** (`96e2131` + `3ff0382` + `2e77d67`), with **one claim of this entry
  corrected**. (a) The empty tuple is gone: the layer now builds a typed `RagasSample` from the real
  per-question question, answer, contexts and reference. (b) The runner writes `sample.question`, never the id,
  and the script scores what it receives — its re-answering path was removed. (c) **This entry's
  `gr3_after.json` claim was wrong**: the runner always passes `--after-output` and redirects that write (the
  redirect is documented in `subprocess_ragas_runner.py`), and a full 26-minute run with integration left
  `docs/benchmarks/` byte-identical. The file that broke the guard on the host was a residue of a **manual**
  script run, not of the gate: the guard was reporting real dirt, correctly. (d) The `0.6825` reading is
  superseded by the 2026-10-15 recalibration: the baseline records
  `metrics.faithfulness = 0.8988195615514334` with `faithfulness_min = 0.8`. The fix shape this entry proposed
  — the real question, answer and contexts through the port, no id as the question — is exactly what E3b
  implemented.
  **Its last live sentence is fixed 2026-10-18.** "The subprocess inside the gate still exits 1" was not a
  provider problem but a configuration-resolution one: the child script runs in a **temporary directory**, and
  pydantic-settings resolves a dotenv **relative to the working directory**. The settings the parent had already
  resolved were never exported into the child's environment, so the child died building its own `Settings`. The
  runner now passes them as **values derived from the settings model's own fields** — never the path to a file,
  which would be a second source of truth in a directory that exists to be disposable — so a setting added later
  cannot be forgotten. Diagnostic evidence: the operator's traceback (`scripts/run_ragas_evaluation.py:422`,
  `Settings.model_validate` under `cwd=/tmp` ⇒ `ValidationError` on `neo4j_uri` / `neo4j_user` /
  `neo4j_password`) matched the 438-byte note R5 had redacted, byte for byte.
- **E2 The layer-5 contexts do not depend on the question.** `fetch_contexts(qtype="global")` returned
  exactly the same **164 contexts, 674 623 chars (659 KiB), zero with a `chunk_id`** for `gen-001`,
  `gen-002` and `gen-003` on production (0.2–0.3 s each, measured 2026-10-15 over the SSH tunnel). Two
  consequences: the claims-faithfulness score is a self-consistency check of the model against a fixed
  blob rather than a per-question retrieval quality signal (`cited_context_share` is null by design,
  `contexts_with_ids = 0`), and every claims/verify/pairwise prompt carries ~659 KiB (~170 k tokens),
  which is the "674 KB per question" first measured on 2026-10-11 and is most of the gate's 72 s per
  question. Worth doing with the retrieval work: find why the chunk half is absent for `global` and why
  the entity part is identical across questions before tuning anything on top of this metric.
  **Resolved 2026-10-15** by `feat/eval-question-aware-global-retrieval` (`4ed3c96`): the root cause was
  that the evaluation adapter built a `GlobalQueryUseCase` and never called it, so the layer composed
  its own answer from the first fifteen of every summary. The ranking now lives in one place
  (`GlobalQueryUseCase._rank`, shared by `ask` and the new `select`), the port gained the additive
  `answer_global`, and the layer answers global questions through the production path. Measured effect
  on the same 35 questions: `faithfulness` 0.7913 → **0.8988**, `pairwise_win_rate` 0.3714 (13/35) →
  **0.9714** (34/35), `cited_context_share` undefined → **0.5143**, contexts 164 → **8** with ids. What
  remains of the original item is the *pool recall* half, tracked as the README's retrieval-quality
  step (candidate generation, hybrid lexical + vector).

## Closed (for reference)

- The 286 dangling edges of `knowledge:essential-graphrag` (90 MENTIONS + 196 RELATED): closed 2026-10-02 —
  two mutual merge cycles, fixed by clearing the winners' markers; audits `passed` 0/0/0; the class is now covered
  by `ENDPOINT_MENTIONS_MERGED_INVALID` and `ENDPOINT_RELATED_MERGED_INVALID`. See
  `odd/specs/tech-debt-essential-graphrag-endpoints.md` and
  `evidence-bundles/repoint-break-cycles-20261002.json`.

- **E3 The readiness gate's warnings told two stories and read as one.** Two defects the
  warning-provenance fix deliberately left open, recorded rather than folded into it.
  **(a)** The retrieval layer invokes the secondary RAGAS scorer with an empty result set, which
  makes the script exit non-zero and the layer report `RAGAS unavailable: subprocess ragas failed`
  on **every** run - a warning that cries in every report is one operators learn to skip, the same
  failure mode as noisy drift. **(b)** The generation layer feeds that scorer a phantom dataset
  (`question` = the question id, empty answers and contexts) and the script re-answers every
  `local` question from the graph, so the secondary number measures something that never touched
  the evaluated answers. Both need their own decision: (a) is a skip-when-empty in the layer;
  (b) is a feature, and it is the reason the drop warning was misattributed in the first place.
  **Closed 2026-10-18, both halves.** (a) by `0f6f4d2` (skip-when-empty in the retrieval layer:
  no call, no crying warning, the fact in the rationale). (b) by `feat/e3b-real-answers-to-ragas`
  (`odd/tasks/e3b-real-answers-to-ragas.md`): the port carries a typed `RagasSample` with the
  real question, the produced answer and the contexts, the generation layer mirrors the E3a
  skip, and `scripts/run_ragas_evaluation.py` is a pure scorer — the re-answering
  (`_eval_all`/`_proxy_compose`/`_evaluate_*`) is eradicated, rows missing answer or contexts
  are skipped, an all-skipped run fails loudly, and the reference travels in the input JSONL as
  evidence only (never into the metric). The historical `0.7143` stays labelled as measured
  over re-answered questions; new runs measure the pipeline's own answers.

- **The project-owned RAGAS baseline exists (2026-10-19).** The prerequisite the agent-cache spec asked
  for: the canonical pair of `book-graph-rag evaluate --layer generation` runs, with its provenance and its
  band, is recorded in `docs/spec/08-agent-cache-and-recipes.md`. The secondary scorer had been unmeasurable
  through the supported path since 2026-10-09 ("RAGAS did not measure"); E3b made it score the pipeline's own
  answers and the subprocess configuration fix (PR #49) made the child able to resolve its settings at all.
  Read the band before any delta: the answer-dependent metrics move ~9 points between identical runs, while
  the context metric moved 0.0006.

- **The extraction layer's placeholder is gone; the coverage instrument is measured (2026-10-19,
  `6062e0b`).** Closed by `feat/extraction-coverage-instrument` (`6062e0b`, docs in the follow-up
  commit; contract in `odd/tasks/extraction-coverage-instrument.md`). The layer used to be a hard-coded
  stub reporting `pending` with `coverage: 0.0, threshold=None` — a zero that reads like a measurement.
  It now reports the operator-approved quotient (S1): numerator `COUNT(DISTINCT e)` of **active**
  entities (`merged_into IS NULL OR merged_into = ''`) with at least one incoming
  `(:Chunk)-[:MENTIONS]->(e)`, denominator = the same active population; merged entities are excluded
  from both halves, so the audit inventory (21393, includes merged) is never the denominator. The
  rationale prints the raw numbers, the explicit denominator and the per-book breakdown. The dead-letter
  count (`failed_checkpoints`, no threshold — inventing one would invent a goal) reuses the **existing**
  `CheckpointPort.fetch_state` over each book's chunk range; the admitted boundary is that a FAILED row
  outside a book's *current* range is not counted. **Evidence** (live graph, read-only, 2026-10-19,
  commit `bd6c9e8`): coverage **20720/20720 = 1.0**; per-book mentioned entities 7110 / 6526 / 5906 /
  1178 (chunks 1515 / 987 / 657 / 299); `failed_checkpoints = 2` in 1 source; `ENTITY_UNMENTIONED = 0`.
  The instrument is born a **guardian of an invariant** (REQ-PROV-01 ⇒ ratio 1.0 until the pipeline
  breaks). The layer stays optional/non-blocking: below the 0.95 threshold it keeps `status = PASSED`
  and reports a corpus-diagnosis warning (a WARNING status on an optional layer would be masked by
  `derive_overall_status`). Historical records that still say `extraction pending` (spec 07 §5 item 6,
  README's 2026-10-09 run) are kept as written; the spec 06 baseline checkbox stays open because this
  measures connectivity/provenance (REQ-PROV-03), never semantic fidelity.

- **Fase 2 léxica (option B): el instrumento está listo, la corrida es del humano (2026-10-19,
  rama `feat/retrieval-phase2-lexical`).** Reabierta bajo la enmienda del ADR (spec 09 §10) con el
  criterio de corte del operador fijado antes de cualquier corrida: shippear solo con **>= +1
  referencia neta sin perder ninguna** contra la línea base commiteada (`0.125`, leída de
  `data/evaluation/retrieval_baseline.json` — el `COMMITTED_BASELINE` hardcodeado del script se
  eliminó), fila de control en la misma corrida y corrida **VOID** si el control no la reproduce.
  Entregado: `--shape-ab` en `scripts/eval_fulltext_recall.py` (shape frase con escape Lucene
  caller-side — no existía escape alguno en el repo; sin él se mediría un error de sintaxis),
  analyzer `standard-no-stop-words` declarado en el DDL de `ensure_indexes` (cero cambio de
  número: es el analyzer observado en producción), recreación gateada idempotente
  (`scripts-ops/recreate_chunk_text_index.py`, dry-run por defecto, approval + fingerprint) y
  la métrica `latency_p95_graph_floor_ms` (p95 nearest-rank sobre `fetch_contexts`, n = 12,
  umbral 250 ms `<=`, piso del camino de grafo — el envelope de MCP requiere su propio
  benchmark). **Pendiente: la corrida del operador** contra el grafo de producción (read-only)
  con las variantes pre-registradas; los números medidos se anexan en spec 09 §10. El rango
  de B está conocido de antes: máximo 3 referencias (las del pool fuera del top-10).

- **Fase 2 léxica — CERRADA bajo Opción C (2026-10-20).** La línea `Pendiente: la corrida del
  operador` de la entrada de arriba quedó satisfecha: las dos variantes pre-registradas se corrieron
  en hardware real (Neo4j 5.23, Orange Pi) y ninguna alcanza el objetivo. Acta completa en
  **spec 09 §11**; acá los números, que son el motivo del cierre:

  | variante | hits / 120 | precision@10 | veredicto |
  |---|---|---|---|
  | — (baseline commiteado, `standard-no-stop-words`) | 15 | **0.1250** | referencia |
  | 1 · forma (frase escapada) | 0 | **0.0000** | −15 netas, descartada |
  | 2 · analyzer `standard` | 16 | **0.1333** | +1 neto (ret-014), 0 perdidas, pero no 0.150 |

  La variante 2 cumple la barra antirruido de la enmienda (monotonía estricta) y **no** el objetivo
  `0.150` (18 hits). Se descartó para no matar el canario de drift (la fila de control habría
  declarado `RUN VOID` en cada corrida, porque el baseline se midió con el otro analyzer), no
  reintroducir la divergencia de configuración que S5 eliminó, y no abrir una segunda ventana de DDL.
  **Rollback medido**: índice recreado con `standard-no-stop-words`, control de vuelta en **0.1250
  (15/120), cero drift** — producción y `data/evaluation/retrieval_baseline.json` vuelven a coincidir.
  **Sin tercer intento**, conforme al contrato fijado antes de correr.

  **Evidencia de cuello de botella**: mover el analyzer mueve **una** referencia de 120, y los tres
  cross-encoders ya habían quedado por debajo del orden fulltext puro. Dos palancas independientes
  sobre la etapa de *ranking* no mueven una métrica que es de conjuntos en el top-10 ⇒ las
  referencias faltantes no están mal ordenadas, **no están en el pool**. Es el mismo mecanismo que
  la spec 09 §3 identificó y lo que deja a la alternativa A (híbrido vectorial nativo) como el único
  camino no construido, con el resultado de fase 0 (§9) ya en contra.

  **Subproducto operativo (PR #55)**: el primer `--apply` de la corrida falló con `CypherSyntaxError`
  —el DDL usaba `'fulltext.analyzer'` y una clave de mapa Cypher exige identificador o backticks—.
  Los pines de bytes no podían verlo (un statement inválido es idéntico a su propia copia) y pasó
  tres PRs en verde. Corregido con el statement que Neo4j 5.23 aceptó y con un guard sintáctico
  (`tests/unit/test_cypher_map_keys.py`). El failure path de PR #54 fue lo que lo hizo ruidoso en vez
  de silencioso.
