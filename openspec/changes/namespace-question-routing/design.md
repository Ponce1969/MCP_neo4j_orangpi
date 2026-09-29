# Design: Namespace Question Routing

## Architecture

The feature follows the repository's hexagonal boundaries:

```text
Gentle-AI / caller
        |
        v
RouteQuestionUseCase  (application)
        |
        +--> EmbeddingProviderPort       (existing)
        +--> NamespaceProfilePort         (new)
        +--> ScopeResolverPort            (existing, final validation)
        +--> RoutingTelemetryPort         (new, optional)
                         |
                         v
SentenceTransformerAdapter / profile store / SQLite adapter (infrastructure)
```

The router is a classifier, not an authorization mechanism. Its output is only a
candidate namespace. The existing `CatalogScopeResolver` remains authoritative and the
MCP server continues to require a validated scope.

## Data sources

Profile construction uses the following priority:

1. `Book`, `Chapter`, `Section`, and `Chunk.text` grouped by `book_id`;
2. active, namespaced entity names/descriptions as supplemental signals;
3. namespace-filtered community summaries only as supplemental signals.

Chunks and editorial structure are primary because community summaries can contain
shared entities after cross-namespace resolution and do not carry a direct `book_id`.
The profile builder is read-only with respect to Neo4j.

## Unit 0 artifacts

Unit 0 creates:

- `data/evaluation/namespace_routing_dataset.jsonl`: hand-curated routing labels;
- a manifest entry with SHA-256 and record count;
- `docs/ops/namespace-routing-baseline.md`: the verified production baseline and
  explicit evidence/safety boundary;
- this OpenSpec proposal, design, specification, and task list.

The dataset labels routing behavior, not answer correctness. It includes single-book,
ambiguous, cross-book, and out-of-domain examples in English and Spanish.

## Domain contract (Unit 1)

```text
NamespaceProfile
- namespace: SourceNamespace
- centroid: EmbeddingVector
- model_id: str
- dimension: int
- profile_version: str

RouteDecision
- selected_namespace: SourceNamespace | None
- candidates: ordered scores
- top_score: float
- margin: float
- confidence: float
- route_kind: single | multi | abstain | out_of_domain
- reason: stable machine-readable code
```

The cosine and decision functions are pure domain logic. They must handle zero-norm
vectors and mismatched dimensions deterministically. The domain must not import Neo4j,
SQLite, NumPy, or sentence-transformers.

## Application flow (Units 2-3)

```text
question
  -> lexical hints
  -> one local query embedding when needed
  -> profile repository
  -> score every active namespace
  -> threshold + top-1/top-2 margin
  -> RouteDecision
  -> CatalogScopeResolver
  -> explicit MCP source_id / controlled fan-out
```

The lexical stage may route obvious title/TOC terms without embedding. The embedding
stage is the fallback, not an LLM call. Thresholds and margins are configuration/data
products calibrated against the routing dataset; they are not arbitrary constants.

For ambiguous or cross-book decisions, the first runtime version returns an explicit
abstention/multi result. It does not drop `source_id` or turn an ambiguous request into
an unscoped query. A caller may issue independent scoped calls for top-2 candidates.

## Profile artifact

The first profile store is local and immutable per generation run (JSON or SQLite).
Every profile records:

- namespace;
- embedding model id and dimension;
- normalization strategy;
- catalog version/hash;
- graph snapshot identifier;
- profile generation timestamp;
- source document counts.

No profile needs to be written to Neo4j for the initial rollout.

## SQLite telemetry (Unit 4)

SQLite has two logically separate concerns:

1. `routing_cache`: safe cache key, namespace, score, profile/model versions, expiry;
2. `routing_events`: routing decision, fallback, correction, evidence ids, latency, and
   optional answer reference.

Raw question/answer columns are disabled by default and belong to an explicit opt-in
telemetry policy. The adapter is not `QueryLoggerPort`, and it must not change the
metadata-only/HMAC contract of `QueryLogEntry`.

Writes must use parameterized SQL, bounded retention, a busy timeout/WAL policy suitable
for the Pi, and local file permissions. A correction label is required before an event
is promoted into a golden dataset.

## Evaluation

Routing is evaluated separately from retrieval and generation:

- top-1 accuracy;
- top-2 recall;
- wrong-book rate;
- abstention rate and coverage;
- cross-book detection rate;
- p50/p95 local routing latency.

End-to-end retrieval/generation evaluation is required before production enablement,
but a routing pass must not be inferred from a good answer produced by another path.

## Security and operational boundary

- Unit 0 is repository-only and read-only.
- Profile generation uses supported read paths and does not mutate production.
- No third-party container is touched.
- Neo4j mutation, if ever proposed, requires backup, dry-run, human approval, and audit.
- MCP bearer authentication, Tailscale binding, and fail-closed namespace enforcement
  remain unchanged.

## Work-unit boundaries

Each unit includes its tests/evidence and one clear rollback:

| Unit | Deliverable | Rollback |
|------|-------------|----------|
| 0 | OpenSpec, dataset, baseline, manifest | Revert docs/data only |
| 1 | Domain contracts/classifier | Remove domain models/functions |
| 2 | Read-only profile builder/artifact | Disable profile generation |
| 3 | Runtime router/abstention | Disable caller integration; explicit scope remains |
| 4 | SQLite cache/telemetry | Disable adapter/delete local DB |
| 5 | Integration/evaluation/rollout | Feature flag off; no graph rollback needed |
