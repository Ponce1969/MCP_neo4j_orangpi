"""Docs consistency guard: roadmap and spec status must reflect the implemented state."""

from __future__ import annotations

from pathlib import Path

import yaml


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_roadmap_phase3_marked_done() -> None:
    roadmap = _read("docs/spec/roadmap.md")
    assert "Phase 3 status (2026-09-07): **implemented**" in roadmap


def test_spec03_status_is_implemented() -> None:
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert spec.startswith("# 03 — Semantic Entity Resolution")
    assert "Status: Implemented (Phase 3" in spec


def test_spec03_high_band_human_confirm_documented() -> None:
    """The policy clarification (human-confirm for high band) must be documented."""
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert "human-confirm queue" in spec
    assert "Policy clarification (maintainer, 2026-09-06)" in spec


def test_spec03_escalation_note_present() -> None:
    """The F1-beats-baseline escalation must be honestly documented, not hidden."""
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert "Escalation note" in spec
    assert "0.695" in spec  # best hybrid retrieval F1 recorded


def test_spec03_open_decisions_resolved() -> None:
    """All [OPEN] decision markers must be struck through (resolved), not open."""
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    import re

    unresolved = [
        line
        for line in spec.splitlines()
        if "[OPEN]" in line and not line.strip().startswith("- ~")
    ]
    assert unresolved == [], f"Unresolved [OPEN] decisions: {unresolved}"
    assert re.search(r"RESOLVED|resolved in|~~", spec)


def test_spec03_no_target_stage_markers() -> None:
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert "`[TARGET]`" not in spec


# ── Agent-ops docs + skill (runbook / AGENTS.md §7.5 / usage skill) ──────────

MCP_TOOLS = (
    "find_entity",
    "traverse_relationships",
    "search_chunks",
    "list_entities",
    "count_entities",
    "search_rag",
    "query_cypher",
    "ask_global",
)


def test_agents_and_runbook_name_unit_and_restart_command() -> None:
    """Both normative docs must name the real unit and the exact restart command."""
    restart = "sudo systemctl restart mcp-server"  # no-live-deployment-allow
    for path in ("AGENTS.md", "docs/ops/mcp-service.md"):
        text = _read(path)
        assert "mcp-server.service" in text, f"{path} lost the unit name"
        assert restart in text, f"{path} lost the restart command"


def test_runbook_names_smoke_script_and_manual_instance_warning() -> None:
    """The runbook must point at the smoke script and warn about the restart loop."""
    runbook = _read("docs/ops/mcp-service.md")
    assert "scripts-ops/mcp_smoke_book4.py" in runbook
    assert "Warning" in runbook
    assert "manual instance" in runbook
    assert "restart loop" in runbook


def test_usage_skill_covers_all_tools_and_scope_form() -> None:
    """The usage skill must mention every MCP tool and the exact scope form."""
    skill = _read(".agents/skills/book-graph-mcp-usage/SKILL.md")
    for tool in MCP_TOOLS:
        assert f"`{tool}`" in skill, f"skill does not mention tool {tool}"
    assert "corpus:source" in skill


def test_usage_skill_covers_every_active_catalog_source() -> None:
    """Every source marked active in catalog.yaml must be documented in the skill."""
    catalog = yaml.safe_load(_read("catalog.yaml"))
    active_sources: list[str] = [
        source_id
        for corpus in catalog["corpora"].values()
        for source_id, source in corpus["sources"].items()
        if source.get("status") == "active"
    ]
    assert active_sources, "catalog.yaml lists no active source"
    skill = _read(".agents/skills/book-graph-mcp-usage/SKILL.md")
    for source_id in active_sources:
        assert source_id in skill, f"skill does not mention active source {source_id}"


def test_runbook_documents_external_client_access() -> None:
    """The client-access guidance (ACL, no wildcard bind, auth reading) must stay documented."""
    runbook = _read("docs/ops/mcp-service.md")
    assert "## 7. Client access (external MCP clients over Tailscale)" in runbook
    assert "Do NOT bind 0.0.0.0" in runbook
    assert '"dst": ["gonpatri:8003"]' in runbook
    assert "tailscale serve --bg --https=443" in runbook
    assert "bearer_token_env_var" in runbook
    assert "Test-NetConnection gonpatri -Port 22" in runbook
    assert "401" in runbook
    assert "timeout" in runbook
