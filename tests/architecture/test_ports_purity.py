"""Guardian test: ports/ depends only on stdlib, pydantic, domain and other ports.

``scripts/validate_architecture.py`` scans ``domain/`` and ``application/`` but
NOT ``ports/`` — the purity of the ports layer is convention (AGENTS.md §2),
not enforcement. This test closes that gap: a port must never import
infrastructure, application, config, or a vendor library (neo4j, openai,
sentence_transformers, ...), because the moment it does, the "what" knows the
"how" and the hexagon is gone.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_PORTS_DIR = Path("src/book_graph_rag/ports")

#: First-party module roots a port may import (domain models and port siblings).
_ALLOWED_FIRST_PARTY: tuple[str, ...] = (
    "book_graph_rag.domain",
    "book_graph_rag.ports",
)

#: Third-party roots allowed in ports (pydantic defines the domain models).
_ALLOWED_THIRD_PARTY = frozenset({"pydantic"})


def _iter_port_files() -> list[Path]:
    """All port modules; ``__init__.py`` re-exports are exempt, as in the script."""
    return sorted(p for p in _PORTS_DIR.rglob("*.py") if p.name != "__init__.py")


def _imported_modules(py_file: Path) -> list[str]:
    """Absolute module names imported by ``py_file`` (relative imports ignored)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and not node.level:
            modules.append(node.module)
    return modules


def _is_allowed(module: str) -> bool:
    root = module.split(".")[0]
    if root in sys.stdlib_module_names:
        return True
    if root in _ALLOWED_THIRD_PARTY:
        return True
    return any(module == base or module.startswith(base + ".") for base in _ALLOWED_FIRST_PARTY)


@pytest.mark.parametrize("py_file", _iter_port_files(), ids=lambda p: p.name)
def test_port_file_imports_only_allowed_modules(py_file: Path) -> None:
    offenders = [m for m in _imported_modules(py_file) if not _is_allowed(m)]
    assert offenders == [], (
        f"{py_file} imports modules outside stdlib/pydantic/domain/ports: {offenders}. "
        "Vendor and infrastructure imports belong in infrastructure/ adapters."
    )
