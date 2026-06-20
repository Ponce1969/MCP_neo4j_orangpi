# AGENTS.md - Directivas de Desarrollo para IA

## 1. Filosofía y Herramientas
- **Gestor de Paquetes:** Usamos EXCLUSIVAMENTE `uv`. PROHIBIDO usar `pip`, `poetry` o `pipenv`.
- **Tipado y Linting:** `mypy` en modo `strict` y `ruff` con reglas estrictas. No se considera una tarea terminada si `uv run ruff check .` o `uv run mypy .` fallan.
- **Pre-commit:** Todo commit debe pasar por los hooks de pre-commit.

## 2. Arquitectura Hexagonal (Puertos y Adaptadores)
- **Dominio (`domain/`):** Solo modelos de Pydantic y lógica de negocio pura. PROHIBIDO importar librerías externas (no `neo4j`, no `openai`, no `fitz`).
- **Puertos (`ports/`):** Interfaces abstractas (`abc.ABC`). Definen el "qué" se hace, no el "cómo".
- **Infraestructura (`infrastructure/`):** Implementaciones concretas de los puertos. Aquí viven las librerías externas.
- **Aplicación (`application/`):** Casos de uso. Solo dependen de los Puertos, NUNCA de la Infraestructura directamente.

## 3. Configuración y Secretos
- PROHIBIDO hardcodear URLs, API keys, usuarios o contraseñas.
- Toda configuración debe venir de `pydantic_settings.BaseSettings`.
- Las contraseñas y keys DEBEN usar `pydantic.SecretStr`.
- Si falta una variable en `.env`, la app debe fallar en el arranque (Fail-Fast).

## 4. Flujo de Trabajo SDD
1. Lee el `Spec.md` correspondiente en `docs/specs/`.
2. Si necesitas crear un script de ayuda, ponlo en `scripts/`.
3. Escribe el código en `src/`.
4. Ejecuta los validadores (`uv run ruff check .`, `uv run mypy .`, `uv run python scripts/validate_architecture.py`).
5. Solo cuando todo pase, considera la tarea completada.

## 5. Gates de calidad (antes de entregar)
Antes de marcar una tarea como lista, los TRES deben pasar:
```bash
uv run ruff check .
uv run mypy .
uv run python scripts/validate_architecture.py
```
Si alguno falla, no es done.