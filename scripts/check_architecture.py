#!/usr/bin/env python3
"""MCP OranPi architecture dependency linter.

Verifies hexagonal architecture boundaries:
- domain/ must not import from infrastructure or presentation
- application/ must not import from presentation
- infrastructure/ must not import from presentation
- presentation/ may import from application and domain

Exit code 0 = clean, 1 = violations found.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "mcp_oranpi"

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "build",
}

# Hexagonal dependency rules.
# Key = layer name, Value = set of layers this layer may import from.
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "domain": set(),  # Domain imports nothing
    "application": {"domain"},  # Application may import from domain
    "infrastructure": {"domain"},  # Infrastructure may import from domain
    # Presentation may import from domain and application
    "presentation": {"domain", "application"},
}

# Top-level modules that are not inside any layer.
# These may import from any layer (they are the glue).
EXEMPT_MODULES: set[str] = {"server", "config", "__init__"}

# Specific cross-layer import exemptions.
# These are intentional violations of the strict layering rule, allowed
# because they follow the dependency inversion pattern:
#
# application → infrastructure.command_runner: Constructor DI injection.
#   The application layer depends on the CommandRunner abstraction;
#   the concrete implementation is wired at the composition root.
#
# application → infrastructure.workspace_resolver: Same DI pattern.
#
# application → infrastructure.parsers: Pure transformation functions
#   that convert raw command output to domain models. No state, no side
#   effects, no infrastructure dependencies beyond strings→models.
#
# These exemptions are intentionally narrow — only specific modules
# are allowed, not the entire infrastructure package.
ALLOWED_CROSS_LAYER: dict[str, dict[str, set[str]]] = {
    "application": {
        # application may import these specific infrastructure modules
        "infrastructure": {
            "command_runner",
            "parsers",
            "workspace_resolver",
        },
    },
}


@dataclass(slots=True)
class Violation:
    """An architecture boundary violation."""

    file: str
    line: int
    source_layer: str
    target_layer: str
    import_statement: str


def get_layer(file_path: Path) -> str | None:
    """Determine which architectural layer a file belongs to.

    Returns 'domain', 'application', 'infrastructure', 'presentation',
    or None for top-level modules.
    """
    try:
        relative = file_path.relative_to(SRC_DIR)
    except ValueError:
        return None

    parts = relative.parts

    if len(parts) < 2:
        return None

    layer = parts[0]
    if layer in ALLOWED_IMPORTS:
        return layer

    return None


def extract_imports(file_path: Path) -> list[tuple[int, str]]:
    """Extract all import statements from a Python file.

    Returns list of (line_number, import_path) tuples.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    imports: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))

    return imports


def get_import_layer(import_path: str) -> str | None:
    """Determine which layer an import path targets.

    Returns 'domain', 'application', 'infrastructure', 'presentation',
    or None for external imports.
    """
    if not import_path.startswith("mcp_oranpi"):
        return None  # External import, no layer violation

    parts = import_path.removeprefix("mcp_oranpi.").split(".")
    if not parts:
        return None

    first = parts[0]
    if first in ALLOWED_IMPORTS:
        return first

    if first in EXEMPT_MODULES:
        return None  # Top-level module, exempt

    return None


def check_file(file_path: Path) -> list[Violation]:
    """Check a single Python file for architecture violations."""
    source_layer = get_layer(file_path)
    if source_layer is None:
        return []

    allowed = ALLOWED_IMPORTS.get(source_layer, set())
    violations: list[Violation] = []

    for line_no, import_path in extract_imports(file_path):
        target_layer = get_import_layer(import_path)
        if target_layer is None:
            continue  # External or exempt import

        if target_layer == source_layer:
            continue  # Intra-layer imports are always allowed

        if target_layer not in allowed:
            # Check cross-layer exemption list
            source_exemptions = ALLOWED_CROSS_LAYER.get(source_layer, {})
            allowed_modules = source_exemptions.get(target_layer, set())
            if allowed_modules:
                # Check if the specific module is exempted
                parts = import_path.removeprefix("mcp_oranpi.").split(".")
                if len(parts) >= 2 and parts[1] in allowed_modules:
                    continue  # Specific module is exempted
            violations.append(
                Violation(
                    file=str(file_path.relative_to(PROJECT_ROOT)),
                    line=line_no,
                    source_layer=source_layer,
                    target_layer=target_layer,
                    import_statement=import_path,
                )
            )

    return violations


def scan_directory() -> list[Violation]:
    """Scan all Python files in the source directory."""
    all_violations: list[Violation] = []

    for py_file in SRC_DIR.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in py_file.parts):
            continue
        all_violations.extend(check_file(py_file))

    return all_violations


def main() -> int:
    """Run the architecture linter."""
    if not SRC_DIR.exists():
        print(f"Source directory not found: {SRC_DIR}")  # noqa: T201
        return 1

    violations = scan_directory()

    if violations:
        print("Architecture boundary violations detected:\n")  # noqa: T201
        for v in violations:
            print(  # noqa: T201
                f"  [{v.source_layer} -> {v.target_layer}] "
                f"{v.file}:{v.line}\n"
                f"    import {v.import_statement}\n"
            )
        print(f"Found {len(violations)} violation(s).")  # noqa: T201
        return 1

    print("No architecture violations detected.")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())