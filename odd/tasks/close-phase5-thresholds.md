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
- [x] Decidir valores de umbral (aprobación usuario: 0.65 / 0.60+0)
- [x] Fijar thresholds en generation_baseline.json (faithfulness_min=0.65)
- [x] Fijar thresholds en resolution_baseline.json (f1_min=0.60, hard_over_merge_max=0.0)
- [x] Commitear artifacts untracked (resolution_metrics.json; gr3_after.json no existe en local — solo en OrangePi)
- [x] Gates de calidad: ruff ✓, mypy ✓, validate_architecture ✓, 63 tests eval/gate ✓
- [x] Verificar readiness gate en OrangePi (E2E) → **exit 0 PASSED** (2026-09-23 00:14Z): structure audit passed; resolution f1 0.641≥0.60 + over 0.0≤0.0; generation faithfulness 0.912≥0.65; retrieval informativo 0.0549; extraction deferred R6.1. Warnings informativos (RAGAS secondary, low precision@k). Video del gate: /tmp/gate-readiness-20260923.json. Evidencia E2E committeada: resolution_metrics.json (generated_at 2026-09-23T00:14:49Z) en commit de cierre. Backup del anterior: /tmp/resolution_metrics.backup-20260922.json.

## Decisión pendiente registrada
- docs/benchmarks/gr3_after.json: untracked SOLO en el servidor (no existe en local); revisar si se trae al repo como artifact de benchmark (Phase 5 §4 formalize before/after) — decidir en próxima iteración.
- warnings informativos: RAGAS drop vs baseline y precision@k bajo quedan como follow-up de evaluación; no bloquean (R6.1: retrieval no blocking).

## Next step
Confirmar readiness gate deja de ser INCOMPLETE por thresholds.