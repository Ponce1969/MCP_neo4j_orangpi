# Centroid window fix — profile embeddings stop being silently truncated

Feature: the namespace router's per-book centroid is built from texts the embedding model truncates
at its own window (128 tokens for `paraphrase-multilingual-MiniLM-L12-v2`), while graph chunks are up
to 1500 characters (~375 tokens). The centroid therefore never sees roughly two thirds of each chunk.
This unit makes that loss impossible, **without** touching any other embedding caller.

Origin: `odd/tasks/tool-intent-router.md` L16 recorded margins of 0.044478 on the production host and
named this truncation as the probable cause. It was never proven to be the cause — this unit exists to
test that hypothesis, not to assume it.

## Specs

- **S1** — The fix is **opt-in and never global**. The same `SentenceTransformerAdapter` instance
  serves routing questions, entity-resolution anchors and evaluation runs; changing their embeddings
  would invalidate every number this project has already measured. With the flag off, the
  `model.encode(...)` call must be **identical** to today.
- **S2** — With the flag on, a text longer than the model window must reach the model **in full**, as
  consecutive token-bounded windows, and produce **exactly one vector per input text, in input order**.
  No window may be dropped.
- **S3** — The window budget comes from the model (`max_seq_length`), minus its special tokens. No new
  configuration setting is invented, and the fallback constant must reproduce the historical
  truncation point rather than pick an arbitrary one.
- **S4** — Measuring is part of the unit: the same question, the same artifact settings, **before and
  after** regeneration. The report must show **margin and retrieved namespaces together**, per the
  maintainer's warning: *"No confundas una mejora del margen con una mejora demostrada de la
  respuesta completa."*
- **S5** — Out of scope, explicitly: no change to BTRAG, no change to `agentic-memory`, no fusion of
  the three projects, no change to the `ask_global` community tail (registered separately in Engram
  obs 1830). The `top_n=8` truncation and the batch-scored relevance numbers are **not** touched here.

## Tasks

| ID | Spec | Route | Commit |
|----|------|-------|--------|
| T8 | S1, S2, S3 | delivered — `window_texts` pure helper + opt-in `window_long_texts` flag, on only in `scripts/build_namespace_profiles.py` | `22d73e3`-based branch, uncommitted |
| T9 | S4 | pending — **needs the production host**: regenerate profiles with the flag on, then run `route` with the same question and compare margin *and* namespaces against `0.044478` / `{graphrag-agentic, essential-graphrag}` | — |
| T10 | S4 | pending — if the margin moves, check whether the **communities** retrieved by the same question changed; identical books should mean identical community selection, so any drift means something else moved | — |

## Measurement protocol (fixed before looking at results)

1. Same question, verbatim: `¿cómo se conectan los patrones de orquestación?` — the one that produced
   `fanout` with `graphrag-agentic` + `essential-graphrag` and margin `0.004862` locally / `0.044478`
   on the Huyen question.
2. Baseline is the **current** artifact on the host, measured today: `data/router/namespace_profiles.json`,
   44 355 bytes, `generated_at` 2026-11-05, four profiles, all centroid norms 1.000000.
3. Run `route` first (cheap, no graph query, no BTRAG). Only if the margin moves, run `orchestrate`.
4. Record three things per run: `top_score`, `margin`, and the **top-3 namespaces in order**. A margin
   that rises while the ranking changes is not a win — it is a different decision.
5. If the margin does not move, say so and stop. The hypothesis would be wrong, and the next suspect is
   the gate/margin policy itself, not the embeddings.

## Log

- **L1** (maintainer, verbatim — the sequencing rule that created this branch): *"No arranquemos con
  el fix del centroide todavía. Tenemos un principio de ingeniería que no vamos a romper: no mezclar
  nuevas features con un smoke test pendiente."* The smoke was closed first (`4e6bb9d`, L16 there),
  and only then this branch was cut.
- **L2** (maintainer, verbatim — the trap to avoid): *"No confundas una mejora del margen con una
  mejora demostrada de la respuesta completa."*
- **L3** (RED honesty, recorded because it matters): the first failing run was a `TypeError` for the
  missing constructor flag, **not** a demonstration that text is truncated. Our adapter never truncates
  — it *lets* the tokenizer do it silently. So the RED proves the API gap, and the defect itself is
  argued from `model.encode(...)` receiving no `truncation`/`max_length` plus the model's documented
  window. **Closed:** `test_flag_on_makes_tail_tokens_contribute_to_the_vector` (in
  `tests/unit/test_windowed_profile_embedding.py`) now proves the defect itself — a truncating fake
  tokenizer plus an id-derived vector show that with the flag off the tail never reaches the vector,
  and with it on it does.
- **L4** (hygiene, found by the parent): the unit's test file was created as
  `tests/unit/test_sentence_transformer_adapter.py` while `tests/test_sentence_transformer_adapter.py`
  already existed, and `tests/unit/` has no `__init__.py` — the classic pytest basename collision.
  Renamed before commit. **Measured:** the full suite passed *with* the homonym present
  (`2579 passed`, `importlib` mode), so the collision was latent, not active — the rename was
  hygiene, not a bugfix.
