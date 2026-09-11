"""Tests for gates.yaml readiness gate extension (Slice C, T-C.4)."""

from __future__ import annotations

from pathlib import Path

from book_graph_rag.domain.gate_models import GatePolicy
from book_graph_rag.infrastructure.gate_policy_loader import GatePolicyLoader


def _phase4_gates_yaml_bytes() -> bytes:
    return Path("gates.yaml").read_bytes()


def test_existing_expose_mcp_audit_unchanged() -> None:
    """The existing expose-mcp audit gate definition is byte-untouched."""
    loader = GatePolicyLoader(Path("gates.yaml"))
    policy = loader.load()
    gate = next(g for g in policy.gates if g.name == "expose-mcp")
    assert gate.version == "1.0.0"
    assert gate.required_dimensions == {
        "hierarchy": "pass",
        "endpoints": "pass",
        "uniqueness": "pass",
        "coverage": "pass",
    }
    assert gate.max_severity == "blocking"


def test_expose_mcp_readiness_definition() -> None:
    """The readiness gate expose-mcp-readiness is loaded from gates.yaml."""
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    readiness = next(g for g in policy.readiness_gates if g.name == "expose-mcp-readiness")
    assert readiness.version == "1.0.0"
    assert readiness.audit_gate_ref == "expose-mcp"
    required = {r.layer for r in readiness.required_layers}
    optional = {r.layer for r in readiness.optional_layers}
    assert required == {"structure", "resolution", "generation"}
    assert optional == {"extraction", "retrieval"}
    assert not required & optional


def test_audit_gate_ref_resolves() -> None:
    """The readiness gate's audit_gate_ref names an existing audit gate."""
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    readiness = next(g for g in policy.readiness_gates)
    audit_names = {g.name for g in policy.gates}
    assert readiness.audit_gate_ref in audit_names


def test_required_layers_no_overlap() -> None:
    """Required and optional layer sets do not overlap."""
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    for readiness in policy.readiness_gates:
        required = {r.layer for r in readiness.required_layers}
        optional = {r.layer for r in readiness.optional_layers}
        assert not required & optional


def test_phase4_gate_tests_unmodified() -> None:
    """GatePolicy still validates audit gates exactly as in Phase 4."""
    policy = GatePolicyLoader(Path("gates.yaml")).load()
    assert isinstance(policy, GatePolicy)
    assert any(g.name == "expose-mcp" for g in policy.gates)
