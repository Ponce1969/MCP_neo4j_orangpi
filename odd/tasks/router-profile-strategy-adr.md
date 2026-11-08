# ADR — Router profiles: from one mean centroid to per-book prototypes

**Status:** proposed, not implemented. No code written for this unit.
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
  decision can be explained instead of merely reported. The single `centroid` field is **removed**:
  keeping both would let two scoring paths coexist and drift.
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
  source identifiers. The per-kind caps stay as they are; raising them is a separate decision with its
  own cost (build time and artifact size).
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

## 8. Open questions for the maintainer

1. **k**: start at 8? The sweep will decide, but the range to sweep is a judgement call (4 / 8 / 16
   versus 4 / 6 / 8 / 12).
2. **Provenance ids in the artifact** (S1): they cost a few KB and buy explainability. Worth it, or
   keep the artifact minimal and accept opaque vectors?
3. **Should the mean centroid survive as a diagnostic field** next to the prototypes, so a future
   investigation can compare the two geometries? It doubles nothing meaningful (384 floats) but S1
   argues against keeping two scoring paths — the field could exist *without* being scored.
