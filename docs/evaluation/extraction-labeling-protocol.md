# Extraction labeling protocol (v1)

**Purpose.** Produce the labeled ground truth that layer 2 of the readiness gate has never had, so
that extraction *fidelity* can be measured instead of assumed. Spec 06 §11 deferred the layer
precisely because this artifact did not exist; this document is the instruction set that creates it.

**What it measures.** Whether the entities the pipeline attached to a chunk are the entities a
careful human reader finds in that chunk's text, and whether it found all of them. **What it does
not measure**: whether extraction is useful, whether relations are right, whether the chunking
itself is sane. Those are different instruments.

**Authority.** This protocol is normative for the dataset. If the protocol and someone's intuition
disagree, the protocol wins until it is amended in this file with a dated entry.

---

## 1. The unit: one chunk

The unit of labeling is a **chunk**, identified by `(book_id, chunk_index)`, displayed as
`{book_id}:{chunk_index}` (e.g. `knowledge:agentic-architectural-patterns:979`). The labeler reads
the chunk's text and lists the entities present in **that text**.

A chunk is the right unit because it is the unit the extractor works on: the pipeline sends one
chunk to the LLM and commits whatever entities come back for it. Labeling a whole book would
measure something no stage of the pipeline produces.

**One entity counts once per chunk**, no matter how many times it is mentioned or under how many
surface forms.

### 1.1 What counts as an entity

An entity is a **named thing** that the book treats as a thing, and that fits one of the nine
declared types:

| type | for |
|---|---|
| `pattern` | named architectural/design patterns (ReAct, RAG, GraphRAG, …) |
| `agent` | named agents, agent roles, agent frameworks-as-actors |
| `component` | named modules, services, layers, stores |
| `concept` | named techniques, properties, metrics, principles |
| `tool` | named products, libraries, CLIs, databases |
| `framework` | named methodological frameworks that are not `tool` |
| `mcp` | Model Context Protocol concepts and named MCP servers/clients |
| `llmops` | named operational practices in the LLM lifecycle |
| `risk` | named failure modes, hazards, anti-patterns |

The set is **closed**: it is the `EntityType` literal in `src/book_graph_rag/domain/models.py:71`.
An entity that fits none of the nine is **not labeled**. If a real thing in the chunk fits two
types, choose the type that the chunk's sentence is actually about.

**Not labeled**: pronouns and generic nouns ("the system", "the model"), people's names, page
numbers, bibliographic entries, URL fragments, and anything that is only a verb phrase.

### 1.2 Relations are out of scope in v1

`RelationshipType` is also a closed set (`domain/models.py:80`, eight types), but relations are
**not** labeled in v1, deliberately:

- a relation label is only comparable when **both** endpoints matched, so its cost per useful data
  point is several times an entity label's;
- an unmatched endpoint turns a correct relation into an unscoreable one, which produces a metric
  that moves with entity matching rather than with relation extraction.

The measurement is therefore **entity fidelity**. Relation fidelity needs its own protocol and its
own block, and saying so here is cheaper than discovering it in the numbers.

---

## 2. Blind labeling — the rule that makes this a measurement

**The labeler never sees what the extractor produced for the chunk being labeled.** The sheet
contains the chunk text and nothing else about predictions.

Why this is not bureaucracy: if the sheet lists the extracted entities, the labeler's job silently
becomes "does this list look right", the label set converges on the extractor's output, and the
resulting F1 measures the agreement of the extractor with itself. Spec 06 §11 deferred this layer
for exactly that reason; a protocol that reintroduces it would close the checkbox with a number
that means nothing.

If a chunk cannot be labeled blind (for example, the sheet generator was modified), the label is
**void**, not "probably fine".

---

## 3. The sample: deterministic, committed, pre-registered

### 3.1 Frame and size

The frame is every `:Chunk` in the four active books. Census: **1520 / 989 / 302 / 657 = 3468**
chunks.

Allocation is **equal per book** — 12 chunks each, **48 total** — not proportional: with
proportional allocation the 302-chunk book would contribute three chunks and its precision would be
unmeasurable, while the 1520-chunk book already has plenty of text in common with the others.

48 chunks at roughly five entities per chunk gives ~240 labels; the 95 % interval on F1 is then
about ±5 points, which is enough to detect the regression size that matters (a 10-point drop) and
not enough to chase noise. **The size is fixed before any labeling and before any prediction is
looked at**: shrinking it after seeing a bad result would be choosing the sample after the outcome.

### 3.2 Selection rule

For each book, order the chunks by `chunk_index` and select the 12 with the smallest

```
blake2b(f"{book_id}:{chunk_index}:extraction-fidelity-v1", digest_size=8)
```

Ties broken by `chunk_index`. Hash-based selection is used instead of a PRNG because it is
reproducible in any language and any future Python version, with no seed state to preserve, and
instead of a regular stride because chunk boundaries carry periodic structure (section breaks) that
a stride would sample in phase.

The resolved list of 48 `(book_id, chunk_index)` pairs is **committed** as part of the dataset, so
"the sample" is a fact about the repository and not about somebody's local run.

### 3.3 When the sample must be redrawn

If the chunker changes — chunk size, overlap, or how text is split — the old labels no longer
describe the chunks that exist. The sample is then redrawn with a new version suffix
(`extraction-fidelity-v2`) and the dataset is versioned, never silently reused. Reusing labels
across a rechunking would produce a fidelity number that measures how far the text moved.

---

## 4. Match rules

### 4.1 Normalization

Two surface forms are the same when, after normalization, they are equal. Normalization produces two
keys, and two forms are the same when **either** key matches.

**Key 1 — words** (`key_words`):

1. Unicode NFKC;
2. case folding;
3. accent folding (`NFD`, drop combining marks);
4. `&` → `and`;
5. every character that is not a letter, digit or space → space;
6. collapse runs of whitespace, trim.

**Key 2 — compact** (`key_compact`): `key_words` with every space removed.

So `GraphRAG`, `graph-rag` and `Graph RAG` are **one form** (all three give `key_compact =
"graphrag"`), while `MCP` and `Model Context Protocol` are **not** (different characters — that is a
job for `surfaces`, see §4.2).

`key_compact` is not a loosening of the rule: it can only equate forms whose letter/digit sequence is
**identical**, because it removes separators and never invents or drops a character. The accepted
consequence is that `a b c` equals `abc`; the rejected alternative was splitting camel case and
acronym boundaries, which would turn `OpenAI` into `open ai` and `PyMuPDF` into `py mu pdf`, creating
boundaries the text never had. Given a choice between two small errors, the one that merges identical
character sequences is smaller than the one that manufactures words.

### 4.2 Aliases and surface forms

A labeled entity carries **all** surface forms the chunk uses for it, plus the form the labeler
judges canonical. `MCP` and `Model Context Protocol` mentioned in the same chunk are **one** entity
with two surface forms, not two entities.

A prediction matches a label when the normalized prediction surfaces (`name`, `canonical_name`,
every entry in `aliases`) **intersect** the normalized label surfaces. This is why the labeler must
record the aliases: it lets the extractor be right under a name the labeler happened not to choose.

### 4.3 `merged_into`

Entity resolution soft-deletes duplicates by setting `merged_into` on the loser (never
`DETACH DELETE`). The scoring therefore works on **canonical** identities:

- a **prediction** that is merged is followed to its canonical target before matching, so an
  extraction that was correct at index time and later merged is not counted as a miss;
- **predictions that collapse to the same canonical entity are counted once**, otherwise the
  extractor could inflate recall by emitting the same thing twice;
- the **label** side is never merged: labels describe text, not entities, and a labeled entity that
  happens to be a merge loser is still a correct reading of the chunk.

### 4.4 One-to-one matching

Matching is **one-to-one**: each label matches at most one prediction and each prediction at most
one label. When several predictions alias the same label, the match goes to the one with the most
shared surface forms (tie: lexicographically smallest canonical id). Without this, one generous
label could absorb three redundant predictions and precision would report a number nobody can act
on.

### 4.5 The type axis is separate

For every **matched** pair, the type agreement is recorded. Type agreement is reported as its own
number (`type_agreement = matching_types / matched_pairs`), **not** folded into precision or recall:
counting a right entity with the wrong type as both a false positive and a false negative would
double-count one defect and make the metric unreadable.

`matched` is defined by surface intersection regardless of type, so a type error never changes how
many entities were found — only the type axis moves.

---

## 5. The label file

One JSON object per line, in `data/evaluation/extraction_dataset.jsonl`, registered in
`data/evaluation/MANIFEST.json` with its `sha256`, `record_count` and
`provenance: "manually curated (protocol v1)"` — the same shape the resolution dataset uses.

```json
{"chunk_id": "knowledge:agentic-architectural-patterns:979", "book_id": "knowledge:agentic-architectural-patterns", "chunk_index": 979, "entities": [{"surfaces": ["ReAct", "Reasoning and Acting"], "canonical": "ReAct", "type": "pattern", "confidence": "high"}], "notes": "", "completed": true}
```

Field rules:

- `entities` is sorted by `canonical` (normalized) so two labelers produce comparable files;
- `surfaces` includes the canonical form and every other form used **in this chunk**; duplicates
  after normalization are an error;
- `type` comes from the closed set in §1.1; anything else is a schema error, not a judgment call;
- `confidence` is `"high"` (default) or `"low"`. `low` is for genuinely contested calls — a thing
  that is arguably a `concept` and arguably a `risk`. The metric reports the headline number over
  all labels and, beside it, the same computation with `confidence: "low"` labels dropped **and
  predictions held fixed**, so a reader can see whether the result is label-noise-bound. That second
  number is a **pessimistic bound**, not a second estimate: dropping a label while keeping its
  prediction makes that prediction a false positive, i.e. it prices the case where every contested
  label was an error. If the headline and the bound sit close together, no contested label is
  carrying the result; if they diverge, the number depends on judgments the labeler flagged;
- `notes` is free text, never read by a metric — it exists so a later reader can understand a
  surprising label without re-deriving it.
- `completed` is `false` until a human confirms the chunk and `true` once they have. It exists
  because `entities: []` is ambiguous: an empty list is both "not labeled yet" and "labeled, and
  there is nothing typeable in this chunk" — and the paragraph below declares the second case
  legitimate, so without an explicit marker a resuming labeler cannot tell a pending chunk from a
  finished empty one. A row with `completed: false` is **not** a label: the committed dataset has
  every row `true`, and the metric must never be run over a file that still contains pending rows.

An empty `entities` list is a legitimate label: some chunks are references, tables of contents or
code fragments with no typeable named thing.

---

## 6. Procedure and budget

1. **Generate the sheet** (read-only): the export script samples per §3.2 and prints, per chunk, the
   `chunk_id` and the chunk **text** — and nothing about predictions.
2. **Label 12 chunks per session**, one book per session. Budget: **~3 minutes per chunk** including
   reading, so ~40 minutes per book, **~2.5–3 hours total**, splittable across days without loss.
3. **Validate the file** against the schema before committing (duplicate normalized surfaces, type
   outside the closed set, `merged_into`-shaped ids in labels — all rejected).
4. **Commit the labels** with the MANIFEST entry, then run the metric.

The calibration pass is the labeler's own: after finishing a book, re-read two chunks labeled
earlier. If the earlier labels now look wrong, that is information about the protocol's clarity —
amend this file with a dated note **before** labeling more, because a protocol change mid-way makes
the earlier labels incomparable.

---

## 7. What the number will and will not authorize

The first measurement produces a **baseline**, not a threshold. Extraction stays `blocking: false`
in `gates.yaml` until a baseline exists with a finalized threshold derived from the measured
dispersion, following the same order the resolution, retrieval and generation layers followed.

Until then, layer 2 keeps reporting what it reports today (coverage, a connectivity invariant), and
this dataset makes the *fidelity* half possible without silently changing what the gate decides.

---

## Amendments

**2026-10-20 — §4.1 gains the compact key.** The v1 steps, applied literally, did not produce what
v1's own example claimed: `GraphRAG` normalized to `graphrag` while `graph-rag` and `Graph RAG`
normalized to `graph rag`, so the three forms the example called "one form" were in fact two. Found
by the implementation, before any label existed, which is the cheapest possible moment for a protocol
defect to surface. Resolution recorded in §4.1: a second separator-insensitive key, chosen over
case-boundary splitting because it can only equate identical character sequences. No label file
existed yet, so nothing had to be re-labeled.

**2026-10-20 — §4.1 second key, implementation note.** The metric matches on `key_compact` alone
rather than evaluating both keys, because the words key is subsumed: if `key_words` are equal then
`key_compact` are necessarily equal, so the single test implements the stated rule exactly. Recorded
because a reader will notice the words key is produced and not consulted, and should find the reason
where they look for it rather than reconstruct it.

**2026-10-20 — §5 gains `completed`.** The interactive labeler exposed an ambiguity in v1: `entities:
[]` is the skeleton's "not labeled yet" and also a legitimate finished label for a chunk with nothing
typeable in it, so "resume at the first unfinished chunk" could not be implemented without guessing.
The field is additive and means what it says — a row with `completed: false` is not a label. No label
file existed when the field was added, so nothing had to be migrated.
