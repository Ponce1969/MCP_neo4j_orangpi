# Answer quality — a project-owned baseline and a replacement for answer_relevancy

The gate has numbers; the standalone RAGAS run has different numbers over a different
dataset, and one of its metrics is broken. This feature gives the project **one baseline it
owns**, over the dataset the gate uses, with a deterministic signal where the broken metric
was.

## Specs

S1. The baseline is built over the gate's own dataset, so its numbers are comparable with the
    gate and with the gate's history. (L1: "Las 35 preguntas del gate")
S2. `answer_relevancy` is **not used** and is documented as broken, with its evidence, so
    nobody tunes against it. (the 2026-10-10 probe: a deterministic `0.0` even when the answer
    is the question echoed back, in both languages)
S3. Its replacement is **deterministic and costs no model call**: evidence usage — how far the
    answer leans on the contexts it received. (L1: "Uso de la evidencia recuperada")
S4. The signal reports two auditable numbers per answer: how many of the provided contexts the
    answer cites, and what share of the answer's content terms appear in those contexts. Both
    are computed from data the answer already carries, so the signal can never disagree with
    the citations.
S5. The level-0 vs level-1 comparison is redone with the new metrics: `contexts` (shipped
    2026-10-10) make an answer's grounding measurable for the first time.
S6. Gates: `ruff` + `mypy` + `scripts/validate_architecture.py` + `pytest` green before done.

## Tasks

- T1 — `domain/answer_grounding.py`: the deterministic signal as a pure function, test-first.
  S3, S4. Inline. `feat(eval): deterministic evidence-usage signal`
- T2 — Report it in the generation evaluation, per question and aggregated. S4. Inline,
  test-first. `feat(eval): report the evidence-usage signal`
- T3 — Document the broken metric where the evaluation numbers live. S2. Inline.
  `docs(eval): record that answer_relevancy is broken`
- T4 — Build the baseline over the gate dataset and store the artifact. S1. Inline.
  `chore(eval): project-owned baseline over the gate dataset`
- T5 — Level 0 vs 1 with the new metrics, recorded as evidence. S5. Inline.
  `docs(eval): level 0 vs level 1 with the new metrics`

## Log

L1 — 2026-10-10, user (decisions, verbatim answers to the two questions): "Las 35 preguntas
del gate" and "Uso de la evidencia recuperada".

L2 — 2026-10-10, context that motivates the change: `answer_relevancy` was probed and is broken
(a perfect answer and the question echoed back both score `0.0`; language, citation markers,
flakiness and the embedder were each ruled out by measurement). The same day, C1 shipped
`contexts` inside the `ask_global` payload, which is what makes an evidence-usage signal
possible and faithfulness measurable over answers obtained through the MCP.
