"""Gate policy loader tests following the CatalogLoader pattern."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.infrastructure.gate_policy_loader import (
    GatePolicyLoader,
    GatePolicyLoadError,
)


def _valid_policy_text() -> str:
    return """
version: 1.0.0
gates:
  - name: expose-mcp
    version: 1.0.0
    required_dimensions:
      hierarchy: pass
      endpoints: pass
      uniqueness: pass
      coverage: pass
    max_severity: blocking
"""


def test_loader_reads_valid_policy(tmp_path: Path) -> None:
    """A well-formed gates.yaml loads into a validated GatePolicy."""
    path = tmp_path / "gates.yaml"
    path.write_text(_valid_policy_text(), encoding="utf-8")

    policy = GatePolicyLoader(path).load()

    assert policy.version == "1.0.0"
    assert len(policy.gates) == 1
    gate = policy.gates[0]
    assert gate.name == "expose-mcp"
    assert gate.required_dimensions == {
        "hierarchy": "pass",
        "endpoints": "pass",
        "uniqueness": "pass",
        "coverage": "pass",
    }


def test_loader_fails_when_file_missing(tmp_path: Path) -> None:
    """A missing policy file raises GatePolicyLoadError before evaluation."""
    missing = tmp_path / "missing.yaml"
    with pytest.raises(GatePolicyLoadError, match="Cannot read"):
        GatePolicyLoader(missing).load()


def test_loader_fails_on_malformed_yaml(tmp_path: Path) -> None:
    """Invalid YAML is rejected at load time."""
    path = tmp_path / "gates.yaml"
    path.write_text("gates: [unclosed", encoding="utf-8")
    with pytest.raises(GatePolicyLoadError, match="Invalid YAML"):
        GatePolicyLoader(path).load()


def test_loader_fails_when_root_is_not_mapping(tmp_path: Path) -> None:
    """A YAML scalar/list root is rejected."""
    path = tmp_path / "gates.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(GatePolicyLoadError, match="must be a YAML mapping"):
        GatePolicyLoader(path).load()


def test_loader_fails_on_duplicate_gate_name(tmp_path: Path) -> None:
    """Duplicate gate names are rejected at load time."""
    path = tmp_path / "gates.yaml"
    path.write_text(
        """
version: 1.0.0
gates:
  - name: expose-mcp
    version: 1.0.0
    required_dimensions:
      hierarchy: pass
    max_severity: blocking
  - name: expose-mcp
    version: 1.0.1
    required_dimensions:
      endpoints: pass
    max_severity: warning
""",
        encoding="utf-8",
    )
    with pytest.raises(GatePolicyLoadError, match="duplicate gate names"):
        GatePolicyLoader(path).load()


def test_loader_fails_on_unknown_dimension(tmp_path: Path) -> None:
    """An unknown dimension key in required_dimensions is rejected."""
    path = tmp_path / "gates.yaml"
    path.write_text(
        """
version: 1.0.0
gates:
  - name: bad-gate
    version: 1.0.0
    required_dimensions:
      unknown-dim: pass
    max_severity: blocking
""",
        encoding="utf-8",
    )
    with pytest.raises(GatePolicyLoadError):
        GatePolicyLoader(path).load()


def test_loader_fails_on_invalid_gate_version(tmp_path: Path) -> None:
    """Gate version must follow semver x.y.z."""
    path = tmp_path / "gates.yaml"
    path.write_text(
        """
version: 1.0.0
gates:
  - name: expose-mcp
    version: "1.0"
    required_dimensions:
      hierarchy: pass
    max_severity: blocking
""",
        encoding="utf-8",
    )
    with pytest.raises(GatePolicyLoadError):
        GatePolicyLoader(path).load()


def test_loader_fails_on_invalid_gate_name(tmp_path: Path) -> None:
    """Gate names must be kebab-case starting with a letter."""
    path = tmp_path / "gates.yaml"
    path.write_text(
        """
version: 1.0.0
gates:
  - name: 1bad-name
    version: 1.0.0
    required_dimensions:
      hierarchy: pass
    max_severity: blocking
""",
        encoding="utf-8",
    )
    with pytest.raises(GatePolicyLoadError):
        GatePolicyLoader(path).load()


def test_default_repo_gates_yaml_loads() -> None:
    """The repository-root gates.yaml is present and loadable."""
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    assert any(g.name == "expose-mcp" for g in policy.gates)
