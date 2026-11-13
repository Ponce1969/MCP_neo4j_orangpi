# ADR — Router profiles: from one mean centroid to per-book prototypes

**Status:** **pure prototypes REFUTED by measurement (L3).** T5 restores the centroid as the default
scoring path; the blend ships disabled behind `ROUTER_BLEND_W=1.0`, and the prototypes stay in the
artifact as diagnostics.
**Supersedes:** nothing. It supersedes the *hypothesis* refuted in
`odd/tasks/centroid-window-fix.md` (L5-L6), not a previous decision.
**Origin:** T9 measured +25.9% relative margin and an unchanged ranking; every book rose by
0.006-0.007 and the top-1→top-3 spread barely moved (0.0347 → 0.0354). Added text that lifts every
candidate equally is shared background, not discriminative signal.

## 1. Context and the real defect

`ordered_profile_texts` concatenates chapter titles, section titles, up to **200 chunk texts**, up to
**500 entities** and up to **100 community summaries**
(`neo4j_namespace_profile_source.py:69-72`), embeds them, and averages them into **one vector per
book** (`build_namespace_profiles_use_case.py:62-68`, `mean_normalized`). Four books of the same
corpus share most of their vocabulary, so the four means converge toward the corpus mean and any
cosine between them and a question lands in a narrow band. The router then measures a margin of
0.006 against a 0.05 threshold and abstains.

That is a **geometric** defect: the mean of a multimodal set of texts is a point that describes no
text in particular. A book that *contains* a passage about orchestration does not have a *mean* about
orchestration.

Constraints that shape the answer, read from the code rather than assumed:

| Constraint | Value | Consequence |
|---|---|---|
| Texts per book | ≤ ~1300 (200 chunks + 500 titles + 500 entities + 100 summaries) | the candidate pool is bounded, so per-fragment scoring is thinkable |
| Vector dimension | 384 | 1300 × 384 ≈ 500 k floats per book, ≈ 2 M for four books |
| Domain layer | pure Python, `math` only (`routing_models.py:11-17`) — no numpy | 2 M float multiplies per query in Python is **seconds**, not milliseconds |
| Artifact today | 44 355 B for four centroids; JSON of floats | per-fragment JSON ≈ tens of MB |
| Gatekeeping | `min_margin=0.05` keeps `wrong_namespace_rate` at **0.0** | any change must preserve that, not just raise the margin |

## 2. Options

**A — keep the single mean centroid (today).** Baseline: margin 0.006120 on the reference question,
ranking right by luck of vocabulary. Costs nothing, fixes nothing.

**B — full per-fragment vectors, score = max (or top-k mean) over fragments per book.**
Best fidelity; the score becomes "the best passage in this book", which is exactly the semantics the
question has. But: ~2 M floats, a JSON artifact in the tens of MB, and scoring inside the pure-Python
domain at **seconds per question** — unacceptable for a router that runs before every query. Would
force either numpy into the domain (against this repo's layer rule) or a new scoring port plus an
infrastructure adapter with numpy, i.e. a much larger change than the problem needs.

**C — K prototypes per book, score = max over the book's prototypes. (recommended)**
Each book stores `k` representative vectors selected from its own texts by **deterministic
farthest-point sampling** (greedy: start from the text closest to the book's mean, then repeatedly add
the text farthest from everything already chosen). Four books × 8 prototypes = **32 vectors ≈ 50 KB**
— the same order as today's artifact — and one query costs 32 dot products, which is *faster* than
today's four if the prototype set stays small. No numpy, no new dependency, no port. Determinism is
bought by construction: no RNG, no seed, stable tie-breaks.

The trade it makes: it does not find the single best passage, it finds the best of *k* regions of the
book. Given the pool is already capped at ~1300 texts and the question is "which book", that is the
right resolution — and if it saturates, option B remains the documented escape hatch.

## 3. Decision (proposed)

**Adopt C, with k = 8 as the starting value**, and treat k as a measured parameter, not a taste: the
calibration tool that already exists (`scripts/calibrate_namespace_routing.py`) sweeps thresholds over
cached scores, so k = 4 / 8 / 16 will be compared on the committed 31-label dataset
(`data/evaluation/namespace_routing_dataset.jsonl`) before anything is merged.

## 4. Specs

- **S1** — The profile artifact is versioned **2.0.0** and each profile carries
  `prototypes: tuple[Vector, ...]` (1..k of them), `k`, and **`prototype_sources: tuple[str, ...]`** —
  a short stable identifier per prototype (e.g. `kind:ordinal` of the text it came from), so a routing
  decision can be explained instead of merely reported. The mean `centroid` **stays persisted as a
  passive diagnostic field** (maintainer's ruling, §10): it is never read by the scoring path, and a
  test must prove that removing it changes no score. Two geometries can then be compared post-hoc
  without the router ever consulting the second one.
- **S2** — Scoring: `score(book) = max(cosine(question, p) for p in prototypes)`. The ranking is
  sorted by `(-score, namespace.source_id)` exactly as today, so ties stay deterministic.
- **S3** — Prototype selection is deterministic: farthest-point sampling seeded from the text closest
  to the book's mean, ties broken by text identifier. No RNG, no seed, same input ⇒ same artifact,
  byte for byte. That property is what makes the A/B measurement meaningful and is non-negotiable.
- **S4** — Version gating: `namespace_profile_version` goes `1.0.0 → 2.0.0`; the reader keeps rejecting
  a mismatched artifact (it already validates `profile_version`, `model_id`, `dimension`). **The cache
  key needs no new dimension**: `profile_version` is already one of its seven, so a v2 artifact
  invalidates cached answers by design.
- **S5** — Acceptance is **two-sided**, and the second side is the gate:
  (i) the margin on the reference question rises materially; (ii) `wrong_namespace_rate` on the 31
  labels **stays 0.0**. A wider margin achieved by committing to wrong books is a regression, not a
  win. Thresholds are re-derived, never copied: max-pooling shifts the whole score distribution, so
  `min_top_score=0.10` and `min_margin=0.05` become meaningless values in the new geometry.
- **S6** — The builder changes shape, not dependencies: per book it embeds the profile texts (with the
  window fix already in place), selects k prototypes by S3, and persists prototypes plus their
  source identifiers. k is a **CLI parameter** (`--k`) so the sweep is three builds of the same code
  rather than three code changes. The per-kind caps stay as they are; raising them is a separate
  decision with its own cost (build time and artifact size).

## 9. Tasks

| ID | Spec | Route | Commit |
|----|------|-------|--------|
| T1 | S1, S3, S4, S6 | done — v2 profile shape, deterministic `select_prototypes`, builder with `--k`, store/reader shape gate, `profile_version` 2.0.0 | `73f8867` |
| T2 | S2, S4 | pending — the router scores against prototypes (max cosine) and the store's version gates get wired at the composition root, so a stale v1 artifact fails loudly instead of abstaining quietly | — |
| T3 | S7 | pending — `strategy`, `k` and `best_prototype_source` in the `route` payload | — |
| T4 | S5 | done — three builds (229 / 414 / 783 KB, linear in k) and the sweep; **the ADR failed its acceptance** (L3) | host builds + L3 |
| T5 | S2, S5 | done — default back to the centroid; blend behind `ROUTER_BLEND_W` (default 1.0); prototypes and sources stay passive diagnostics | `1b491ff` |
| T6 | S2, S5, L9 | pending — persist `central_centroid` in the artifact as a passive field and add `ROUTER_SCORING` (`centroid` default | `central`), so the only measured candidate is switchable the day the labels justify it. The default must not change any route. | — |
- **S7** — Observability: the `route` payload gains `strategy: "prototypes"`, `k`, and per-candidate
  `best_prototype_source`, so a measurement is self-describing and a surprising decision is traceable
  to a text instead of to a vector.
- **S8** — Out of scope, explicitly: fan-out behaviour, the three fallback states, the cache write
  policy, `ask_global`, BTRAG, and the aggregation of community summaries. This ADR touches profile
  construction and the routing score, nothing else.

## 5. Impact

| Area | Impact |
|---|---|
| `build_namespace_profiles.py` / `build_namespace_profiles_use_case.py` | + farthest-point selection, + prototype source ids, − mean aggregation. Same ports, same caps, same CLI flags. |
| `domain/routing_models.py` | `NamespaceProfile` gains prototypes and loses `centroid`; `score_against_profiles` becomes `score_against_prototypes`; `mean_normalized` stays (the builder still uses it for the seed point). |
| `application/route_question_use_case.py` | no logic change: it calls the scoring function and `decide_route`. |
| `infrastructure/json_namespace_profile_store.py` | artifact shape v2, larger but same order of magnitude (~50 KB). |
| `config.py` | `namespace_profile_version` default to `2.0.0`; nothing else. |
| Tests | one per new rule: prototype determinism (same input ⇒ identical artifact), max-pooling beats mean on a synthetic multimodal book, single-prototype book degenerates to today's behaviour, v1 artifact rejected, cache key changes when `profile_version` changes. |
| Calibration | `scripts/calibrate_namespace_routing.py` reused as-is over cached scores, for k and thresholds. |

## 6. Measurement plan — fixed before looking

1. **Baseline**: today's v1 artifact. Two numbers: the reference question's margin (0.006120) and the
   31-label metrics (`single_accuracy`, `wrong_namespace_rate`, `abstention_rate`) from
   `evaluate_namespace_routing.py`.
2. **After**: v2 artifact at k = 4, 8, 16. Same two numbers per k.
3. **Report**: one table with margin **and** the three label metrics side by side — never the margin
   alone, which is the mistake this stage already made once.
4. **Stop rule**: if no k raises the margin while holding `wrong_namespace_rate` at 0.0, the ADR is
   wrong, option B is next, and the gate threshold becomes the suspect instead.

## 7. Risks

- **Max-pooling inflates scores and commits more often.** That is the point, and the danger: the
  router could start naming wrong books. Mitigated only by S5(ii), measured on 31 labels.
- **k too small** loses multimodality (back to a pseudo-centroid); **k too large** drifts back toward
  the mean and re-inflates the artifact. Hence k is swept, not chosen.
- **Determinism is load-bearing**: a non-deterministic selector would make every comparison
  meaningless. Farthest-point sampling was chosen over k-means precisely because it has no seed.
- **Provenance identifiers must be stable across rebuilds** or `best_prototype_source` becomes noise;
  they derive from `kind:ordinal`, which the source adapter already produces deterministically.
- **The Pi must regenerate the artifact** before any production measurement; a v1 artifact on a v2
  reader is rejected by design, which is the safe failure.

## 8. Open questions for the maintainer — ANSWERED (§9)

1. **k**: sweep **4 / 8 / 16** (§9). Chosen for symmetry: it shows quickly whether 4 under-fits and
   whether 16 saturates back toward individual-fragment noise.
2. **Provenance ids**: **yes** (§9). A few strings buy auditable explainability.
3. **Mean centroid as diagnostic**: **yes, persisted and never scored** (§9).

## 10. Log

- **L1** (maintainer, verbatim — the three rulings that unblock S1-S7, 2026-11-06):
  *"1. Valor de k. Mi recomendación: Arrancar con el barrido propuesto de 4 / 8 / 16. Por qué: Cubre
  simétricamente las potencias de 2, permitiendo ver rápido si 4 se queda corto (sub-fitting) o si 16
  empieza a saturar y acercarse al comportamiento ruidoso de los fragmentos individuales.*
  *2. IDs de procedencia en el artefacto (prototype_sources). Mi recomendación: Sí, rotundo. Por qué:
  El costo en espacio es insignificante (unos pocos strings de metadatos), pero el retorno de valor es
  gigante. Pasar de un veredicto ciego (\"ganó el libro X con score 0.61\") a una explicabilidad
  auditable (\"ganó el libro X por el fragmento clave Y\") cambia por completo la calidad de la
  depuración del router.*
  *3. ¿Sobrevive el centroide medio como campo de diagnóstico? Mi recomendación: Sí, guardarlo. Por
  qué: Por el costo despreciable de 384 floats por libro, tener el centroide medio persistido en el
  JSON como metadato de diagnóstico pasivo nos permite correr métricas de correlación geométricas
  post-hoc sin alterar en nada la función de ruteo principal.*"
  *"Dale luz verde a la implementación del prototipo y medimos los nuevos márgenes!"*
- **L2** (spec amendment caused by L1): S1 originally removed the `centroid` field outright, to keep a
  single scoring path. The maintainer ruled it should stay as a passive diagnostic. The rule is now
  enforced differently: the field persists, and a test asserts that **the scoring path never reads
  it** — so the drift S1 feared is impossible by construction instead of by absence.

- **L3** (MEASURED, 2026-11-06, offline over the 31 committed labels with the real evaluator plus a
  validated harness — the harness was accepted only after reproducing the host's k=8 numbers exactly:
  `13 correct / 0 wrong / 0.5909 / 0.4194`). Same artifact (k=8) for every strategy, so the only
  variable is the scoring function; grids `min_top_score` 0.10-0.75 × `min_margin` 0.00-0.25, 84
  points each:

  | strategy | best accuracy with `wrong_namespace_rate == 0.0` | thresholds | abstention | accuracy with no margin gate | wrong |
  |---|---|---|---|---|---|
  | `centroid` (passive field, v2) | 0.6364 | 0.10 / 0.05 | 0.5161 | **0.9091** | 0.0909 |
  | `maxproto` (the ADR's proposal) | 0.5909 | 0.10 / 0.05 | 0.4194 | 0.8636 | 0.1364 |
  | `top3` (soft pooling) | 0.0909 | 0.35 / 0.00 | 0.9355 | 0.5000 | 0.5000 |
  | `blend` w=0.3 | **0.6818** | 0.10 / 0.05 | **0.3548** | 0.8636 | 0.1364 |
  | `blend` w=0.5 | **0.6818** | 0.10 / 0.05 | 0.4194 | 0.8636 | 0.1364 |
  | `blend` w=0.7 | 0.6364 | 0.10 / 0.05 | 0.4516 | 0.8182 | 0.1818 |

  Four conclusions, including the one that refutes my own earlier reasoning:
  1. **Pure prototypes fail.** 0.5909 against the 0.682 recorded baseline, with `wrong_namespace_rate`
     held at 0.0. No threshold recovered it: every strategy's best point sits at (0.10, 0.05) and the
     plateau is flat from 0.10 to 0.25, so **the thresholds were never the problem** — my "we forgot to
     recalibrate" hypothesis was wrong, and the earlier measurement was already at the ceiling.
  2. **Soft pooling is catastrophic** (0.0909): averaging the top-3 prototypes flattens the signal and
     the margin stops separating anything. Cheapest candidate killed with the most clarity.
  3. **The mean was not the defect.** With no margin gate at all, the centroid ranks *better* than
     max-pooling (0.9091 vs 0.8636). It was not flattening needed signal; it was smoothing noise.
  4. **The blend is the only survivor**: `w·centroid + (1−w)·maxproto` matches the historical baseline
     (0.6818) at `w = 0.3-0.5` and reaches it while abstaining **12 points less** at `w = 0.3`
     (0.3548 vs 0.4839). Same accuracy, more coverage — but that gap is one or two questions on
     n = 22, so it is a hypothesis, not a result.
  5. Secondary finding: the centroid strategy scores 0.6364 on the **v2** artifact versus the 0.682
     recorded on v1. The window fix and the `ORDER BY` additions moved the mean slightly; the delta is
     one question, i.e. inside the instrument's resolution, but it is a real movement and belongs in
     the record rather than in a footnote.
- **L4** (instrument limit, must be quoted whenever these numbers are used): 22 single-labelled
  questions, so **one question is 4.5 accuracy points**. Every difference in L3 is one or two
  questions. Any claim stronger than "the blend is promising" requires more labels, not more geometry.

- **L5** (maintainer's decision, verbatim — 2026-11-06): *"Mi decisión final es la Opción 1 (la
  conservadora): Default al centroide (w=1.0): El sistema sigue funcionando como en main (0.682). El
  blend queda disponible detrás de una perilla: Por ejemplo, ROUTER_BLEND_W=0.3. Si en el futuro
  ampliamos el dataset a ~100 etiquetas y el blend sigue rindiendo, lo activamos. Por ahora, apagado.
  Prototipos y prototype_sources: Quedan en el artefacto v2 como diagnóstico (para que se pueda
  inspeccionar la geometría). El ADR se actualiza: Documentamos que la intuición original (promediar
  destruye información) era incorrecta. Los datos demuestran que la media regulariza y es mejor."*
- **L6** (the correction this ADR must carry, stated plainly): the hypothesis that opened this document
  — *a mean over heterogeneous texts destroys the signal, so maximal fidelity must be better* — is
  **wrong on this corpus**. Measured: pure prototypes 0.5909 against the centroid's 0.6364 on the same
  artifact, and with no margin gate at all the centroid ranks better (0.9091 vs 0.8636). The mean was
  not flattening signal; it was **smoothing noise**. The prototypes remain useful for two things the
  measurements did establish: they are what makes a decision *explainable* (a fragment, not a vector),
  and the blend is the only variant that ever matched the baseline while abstaining less. Neither is
  enough to change the default without more labels.
- **L7** (PRODUCTION, 2026-11-06, post-merge `e5dc53f` / PR #71): the v2 artifact was regenerated in
  the main checkout on the host and the router ran with the shipped default. The JSON says
  `strategy: "centroid"`, `profile_k: 8`, `reason: "low_margin"`, the same fan-out as before, and
  `best_prototype_source` per candidate — so the diagnostics work even though the centroid is what
  scored.

  Same question as the 2026-11-05 smoke, so the artifact swap is the only variable:

  | | v1 artifact (2026-10-08) | v2 artifact (2026-11-06) | delta |
  |---|---|---|---|
  | top-1 `ai-engineering-huyen` | 0.622052 | 0.614659 | −0.007393 |
  | top-2 `agentic-architectural-patterns` | 0.577574 | 0.579555 | +0.001981 |
  | **margin** | **0.044478** | **0.035104** | **−0.009374 (−21.1%)** |
  | ranking | Huyen, AAP, graphrag | idéntico | same decision |

  **The v2 artifact is NOT an improvement on the centroid path, and this record exists so nobody
  assumes it is.** The margin narrowed 21% and the top-1 dropped, matching what the labels had already
  said: the centroid strategy scores 0.6364 on the v2 artifact against the 0.682 recorded on v1. Two
  compounding causes, both of them prerequisites for correctness rather than regressions: the window
  fix changed what each profile text embeds (the whole text instead of its first 128 tokens), and two
  queries gained an `ORDER BY` so the rows under each cap are deterministic. Both were necessary —
  silent truncation and non-reproducible caps — and both moved the mean.

  Consequence worth keeping in view: `main` now routes with a *correct* artifact and a slightly
  narrower margin than the artifact it replaced. If a future measurement shows the router abstaining
  more than before, this is the first place to look, and the answer is not to revert the window fix.
- **L8** (DIAGNOSTIC, 2026-11-06, offline over the v2 artifact and the 31 labels — the anisotropy
  measurement this ADR owed before anyone spends a day on an embedding-model swap):

  **A. Centroid similarity between books.** Pairwise cosines: 0.7031 / 0.8450 / 0.8617 / 0.8822 /
  0.9109 / 0.9317, **mean 0.8558** between different books. A book-specific question scores 0.55-0.61
  against all three candidates, i.e. **below the books' mutual similarity**.

  **B. How much of each centroid is the common direction.** Cosine with the global mean centroid:
  0.9081 (essential-graphrag) … 0.9861 (graphrag-agentic). **91-99% of every book's direction is
  shared**; the discriminant lives in a 1-9% residual. That is the arithmetic reason the margins are
  ~0.005: there is almost nothing left to separate.

  **C. Effective rank.** SVD of the four centred centroids: singular values 0.5459 / 0.3134 / 0.1909,
  first component **68.9%** of the variance.

  **D. The prototype method was self-defeating — measured.** Mean cosine **within** a book's
  prototypes 0.0599 (n=112) versus **across** books 0.0898 (n=384): the gap is **−0.0299, negative**.
  Prototypes from *different* books are more similar to each other than to their own book's siblings.
  The cause is structural and it is the method itself: farthest-point sampling **maximises** intra-book
  spread, so it selects the least representative texts of each book by construction. Any future "more
  or better prototypes" idea is dead on arrival; a selector for routing should pick the *most
  central* texts, not the most distant ones.

  **E. Where the labels actually sit.** Margin buckets over 31 questions: 7 in [0.00, 0.02), 8 in
  [0.02, 0.05), 11 in [0.05, 0.10), 5 above 0.10; median **0.0509**, i.e. the distribution is centred
  on the decision boundary, and **15 of 31 (48%) already sit in the doubt band**. This corrects an
  earlier worry of mine: the label set is *not* dominated by easy cases, so growing it does add
  information.

  **F. The interpretation that matters, and its control.** The high inter-book similarity could be
  (i) an anisotropic space that compresses everything, or (ii) four books about AI architecture that
  genuinely overlap in meaning. The known control settles it against (i): an off-domain question
  ("receta de empanadas de carne") scores **0.1009** against the same centroids. The space does
  separate off-domain content, so the 0.86 is largely **real semantic overlap**. Consequence: an
  embedding-model swap is a coin flip, not an obvious win, and it must not be the next step without a
  control experiment — embed chunks from each book and measure cross-book nearest-neighbour confusion.
  If book A's chunks routinely land closest to book B's centroid, no model change fixes that; the
  limit is ontology, which is exactly what `docs/spec/06` already recorded as "the gap is ontology,
  not hallucination".

- **L9** (AGREGACIÓN: sweep of eight strategies, 2026-11-06, offline over the 800 exported chunk vectors
  and the 31 labels — same harness, thresholds grid 0.10-0.75 × 0.00-0.25, acceptance two-sided):

  | strategy | accuracy with `wrong_namespace_rate == 0.0` | thresholds | abstention | accuracy with no gate | wrong |
  |---|---|---|---|---|---|
  | `centroid` (shipped) | 0.6364 | 0.10 / 0.05 | 0.5161 | **0.9091** | 0.0909 |
  | `chunk_mean` (chunks only) | 0.5455 | 0.10 / 0.05 | 0.4516 | 0.6818 | 0.3182 |
  | **`central_mean`** (mean of the 20 chunks closest to the book mean) | **0.6818** | 0.10 / 0.05 | **0.2903** | 0.7727 | 0.2273 |
  | `maxproto` (farthest-point) | 0.5909 | 0.10 / 0.05 | 0.4194 | 0.8636 | 0.1364 |
  | `max_chunk` | 0.1818 | 0.70 / 0.10 | 0.8710 | 0.7727 | 0.2273 |
  | `topk_mean` k=3 | 0.3636 | 0.65 / 0.05 | 0.6129 | 0.7727 | 0.2273 |
  | `topk_mean` k=5 | 0.3636 | 0.65 / 0.05 | 0.6129 | 0.7273 | 0.2727 |
  | `topk_mean` k=10 | 0.3636 | 0.65 / 0.00 | 0.6452 | 0.6818 | 0.3182 |

  **1. Chunk-level scoring is the wrong instrument for routing, and my prediction was wrong.**
  I expected `topk_mean` to beat the centroid; it scored 0.3636 and `max_chunk` 0.1818. The reason is a
  change of frame worth keeping: kNN answers *"is there a passage in this book about this?"* — and for
  a cross-cutting question there is one in several books. Routing asks *"what is this book about?"*,
  which is a **distribution**, not a neighbour. The mean was not washing out signal; it was answering
  the right question.
  **2. The heterogeneous mix helps.** The artifact's centroid (chapters + sections + chunks + entities
  + summaries) beats the chunks-only mean, 0.6364 against 0.5455. Dropping entities and summaries
  hurts. That is the third refutation of the premise this ADR opened with.
  **3. The only improvement is the maintainer's idea**, which I had dismissed in L8 as "the mean in
  disguise": the mean of the 20 chunks *closest* to the book mean. It matches the shipped accuracy
  (+1 question of 22) and cuts abstention from 0.5161 to **0.2903** with zero wrong answers. Weak on
  accuracy, consistent on coverage — a hypothesis, not a result, on n=22.
  **4. Every optimum lands again on (0.10, 0.05)** — the fourth time the current thresholds are the
  best point in the grid.
  **5. Method note worth more than the numbers**: the sweep used a fast reimplementation of the
  decision policy, verified by an `assert` against the real `decide_route` over 40 random cases. That
  assert **caught a real bug** in the harness (comparing `namespace.source` against the prefixed
  `source_id`). Without it the whole table would have been silent garbage presented with confidence.
- **L10** (maintainer's decision on L9, verbatim): *"Vamos con la Opción 1. Agregá central_centroid al
  artefacto v2 como campo pasivo (no toca el scoring). Registrá el resultado en el ADR (L9) para que
  quede documentado que el central_mean es la candidata cuando haya datos. Dejá un ROUTER_SCORING (o
  similar) listo para switchear entre centroid y central cuando lleguemos a ~100 etiquetas. No lo
  cableamos hoy. Coincido en que con n=22, es una hipótesis, no un resultado."*
  Consequence for T6: `ROUTER_SCORING` must be a **working** switch with the safe default, never dead
  configuration — dead knobs are the failure this project already refused once (T6 of the intent
  router). And `central_centroid` must be **optional in the artifact**: the production artifact was
  built before it existed, and making it required would break the router at startup for a field nobody
  scores yet. Missing field + `scoring=central` fails closed with a message naming the rebuild;
  missing field + `scoring=centroid` loads normally.
