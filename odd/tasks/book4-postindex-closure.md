# Book 4 — post-index closure: edge cleanup, merge to main, 4-source routing, re-gate, MCP restart

## Context

`odd/tasks/book4-huyen-intra-resolution.md` closed the 4th namespace (989/989 chunks,
354 merges / 409 duplicates folded, scoped + global audits `passed 0/0/0`). Four
integrity gaps were measured and registered in Engram (obs 1449). The maintainer
authorized, on 2026-10-01:

- **(a)** delete the one RELATED edge in `knowledge:ai-engineering-huyen` that points at a
  soft-deleted duplicate.
- **(b)** delete the 5 self-loop `(:Entity)-[:RELATED]->(:Entity)` edges (2 in this
  namespace, 2 in `essential-graphrag`, 1 in `graphrag-agentic`).
- **(c)** postponed: the pre-existing `essential-graphrag` debt (90 MENTIONS + 196 RELATED
  edges touching merged entities) → **technical debt, do not touch now**.
- **(4)** postponed: case-insensitive / semantic duplicate detection → registered for the
  semantic resolution phase.

Then: push the branch and merge to `main`, rebuild the namespace routing profiles and
recalibrate thresholds with the 4 sources, re-gate persisting the report, and restart the
MCP service in a controlled way so the 4th namespace is exposed.

## Findings that shape the plan

- The MCP server is a **systemd unit** (`/etc/systemd/system/mcp-server.service`,
  `User=gonzalo`, `ExecStart=uv run book-graph-rag-mcp serve`, `Restart=on-failure`), and it
  is in a **crash-loop: 43.447 restarts**. Cause: it binds `100.106.85.109:8003` and the
  port is held by a stale manual process (`PID 1163609`, started 2026-09-26, still serving).
  The controlled restart is therefore: stop the squatter, let systemd bind the port.
- `data/router/namespace_profiles.json` (Pi-local artifact, untracked) has **3 profiles**
  (snapshot `pi-prod-2026-09-29`); the 4th source is missing.
- Routing thresholds are **code defaults** in `domain/routing_models.py`
  (`min_top_score=0.10`, `min_margin=0.05`, calibrated 2026-09-29) and documented in
  `docs/ops/namespace-routing-baseline.md`; nothing else persists them.
- The committed routing dataset has **31 labels and none for `ai-engineering-huyen`**
  (10 + 10 + 9 + out-of-domain). "Calibrate with 4 sources" therefore means: 4 candidate
  centroids compete for the score, while labeled coverage stays 3 namespaces —
  that limitation must be stated in the report, not hidden.

## Tasks

- [x] T1: `scripts-ops/cleanup_dangling_edges.py` — dry-run by default, `--apply --backup --approval` (archive gate), scoped dangling-merged + graph-wide self-loops; gates + commit
- [x] T2: Pi — fresh backup, dry-run, apply (a)+(b), verify: 0 dangling in this namespace, 0 self-loops, scoped audit still `passed`
- [x] T3: push `feat/book4-huyen-intra-resolution` + merge to `main` + push `main` + align the Pi working tree (two files are untracked copies there)
- [x] T4: Pi — rebuild `namespace_profiles.json` with the 4 sources, with graph-snapshot provenance
- [x] T5: Pi — recalibrate routing thresholds with 4 sources; compare against 0.10/0.05; persist the decision (code default + ops doc) if it changes
- [ ] T6: Pi — global re-gate persisting `data/evaluation/gate_report.json`
- [x] T7: Pi — controlled MCP restart (release the port, systemd binds, healthy) + smoke exposing the 4th namespace
- [ ] T8: close — Engram (technical debt for (c), semantic duplicates for the next phase) + outcome report

## Evidence

- **T1 (2026-10-01)**: commit `85167c7` on `feat/book4-huyen-intra-resolution`; pre-commit green (ruff,
  mypy, architecture, ruff format). Only relationships are deleted.
- **T2 (Pi)**: backup `~/backups_neo4j/bookgraph_backup_20261001T232413Z.json` (175 MB) taken right
  before the mutation. Dry-run reported exactly: 5 self-loops graph-wide and 1 dangling RELATED in
  this namespace (197 graph-wide). Apply: **self-loops 5 -> 0**, **dangling in this namespace
  1 -> 0**, remaining foreign debt **196** (the deferred `essential-graphrag` case). Independent
  re-query confirmed both zeros; scoped audit still `passed 0/0/0`; namespace RELATED 12.991 -> 12.988
  (the deleted 3 edges), MENTIONS 12.664 and active entities 6.963 unchanged.
- **T3**: `feat/book4-huyen-intra-resolution` pushed; `main` fast-forwarded `3be3a04..85167c7` and
  pushed. Second work unit (`924aecc` docs, `bd7d84e` smoke) merged on top: `main = bd7d84e`.
  The Pi needed `mv` of the scp'd copies aside before pulling: the first `pull --ff-only` aborted
  (untracked files would be overwritten), the second succeeded and the files are tracked now.
- **T4 (Pi)**: `scripts/build_namespace_profiles.py --graph-snapshot pi-prod-2026-10-01` ->
  **4 profiles** (agentic-architectural-patterns, ai-engineering-huyen, essential-graphrag,
  graphrag-agentic), dim=384, `paraphrase-multilingual-MiniLM-L12-v2`, artifact
  `data/router/namespace_profiles.json` (Pi-local, untracked).
- **T5 (Pi)**: `scripts/calibrate_namespace_routing.py --json-only` swept 84 combinations over 31
  labels. Recommendation = **0.10 / 0.05, the existing defaults** (accuracy 0.682, wrong 0.0,
  abstention 0.484, multi 0.667) -> the numeric defaults did NOT change. Documented evidence moved
  (2026-09-29 three-namespace run: 0.727 / 0.0 / 0.452), so `domain/routing_models.py` and
  `docs/ops/namespace-routing-baseline.md` were updated (commit `924aecc`). Explicit limitation:
  the dataset has **no label for the new namespace**, so this measures the perturbing effect of a
  4th candidate, not quality on the new book.
- **T7 (Pi)**: root cause of the MCP crash-loop (counter 43.558): the systemd unit
  `mcp-server.service` could not bind `100.106.85.109:8003` because a manual process from
  2026-09-26 (PID 1163609) held the port. Killed the squatter; systemd bound within its 5 s restart
  window (`active`, MainPID 742075 -> python 742078, "Uvicorn running on
  http://100.106.85.109:8003", no bind error). Smoke `scripts-ops/mcp_smoke_book4.py`: handshake
  `book-graph-rag 1.28.0`, `count_entities` for all four namespaces (7111 / 1241 / 6078 / **6963**),
  `search_rag` inside the new namespace returning entities with page + chunk_index provenance,
  cross-book isolation holding, 8 tools exposed.
