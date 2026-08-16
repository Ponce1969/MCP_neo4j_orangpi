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

## 6. Política de exposición vía MCP (`agentic-patterns`)

Este proyecto (cuando la fase 07 del MCP server esté lista) se consume vía el
MCP **`agentic-patterns`** (remote SSE, hosteado en el OrangePi via Tailscale).

**El criterio de uso es OPT-IN, no por defecto.**

- **Estado por defecto: `enabled: false`** en el runtime del agente
  (`~/.config/opencode/opencode.json` -> `mcp.agentic-patterns.enabled`).
- **Se habilita on-demand**, sólo durante sesiones de **diseño de arquitectura
  multi-agente nueva** o casos específicos donde se necesita resolver patrones
  del libro contra el grafo Neo4j.
- **NO se porta a pi.** Pi queda acotado a los MCPs de trabajo diario
  (`oranpi` infra, `context7` docs). Este MCP vive únicamente en opencode para
  que el ruido en el system prompt de pi sea mínimo.
- **Menos es más:** un MCP habilitado inyecta TODAS sus tool descriptions en el
  system prompt en cada turno, gasta tokens, y distrae al modelo. No exponer lo
  que no aporta al 95 % de las sesiones es una decisión de ingeniería, no un
  capricho.

### Regla para cualquier agente que asista a este repo
- **NUNCA** auto-habilitar `agentic-patterns` sin confirmación explícita del humano.
- Si una tarea no requiere resolver patrones del libro contra el grafo, dejá el
  MCP apagado y usá las tools generales del runtime.
- Si una tarea SÍ lo requiere (ej. "diseñá una arquitectura multi-agente usando
  patrones del libro"), el humano debe confirmar el `enabled: true` antes de
  empezar; el agente no lo prende solo.
- Este criterio aplica también tras la fase 07: el MCP server se expone, pero
  el consumo es opt-in por sesión, no permanente.

## 7. REGLA DE ORO: Servidor OrangePi en PRODUCCIÓN (obligatorio)

Este proyecto corre en el OrangePi `100.106.85.109` (Tailscale; no hay puertos
abiertos). SSH directo: `ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes gonzalo@100.106.85.109`

**En ese servidor hay 23 contenedores Docker en producción, todos de proyectos
distintos (incluyendo Postgres de otros proyectos). NO tocar nada que no sea
del proyecto `bookgraph` (contenedor `bookgraph-neo4j`).**

- **NUNCA borrar nada sin consentimiento explícito del humano:** no `docker
  prune`, no `docker rmi`, no `docker volume rm`, no `DROP DATABASE`, no borrar
  archivos arbitrarios. Preguntar SIEMPRE antes de cualquier borrado.
- **NUNCA** reiniciar, parar o reconstruir contenedores de otros proyectos.
- Operaciones sobre el grafo del proyecto: solo vía `scripts/run_full_pipeline.py`
  (que hace auto-backup a `~/backups_neo4j/` antes de un `--fresh`) o comandos
  explícitos aprobados por el humano.
- Copiar esta regla a cualquier subagente que vaya a tocar el servidor.