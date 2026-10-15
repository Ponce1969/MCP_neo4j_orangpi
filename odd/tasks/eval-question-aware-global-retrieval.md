# eval-question-aware-global-retrieval — make the layer-5 global path the production path

The layer-5 evaluation composes its answer from **the first 15 of 164 community summaries in
database order**, chosen **without looking at the question**, and then verifies claims against
all 164. The production `ask_global` path scores the summaries against the question, keeps
`ask_global_top_n` (8) and composes a cited answer with the production composer. The adapter
that the evaluation uses **already constructs** `GlobalQueryUseCase` and never calls it, so the
good path is wired and dormant while the metric measures a path production never runs. This
feature makes the evaluation use the production path, then validates the ground truth that the
measurement depends on.

Branch: `feat/eval-question-aware-global-retrieval` (from `main`). Evidence lives in
`odd/backlog.md` E2 and in the merge-time findings recorded in this document's Log.

## Specs

Verbatim requirements from the owner's authorization (2026-10-15). Do not paraphrase these.

- **S1 — Authorization.** The owner chose the menu option whose description reads, verbatim:
  "Arreglar la recuperación question-blind del path de evaluación (misma ranking de comunidades
  que ask_global) + validar el ground truth. Es el que hace que las métricas de las capas 4/5
  signifiquen algo."
- **S2 — Same ranking as `ask_global`.** The global selection must be the ranking production
  already uses: score community summaries against the question in batches
  (`ask_global_score_batch_size`), keep the top `ask_global_top_n`, in that order.
- **S3 — Measure the production answer.** For `qtype: global` the layer-5 answer must be the
  one the production path composes (same composer, citations included), not a parallel prompt.
- **S4 — Contexts are what the answer used.** The claims, the pairwise judge, the grounding
  signal and the RAGAS payload must receive the selected contexts, not all summaries.
- **S5 — Validate the ground truth.** Before trusting the retrieval numbers, check that the
  references the dataset asks for exist and are reachable: `ret-018` does not appear even at
  N=50, and 5 of 23 references never enter the pool at N=40.
- **S6 — Re-baseline honestly.** The instrument changes, so `generation_baseline.json` is
  re-measured on production and the superseded value is named as such in the README, in
  `AGENTS.md` and in the baseline artifact.

## Non-goals

- **The RAGAS feed (backlog E1) stays out.** The owner routed it to separate work; after this
  feature the payload the layer passes is production-shaped, which is what E1 will need, but
  the runner keeps receiving what `_run_ragas` gives it today.
- **No production behavior change.** The MCP path (`ask_global`, `search_rag`, `search_chunks`)
  is not modified; only the evaluation's port and adapter are.
- **No reranker.** `RERANK_ENABLED` stays off (measured worse; the previous feature).
- **The dataset is not edited silently.** If the ground-truth check finds a bad reference, that
  is reported and decided by the owner before any dataset byte changes.

## Findings this starts from

- `Neo4jRetrievalAdapter.__init__` builds a `GlobalQueryUseCase` (line 49) and **never uses it**:
  `_global_uc` occurs exactly once in the file.
- `fetch_contexts(qtype="global")` calls `get_summaries_by_level(detail_level)` and returns every
  summary; the port method passes no question, and the adapter's docstring in
  `community_adapter.py` calls that unscoped read the "legacy contract used by
  `infrastructure/neo4j_retrieval_adapter.py`".
- Measured consequence: `gen-001`, `gen-002` and `gen-003` produced **byte-identical** context
  blobs (164 contexts, 674 623 chars, 0 with a `chunk_id`), so the layer-5 faithfulness was a
  self-consistency check against a fixed blob, and each claims/verify/pairwise prompt carried
  ~659 KiB.
- The retrieval layer (layer 4) is **not** affected: `retrieval_dataset.jsonl` is 20 records,
  all `qtype: local`, so it measures the chunk path (which is why the chunk-only denominator
  fix mattered).

## Tasks

Format: ID · specs · route · commit.

- **T1 · S2** — Extract the production ranking into one shared helper and make
  `fetch_contexts(qtype="global")` question-aware through it. Route: inline, test-first.
  **DONE** (see L4): the ranking lives in `GlobalQueryUseCase._rank`, shared by `ask` and the
  new public `select`; the adapter's global branch calls `select`.
- **T2 · S3, S4** — Add the typed global answer to the port and the adapter; route the layer-5
  global questions through it so the answer, the claims, the judge, the grounding and the RAGAS
  payload all receive the selected contexts. Route: inline, test-first. **DONE** (see L5):
  `GlobalRetrievalAnswer` + `GraphRetrievalPort.answer_global`, the generation layer routes
  global questions through it, and the citation ids it carries reach the evidence-usage signal.
- **T3 · S5** — Ground-truth validation against production (read-only, SSH tunnel): does each
  reference chunk exist, and is it reachable by the fulltext query the layer uses? Report before
  deciding whether the dataset is wrong. Route: inline. **DONE** (see L6): every one of the 23
  references exists and is indexed; the three that never return are a lexical-coverage miss, not
  a broken label, so the dataset needs no fix.
- **T4 · S6** — Re-baseline `generation_baseline.json` on the new instrument (35 questions
  against production) and update README / AGENTS / the artifact's `model_ids`. Route: inline.
  **DONE** (see L11): the successful run is 35/35 in 7185 s and the artifact now records
  `0.8988` with `faithfulness_min = 0.85` (re-derived mechanism-first by the owner).
- **T5 · S1** — Closure: feature doc, backlog entry for E2, README/AGENTS, commits and PR.
  Route: inline. **IN PROGRESS.**
- **T5 · S1** — Closure: feature doc, backlog entry for E2, README/AGENTS, commits and PR.
  Route: inline.

Gates before any "done": `uv run ruff check .` · `uv run mypy .` · `uv run python scripts/validate_architecture.py` · `uv run pytest tests/ -q --ignore=tests/integration`.

## Log

- **L1 (user, verbatim).** "Mergeá #22 y arrancá con E2 (recomendado)" — the option label; its
  description is quoted as S1.
- **L2 (evidence, exploration before any write).** `data/evaluation/retrieval_dataset.jsonl`:
  20 records, all `qtype: local`, 23 references ⇒ layer 4 is unaffected and S1's phrase "capas
  4/5" is only accurate for layer 5. `community_adapter.get_summaries_by_level(level, scope=None)`
  returns every `:CommunitySummary` at that level, unfiltered by question, and its docstring
  documents that unscoped read as the legacy contract the retrieval adapter uses.
  `GlobalQueryUseCase.ask` = fetch all → `score_communities` in batches of
  `ask_global_score_batch_size` (20) under `summary_max_concurrency` (3) → sort desc → keep
  `ask_global_top_n` (8) → `llm_port.compose_answer(question, top)`, returning
  `answer`, `citations`, `contexts`, `usage`. `Neo4jRetrievalAdapter` builds that use case in
  `__init__` and never calls it; its own `compose_answer` prompt uses `contexts[:15]`.
- **L3 (design decision, with the heuristic that drives it).** The port's two-call shape
  (`fetch_contexts` then `compose_answer`) cannot carry the production path faithfully, because
  production selects and composes in **one** pass and the composer needs the summary ids for
  citation, which a `RetrievalContext` (frozen `{chunk_id, text}`, S6 of the previous feature)
  cannot hold. Chosen: an **additive, typed** port method for the global answer, rather than
  memoising inside the adapter (hidden state, violates "explicit dependencies") or rebuilding
  ids from texts (impossible). Rationale per the engineering heuristics: rule 1 (the dormant
  `_global_uc` stops being dead wiring), rule 3 (root cause: the evaluation never used the
  production path), rule E (one ranking implementation), rule 4 (extension, not contract
  breakage: the existing two methods keep working for `local`).
- **L4 (evidence, T1 RED→GREEN, and a refinement of L3).** RED first, and it had to be earned
  twice: the first fixture failed on `CommunitySummary`'s **derived** id (the model rejects a
  hand-written one: "id must be the stable hash of level and sorted entity_ids"), which would
  have been a construction failure pretending to be a behavioural one; with the fixture fixed
  the red is exactly the defect — `llm.batches == []` (the scorer is never consulted) and all
  summaries returned in database order. GREEN: the ranking moved into
  `GlobalQueryUseCase._rank`, which returns the ranking **and the batch count it issued** so the
  `usage.llm_calls` number cannot drift from the batching; `ask` and the new public `select`
  both use it (refinement of L3: the shared ranking lives in the use case, not duplicated in
  the adapter), and the adapter's global branch calls `select`. The new file
  `tests/unit/test_neo4j_retrieval_adapter_global.py` pins five things: the selection depends on
  the question, only `ask_global_top_n` survive in score order, scoring travels in batches of
  `ask_global_score_batch_size`, the level is read once, and summaries never claim a
  `chunk_id`. Verified: 28 targeted tests pass (5 new + 16 use-case + 7 adapter), `ruff` clean,
  `ruff format --check` clean, `mypy --strict` clean over 454 files, architecture green. One
  pre-existing test pinned the blind behaviour (`test_global_qtype_does_not_call_the_reranker`):
  its fixture was updated to the new premise while **keeping** its reranker assertion and
  **adding** the scorer assertion — updated, never weakened; the now-unused `_FakeSummary` in
  that file was pruned with it.
- **L5 (evidence, T2 RED→GREEN).** RED was behavioural and precise: the first new test in
  `tests/unit/test_evaluate_generation_layer.py` asserted that a global question reaches the
  production entry point, and failed with `assert [] == ['what is MCP?']` — the layer never
  called it. To keep that red behavioural rather than structural, the domain type
  (`GlobalRetrievalAnswer`) landed first, then the routing. GREEN: `GraphRetrievalPort` gained
  the **additive** `answer_global` (the two existing methods are untouched and still serve
  `local`), the adapter implements it by delegating to `GlobalQueryUseCase.ask` and builds
  `contexts` and `citation_ids` from the same list so they cannot drift, and the generation
  layer routes `qtype == "global"` through it while keeping the fetch-then-compose pair for
  `local`. Two consequences pinned by tests: the claims, the judge, the grounding and the RAGAS
  payload now receive the eight-or-fewer summaries the answer actually used instead of 164
  arbitrary ones, and `cited_context_share` **stops being undefined** for global questions
  because the production path hands over citable summary ids (the previous test asserted the
  undefined case; it was rewritten to the defined one and a new test keeps the undefined case
  alive for entity-only local contexts). Three pre-existing tests pinned the replaced
  behaviour: the orchestration-order trace (now traces `answer_global`), the evidence-usage
  assertion (now 2 ids provided, share 0.0) and, from T1, the reranker test. All three were
  updated to the new requirement with their intent intact — no assertion was weakened. The
  other port doubles gained an explicit `answer_global`; the retrieval layer's double raises
  `NotImplementedError` on purpose so a future test cannot silently pass on a path that layer
  does not own. Verified: 936 unit tests pass, the full suite is **2100 passed, 3 skipped, 0
  failed**, and `ruff`, `ruff format --check`, `mypy --strict` (454 files) and the architecture
  validator are clean. Committed as `4ed3c96`: T1 and T2 are one behaviour — "the layer-5 global
  path is the production path" — so they travel together, because the routing needs the shared
  ranking and the ranking alone would leave the metric unchanged.
- **L6 (evidence, T3 — the dataset is sound, and the probe had to be fixed three times before it
  could say so).** `scripts/verify_retrieval_references.py` (committed, read-only) replays the
  layer's own query per question at the gate's limits and checks each reference at three levels:
  exists in the graph, reachable for its own text, reachable for its question. Verdict against
  production: **references=23, reachable=20, never-returned=3, missing-from-graph=0,
  exists-but-not-indexed=0** — the labels are valid. The three that never come back are `…:980`
  (ret-005), `…:35` (ret-007) and `…:1114` (ret-018); all three exist (970/1500/1500 chars) and
  the index finds them when queried with their own words, so they miss because the question's
  vocabulary does not appear in them: **lexical coverage, not a bad label**. This refutes the
  earlier hypothesis — recorded in the README as a possibility — that the hand-crafted ground
  truth was itself in question. Reach per limit is measured too: at `@10` 12 of 23 references
  land, at `@40` 18, at `@50` 21.

  Three landmines, one root cause — **the chunk identity is composed, not stored**: (1) the first
  version asked Neo4j for a `chunk_id` property, which does not exist, so every reference looked
  missing, and Neo4j's own warning is what exposed it; (2) the second read `node.chunk_id` in the
  index query for the same reason, so every reference looked unindexed; (3) between them,
  `session.run(_INDEX_CYPHER, query=…)` collided with `run`'s first positional parameter — hidden
  until the earlier bug was fixed. The fix that ends the class: index membership now travels
  through **`Neo4jQueryAdapter.search_chunks`**, the same call the layer makes, so the id is
  composed by the adapter instead of rebuilt in the probe. A smaller fourth: a `ruff check`
  failure inside the probe's job short-circuited the chain while stderr was redirected, hiding a
  traceback and costing a round trip.
- **L7 (evidence, T4's smoke — the instrument change is real, measured on one question).**
  `gen-001` through the new path (production Neo4j over the tunnel, DeepSeek as the query role,
  one-record manifest): exit 0 in **254 s**, `status: passed`, `faithfulness = 1.0` over **19
  claims** (the blind path extracted 1), `pairwise_win_rate = 1.0` (the production answer won its
  comparison; the blind one lost, 0/1), and the grounding block — the assertion that matters:

  | signal | blind path | production path |
  |---|---|---|
  | `contexts_provided` | 164 | **8** (`ask_global_top_n`) |
  | `contexts_with_ids` | 0 | **8** |
  | `cited_context_share` | `null` (undefined) | **0.375** (cited 3 of 8) |
  | `term_grounding` | 0.875 | 0.868 |

  So the metric stopped being a tautology over a fixed blob: it is now computed over the
  summaries the answer was allowed to use, and the citation share carries information instead of
  being undefined. Cost, stated for the record: 254 s per question against the blind path's
  72 s (nine scoring batches, the production composer, and 19 claims to verify instead of one),
  which is why the 35-question run is bounded by a three-hour timeout rather than the earlier
  42-minute one.
- **L8 (in progress — T4 failed after 69 minutes and the failure is not yet named).** The
  35-question gate exited **2 after 4133 s with no report**. Exit 2 is this CLI's shape for a
  `ValueError` escaping the layer (`evaluate` turns `ValueError` into a `click.UsageError`), so
  the failure is not a Neo4j or provider outage — those exit 13 or 1. The traceback is lost:
  **my own job redirected stderr to `/dev/null`**, the second time today I hid the evidence I
  needed (the first was the label probe). Two more landmines in the same job: piping a long run
  through `tail` buffers its progress away, and the layer writes nothing until the end, so one
  failing question costs the whole hour and reports only an exit code.

  What is ruled out by measurement: the **last five questions all pass** (`scripts-ops`
  diagnostic, 923 s for five, `contexts=8 ids=8`, pairwise 1/1 for every one). With ~185 s per
  question, 4133 s is roughly **question 22**, not the end — an inference I made and then
  refuted. The tool that answers it was written for exactly this: `scripts-ops/diagnose_generation_layer.py`
  drives the same use case and adapters one record at a time, prints per-question metrics and
  progress, and **keeps going after a failure** so it names the question and the traceback
  instead of dying. It is running over all 35 now; the cause, the fix and the decision it forces
  come next.
- **L9 (evidence, and it changes how the baseline must be read: the instrument is noisy at the
  question level).** `gen-001`, same path, same model, same day, measured three times:

  | run | claims | faithfulness | pairwise |
  |---|---|---|---|
  | smoke (one-record manifest) | 19 | 1.0 | 1/1 |
  | diagnostic run 1 | 24 | 0.708 | 1/1 |
  | variance run 2 | 14 | 0.929 | 0/1 |

  The claim count moves by 70% and the score by 0.29 on the same input, because the extractor and
  the verifier are LLM calls whose output length varies; the pairwise verdict flips too. Two
  consequences for S6: the **aggregate over 35 questions** is the only number worth committing
  (a single question is not), and the threshold's margin has to be read against that spread
  rather than against one run's decimal. The first three questions of the full diagnostic were
  green (`0.708`, `0.778`, `0.846`) with `contexts=8 ids=8` on all of them, which also confirms
  the new instrument is stable in *shape* even when its numbers are not stable in value.
- **L10 (evidence, the two fixes the failure forced, and the tool that named the cause).** The
  cause: `ValueError: batched community scoring left 1 summary(ies) unscored:
  ['efa6b206287be7b4']` — the scoring model omitted one id and the batched scoring raises on
  purpose. It happened once inside the 35-question run and again at `gen-004` of the diagnostic,
  so it is stochastic, not one bad input. **Fix A** (`1bc0a79`): one retry carrying only the
  missing summaries, with the loud failure kept as the last resort — nothing is invented. RED was
  `assert 1 == 2` on the call count. **Fix B** (`be22e50`): the layer evaluates each question
  independently, records the failure with its id, keeps going, names the failed questions in the
  warnings and **refuses to report PASSED over a partial sample** — it reports `UNREACHABLE` with
  the count, the ids and the metrics of the questions that ran. RED was the exception aborting the
  whole use case. **The tool** (`03d8e98`, `scripts-ops/diagnose_generation_layer.py`) drives the
  same use case one record at a time and keeps going after a failure; it named the cause on its
  first run. Verified after both: **2101 passed, 3 skipped** in the full suite, the single failure
  being `test_gr3_after_removed_or_distinct` — the gate's RAGAS subprocess regenerates the
  untracked `docs/benchmarks/gr3_after.json` on every run (the scratch half of E1; deleted, and one
  `--after-output` line in the runner would close it for good).
- **L11 (evidence, T4 DONE — the run that produced the baseline).** `evaluate --layer generation`
  over all 35 questions against production: **exit 0 in 7185 s, 35/35 evaluated, no failures**.
  `status: passed`, `faithfulness = 0.8988195615514334` over **593 claims** (the blind instrument
  managed 0.7913 over 115), `pairwise_win_rate = 0.9714285714285714` (**34/35**, against 13/35
  before), `mean_term_grounding = 0.8319`, and `mean_cited_context_share = 0.5143` where it used to
  be undefined. Every question received **8 contexts, 8 with ids**. RAGAS is still unavailable (E1,
  unchanged, warning only). The near-1.0 pairwise rate is reported with its caveat: the judge sees
  both answers and the production answer carries citations, so it is a strong signal, not a
  verdict.

  **Threshold decision (owner, 2026-10-15).** `faithfulness_min = 0.85`, re-derived
  mechanism-first: ~5 % below the measurement, and ≈ 3.9 σ below it if the 593 claim verdicts are
  treated as independent (SE ≈ 0.012; they correlate within one answer, so the true band is wider
  and the margin is deliberately not tighter). Keeping `0.65` would sit ≈ 20 σ below the
  measurement and would no longer gate anything: a 25 % regression would pass unnoticed.
  **Committed:** `metrics.faithfulness = 0.8988195615514334`, `faithfulness_min = 0.85`,
  `code_commit = 03d8e98`; the guard in `tests/unit/test_evaluation_baseline_loader.py` carries the
  explanation of why the threshold moved, and `README.md`, `AGENTS.md` and `data/evaluation/README.md`
  carry the new pair plus the instrument change. `odd/backlog.md` E2 is marked resolved (`4ed3c96`)
  with the measured effect, and its pool-recall half remains the README's retrieval-quality step.
