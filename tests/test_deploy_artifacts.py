"""Tests for deployment artifacts (T-07.9) and T-H.2 deploy-contract tests."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from book_graph_rag.config import Settings


@pytest.fixture
def deploy_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "deploy"


@pytest.fixture
def env_example() -> Path:
    return Path(__file__).resolve().parents[1] / ".env.example"


# ── Live-deployment guard (T-H.2) ────────────────────────────────────────────
# The deployment surface (deploy/, tests/, scripts/) must never perform a live
# deployment from a test. Lines that legitimately document a live-deployment
# step (for example the install/rollback instructions in deploy/README.md) must
# carry the explicit line-level pragma ``no-live-deployment-allow``; executable
# surfaces (tests/ and scripts/) are expected to carry no such references.

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]

_LIVE_DEPLOY_SCAN_ROOTS: Final = (
    _PROJECT_ROOT / "deploy",
    _PROJECT_ROOT / "tests",
    _PROJECT_ROOT / "scripts",
)

_LIVE_DEPLOY_ALLOW: Final = "no-live-deployment-allow"

_PRODUCTION_HOST: Final = "100.106.85.109"  # no-external-endpoints-allow

# Live-deployment action tokens. Bare ``docker`` is allowed so testcontainers
# imports do not trip the guard; only exec/compose against a live host is banned.
_LIVE_DEPLOYMENT_PATTERNS: Final = (
    (re.compile(r"\bssh\b"), "ssh"),  # no-live-deployment-allow
    (re.compile(r"\bsystemctl\b"), "systemctl"),  # no-live-deployment-allow
    (re.compile(r"docker\s+exec"), "docker exec"),  # no-live-deployment-allow
    (re.compile(r"docker\s+compose"), "docker compose"),  # no-live-deployment-allow
    (re.compile(r"docker-compose"), "docker-compose"),  # no-live-deployment-allow
    (re.compile(r"\bscp\b"), "scp"),  # no-live-deployment-allow
    (re.compile(r"\brsync\b"), "rsync"),  # no-live-deployment-allow
)

_CURL_RE: Final = re.compile(r"\bcurl\b")


def _iter_scan_text_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        if path.suffix in {".pyc", ".pyo", ".so", ".dll", ".dylib"}:
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _service_directive_value(content: str, directive: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{directive}="):
            return stripped.split("=", 1)[1]
    raise AssertionError(f"{directive}= not found in service unit")


def _service_bind_host(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("Environment=MCP_BIND_HOST="):
            return stripped.split("=", 2)[2]
    raise AssertionError("Environment=MCP_BIND_HOST= not found in service unit")


def _line_live_deployment_violation(line: str) -> str | None:
    """Return the first live-deployment violation label for ``line``, else None."""
    if _LIVE_DEPLOY_ALLOW in line:
        return None
    for pattern, label in _LIVE_DEPLOYMENT_PATTERNS:
        if pattern.search(line):
            return label
    if _CURL_RE.search(line) and _PRODUCTION_HOST in line:
        return "curl to production"
    return None


def _find_live_deployment_matches() -> list[str]:
    matches: list[str] = []
    for root in _LIVE_DEPLOY_SCAN_ROOTS:
        for path in _iter_scan_text_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                label = _line_live_deployment_violation(line)
                if label is not None:
                    matches.append(
                        f"{path.relative_to(_PROJECT_ROOT)}:{lineno}: {label}"
                    )
    return matches


def test_systemd_service_file_exists(deploy_dir: Path) -> None:
    """deploy/mcp-server.service is created with the unit file."""
    service_path = deploy_dir / "mcp-server.service"

    assert service_path.exists()


def test_systemd_service_file_contains_required_directives(deploy_dir: Path) -> None:
    """The service file contains the expected systemd directives and paths."""
    service_path = deploy_dir / "mcp-server.service"
    content = service_path.read_text(encoding="utf-8")
    repo_path = "/home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi"

    assert "[Unit]" in content
    assert "Description=Book Graph RAG MCP Server" in content
    assert "After=network.target docker.service" in content
    assert "[Service]" in content
    assert "ExecStart=/home/gonzalo/.local/bin/uv run book-graph-rag-mcp serve" in content
    assert f"EnvironmentFile={repo_path}/.env" in content
    assert f"WorkingDirectory={repo_path}" in content
    assert "Restart=on-failure" in content
    assert "[Install]" in content
    assert "WantedBy=multi-user.target" in content


def test_deploy_readme_exists(deploy_dir: Path) -> None:
    """deploy/README.md is created with deployment instructions."""
    readme_path = deploy_dir / "README.md"

    assert readme_path.exists()


def test_deploy_readme_contains_key_steps(deploy_dir: Path) -> None:
    """README.md documents copy, reload, enable, start and status steps."""
    readme_path = deploy_dir / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    assert "mcp-server.service" in content
    assert "systemctl daemon-reload" in content  # no-live-deployment-allow
    assert "systemctl enable mcp-server" in content  # no-live-deployment-allow
    assert "systemctl start mcp-server" in content  # no-live-deployment-allow
    assert "systemctl status mcp-server" in content  # no-live-deployment-allow
    assert "journalctl -u mcp-server -f" in content


def test_systemd_service_binds_private_interface_only(deploy_dir: Path) -> None:
    """The service unit binds MCP to a private/Tailscale interface (R7)."""
    service_path = deploy_dir / "mcp-server.service"
    content = service_path.read_text(encoding="utf-8")

    assert "Environment=MCP_BIND_HOST=100.106.85.109" in content  # no-external-endpoints-allow
    assert "MCP_BIND_HOST=0.0.0.0" not in content


def test_deploy_readme_documents_private_bind(deploy_dir: Path) -> None:
    """README documents the private/Tailscale MCP_BIND_HOST requirement."""
    readme_path = deploy_dir / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    assert "MCP_BIND_HOST" in content


def test_env_example_includes_mcp_settings(env_example: Path) -> None:
    """.env.example documents the MCP settings added in Fase 07."""
    content = env_example.read_text(encoding="utf-8")

    assert "MCP_BIND_HOST=127.0.0.1" in content
    assert "MCP_PORT=8003" in content
    assert "MCP_LOG_PATH=logs/mcp_queries.jsonl" in content
    assert "MCP_LOG_RETENTION_DAYS=7" in content


def test_service_execstart_matches_packaged_cli_entrypoint(deploy_dir: Path) -> None:
    """ExecStart uses the exact console script name packaged by pyproject.toml."""
    pyproject = tomllib.loads(
        (deploy_dir.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    scripts = pyproject["project"]["scripts"]

    assert scripts["book-graph-rag-mcp"] == "book_graph_rag.mcp_server_main:main"

    content = (deploy_dir / "mcp-server.service").read_text(encoding="utf-8")
    assert "ExecStart=/home/gonzalo/.local/bin/uv run book-graph-rag-mcp serve" in content

    # The packaged entrypoint actually exposes the ``serve`` subcommand.
    from book_graph_rag.mcp_server_main import mcp_cli

    assert "serve" in mcp_cli.commands


def test_service_environment_file_matches_working_directory(deploy_dir: Path) -> None:
    """EnvironmentFile points at <WorkingDirectory>/.env (the repo root .env)."""
    content = (deploy_dir / "mcp-server.service").read_text(encoding="utf-8")
    working_dir = _service_directive_value(content, "WorkingDirectory")
    env_file = _service_directive_value(content, "EnvironmentFile")

    assert env_file == f"{working_dir}/.env"


def test_service_bind_host_is_tailscale_private_address(deploy_dir: Path) -> None:
    """The MCP_BIND_HOST is a valid Tailscale CGNAT address, never a wildcard."""
    content = (deploy_dir / "mcp-server.service").read_text(encoding="utf-8")
    host = _service_bind_host(content)

    assert host not in {"0.0.0.0", "::", ""}
    address = ipaddress.ip_address(host)
    assert address in ipaddress.ip_network("100.64.0.0/10")


def test_settings_accept_service_advertised_production_bind(
    deploy_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A production Settings using the service's advertised bind host validates (R7)."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    content = (deploy_dir / "mcp-server.service").read_text(encoding="utf-8")
    advertised_host = _service_bind_host(content)

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "fake-password",  # pragma: allowlist secret
            "app_env": "production",
            "mcp_bind_host": advertised_host,
        }
    )

    assert settings.app_env == "production"
    assert settings.mcp_bind_host == advertised_host


def test_env_example_mcp_values_parse_into_settings(
    env_example: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP_* values documented in .env.example are accepted by Settings."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    mcp_values: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("MCP_") and "=" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition("=")
            mcp_values[key.lower()] = value

    assert mcp_values, ".env.example must document at least one MCP_* setting"

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "fake-password",  # pragma: allowlist secret
            **mcp_values,
        }
    )

    assert settings.mcp_bind_host == "127.0.0.1"
    assert settings.mcp_port == 8003
    assert settings.mcp_log_path == Path("logs/mcp_queries.jsonl")
    assert settings.mcp_log_retention_days == 7


def test_no_live_deployment_in_deploy_tests_or_scripts() -> None:
    """Deploy docs, tests, and scripts never perform a live deployment."""
    matches = _find_live_deployment_matches()
    assert not matches, "Found live-deployment references:\n" + "\n".join(matches)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("ssh gonzalo@host", "ssh"),  # no-live-deployment-allow
        ("sudo systemctl start mcp-server", "systemctl"),  # no-live-deployment-allow
        ("docker exec bookgraph-neo4j bash", "docker exec"),  # no-live-deployment-allow
        ("docker compose up -d", "docker compose"),  # no-live-deployment-allow
        ("docker-compose up", "docker-compose"),  # no-live-deployment-allow
        ("scp deploy/mcp-server.service host:/tmp/", "scp"),  # no-live-deployment-allow
        ("rsync -a ./ host:/srv/", "rsync"),  # no-live-deployment-allow
        (f"curl {_PRODUCTION_HOST}:8003/sse", "curl to production"),  # no-live-deployment-allow
        ("After=network.target docker.service", None),
        ("journalctl -u mcp-server -f", None),
        ("sudo systemctl daemon-reload  # no-live-deployment-allow", None),
    ],
)
def test_line_live_deployment_violation(line: str, expected: str | None) -> None:
    """The guard flags each live-deployment action and allows docs/read-only lines."""
    assert _line_live_deployment_violation(line) == expected
