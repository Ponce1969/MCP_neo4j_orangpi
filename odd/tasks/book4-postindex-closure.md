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

- [ ] T1: `scripts-ops/cleanup_dangling_edges.py` — dry-run by default, `--apply --backup --approval` (archive gate), scoped dangling-merged + graph-wide self-loops; gates + commit
- [ ] T2: Pi — fresh backup, dry-run, apply (a)+(b), verify: 0 dangling in this namespace, 0 self-loops, scoped audit still `passed`
- [ ] T3: push `feat/book4-huyen-intra-resolution` + merge to `main` + push `main` + align the Pi working tree (two files are untracked copies there)
- [ ] T4: Pi — rebuild `namespace_profiles.json` with the 4 sources, with graph-snapshot provenance
- [ ] T5: Pi — recalibrate routing thresholds with 4 sources; compare against 0.10/0.05; persist the decision (code default + ops doc) if it changes
- [ ] T6: Pi — global re-gate persisting `data/evaluation/gate_report.json`
- [ ] T7: Pi — controlled MCP restart (release the port, systemd binds, healthy) + smoke exposing the 4th namespace
- [ ] T8: close — Engram (technical debt for (c), semantic duplicates for the next phase) + outcome report

## Evidence

(filled per task as the work unit closes)
