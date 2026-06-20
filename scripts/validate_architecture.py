# scripts/validate_architecture.py
"""Guardián de la arquitectura hexagonal. Los agentes deben ejecutarlo antes de commit.

Verifica que `domain/` y `application/` no importen módulos de infraestructura
ni librerías externas prohibidas (neo4j, openai, fitz, instructor, etc.).
"""

import ast
import sys
from pathlib import Path

# Windows: por defecto stdout/stderr usan cp1252 que no soporta emojis (✅❌🚨).
# Reconfiguramos a UTF-8 en arranque paraqr que el script sea multiplataforma.
# `sys.stdout.reconfigure` está disponible desde Python 3.7.
if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding is not None and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# Módulos prohibidos en domain y application (librerías de infra + el propio pkg infra)
FORBIDDEN_IN_DOMAIN = {"neo4j", "openai", "fitz", "instructor", "infrastructure"}
FORBIDDEN_IN_APPLICATION = {"neo4j", "openai", "fitz", "instructor", "infrastructure"}


def _module_root(name: str | None) -> str | None:
    """Devuelve la raíz del módulo (primer segmento antes de un punto), o None."""
    if name is None:
        return None
    return name.split(".")[0]


def check_imports(file_path: Path, forbidden: set[str]) -> bool:
    """Verifica que un archivo no importe módulos prohibidos."""
    ok = True
    with file_path.open(encoding="utf-8") as f:
        tree = ast.parse(f.read())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `import os, sys` → revisar TODOS los nombres, no solo el primero
            for alias in node.names:
                root = _module_root(alias.name)
                if root in forbidden:
                    print(f"❌ VIOLACIÓN ARQUITECTÓNICA en {file_path}: 'import {alias.name}'")
                    ok = False
        # node.module puede ser None para 'from . import x' (import relativo puro)
        # node.level > 0 indica import relativo ('.' '..' etc.) — los ignoramos.
        elif isinstance(node, ast.ImportFrom) and node.module is not None and not node.level:
            root = _module_root(node.module)
            if root in forbidden:
                names = ", ".join(a.name for a in node.names)
                print(
                    f"❌ VIOLACIÓN ARQUITECTÓNICA en {file_path}: "
                    f"'from {node.module} import {names}'"
                )
                ok = False
    return ok


def scan_dir(base: Path, forbidden: set[str]) -> bool:
    is_valid = True
    if not base.exists():
        return True
    for py_file in base.rglob("*.py"):
        # __init__.py puede re-exportar; lo saltamos para no generar ruido
        if py_file.name == "__init__.py":
            continue
        if not check_imports(py_file, forbidden):
            is_valid = False
    return is_valid


def main() -> None:
    base_dir = Path("src/book_graph_rag")
    is_valid = True

    # 1. El Dominio NO puede importar Infraestructura ni librerías externas (excepto pydantic)
    is_valid &= scan_dir(base_dir / "domain", FORBIDDEN_IN_DOMAIN)

    # 2. La Aplicación NO puede importar Infraestructura directamente
    is_valid &= scan_dir(base_dir / "application", FORBIDDEN_IN_APPLICATION)

    if is_valid:
        print("✅ Arquitectura Hexagonal validada correctamente.")
        sys.exit(0)
    print("🚨 Se encontraron violaciones de arquitectura. Corrige el código.")
    sys.exit(1)


if __name__ == "__main__":
    main()
