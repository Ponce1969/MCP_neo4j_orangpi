# Spec: Gated CLI Dispatch for the MCP Server

**Change:** `cli-mcp-dual-primitives`
**Status:** DRAFT (frozen; not approved)
**Keywords:** RFC 2119. Scenarios: GIVEN/WHEN/THEN.

## 1. Scope

One additive MCP tool that dispatches an explicit allowlist of existing project CLIs as
sterile, time-boxed subprocesses. The blanket "MCP + parallel CLI primitives" pattern is
rejected (see `proposal.md`); this spec codifies only the gated runner slice.

## 2. Requirements

### REQ-CLI-01 — Single gated tool
The MCP server MUST expose exactly one new tool (`run_approved_cli`) and MUST NOT add
per-command MCP tools. Its schema MUST be generated from `CliRunRequest` (one schema).
GIVEN a model needing an audit, WHEN it calls `run_approved_cli(name="audit")`, THEN it
uses one schema instead of N tool schemas.

### REQ-CLI-02 — Allowlist authority
All dispatch targets MUST come from the versioned allowlist
(`config/cli_dispatch.json`); a target not in the allowlist MUST be rejected with
`CliDispatchError` before any execution.
GIVEN `name="ls"`, WHEN the tool runs, THEN it fails closed without executing.

### REQ-CLI-03 — No shell
The runner MUST invoke subprocesses with a sequence argv and `shell=False`; it MUST
NEVER join argv or accept shell syntax in `args`.
GIVEN `args={"expression": "find / -name *.env"}`, WHEN validated, THEN it is rejected
by `args_schema` (free-form strings are not in any entry's schema).

### REQ-CLI-04 — Sterile environment and secret exclusion
The child process MUST receive only `PATH`, `HOME`, a resolved `UV` path, and the
entry's `env_allowlist` entries. `NEO4J_*`, provider keys, and `MCP_ACCESS_TOKEN` MUST
be absent from the child env for `read_only: true` entries.
GIVEN a read-only entry, WHEN the runner starts it, THEN no credential variable is
visible to the subprocess.

### REQ-CLI-05 — Timeout and output caps
Every dispatch MUST enforce the entry's `timeout_s`; on expiry the process group MUST
be killed and `timed_out=True`. stdout/stderr MUST be capped at `max_output_bytes`
(default 64 KiB) with a truncation marker.
GIVEN a hung command, WHEN `timeout_s` elapses, THEN the child is killed and no partial
output is returned as success.

### REQ-CLI-06 — Read-only default; writes need approval
Allowlist entries MUST default to `read_only: true`. A write-capable entry (e.g. an
index or calibration apply) MUST NOT be added without the AGENTS.md §7.1 approval gate
and MUST require an `--approval <file>` artifact containing the word `approve`.
GIVEN a request for a write entry, WHEN no approval artifact is present, THEN the
runner refuses before executing.

### REQ-CLI-07 — Fail-closed arity
Unknown `name`, argv not matching `args_schema`, out-of-repo `cwd`, or any unexpected
deviation MUST produce a typed error and MUST NOT leave a partially executed command.
GIVEN `args` containing an undeclared key, WHEN validated, THEN the request is rejected
in full.

### REQ-CLI-08 — No regression to existing boundaries
The change MUST NOT weaken `ToolRiskTier`/`ResourcePolicy`, `require_scope`, the
namespace router, or the 8 in-process tools. The 8 tools MUST remain in-process
(converting them to subprocesses is out of scope and rejected).
GIVEN `run_approved_cli` registered, WHEN the 8 tools are listed, THEN the surface is
9 tools total, budgets unchanged.

## 3. Constraints

- Hexagonal: domain (`cli_contracts.py`) stdlib + Pydantic only.
- Gates before any milestone: `uv run ruff check .`, `uv run mypy .`,
  `uv run python scripts/validate_architecture.py`, pytest.
- Implementation units MUST NOT touch the Orange Pi; the runner smoke is part of the
  release gate only (read-only).
- Slices under the 400-line review budget; frozen until maintainer approval.