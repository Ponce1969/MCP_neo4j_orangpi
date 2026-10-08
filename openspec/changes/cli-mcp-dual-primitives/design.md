# Design: Gated CLI Dispatch for the MCP Server (CLI-MCP dual primitives)

## 1. Components

```
McpServerAdapter (FastMCP, 8 tools)
   └── run_approved_cli            (NEW, the only addition)
         └── CliDispatchConfig     (allowlist from config, not code)
         └── SafeCLIRunner         (infrastructure)
               ├── subprocess.run(argv, shell=False, timeout=…)
               ├── sterile env (allowlisted vars only, secrets excluded)
               ├── output cap + truncation
               └── read-only default + approval-gated write entries
```

Ports/adapters follow the existing hexagonal shape:

- `ports/cli_runner_port.py` — `CliRunnerPort` (protocol: `run(spec) -> CliRunResult`).
- `domain/cli_contracts.py` — Pydantic models for specs and results (pure domain,
  stdlib + Pydantic).
- `infrastructure/safe_cli_runner.py` — subprocess implementation.
- `infrastructure/cli_dispatch_config.py` — allowlist loader (JSON/YAML mirroring the
  catalog pattern, versioned).

## 2. Allowlist model (config, not code)

```python
# domain/cli_contracts.py
class CliEntry(BaseModel):
    """One allowlisted executable. argv beyond the declared schema is rejected."""
    model_config = ConfigDict(frozen=True, strict=True)
    name: str                      # stable key, e.g. "audit"
    argv_template: tuple[str, ...] # e.g. ("${UV}", "run", "book-graph-rag", "audit", "--target", "bookgraph-neo4j")
    args_schema: dict[str, Any]    # JSON schema for *allowed* extra args (Pydantic-compatible)
    timeout_s: float = Field(default=2.0, gt=0.0)   # 2s default per proposal
    read_only: bool = True
    env_allowlist: tuple[str, ...] = ()
    max_output_bytes: int = Field(default=64 * 1024, gt=0)
    cwd: str | None = None         # repo root only; never arbitrary paths

class CliRunRequest(BaseModel):
    """Agent-facing request: name + validated args, nothing else."""
    model_config = ConfigDict(frozen=True, strict=True)
    name: str
    args: dict[str, Any] = Field(default_factory=dict)   # validated against args_schema

class CliRunResult(BaseModel):
    """Result envelope; output capped, never streams secrets."""
    model_config = ConfigDict(frozen=True, strict=True)
    name: str
    exit_code: int
    stdout: str = Field(max_length=64 * 1024)
    stderr: str = Field(max_length=64 * 1024)
    timed_out: bool
    duration_ms: float
```

Allowlist ships in `config/cli_dispatch.json` (versioned, same review path as
`catalog.yaml`). Default entries (all `read_only: true`):

| name | command | timeout |
|---|---|---|
| `audit` | `book-graph-rag audit --target bookgraph-neo4j` (+ optional `--scope`) | 300s |
| `gate` | `book-graph-rag gate expose-mcp --target bookgraph-neo4j --output <tmp>` | 600s |
| `evaluate` | `book-graph-rag evaluate retrieval|resolution --target bookgraph-neo4j` | 600s |
| `route` | `scripts/route_question.py <question>` | 30s |
| `profiles-dry-run` | `scripts/build_namespace_profiles.py --dry-run` | 120s |

No entry ever accepts `shell=True`, pipes, env overrides of secrets, or arbitrary
`cwd`. Write-capable entries (e.g. future `index` pre-flight) REQUIRE the
`--approval <file>` gate (AGENTS.md §7.1) and are out of scope for this slice.

## 3. SafeCLIRunner guarantees

- **No shell**: argv is always a sequence; nothing is joined or passed to a shell.
- **Sterile env**: `env={PATH, HOME, UV: <resolved uv path>}` plus entries in
  `env_allowlist` ONLY; `NEO4J_*`, `OPENAI_*`, `MCP_ACCESS_TOKEN` and all secrets are
  explicitly excluded from the child env. Read-only entries MUST NOT receive graph
  credentials, so a rogue allowlisted command cannot reach Neo4j.
- **Timeout**: hard `subprocess.run(..., timeout=entry.timeout_s)`; on expiry the
  process group is killed and `timed_out=True` (no partial output is trusted).
- **Output cap**: stdout/stderr truncated at `max_output_bytes`; oversized output is
  truncated with a marker (mirror of the runtime tools' truncation policy).
- **Fail-closed**: unknown `name`, args not matching `args_schema`, or env/cwd
  deviations MUST raise `CliDispatchError` (typed, no partial execution).
- **Repo binding**: `cwd` defaults to the configured repo root; the runner MUST refuse
  any path outside it (same confinement semantics as the workspace tool).

## 4. MCP tool registration (McpServerAdapter)

```python
@server.tool()
async def run_approved_cli(name: str, args: dict[str, Any] | None = None) -> str:
    """Run one allowlisted project CLI (read-only). See config/cli_dispatch.json."""
    result = await self._cli_runner.run(CliRunRequest(name=name, args=args or {}))
    return cli_result_to_text(result)  # compact, capped, ansistripped
```

The tool is registered through the same `registerTool` path as the 8 existing tools;
its schema is generated from `CliRunRequest` via Pydantic (one schema, not N).

## 5. Integration and reuse

- Reuses `ToolRiskTier`/`ResourcePolicy` semantics: `run_approved_cli` is tier
  MEDIUM and inherits rate/budget limits (30 calls/min, timeout 60s per call budget —
  the per-entry timeout may be larger, the call budget is server-side).
- Reuses `require_scope` where the dispatched CLI needs a scope (e.g. `audit --scope`);
  the runner remains scope-unaware; the MCP layer enforces scope as today.
- Verification story: unit tests with a **stub** runner (no real subprocess) +
  integration test that `run_approved_cli` refuses `name="ls"` and
  `args={"--shell": "…"}`; the real runner is exercised only in a read-only smoke on
  the Orange Pi during the release gate.