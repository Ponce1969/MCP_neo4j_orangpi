# Feature: close-phase5-thresholds

## Objective
Cerrar Fase 5 del roadmap: fijar umbrales numéricos (thresholds_finalized) en los
baselines de las capas bloqueantes (resolution, generation) para que el readiness
gate `expose-mcp-readiness` deje de reportar INCOMPLETE (11) y mida contra valores
reales. El libro 1 (Agentic Architectural Patterns) queda sellado antes de indexar
otros libros.

## Problem
`data/evaluation/generation_baseline.json` y `resolution_baseline.json` tienen
`thresholds_finalized: false` → el gate reporta INCOMPLETE (11) aunque las métricas
pasen (design mechanism-first, R9). Retrieval es capa OPTIONAL (nunca bloquea) →
no requiere baseline con umbral.

## Why
AGENTS.md §7.4 + spec 06: los umbrales se fijan post-medición en una delta de Fase 5.
Un gate que siempre da INCOMPLETE es un "problema aparente" del libro 1.

## Scope
- `data/evaluation/generation_baseline.json`: thresholds_finalized=true, faithfulness_min=<>
- `data/evaluation/resolution_baseline.json`: thresholds_finalized=true, f1_min=<>, hard_over_merge_max=<>
- Commitear artifacts untracked: `data/evaluation/resolution_metrics.json`, `docs/benchmarks/gr3_after.json`
- NO tocar código, NO tocar grafo, NO tocar gates.yaml

## Constraints / non-goals
- No se re-corre evaluación (usar métricas ya medidas).
- No se indexa otro libro.
- El gate se verifica en el OrangePi (read-only, una corrida) al final.

## Evidence (métricas reales)
- Generation baseline (11/9): faithfulness 0.6825, answer_relevancy 0.5953, context_precision 0.474
- Resolution baseline (10/9): f1 0.6329, hard_over_merge 0.0, multilingual_f1 1.0, auto_merge 29/29
- resolution_metrics.json (19/9): retrieval_f1 0.6410, hard_over_merge 0.0, multilingual_f1 1.0
- Audit produccion (22/9): PASSED, 0 hallazgos, 7111 entidades, 1520 chunks

## Checklist
- [ ] Decidir valores de umbral (aprobación usuario)
- [ ] Fijar thresholds en generation_baseline.json
- [ ] Fijar thresholds en resolution_baseline.json
- [ ] Commitear artifacts untracked
- [ ] Gates de calidad: ruff, mypy, validate_architecture, tests eval/gate
- [ ] Verificar readiness gate en OrangePi (read-only) y commit (evidence: hash commit)

## Next step
Confirmar readiness gate deja de ser INCOMPLETE por thresholds.