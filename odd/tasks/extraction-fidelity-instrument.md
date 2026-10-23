# Extraction fidelity instrument — layer 2 measured, not assumed

Feature doc for the block approved on 2026-10-20 after phase 2 closed. Layer 2 (extraction
fidelity) is the only acceptance checkbox still open for a reason that is **not** "not built": spec
06 §11 deferred it because no labeled extraction dataset exists, and without ground truth any
precision/recall/F1 would score the extractor against itself. This block builds the ground truth
infrastructure first and the instrument second.

Scope boundary: **no graph mutation, no production change**. The labels are a human artifact; the
code makes those human hours produce a measurement.

## Specs

- **S1** (operator, verbatim, 2026-10-20): "Arrancamos por el Paso 1 del bloque de fidelidad de
  extracción: protocolo de etiquetado primero —unidad de muestra, reglas de match, alias,
  merged_into, eje de tipos—, después el loader y la métrica con fixture sintético en RED. Sin tocar
  el grafo."
- **S2** (operator, verbatim): "El protocolo va antes que tus horas de etiquetado, porque es lo que
  hace que no se pierdan."
- **S3** (operator, verbatim): "Paso 1 a cargo del agente: enmienda a spec 06 §11, protocolo de
  etiquetado y suite/métrica con fixture sintético en RED."
- **S4** (operator, verbatim): "En paralelo, anotar el análisis read-only sobre los chunks de
  ret-005 y ret-018 para entender la brecha del pool."
- **S5** The labeling must be **blind**: the labeler never sees what the extractor produced for the
  chunk being labeled. A sheet that shows the predictions converts the exercise into ratification
  and the resulting F1 measures agreement with itself, which is the exact failure spec 06 §11
  refused.
- **S6** The sample is **deterministic and committed**: same seed, same chunk ids, re-derivable by
  anyone. A sample that cannot be reproduced cannot be extended, audited, or re-labeled.
- **S7** The metric must state, per item, **what it counted**: precision/recall/F1 over entity
  identity (pooled and per book), with type agreement reported **separately** so a correct entity
  with a wrong type is not double-counted as both a false positive and a false negative.
- **S8** First iteration is **measurement-only**: no threshold, no blocking. A threshold derived
  before any measurement would be invented; the gate keeps extraction optional until a measured
  baseline exists (the same order the other layers followed).

## Tasks

- **T1** [S1,S2,S3] This feature document. → commit 1
- **T2** [S1,S2] `docs/evaluation/extraction-labeling-protocol.md`: sample design, the blind-sheet
  rule, match rules (normalization, aliases, `merged_into`, type axis), the label file format, and
  the labeling procedure with its time budget. → commit 1
- **T3** [S3] Spec 06 §11 amendment: the protocol now exists, what it does and does not authorize,
  and the checkbox stays open until a measured baseline lands. → commit 1
- **T4** [S1,S3,S7] Domain label models + the pure metric with a **synthetic fixture, RED first**:
  `LabeledChunk`/`LabeledEntity`, normalization, alias matching, merged-canonical resolution,
  pooled and per-book P/R/F1, separate type agreement. → commit 2
- **T5** [S1,S5,S6] The blind labeling sheet: a read-only script that samples deterministically from
  the live graph, prints chunk text **without predictions**, and emits the empty label skeleton.
  This is the piece that makes the human hours possible; it queries the graph but writes nothing to
  it. → commit 2
- **T6** [S4] The `ret-005`/`ret-018` pool diagnostic: local part (references, adjacency, question
  shape) plus a read-only script for the reachability half, since only the production fulltext index
  can answer it. → commit 3

## Log

**L1** (2026-10-20, operator, verbatim): "Arrancamos por el Paso 1 del bloque de fidelidad de
extracción: protocolo de etiquetado primero —unidad de muestra, reglas de match, alias, merged_into,
eje de tipos—, después el loader y la métrica con fixture sintético en RED. Sin tocar el grafo. En
paralelo, el diagnóstico read-only de ret-005 y ret-018: si sus referencias no existen como chunks,
toda la Fase 2 queda explicada y no hay nada más que intentar sobre el ranking."
