# Proposal: Gated CLI Dispatch for the MCP Server (CLI-MCP dual primitives)

**Change:** `cli-mcp-dual-primitives`
**Status:** DRAFT (frozen; not approved)
**Store:** OpenSpec (`openspec/changes/cli-mcp-dual-primitives/`)
**Technical register:** English; RFC 2119 keywords; GIVEN/WHEN/THEN scenarios.

> **Verdict recorded from the architecture review (2026-09-30, CodeGraph + repo audit):**
> the *blanket* dual-primitive premise (turn MCP operations into short-lived CLI
> subprocesses "<5ms" to save latency/RAM/tokens) is **rejected** for this codebase.
> A narrow slice — a single gated `run_approved_cli` MCP tool dispatching an explicit
> read-only allowlist — is **accepted** as the only defensible version. This proposal
> documents both halves so the rejection is not re-litigated.

## Decision

Add ONE MCP tool to `McpServerAdapter`: `run_approved_cli`. It dispatches a
command from an explicit allowlist of existing project CLIs
(`book-graph-rag audit|gate|evaluate`, `scripts/route_question.py`,
`scripts/build_namespace_profiles.py --dry-run`) as a subprocess with: no `shell=True`,
a **sterile environment**, a per-entry timeout, Pydantic-validated argv, capped output,
and fail-closed on anything outside the allowlist.

Everything else in the "dual primitives" framing is explicitly NOT adopted.

## Problem (as stated) vs Reality (as audited)

| Claimed benefit | Reality in this repo | Verdict |
|---|---|---|
| "Subprocess CLI <5ms reduces latency" | All CLIs are Python: interpreter + imports = 200ms–1s per spawn. MCP tool call in-process = function + bolt localhost (~1–10ms). Subprocess is strictly slower for the same work. | ❌ Rejected |
| "Subprocess CLI reduces RAM on the Orange Pi 5 Plus" | A Python subprocess adds ~50–150MB transient RSS (interpreter + imports). The FastMCP server is already resident. Subprocesses RAISE peak RAM on a host with 23 production containers. | ❌ Rejected |
| "Chaining Unix commands saves context tokens" | Real signal: 8 MCP tool schemas inflate the model prompt. The token win comes from a SMALLER MCP surface, not from parallel CLI primitives. Unrestricted Unix chains on a production host increase the ungoverned execution surface. | ⚠️ Partial — met by ONE gated tool, not by many |
| "Preserve fail-closed, sterile environments, no shell=True" | Already the repo norm: `ToolRiskTier`, `ResourcePolicy`, `require_scope`, and `SubprocessRAGASRunner` already spawn subprocesses without `shell=True`. | ✅ Feasible — this slice |

Existing facts that change the shape of the solution:

- CLIs already exist and are the ops surface: `book-graph-rag index/audit/gate/evaluate/query/validate/resolve-entities` (7 commands in `main.py`) plus ~25 scripts (`run_full_pipeline.py`, `run_communities.py`, `build_namespace_profiles.py`, `calibrate_namespace_routing.py`, …).
- Heavy work is ALREADY dispatched to subprocesses where appropriate
  (`SubprocessRAGASRunner` runs `run_ragas_evaluation.py`; a full gate takes ~2.5h on the Pi). Subprocess = long-lived heavy ops, never <5ms primitives.
- The MCP server already runs on the Orange Pi (FastMCP, SSE, port 8003); the graph is reachable over bolt at localhost. There is no cross-machine latency to win back.

## Goals

- Give the agent a gated, allowlisted path to INVOKE the existing CLIs from the MCP
  server (the actual gap) without exposing N new MCP tools.
- Add exactly ONE tool schema to the model prompt instead of N.
- Reuse the existing security machinery (tiers, budgets, fail-closed, sterile env,
  no `shell=True`).
- Keep the entire slice additive, read-only by default, and unit-testable.

## Non-goals

- No reimplementation of MCP tools as CLI subprocesses (`search_chunks`, `ask_global`,
  `query_cypher`, etc. stay in-process; converting them is explicitly rejected).
- No "sub-5ms" performance target — with the Python stack it is unachievable; the goal
  is CORRECT dispatch, not faster-than-in-process dispatch.
- No arbitrary Unix command execution on the Orange Pi; no `shell=True` anywhere.
- No changes to `RouteQuestionUseCase`, `require_scope`, tier budgets, or graph data.
- No mutation of the production graph from the runner (allowlist entries are read-only
  by default; any write-capable entry requires the Phase-2-style approval gate).

## Selection criteria (When MCP vs When CLI — frozen)

| Kind of operation | Chosen primitive | Rationale |
|---|---|---|
| Structured graph reads (the 8 tools) | MCP in-process | latency, auth, scope, tier budgets — already optimal |
| Long-running procedures (audit, gate, evaluate, calibrate, index, communities) | CLI subprocess via `run_approved_cli` | these ARE the existing CLIs; they already run as subprocesses; the runner only gates their invocation |
| Anything needing raw shell/chaining | ❌ Never | fail-closed: runners never accept free-form commands |