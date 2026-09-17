"""Domain-level MCP tool tier registry tests (T-F.1).

The registry is the fail-closed classification boundary: every one of the 8 MCP
tools maps to exactly one risk tier, unknown tools are rejected with a typed
error, and each tool's policy equals the secure default for its tier.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.mcp_security import (
    McpSecurityError,
    ResourcePolicy,
    ToolRiskTier,
    UnknownToolError,
)
from book_graph_rag.domain.tool_tier_registry import (
    TOOL_TIERS,
    policy_for_tool,
    tier_for,
)

#: The 8 tools exposed by McpServerAdapter.create_server (hardcoded contract).
EXPECTED_TOOLS = frozenset(
    {
        "find_entity",
        "traverse_relationships",
        "search_chunks",
        "list_entities",
        "count_entities",
        "search_rag",
        "ask_global",
        "query_cypher",
    }
)

EXPECTED_TIER_BY_TOOL = {
    "find_entity": ToolRiskTier.LOW,
    "traverse_relationships": ToolRiskTier.LOW,
    "search_chunks": ToolRiskTier.LOW,
    "list_entities": ToolRiskTier.LOW,
    "count_entities": ToolRiskTier.LOW,
    "search_rag": ToolRiskTier.MEDIUM,
    "ask_global": ToolRiskTier.MEDIUM,
    "query_cypher": ToolRiskTier.HIGH,
}


def test_registry_covers_exactly_the_8_mcp_tools() -> None:
    """The registry classifies all 8 tools exactly once, with no extras."""
    assert len(TOOL_TIERS) == 8
    assert set(TOOL_TIERS) == EXPECTED_TOOLS


@pytest.mark.parametrize(
    ("tool_name", "expected_tier"),
    sorted(EXPECTED_TIER_BY_TOOL.items()),
)
def test_tier_for_maps_each_tool(
    tool_name: str, expected_tier: ToolRiskTier
) -> None:
    """Each tool maps to its R6 risk tier."""
    assert tier_for(tool_name) is expected_tier


@pytest.mark.parametrize(
    ("tool_name", "expected_tier"),
    sorted(EXPECTED_TIER_BY_TOOL.items()),
)
def test_policy_for_tool_equals_tier_default(
    tool_name: str, expected_tier: ToolRiskTier
) -> None:
    """Each tool's policy is the secure default for its tier."""
    policy = policy_for_tool(tool_name)
    assert policy.tier is expected_tier
    assert policy.to_canonical_json() == ResourcePolicy.default_for(
        expected_tier
    ).to_canonical_json()


def test_unknown_tool_error_is_typed_security_error() -> None:
    """UnknownToolError is a narrow McpSecurityError subtype with a stable code."""
    assert issubclass(UnknownToolError, McpSecurityError)
    assert UnknownToolError("x").error_code == "unknown_tool"


def test_unknown_tool_rejected_fail_closed() -> None:
    """Unknown tool names are rejected with an error that names the tool."""
    with pytest.raises(UnknownToolError, match="does-not-exist"):
        tier_for("does-not-exist")


def test_unknown_tool_policy_also_rejected_fail_closed() -> None:
    """policy_for_tool fails closed on unknown tools too."""
    with pytest.raises(UnknownToolError, match="does-not-exist"):
        policy_for_tool("does-not-exist")


def test_low_tier_policy_is_looser_than_high_tier() -> None:
    """A LOW tool has a strictly weaker policy than a HIGH tool."""
    low = policy_for_tool("find_entity")
    high = policy_for_tool("query_cypher")
    assert high.concurrency_limit == 1 < low.concurrency_limit
    assert high.max_traversal_depth == 2 < low.max_traversal_depth
    assert high.max_rows < low.max_rows
    assert high.max_nodes < low.max_nodes
    assert high.rate_limit_calls < low.rate_limit_calls
    assert high.max_query_retries < low.max_query_retries


def test_all_registry_policies_validate_freshly() -> None:
    """Every registry policy re-validates and satisfies the policy invariants."""
    for tool_name in sorted(EXPECTED_TOOLS):
        policy = policy_for_tool(tool_name)
        # Fresh construction re-runs the frozen/strict validators.
        ResourcePolicy.model_validate(policy.model_dump())
        assert policy.max_nodes >= policy.max_rows
        assert policy.timeout_ms > 0
        assert policy.max_rows > 0
        assert policy.max_traversal_depth >= 0
        assert policy.max_query_retries >= 0
        assert policy.concurrency_limit > 0
        assert policy.rate_limit_calls > 0
        assert policy.rate_limit_window_ms > 0
