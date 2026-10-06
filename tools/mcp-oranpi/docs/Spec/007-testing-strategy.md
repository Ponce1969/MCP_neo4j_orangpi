# 007 - Testing Strategy

## Overview

This document defines the testing approach for the MCP OranPi project, covering unit tests, integration tests, fixtures, mock strategies, and CI considerations.

The testing philosophy follows spec 003: **deterministic, isolated, no flaky tests**.

---

# Testing Principles

1. **No SSH in unit tests** — All SSH interaction is mocked
2. **No real OrangePi in CI** — Integration tests use recorded fixtures
3. **Deterministic** — Same test, same result, always
4. **Fast** — Unit suite runs in < 10 seconds
5. **Strict typing enforced** — `mypy --strict` must pass before tests run

---

# Test Stack

| Tool               | Purpose                          |
| ------------------ | -------------------------------- |
| pytest             | Test runner                      |
| pytest-asyncio     | Async test support               |
| pytest-mock        | Mock/patch helpers               |
| mypy --strict      | Type checking (pre-test gate)    |
| ruff               | Linting (pre-test gate)          |

---

# Test Categories

## Unit Tests

Unit tests have **zero external dependencies**. No SSH, no Docker, no network.

They test:
- Domain model construction and validation
- Output parsers (raw text → domain models)
- Configuration loading and validation
- Command runner whitelist enforcement
- Parameter validation rules
- Log truncation logic

### Run command

```bash
uv run pytest tests/unit/ -v
```

### What is mocked

- `asyncssh.connect` → returns a mock `SSHClientConnection`
- Remote command execution → returns fixture strings
- File system reads → returns fixture data
- Environment variables → set via `monkeypatch`

### What is NOT mocked

- Domain model instantiation
- Pydantic validation
- Pure computation (parsers, formatters)
- Error case construction

---

## Integration Tests

Integration tests verify that **application-layer tool implementations** correctly orchestrate domain models, parsers, and SSH commands.

They test:
- Each MCP tool end-to-end (with mock SSH)
- Tool registration and discovery
- Error propagation from SSH to tool result
- Connection lifecycle (startup, retry, shutdown)

Integration tests use **mock SSH** but **real application logic**. They do NOT need a real OrangePi.

### Run command

```bash
uv run pytest tests/integration/ -v
```

### Mock SSH fixture

```python
# conftest.py

import pytest
from unittest.mock import AsyncMock
from mcp_oranpi.infrastructure.ssh_client import SSHClient


@pytest.fixture
def mock_ssh_client():
    """Mock SSH client that returns canned responses."""
    client = AsyncMock(spec=SSHClient)
    client.connected = True
    client.execute = AsyncMock()
    return client


@pytest.fixture
def ssh_with_docker_ps(mock_ssh_client):
    """SSH client preloaded with docker ps output."""
    mock_ssh_client.execute.return_value = DOCKER_PS_FIXTURE
    return mock_ssh_client


@pytest.fixture
def mock_workspace_resolver():
    """Mock workspace resolver with test workspaces."""
    resolver = WorkspaceResolver(
        root_dir="/home/cerra/codigo",
        workspaces=[
            WorkspaceConfig(id="guardian", path="/home/cerra/codigo/guardian"),
            WorkspaceConfig(id="crm", path="/home/cerra/codigo/crm"),
        ]
    )
    return resolver


@pytest.fixture
def mock_ssh_with_cwd():
    """Mock SSH client that supports cwd parameter for workspace commands."""
    client = AsyncMock(spec=SSHClient)
    client.connected = True
    client.execute = AsyncMock()
    client.execute_with_cwd = AsyncMock()
    return client
```

---

## E2E Tests (Manual / Local Only)

End-to-end tests require a **real OrangePi connection** and are NOT run in CI.

They exist for:
- Verifying real SSH connectivity
- Validating command output parsing against real OrangePi responses
- Confirming ARM-specific commands work (vcgencmd, etc.)

E2E tests are:
- Tagged with `@pytest.mark.e2e`
- Skipped by default (`pytest -m "not e2e"`)
- Run manually with `pytest -m e2e --host=oranpi.local`

E2E tests are outside CI scope and are the developer's responsibility before releases.

---

# Fixture Strategy

## Recorded Fixtures

Fixtures are **recorded real outputs** from the OrangePi, stored as text/JSON files in `tests/fixtures/`.

### Fixture files

```
tests/fixtures/
├── docker/
│   ├── ps_output.json          # docker ps --format json
│   ├── ps_all_output.json      # docker ps -a --format json
│   ├── inspect_nginx.json       # docker inspect nginx
│   ├── logs_nginx.txt           # docker logs nginx --tail 100
│   └── stats_nginx.json         # docker stats --no-stream --format json
├── network/
│   ├── ss_tulnp.txt             # ss -tulnp output
│   └── tailscale_status.json    # tailscale status --json
├── system/
│   ├── top_bn1.txt             # top -bn1 output
│   ├── free_m.txt              # free -m output
│   ├── df_h.txt                # df -h output
│   ├── vcgencmd_temp.txt       # vcgencmd measure_temp output
│   └── systemctl_status.json   # systemctl status output
└── logs/
    ├── journalctl_docker.txt   # journalctl -u docker
    └── var_log_syslog.txt       # /var/log/syslog tail

And workspace fixtures:

```
tests/fixtures/workspace/
├── workspaces.yaml                # Test workspace registry
├── compose_ps_guardian.json       # docker compose ps output for "guardian" workspace
└── compose_services_guardian.txt  # docker compose config --services output
```
```

### How to record new fixtures

1. SSH into the OrangePi
2. Run the actual command
3. Save output to the corresponding fixture file
4. Strip any sensitive data (passwords, tokens, real IPs if needed)
5. Commit the sanitized fixture

### Fixture naming convention

```
{command_name}[_{variant}].{ext}
```

Examples:
- `ps_output.json` — standard docker ps output
- `ps_all_output.json` — docker ps -a variant
- `inspect_nginx.json` — docker inspect for specific container

---

# Mock Strategy per Module

## Domain Layer (`domain/`)

**No mocks needed.** Test pure Python: model instantiation, validation, error construction.

```python
# tests/unit/test_models.py

def test_container_summary_construction():
    container = ContainerSummary(
        id="abc123def456",
        name="nginx",
        image="nginx:latest",
        status="running",
        created="2025-01-01T00:00:00Z",
        ports=[PortBinding(host_port=80, container_port=80, protocol="tcp", host_ip="0.0.0.0")],
    )
    assert container.name == "nginx"
    assert container.ports[0].host_port == 80
```

## Parsers (`infrastructure/parsers.py`)

**Parse fixture data.** No mocks. Feed recorded fixture strings into parser functions, assert domain models come out.

```python
# tests/unit/test_parsers.py

def test_parse_docker_ps():
    raw = load_fixture("docker/ps_output.json")
    containers = parse_docker_ps(raw)
    assert len(containers) > 0
    assert all(isinstance(c, ContainerSummary) for c in containers)

def test_parse_docker_ps_empty():
    containers = parse_docker_ps("")
    assert containers == []

def test_parse_docker_ps_malformed():
    with pytest.raises(ParseError):
        parse_docker_ps("not json at all")
```

## Command Runner (`infrastructure/command_runner.py`)

**Test whitelist enforcement and parameter validation.** No SSH needed.

```python
# tests/unit/test_command_runner.py

def test_allowed_command_executes():
    runner = CommandRunner(ssh_client=mock_ssh)
    result = asyncio.run(runner.run("docker_ps"))
    assert result is not None

def test_forbidden_command_rejected():
    runner = CommandRunner(ssh_client=mock_ssh)
    with pytest.raises(ForbiddenCommandError):
        asyncio.run(runner.run("rm -rf /"))

def test_arbitrary_command_rejected():
    runner = CommandRunner(ssh_client=mock_ssh)
    with pytest.raises(ForbiddenCommandError):
        asyncio.run(runner.run("; cat /etc/passwd"))

def test_parameter_validation_container_name():
    with pytest.raises(InvalidParameterError):
        validate_container_name("../../etc/passwd")

def test_parameter_validation_port():
    assert validate_port(8080) == 8080
    with pytest.raises(InvalidParameterError):
        validate_port(99999)
```

## SSH Client (`infrastructure/ssh_client.py`)

**Mock asyncssh.** Test reconnection logic, timeout handling, connection lifecycle.

```python
# tests/unit/test_ssh_client.py (uses pytest-mock)

async def test_connect_success(mock_asyncssh_connect):
    client = SSHClient(config=ssh_config)
    await client.connect()
    assert client.connected is True

async def test_connect_failure_triggers_retry(mock_asyncssh_connect):
    mock_asyncssh_connect.side_effect = [OSError("connection refused"), None]  # succeed on 2nd try
    client = SSHClient(config=ssh_config)
    await client.connect()  # Should succeed after retry

async def test_command_timeout(mock_ssh_client):
    mock_ssh_client.execute = AsyncMock(side_effect=asyncio.TimeoutError)
    with pytest.raises(CommandTimeoutError):
        await mock_ssh_client.execute("docker ps", timeout=1)
```

## Application Tools (`application/`)

**Mock SSH, test the full tool logic.** This is the most important test category.

```python
# tests/integration/test_docker_tools.py

async def test_list_containers():
    mock_ssh = create_mock_ssh_with_fixture("docker/ps_output.json")
    tool = DockerTools(ssh_client=mock_ssh)
    result = await tool.list_containers(all=False)
    assert result.data is not None
    assert result.error is None
    assert len(result.data["containers"]) > 0

async def test_list_containers_connection_failure():
    mock_ssh = create_mock_ssh_failing("CONN_FAILED")
    tool = DockerTools(ssh_client=mock_ssh)
    result = await tool.list_containers()
    assert result.error is not None
    assert result.error.code == "CONN_FAILED"

async def test_container_logs_truncation():
    mock_ssh = create_mock_ssh_with_fixture("docker/logs_large.txt", size_kb=60)
    tool = DockerTools(ssh_client=mock_ssh)
    result = await tool.container_logs(container="nginx", tail=500)
    assert result.data["truncated"] is True
```

## MCP Handlers (`presentation/mcp_handlers.py`)

**Test that MCP tool registration maps to the correct application method and that inputs/outputs are correctly wired.**

```python
# tests/integration/test_mcp_handlers.py

async def test_tool_registration():
    server = create_test_server()
    tools = server.list_tools()
    tool_names = [t.name for t in tools]
    assert "docker_list_containers" in tool_names
    assert "network_scan_ports" in tool_names
    assert "system_cpu_usage" in tool_names

async def test_tool_input_schema():
    server = create_test_server()
    tool = server.get_tool("docker_container_logs")
    assert "container" in tool.inputSchema["properties"]
    assert tool.inputSchema["properties"]["container"]["type"] == "string"
```

## Workspace Resolver (`infrastructure/workspace_resolver.py`)

**Test path validation and workspace resolution logic.** No SSH needed.

```python
# tests/unit/test_workspace_resolver.py

def test_resolve_valid_workspace():
    resolver = WorkspaceResolver(root_dir="/home/cerra/codigo", workspaces=VALID_WORKSPACES)
    path = resolver.resolve("guardian")
    assert path == "/home/cerra/codigo/guardian"

def test_resolve_unknown_workspace():
    resolver = WorkspaceResolver(root_dir="/home/cerra/codigo", workspaces=VALID_WORKSPACES)
    with pytest.raises(WorkspaceNotFoundError):
        resolver.resolve("nonexistent")

def test_reject_path_escape():
    resolver = WorkspaceResolver(root_dir="/home/cerra/codigo", workspaces=ESCAPE_WORKSPACES)
    # Workspace path resolves outside root directory
    with pytest.raises(PathEscapeError):
        resolver.resolve("escape_attempt")

def test_reject_symlink_escape():
    # Workspace path contains symlink that resolves outside root
    resolver = WorkspaceResolver(root_dir="/home/cerra/codigo", workspaces=SYMLINK_WORKSPACES)
    with pytest.raises(PathEscapeError):
        resolver.resolve("symlink_escape")

def test_workspace_id_validation():
    assert validate_workspace_id("guardian") == "guardian"
    assert validate_workspace_id("my-project-2") == "my-project-2"
    with pytest.raises(ValidationError):
        validate_workspace_id("../../etc")
    with pytest.raises(ValidationError):
        validate_workspace_id("has spaces")
```

## Workspace Tools (`application/workspace_tools.py`)

**Mock SSH, test workspace-scoped tool logic.**

```python
# tests/integration/test_workspace_tools.py

async def test_workspace_list():
    resolver = create_test_resolver()
    tool = WorkspaceTools(ssh_client=mock_ssh, resolver=resolver)
    result = await tool.list_workspaces()
    assert result.error is None
    assert len(result.data["workspaces"]) > 0

async def test_workspace_docker_ps():
    resolver = create_test_resolver()
    mock_ssh = create_mock_ssh_with_fixture("docker/compose_ps.json")
    tool = WorkspaceTools(ssh_client=mock_ssh, resolver=resolver)
    result = await tool.docker_ps(workspace="guardian")
    assert result.error is None
    # Verify command was executed from workspace directory

async def test_workspace_not_found():
    resolver = create_test_resolver()
    tool = WorkspaceTools(ssh_client=mock_ssh, resolver=resolver)
    result = await tool.docker_ps(workspace="nonexistent")
    assert result.error is not None
    assert result.error.code == "WS_NOT_FOUND"

async def test_workspace_disabled():
    resolver = create_test_resolver_with_disabled()
    tool = WorkspaceTools(ssh_client=mock_ssh, resolver=resolver)
    result = await tool.docker_ps(workspace="deleted_project")
    assert result.error is not None
    assert result.error.code == "WS_DISABLED"

async def test_workspace_command_uses_cwd():
    # Verify that workspace tools set cwd to workspace path
    resolver = create_test_resolver()
    mock_ssh = create_mock_ssh(execute_with_cwd=AsyncMock())
    tool = WorkspaceTools(ssh_client=mock_ssh, resolver=resolver)
    await tool.docker_ps(workspace="guardian")
    mock_ssh.execute_with_cwd.assert_called_with(
        "docker compose ps --format json",
        cwd="/home/cerra/codigo/guardian"
    )
```

---

```python
# tests/unit/test_config.py

def test_config_from_env(monkeypatch):
    monkeypatch.setenv("ORANPI_SSH_HOST", "oranpi.local")
    monkeypatch.setenv("ORANPI_SSH_USER", "cerra")
    monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/home/cerra/.ssh/id_rsa")
    monkeypatch.setenv("ORANPI_ROOT_WORKSPACE_DIR", "/home/cerra/codigo")
    config = AppConfig.from_env()
    assert config.ssh.host == "oranpi.local"
    assert config.ssh.port == 22  # default

def test_config_missing_required(monkeypatch):
    monkeypatch.delenv("ORANPI_SSH_HOST", raising=False)
    with pytest.raises(ConfigError):
        AppConfig.from_env()

def test_config_missing_workspace_root(monkeypatch):
    monkeypatch.setenv("ORANPI_SSH_HOST", "oranpi.local")
    monkeypatch.setenv("ORANPI_SSH_USER", "cerra")
    monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/home/cerra/.ssh/id_rsa")
    monkeypatch.delenv("ORANPI_ROOT_WORKSPACE_DIR", raising=False)
    with pytest.raises(ConfigError):
        AppConfig.from_env()

def test_workspace_config_loading():
    config = load_workspace_config("tests/fixtures/workspaces.yaml")
    assert len(config.workspaces) > 0
    assert config.workspaces[0].id == "guardian"

def test_workspace_path_validation():
    config = load_workspace_config("tests/fixtures/workspaces.yaml")
    for ws in config.workspaces:
        assert ws.path.startswith(config.root_workspace_dir)

def test_config_invalid_port(monkeypatch):
    monkeypatch.setenv("ORANPI_SSH_PORT", "not_a_number")
    monkeypatch.setenv("ORANPI_ROOT_WORKSPACE_DIR", "/home/cerra/codigo")
    with pytest.raises(ConfigError):
        AppConfig.from_env()
```

---

# Security Tests

Security tests verify the constraints from spec 004.

```python
# tests/unit/test_security.py

def test_no_arbitrary_command_execution():
    """Verify that arbitrary shell input is REJECTED."""
    runner = CommandRunner(ssh_client=mock_ssh)
    forbidden_inputs = [
        "rm -rf /",
        "docker rm -f $(docker ps -q)",
        "shutdown now",
        "bash -c 'evil'",
        "; cat /etc/passwd",
        "$(cat /etc/shadow)",
    ]
    for cmd in forbidden_inputs:
        with pytest.raises(ForbiddenCommandError):
            runner.validate_command(cmd)

def test_path_traversal_blocked():
    """Verify that path traversal in logs_file is BLOCKED."""
    with pytest.raises(PathNotAllowedError):
        validate_log_path("../../etc/shadow")
    with pytest.raises(PathNotAllowedError):
        validate_log_path("/var/log/../../../etc/passwd")

def test_env_values_never_exposed():
    """Verify docker_inspect_container strips env VALUES."""
    result = parse_docker_inspect(fixture_with_env_secrets)
    for env_name in result.env:
        assert "=" not in env_name  # Just names, no values

def test_dangerous_commands_not_in_whitelist():
    """Verify forbidden commands are NOT in ALLOWED_COMMANDS."""
    dangerous = ["rm", "kill", "shutdown", "reboot", "docker rm", "docker kill"]
    for cmd in dangerous:
        assert cmd not in ALLOWED_COMMANDS
        assert not any(cmd in allowed for allowed in ALLOWED_COMMANDS.values())

def test_workspace_path_escape_blocked():
    """Verify that workspace paths escaping root directory are BLOCKED."""
    with pytest.raises(PathEscapeError):
        validate_workspace_path(
            path="/home/cerra/codigo/../../etc/passwd",
            root="/home/cerra/codigo"
        )

def test_workspace_symlink_escape_blocked():
    """Verify that symlink escapes outside root are BLOCKED."""
    with pytest.raises(PathEscapeError):
        validate_workspace_path(
            path="/home/cerra/codigo/project/logs",  # symlink -> /etc
            root="/home/cerra/codigo",
            resolved_path="/etc"  # after symlink resolution
        )

def test_workspace_id_rejects_traversal():
    """Verify workspace IDs with path traversal are BLOCKED."""
    with pytest.raises(ValidationError):
        validate_workspace_id("../../etc")

def test_cwd_never_from_agent():
    """Verify that workspace tools NEVER accept raw paths from agent input."""
    # The workspace_resolver.resolve() method is the ONLY way to get a cwd.
    # It validates workspace_id against registry, never accepts raw paths.
```
```

---

# CI Pipeline

## Minimum CI Gate

```bash
# 1. Type check
uv run mypy --strict src/

# 2. Lint
uv run ruff check src/ tests/

# 3. Unit tests
uv run pytest tests/unit/ -v --tb=short

# 4. Integration tests
uv run pytest tests/integration/ -v --tb=short

# 5. Security scan
uv run python scripts/vulnerability_scanner.py --severity high
```

All steps must pass (exit 0) before merge.

E2E tests are explicitly excluded from CI (they need a real OrangePi).

---

# Test Organization Rules

1. **One test file per source module**: `test_parsers.py` tests `parsers.py`
2. **Fixtures in `tests/fixtures/`**: No inline fixture data in test files
3. **No network in unit tests**: If a test needs network, it's an integration test
4. **No flaky tests**: Tests must pass 100/100 times
5. **Descriptive test names**: `test_parse_docker_ps_returns_containers` not `test_parser_1`
6. **Test error paths**: Every `ToolError` code must have at least one test that triggers it
7. **Test boundary values**: `tail=0`, `tail=500`, `tail=501` (should fail for 501)

---

# Test Markers

```python
import pytest

pytestmark = pytest.mark.anyio  # All tests are async by default

# For E2E tests that require a real OrangePi
@pytest.mark.e2e
async def test_real_docker_ps():
    ...

# For tests that need specific fixtures
@pytest.mark.fixture("docker/ps_output.json")
async def test_with_real_output():
    ...

# For slow tests (port scans, etc.)
@pytest.mark.slow
async def test_port_scan_full_range():
    ...
```

Standard test run excludes E2E and slow:
```bash
uv run pytest -m "not e2e and not slow"
```

CI runs: `not e2e` only (slow tests are fine in CI).

---

# Coverage Targets

| Layer              | Target Coverage |
| ------------------ | --------------- |
| `domain/`          | 95%+            |
| `infrastructure/`  | 80%+            |
| `application/`     | 90%+            |
| `presentation/`    | 70%+            |
| Overall            | 85%+            |

Why domain is 95%+: Pure Python, no mocks, no excuses. Every model, validator, and error case must be tested.

Why presentation is 70%+: MCP handler wiring is thin glue. The real logic is in application and infrastructure layers.

---

# Risks and Tradeoffs

## Risk: Fixture drift from real OrangePi output

Recorded fixtures may become stale as Docker versions change or the OrangePi config evolves.

**Mitigation**:
- Run E2E tests manually before releases
- Add fixture versioning (record date + Docker version)
- When a parser breaks on new output, update the fixture AND add a test for the old format

## Risk: Mock SSH overshadows real behavior

If mock SSH responses don't match real OrangePi behavior, unit tests pass but integration fails silently.

**Mitigation**:
- E2E tests validate against real hardware
- Fixtures are recorded from real output
- Parser tests use real output data
- Periodic manual E2E validation

## Tradeoff: No Docker/containerized test environment

Creating a Docker-in-Docker test environment would let CI run "real" Docker tests, but adds complexity.

**Accepted tradeoff**: Mock-based unit + integration tests for CI. E2E tests for real hardware validation. This keeps CI fast and simple.

## Tradeoff: 85% coverage target

100% coverage is not required because:
- Some error paths are hard to trigger (SSH connection drops mid-command)
- Presentation layer glue code has diminishing returns
- Chasing coverage numbers leads to useless tests

**Accepted tradeoff**: 85% overall, with higher targets for domain and application layers where the critical logic lives.

## Risk: asyncssh mocking complexity

asyncssh has a complex async API. Mocking it correctly requires understanding its connection lifecycle.

**Mitigation**: Create a single, well-tested `MockSSHClient` fixture in `conftest.py` that all integration tests share. Test the mock itself with a "mock the mock" meta-test to ensure it behaves like the real thing.