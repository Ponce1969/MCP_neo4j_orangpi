"""Domain-level MCP tool tier registry (T-F.1).

Classifies the 8 MCP tools into risk tiers and binds each tier's
``ResourcePolicy``. This module is intentionally pure: stdlib + Pydantic only,
no ports or infrastructure imports (hexagonal domain boundary).
"""

from __future__ import annotations

from book_graph_rag.domain.mcp_security import (
    ResourcePolicy,
    ToolRiskTier,
    UnknownToolError,
)

#: Risk-tier classification for the 8 MCP tools exposed by
#: ``McpServerAdapter.create_server``. Low: deterministic structured reads.
#: Medium: LLM-mediated reads. High: dynamic query execution (disabled by default).
TOOL_TIERS: dict[str, ToolRiskTier] = {
    "find_entity": ToolRiskTier.LOW,
    "traverse_relationships": ToolRiskTier.LOW,
    "search_chunks": ToolRiskTier.LOW,
    "list_entities": ToolRiskTier.LOW,
    "count_entities": ToolRiskTier.LOW,
    "search_rag": ToolRiskTier.MEDIUM,
    "ask_global": ToolRiskTier.MEDIUM,
    "query_cypher": ToolRiskTier.HIGH,
}


def tier_for(tool_name: str) -> ToolRiskTier:
    """Return the risk tier for ``tool_name``, failing closed on unknown names."""
    try:
        return TOOL_TIERS[tool_name]
    except KeyError:
        raise UnknownToolError(f"unknown MCP tool: {tool_name!r}") from None


def policy_for_tool(tool_name: str) -> ResourcePolicy:
    """Return the secure default ``ResourcePolicy`` for a tool's tier."""
    return ResourcePolicy.default_for(tier_for(tool_name))
