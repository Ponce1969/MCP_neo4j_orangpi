# dead-letter-error-redaction — R5 residual: no raw error text in dead-letter, JSONL or checkpoints

The indexing failure paths still persist `error_message = str(error)` verbatim in three places
(`_fail_chunk`, the legacy `_write_dead_letter` JSONL, and the replay's re-fail path), and that
string also reaches the graph through `CheckpointPort.release_lease_to_failed`. Exception text from an
LLM adapter or a driver can carry prompts, URLs, chunks of corpus text or credentials. R5 of
`docs/spec/07-security-and-operational-risks.md` §5 records exactly this as its residual, and the task
is to close it without losing the ability to replay a failed chunk.

## Specs

- **S1 — The persisted error metadata is `type` + `length` + keyed fingerprint, never text.** The task's
  rule, verbatim: **"Drop + HMAC con clave + error_type + length"**. Concretely, after this change the
  raw exception text appears in **none** of these:
  - the dead-letter record written through `DeadLetterPort.write_failed_chunk`,
  - the legacy JSONL line appended by `IndexBookUseCase._write_dead_letter`,
  - the `:Checkpoint` node properties,
  - the argument a writer passes across `CheckpointPort.release_lease_to_failed`.

  What replaces it is exactly three things: the exception **class name** (`error_type`, already stored
  today and unchanged), the **character count** of the raw message (`error_length`), and a **keyed
  HMAC-SHA256 fingerprint** of the raw message (`error_fingerprint`, carrying its key id and algorithm).
  The fingerprint is the existing `QueryFingerprint` type the MCP log already uses, so a hash is only
  stored with the key that produced it — a plain digest over guessable driver text would be
  decorative.
- **S2 — The recovery payload stays intact.** Verbatim from the task: **"preservando intacto el payload
  del chunk/reintento para el recovery"**. `source_id`, `chunk_index`, `page_ref`, the four version
  dimensions, `attempt`, `checkpoint_status` and `timestamp` keep their names, values and meaning;
  `parse_failed_chunk` returns the same `ReplayableChunk` it returns today; and **a record already on
  disk, which carries the legacy `error_message`, still parses and still replays**. Losing replay for
  the sake of redaction would trade a confidentiality risk for an operational one.
- **S3 — One rule, one place.** The decision "what is safe to persist about a failure" is a single
  domain function that all three writers call. No site builds its own redaction, and no site keeps a
  second copy of the key-shape rule: the "is this a real key" question is answered by the same
  `effective_token`/`is_effective_secret_shape` rule the token and the log fingerprint already use.
  (The R2 lesson: the check and the wiring cannot drift if there is only one of each.)
- **S4 — No key means no fingerprint, and never raw text.** When no real fingerprint key is configured,
  the writer stores `error_type` and `error_length` with **no** fingerprint, still drops the raw text,
  and does **not** abort the indexing run. This differs on purpose from the MCP log path, which fails
  closed (`QueryFingerprintError`) because its key is validated at boot: a CLI indexing run must not
  die — and must not lose the retry payload — because a logging key is missing.
- **S5 — Legacy tolerance on read.** The record model keeps accepting a legacy `error_message` when
  reading (marked deprecated, never written as text by this code), so old files keep working; the graph
  reader stops surfacing the raw message at all. The dead-letter port path serializes
  `FailedChunkRecord.model_dump(mode="json")`, which emits `error_message: null` (no text); only the
  legacy JSONL dict writer omits the key entirely.
- **S6 — The register reflects reality.** `docs/spec/07-security-and-operational-risks.md` §5's R5
  residual — which today reads "[VERIFIED] (residual): the indexing dead-letter path still writes
  `error_message = str(error)` verbatim" — moves to the implemented mitigation, naming the code paths
  and the tests. The residual-risk line for the dead-letter path drops from MEDIUM accordingly. This
  is done only after the tests pass, and it states what is **not** covered: nodes written before this
  change are not scrubbed (that would be a graph write under the §7.2 gate).

## Tasks

- **T1 · S1, S3, S4, S5** — done (commit: `330f316`). `RedactedError` + `redacted_error()` in
  `domain/checkpoint_models.py` (next to the record it feeds), the record's new fields with the
  legacy field demoted to read-only, the `Checkpoint`/reader shape and the port signature. Unit tests
  first (RED): the fingerprint recomputed independently, the length, the absence of the raw text in
  the serialized form, the no-key and invisible-key cases. Route: parent, inline.
- **T2 · S2** — done (commit: `330f316`). The three writers and the replay path call the one rule; the
  checkpoint adapter writes the flat node properties and scrubs any legacy `error_message` on the
  node it touches. Tests: the JSONL keys are exactly the new set; a failing chunk leaves no raw text
  in the port call; a **legacy** record still replays end to end. Route: parent, inline.
- **T3 · S6** — done (commit: `a215538`). The register's R5 section, and the operator-facing note for
  `.env.example` (that file is the maintainer's: the snippet is handed over, not edited). Route:
  parent, inline.

## Log

- **L1 (the task, verbatim, 2026-10-16):** "### 3. Siguiente tarea en paralelo: Arrancar con R5 —
  Mientras coordino la ejecución del Cypher en el host, **arranca de inmediato con R5 (Redacción del
  texto de error en Dead-Letter y Checkpoint)**. Recordemos la política acordada: **Drop + HMAC con
  clave + error_type + length**, preservando intacto el payload del chunk/reintento para el recovery."
  It follows the register item R5 ("Mitigation (target): bound/sanitize dead-letter `error_message`")
  and the merge of R2's PR #35 in the same message.
- **L2:** The harness safety policy blocked editing `.env.example` (the maintainer's file), so the
  template lines for the fingerprint comment were handed over instead of committed. R2's independent
  verification and the merge of PR #35 landed the same day (`main = d890243`), before this branch
  started. The initial full-suite run after wiring found 16 failures plus one mypy error in
  `neo4j_command_adapter.py` (a third `Checkpoint(...)` reader had been missed), four ruff
  import-order errors, and a test fake missing the new settings fields. These were fixed by
  extracting `infrastructure/checkpoint_error_metadata.py` as the single inverse mapping and
  updating the fake.
- **L3 (independent adversarial verification, read-only):** Six claims were checked and
  85 focused tests were observed passing. The review falsified two documentation claims:
  `_release_to_failed` is not a method (the writer is `_fail_chunk`), and S5's statement
  that new records omit the field was imprecise (the port serializer emits `null`; the
  legacy JSONL dict writer omits the key). It also found a **MEDIUM** raw-text sink outside
  the four declared R5 sinks: per-question failures are formatted as
  `f"{type(exc).__name__}: {exc}"` and collected in
  `application/evaluate_generation_layer_use_case.py`'s `EvaluationLayerResult.warnings`,
  written by `main.py --output`; `infrastructure/subprocess_ragas_runner.py` also adds
  exception text to `notes`, and the readiness gate re-emits the warnings. This remains a
  recorded next item, outside this change, rather than a silently widened mitigation.
  The **LOW** success-writer consistency gap (stale `error_length` and
  `error_fingerprint_*` could remain when a checkpoint becomes `PROCESSED`) was fixed in
  code follow-up `330f316`; the **LOW** populated-field reader mapping coverage gap (only
  a live-Neo4j test at review time) was also addressed there. A pre-existing **LOW** key
  quality question remains separate: `effective_token` accepts 32 invisible padding
  characters plus one printable character, despite its docstring overstating the length
  floor for that case. Changing the shared secret rule affects both the bearer token and
  the MCP log key, so that requires its own decision. The stale dead-letter shape in
  `docs/spec/01-resumable-indexing.md` and the two documentation claims above are corrected
  in this documentation follow-up.
