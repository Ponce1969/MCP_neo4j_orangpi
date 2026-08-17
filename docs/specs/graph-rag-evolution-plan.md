# GraphRAG Evolution Plan
## Agentic Architectural Patterns Knowledge System

## 1. Mission

The project is NOT a generic PDF RAG.

The goal is to build an enterprise-grade architectural knowledge
system based on:

"Agentic Architectural Patterns for Building Multi-Agent Systems"

The final system must allow AI agents to query the knowledge contained
in the book through a controlled MCP interface.

The architecture is:

PDF
 ↓
Semantic Chunking
 ↓
Knowledge Extraction
 ↓
Neo4j Knowledge Graph
 ↓
Graph Query Layer
 ↓
GraphRAG / Evidence Retrieval
 ↓
Evaluation
 ↓
MCP
 ↓
External AI Agents


The system must preserve:

- semantic relationships
- editorial hierarchy
- textual evidence
- provenance
- source pages
- entity identity
- relationship semantics
- graph paths

The system must be measurable and reproducible.

---

# 2. Current architecture

The current system already contains:

- Python 3.13
- uv
- Pydantic v2
- Neo4j
- async Neo4j driver
- semantic TOC-based chunking
- LLM entity extraction
- relationship extraction
- idempotent MERGE writes
- dead-letter handling
- command/read separation
- GraphQueryPort
- Neo4jQueryAdapter
- full-text indexes
- entity aliases
- canonical names
- relationship traversal
- shortest path
- chunk retrieval
- provenance
- query timeout
- Ragas evaluation

Do NOT rewrite the existing architecture.

Improve incrementally.

---

# 3. Architectural principle

The system has FOUR distinct concerns.

## Knowledge Construction

PDF → Graph

Responsible for creating correct knowledge.

## Knowledge Retrieval

Graph → Evidence

Responsible for finding relevant entities,
relationships, paths and source chunks.

## Answer Generation

Evidence → Answer

Responsible for generating an answer grounded
in retrieved evidence.

## Evaluation

Measures the previous three layers.

Ragas belongs primarily to Evaluation.

Ragas is NOT the production retrieval layer.

---

# 4. Current phase

The project is currently in:

GRAPH RETRIEVAL + EVALUATION

The MCP production interface comes later.

Do NOT jump directly to MCP implementation.

First establish retrieval quality.

---

# 5. Phase P0 — Evaluation infrastructure

FIRST priority.

The current Ragas execution produces:

    Invalid n value
    currently only n = 1 is supported

and:

    LLM returned 1 generations instead of requested 3

This must be resolved before using Ragas results
as a reliable baseline.

Tasks:

1. Identify evaluator LLM.
2. Identify provider.
3. Identify Ragas configuration requesting n=3.
4. Configure the evaluator to use n=1 if the provider only supports n=1.
5. Verify retry/fallback behavior.
6. Run 5-10 evaluation cases.
7. Confirm zero evaluator errors.
8. Confirm reproducible results.
9. Run the complete benchmark only after this succeeds.

Acceptance criteria:

- zero n=3 provider errors
- zero generation mismatch warnings
- evaluator configuration documented
- successful 5-10 case evaluation
- reproducible execution

DO NOT modify Neo4j to solve this problem.

---

# 6. Phase P1 — Graph integrity

Before optimizing retrieval, verify that the graph itself
represents the source correctly.

Measure:

- Book count
- Chapter count
- Section count
- Chunk count
- Entity count
- Relationship count
- MENTIONS count
- orphan relationships
- duplicate entities
- duplicate relationships
- missing provenance
- invalid chapter/section hierarchy

Validate:

Book
 ↓
Chapter
 ↓
Section
 ↓
Chunk
 ↓
Entity

and:

Entity
 ↓
RELATED
 ↓
Entity

Every extracted relationship should be traceable to source evidence.

---

# 7. Phase P2 — Retrieval observability

The retrieval layer must become observable.

Every retrieval operation should expose enough metadata
to diagnose why a result was returned.

For entities record:

- entity_id
- name
- type
- canonical_name
- aliases
- match strategy
- score
- chunk
- page
- chapter
- section

For relationships record:

- source entity
- relationship type
- target entity
- description
- source page
- source chunk

For paths record:

- nodes
- relationships
- path length
- source evidence

For chunks record:

- chunk_index
- book_id
- text
- page_start
- page_end
- chapter
- section
- retrieval score

Do not hide provenance.

---

# 8. Phase P3 — Deterministic retrieval evaluation

Before relying entirely on LLM evaluation,
introduce deterministic retrieval metrics.

Measure:

Entity Recall
Entity Precision
Entity F1

Relationship Recall
Relationship Precision
Relationship F1

Chunk Recall
Chunk Precision

Path Recall

Source Recall

These metrics should be calculated using IDs and known ground truth
whenever possible.

LLM judges should not be used where deterministic comparison
is sufficient.

---

# 9. Phase P4 — Question classification

The benchmark must be classified.

Minimum categories:

- factual
- definition
- entity lookup
- relationship
- multi-hop
- comparison
- chapter/section
- ambiguous

Calculate metrics per category.

Do NOT rely only on global averages.

Example:

factual       0.94
definition    0.91
relationship  0.76
multi-hop     0.58

If multi-hop performs poorly while factual performs well,
investigate graph traversal.

---

# 10. Phase P5 — Retrieval strategy experiments

Evaluate independently:

A. chunk-only
B. entity-only
C. entity + chunk
D. entity + traversal
E. entity + path
F. hybrid

Use exactly the same benchmark.

Record:

- Recall
- Precision
- Faithfulness
- Answer Relevancy
- Context Recall
- Context Precision
- latency

Do not select a strategy based on intuition.

Select using measurements.

---

# 11. Phase P6 — Traversal experiments

Current traversal is clamped to:

    depth <= 3

Do not increase this arbitrarily.

Experiment:

depth=1
depth=2
depth=3

Measure:

- entity recall
- relationship recall
- path recall
- context recall
- context precision
- faithfulness
- latency
- noise

Choose the depth that provides the best trade-off.

A deeper graph traversal is NOT automatically better.

---

# 12. Phase P7 — Top-K experiments

Evaluate multiple retrieval sizes.

Chunks:

3
5
10

Entities:

5
10
20

Compare:

- context recall
- context precision
- faithfulness
- latency
- noise

Goal:

maximize useful evidence.

Do NOT maximize context size.

---

# 13. Phase P8 — Entity disambiguation

Improve entity retrieval only if evaluation demonstrates
that entity ambiguity is causing failures.

Use:

name
canonical_name
aliases
type
description
neighbor entities
source section
source chunk

Avoid treating every name match as the same entity.

---

# 14. Phase P9 — Relationship semantics

Relationships are not merely connections.

The system must preserve:

source
relationship type
target

Example:

Negotiation
    ENABLES
Coordination

is different from:

Negotiation
    RELATED
Coordination

Relationship type must participate in retrieval and evaluation.

---

# 15. Phase P10 — Evidence assembly

Create a controlled evidence assembly layer.

The LLM should receive evidence organized as:

Pattern / Entity
 ↓
Relationships
 ↓
Related concepts
 ↓
Source chunks
 ↓
Chapter
 ↓
Section
 ↓
Pages

Remove:

- duplicate chunks
- irrelevant neighbors
- redundant relationships
- unrelated graph paths

The objective is high information density.

---

# 16. Phase P11 — GraphRAG

Only after retrieval is reliable.

GraphRAG should combine:

STRUCTURED KNOWLEDGE

Neo4j:
- entities
- relationships
- paths
- hierarchy

+

TEXTUAL KNOWLEDGE

Chunks:
- explanations
- definitions
- examples
- trade-offs
- caveats

+

PROVENANCE

- chapter
- section
- page
- chunk

The system should select the retrieval strategy according
to the question.

---

# 17. Phase P12 — Question routing

Different questions require different retrieval.

Example:

Definition:

chunk search
+
entity lookup

Relationship:

entity
+
relationship traversal

Multi-hop:

entity
+
traversal
+
path
+
source chunks

Chapter question:

chapter
+
section
+
chunks

Comparison:

entity A
+
entity B
+
independent evidence

Do not force all questions through the same retrieval pipeline.

---

# 18. Phase P13 — Ragas evaluation

Once retrieval is stable, run Ragas.

Evaluate:

- faithfulness
- answer relevancy
- context precision
- context recall
- additional metrics appropriate to the dataset

Ragas should answer:

"Is the system producing useful grounded answers?"

It should NOT be the only measurement system.

Combine:

Deterministic retrieval metrics
+
Ragas
+
Latency
+
Error rate

---

# 19. Phase P14 — MCP interface

ONLY after the GraphRAG layer is stable.

MCP must expose semantic tools.

Preferred tools:

search_book
find_entity
get_entity
get_related_entities
traverse_relationships
find_path
search_evidence
get_section
get_chapter

Later:

find_pattern
explain_pattern
find_related_patterns
find_pattern_tradeoffs
find_pattern_dependencies
compare_patterns
find_pattern_evidence

Do NOT expose arbitrary Cypher execution to agents.

Avoid:

execute_cypher(cypher)

as a normal production MCP tool.

The MCP layer should protect the graph from uncontrolled queries.

---

# 20. Phase P15 — Agentic architectural reasoning

The final system must support questions such as:

"What pattern should I use?"

"What are the trade-offs?"

"What patterns are related?"

"What dependencies exist?"

"How do these patterns combine?"

"What risks does the book identify?"

"What evidence supports this recommendation?"

"Which chapter explains this pattern?"

The system should return:

- recommendation
- supporting concepts
- relationships
- trade-offs
- source evidence
- chapter
- section
- pages

The agent must be able to distinguish:

FACT
from
INFERENCE

The system must not present an inference as if it were
explicitly stated in the book.

---

# 21. Evaluation hierarchy

Evaluate the system in this order:

LEVEL 1
Graph integrity

LEVEL 2
Retrieval quality

LEVEL 3
Evidence quality

LEVEL 4
Answer quality

LEVEL 5
Agentic usefulness

Never skip directly to Level 4.

If retrieval is wrong, improving the generation prompt
does not solve the underlying problem.

---

# 22. Change policy

Every change must follow:

Hypothesis
 ↓
Experiment
 ↓
Measurement
 ↓
Comparison
 ↓
Decision

Before changing code, document:

Problem
Evidence
Hypothesis
Expected improvement
Files affected
Test plan

After changing code:

Tests
Benchmark
Metrics
Latency
Regression analysis

---

# 23. Baseline policy

Maintain a known-good baseline.

Record:

- git commit
- evaluator model
- evaluator configuration
- GraphRAG configuration
- retrieval parameters
- traversal depth
- top-K
- benchmark version
- metrics
- timestamp

Never compare experiments with different configurations
without documenting the difference.

---

# 24. Regression policy

A change must NOT be accepted simply because
one metric improves.

Example:

multi-hop recall:
0.72 → 0.84

but factual precision:
0.91 → 0.62

Reject the change.

Consider the complete evaluation vector.

---

# 25. Current immediate task

DO NOT implement MCP yet.

DO NOT rewrite Neo4jQueryAdapter yet.

DO NOT add embeddings yet.

DO NOT increase traversal depth yet.

DO NOT increase top-K yet.

FIRST:

1. Fix Ragas evaluator/provider incompatibility.
2. Run 5-10 cases.
3. Confirm clean evaluation.
4. Create baseline.
5. Audit graph integrity.
6. Add retrieval observability.
7. Evaluate retrieval by category.
8. Identify the actual bottleneck.
9. Only then modify retrieval.

---

# 26. Final architectural objective

The final system should look like:

PDF
 ↓
Semantic Indexing
 ↓
Neo4j Knowledge Graph
 ↓
Graph Query Layer
 ↓
Evidence Retrieval
 ↓
GraphRAG
 ↓
Evaluation
 ↓
MCP
 ↓
AI Agents
 ↓
Architectural Reasoning

Neo4j is the knowledge substrate.

GraphRAG is the retrieval/reasoning context layer.

Ragas is the evaluation layer.

MCP is the controlled agent interface.

The AI agent is the consumer.

These responsibilities must remain separated.
