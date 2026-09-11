"""Tests for the backward-compatible GatePolicy.readiness_gates extension."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.evaluation_models import ReadinessGatePolicy, RequiredLayer
from book_graph_rag.domain.gate_models import GatePolicy
from book_graph_rag.infrastructure.gate_policy_loader import GatePolicyLoader


def test_default_empty_readiness_gates_preserves_phase4_load() -> None:
    """A legacy gates.yaml with only 'gates:' loads with empty readiness_gates."""
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    assert policy.readiness_gates == []


def test_gate_policy_unique_readiness_names() -> None:
    """Duplicate readiness gate names are rejected."""
    with pytest.raises(ValidationError):
        GatePolicy(
            version="1.0.0",
            gates=[],
            readiness_gates=[
                ReadinessGatePolicy(
                    name="same", version="1.0.0", required_layers=[]
                ),
                ReadinessGatePolicy(
                    name="same", version="1.0.1", required_layers=[]
                ),
            ],
        )


def test_readiness_gates_round_trip_via_loader(tmp_path: Path) -> None:
    """A gates.yaml with readiness_gates loads and round-trips."""
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
readiness_gates:
  - name: expose-mcp-readiness
    version: 1.0.0
    required_layers:
      - layer: resolution
        blocking: true
    audit_gate_ref: expose-mcp
""",
        encoding="utf-8",
    )
    policy = GatePolicyLoader(path).load()
    assert len(policy.readiness_gates) == 1
    readiness = policy.readiness_gates[0]
    assert readiness.name == "expose-mcp-readiness"
    assert readiness.required_layers == [
        RequiredLayer(layer="resolution", blocking=True),
    ]
    assert readiness.audit_gate_ref == "expose-mcp"
