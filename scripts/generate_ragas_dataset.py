"""Generate a RAGAS evaluation dataset for the book knowledge graph.

Produces 125 question tuples (5 personas × 5 tareas × 5 preguntas each)
classified as ``global`` (requires community summaries / map-reduce) or
``local`` (requires entity/relationship search).  The terminology mapping
from ``llm_adapter`` is injected so generated questions speak in user
language, not graph syntax.

The output is ``evaluation_dataset.jsonl`` — one JSON object per line.
Human curation is expected before running the RAGAS evaluation.

Usage:
    uv run python scripts/generate_ragas_dataset.py
    uv run python scripts/generate_ragas_dataset.py --output custom_path.jsonl
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import click
import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.llm_adapter import _TERMINOLOGY_MAPPING

# ── Personas and their tasks (curated) ──────────────────────────────────────

PERSONAS: dict[str, list[str]] = {
    "Arquitecto de Agentes": [
        "Diseñar un nuevo sistema multi-agente desde cero",
        "Evaluar trade-offs entre patrones de coordinación (Orquestador vs Peer-to-Peer)",
        "Seleccionar el framework adecuado para un caso de uso con restricciones de latencia",
        "Definir la topología de comunicación entre agentes heterogéneos",
        "Diseñar la estrategia de manejo de errores y fallbacks en un pipeline multi-agente",
    ],
    "Developer": [
        "Implementar un agente con herramientas específicas (búsqueda web, código)",
        "Depurar un problema de comunicación entre agentes que usan memoria compartida",
        "Integrar un nuevo modelo LLM en un agente existente (cambio de proveedor)",
        "Optimizar el rendimiento de un pipeline con paralelismo entre agentes",
        "Migrar una arquitectura de un solo agente a multi-agente sin downtime",
    ],
    "Tech Lead": [
        "Decidir entre GraphRAG y RAG tradicional para el contexto de los agentes",
        "Evaluar si usar MCP o A2A para exponer herramientas a los agentes",
        "Comparar LangGraph, CrewAI y AutoGen para un proyecto con deadline ajustado",
        "Definir la estrategia de observabilidad y logging para agentes en producción",
        "Establecer criterios de calidad y seguridad para agentes que acceden a APIs externas",
    ],
    "Investigador": [
        "Explorar patrones emergentes en sistemas multi-agente (2024-2025)",
        "Comparar la taxonomía de memoria en agentes: corto plazo, largo plazo, semántica",
        "Analizar cómo el Critic Pattern mejora la calidad de respuestas en agentes de código",
        "Estudiar la relación entre planificación jerárquica y delegación en agentes autónomos",
        "Investigar el impacto del routing dinámico en la latencia de sistemas multi-agente",
    ],
    "Product Manager": [
        "Entender qué capacidades diferencian un agente simple de uno multi-agente",
        "Evaluar el ROI de migrar de RAG tradicional a GraphRAG para búsqueda empresarial",
        "Identificar qué casos de uso justifican la complejidad de una arquitectura multi-agente",
        "Comparar el time-to-market entre usar CrewAI vs construir una solución custom",
        "Definir los KPIs de éxito para un asistente de código basado en agentes",
    ],
}

# ── Pydantic schemas for the LLM response ──────────────────────────────────


class EvaluationQuestion(BaseModel):
    """Single evaluation question with its query classification."""

    question: str = Field(
        description="The evaluation question in natural language (Spanish or English)"
    )
    type: Literal["global", "local"] = Field(
        description=(
            "'global' if the question needs summarizing across multiple "
            "entities/communities; 'local' if it targets specific "
            "entities, relationships, or patterns"
        )
    )


class TaskQuestions(BaseModel):
    """Questions for a single task."""

    tarea: str = Field(description="The original task description (verbatim)")
    preguntas: list[EvaluationQuestion] = Field(
        min_length=5, max_length=5,
        description="Exactly 5 evaluation questions for this task",
    )


class PersonaQuestions(BaseModel):
    """All task-question groups for one persona."""

    persona: str
    tareas: list[TaskQuestions] = Field(min_length=5, max_length=5)


# ── Prompt ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are generating an evaluation dataset for a GraphRAG system that "
    "queries a Neo4j knowledge graph. The graph was built from the book "
    "\"Agentic Architectural Patterns for Building Multi-Agent Systems.\"\n\n"
    "The graph has these entity types: pattern, agent, component, concept, "
    "tool, framework, mcp, llmops, risk.\n"
    "It has hierarchical community summaries at levels 0-3 (Leiden clusters).\n\n"
    "Generate exactly 5 questions for each task.  The questions should be "
    "realistic — things a real user in this persona would ask when consulting "
    "the book to perform this task.\n\n"
    "For each question, classify it:\n"
    "- 'global': the answer requires summarising across many entities or "
    "communities (low-detail overview, trends, comparisons, 'what does the "
    "book say about...' questions).\n"
    "- 'local': the answer requires finding specific entities, patterns, "
    "frameworks, relationships, or small clusters (precise lookups, "
    "'how does X relate to Y', 'what tools does Z need').\n\n"
    "Mix both types — roughly 2 global + 3 local or 3 global + 2 local per task.\n\n"
    "Use the terminology mapping below to phrase your questions in the "
    "language a real user would use (not in Cypher or graph syntax).\n\n"
    f"{_TERMINOLOGY_MAPPING}"
)

_USER_PROMPT = (
    "Persona: {persona}\n\n"
    "Tasks:\n{tareas}\n\n"
    "Generate 5 realistic evaluation questions per task.  Return the result "
    "as structured JSON matching the expected schema."
)


def _format_tareas(tareas: list[str]) -> str:
    return "\n".join(f"{i}. {t}" for i, t in enumerate(tareas, 1))


def _build_user_prompt(persona: str, tareas: list[str]) -> str:
    return _USER_PROMPT.format(
        persona=persona, tareas=_format_tareas(tareas)
    )


async def _generate_for_persona(
    client: instructor.AsyncInstructor,
    model: str,
    persona: str,
    tareas: list[str],
) -> PersonaQuestions:
    """Call the LLM to generate 5 × 5 = 25 questions for one persona."""
    return await client.chat.completions.create(
        model=model,
        response_model=PersonaQuestions,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(persona, tareas)},
        ],
    )


async def _generate_all(
    client: instructor.AsyncInstructor,
    model: str,
) -> list[dict[str, str]]:
    """Generate all 125 questions and flatten into a list of records."""
    records: list[dict[str, str]] = []
    for persona, tareas in PERSONAS.items():
        result = await _generate_for_persona(client, model, persona, tareas)
        for task in result.tareas:
            for pq in task.preguntas:
                records.append(
                    {
                        "persona": persona,
                        "tarea": task.tarea,
                        "question": pq.question,
                        "type": pq.type,
                    }
                )
    return records


@click.command()
@click.option(
    "--output", "-o",
    default="evaluation_dataset.jsonl",
    help="Output path (default: evaluation_dataset.jsonl)",
)
def main(output: str) -> None:
    """Generate the RAGAS evaluation dataset."""
    settings = Settings()  # type: ignore[call-arg]
    api_key: str = (
        settings.llm_api_key.get_secret_value()
        if settings.llm_api_key is not None
        else "ollama"
    )
    raw_client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=api_key,
        timeout=60.0,
        max_retries=0,
    )
    client = instructor.from_openai(raw_client)

    records = asyncio.run(
        _generate_all(client, settings.llm_model_name)
    )

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    click.echo(f"Generated {len(records)} questions → {output_path}")
    global_count = sum(1 for r in records if r["type"] == "global")
    local_count = sum(1 for r in records if r["type"] == "local")
    click.echo(f"  Global: {global_count}  |  Local: {local_count}")


if __name__ == "__main__":
    main()
