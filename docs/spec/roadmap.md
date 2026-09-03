# Roadmap — Dependency-Ordered, Low-Risk Delivery

> **Status: Active (planning).** This is a plan, not code. Phases are ordered by
> dependency and by "can we do this without touching production or adding risk".

## 1. Governing rule

**No phase mutates production or expands network exposure before evidence and explicit
human approval.** Each phase must pass the gates in `00-governance-and-scope.md` §3 and,
where it changes graph data or exposure, the readiness/security gates in 04/06/07.

## 2. Dependency order

```
Phase 0 (baseline/evidence)
   └─> Phase 1 (namespaces 02) ──> Phase 3 (semantic resolution 03)
   │          └─> Phase 2 (resumable indexing 01, needs source identity from 02)
   └─> Phase 4 (audit scope + readiness gates 04)
   └─> Phase 5 (evaluation baseline + readiness 06)
   └─> Phase 6 (MCP hardening 05 + security 07)
   └─> Phase 7 (guarded production exposure — approval-gated)
```

- **02 precedes 01** because 01's checkpoint `source_id` depends on 02's source identity.
- **02 precedes 03** because namespace bounds the candidate set and prevents cross-
  namespace over-merges.
- **04/06 precede 07/exposure** because a readiness gate and baseline are required before
  exposing the MCP.

## 3. Phases

| Phase | Deliverable | Depends on | Gate | Rollback point |
|-------|-------------|-----------|------|----------------|
| **0 — Evidence baseline** | Commit audit snapshot, evaluation baseline, and a `docs/spec/` traceability check | — | test/lint/type + audit green | No data change; revert docs/artifacts |
| **1 — Namespaces (02)** | Namespaced id scheme + catalog + migration script with `--dry-run` | 0 | audit + migration dry-run diff | Migration is idempotent; delete catalog + revert ids via inverse map |
| **2 — Resumable indexing (01)** | Checkpoint store + atomic chunk writes + replay | 1 | resume/invariant tests green | Delete checkpoint metadata (additive); existing MERGE behavior intact |
| **3 — Semantic resolution (03)** | Staged hybrid resolver + labeled dataset + quarantine/approve | 1 | eval thresholds (F1, over-merge 0) | Merge evidence supports rollback; dry-run before apply |
| **4 — Scoped audit + gates (04)** | Namespace-scoped audits + readiness gate policy | 1 | audit scope tests + gate policy | Gate config/policy is revertible |
| **5 — Evaluation + readiness (06)** | Committed baselines for all layers + CI regression gate | 0 | readiness gate green | Revert thresholds/baseline files |
| **6 — MCP hardening (05 + 07)** | Outcome tools + server-side enforcement + read-only session + log redaction + no `0.0.0.0` | 3, 4, 5 | exposure gate + denylist/read-only tests | Revert tool/alias + config; read-only session on/off |
| **7 — Guarded exposure** | Private-transport deployment behind readiness + security gates | 6 | **human approval + 07 preconditions all met** | Systemd/deploy rollback (`AGENTS.md`), read-only credential revocation |

## 4. Per-phase checklist (each phase repeats this)

1. Name the spec(s) and acceptance criteria implemented.
2. `ruff` + `mypy` + `validate_architecture.py` green.
3. Focused tests green; full suite green.
4. `book-graph-rag audit` shows no new BLOCKING findings.
5. For graph-data changes: dry-run diff reviewed; migration idempotent.
6. For exposure/auth changes: 07 risk register updated; exposure gate approved.

## 5. Rollback points (explicit)

- **Phases 0–3, 5:** additive or metadata-only; rollback = remove the new artifact/
  metadata and restore prior ids/baseline via the recorded inverse mapping.
- **Phase 4:** gate policy/config revert.
- **Phase 6:** tool aliases + config + read-only session revert; no graph data change.
- **Phase 7:** deploy/systemd rollback and read-only credential revocation (already
  documented in `deploy/README.md`).

## 6. Non-goals of this roadmap

- No Orange Pi / production mutation in any phase before Phase 7, and Phase 7 itself is
  approval-gated.
- No universal-benchmark adoption without a project-owned baseline (06 §2).
- No automatic re-enabling of the `agentic-patterns` MCP (`AGENTS.md` §6).

## 7. Decisions (closed 2026-09-02)

- Sequence 02 → 01 → 03 as written. 02-first is confirmed: it is the lowest-risk
  dependency and avoids re-keying checkpoints later. See `02` §6 and `00` §8.
