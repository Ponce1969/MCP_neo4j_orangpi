# Tasks: Gated CLI Dispatch for the MCP Server

**Decision:** DRAFT — pending maintainer approval. **Status:** FROZEN.
**Current phase:** None (frozen before Unit 0 completes).
**Delivery strategy:** Chained work-unit commits below the 400-line review budget;
alternate-review waiver documented (native RDD blocked upstream, see
`namespace-question-routing/tasks.md`).

**Why frozen:** architecture review 2026-09-30 rejected the blanket dual-primitive
premise (see `proposal.md` verdict table). Only the gated `run_approved_cli` slice is
accepted. Freeze until the maintainer approves this scope; unfreeze timing is otherwise
independent of the book-4 index (orthogonal change).

**Production policy:** no Orange Pi/Neo4j mutation during implementation units; the
runner smoke runs read-only during the release gate only.

## Unit 0 — Intent, design and contract (gated on approval)

- [x] 0A.1 Draft `proposal.md`, `design.md`, `spec.md`, `tasks.md` (untracked draft).
- [x] 0A.2 Maintainer review (2026-09-30): scope APPROVED — blanket macro-premise
      rejected as documented; only the gated `run_approved_cli` slice proceeds.
      Implementation stays FROZEN until unfreeze decision.
- [ ] 0A.3 Confirm allowlist default entries (audit/gate/evaluate/route/profiles-dry-run)
      and timeouts.

## Unit 1 — Domain contracts (frozen)

- [ ] 1.1 `cli_contracts.py`: `CliEntry`, `CliRunRequest`, `CliRunResult`,
      `CliDispatchError`.
- [ ] 1.2 Allowlist config schema (`config/cli_dispatch.json`, versioned, catalog-style).
- [ ] 1.3 Unit tests: schema validation, timeout/truncation model, fail-closed arity.

## Unit 2 — SafeCLIRunner (frozen)

- [ ] 2.1 `CliRunnerPort` + `SafeCLIRunner` (sequence argv, `shell=False`, sterile env
      with credential exclusion, process-group kill on timeout, output caps).
- [ ] 2.2 Repo-root confinement for `cwd`; refuse out-of-root paths.
- [ ] 2.3 Stub runner for tests; integration tests assert refusal of `name="ls"` and
      undeclared args.
- [ ] 2.4 Gates green (ruff/mypy/validate_architecture/pytest).

## Unit 3 — MCP integration (frozen)

- [ ] 3.1 Register `run_approved_cli` in `McpServerAdapter.create_server`; tier MEDIUM
      with existing `ResourcePolicy` budgets; schema from `CliRunRequest`.
- [ ] 3.2 Scope propagation where the dispatched CLI accepts `--scope`.
- [ ] 3.3 Regression: 9 tools total, 8 existing tools untouched, budgets unchanged.

## Unit 4 — Evidence and release gate (frozen)

- [ ] 4.1 Read-only smoke on the Orange Pi: `audit` + `route` through the runner; verify
      `NEO4J_*` absence in child env; record evidence under `data/evaluation/`.
- [ ] 4.2 OpenSpec verification report and archive (mirror of
      `namespace-question-routing` flow).