# report-error-redaction — the same redaction rule for generated evaluation and gate reports

The original R5 work closed raw exception text in dead-letter and checkpoint sinks but left
exception-bearing evaluation and readiness warning/rationale strings unredacted. Before this
follow-up, values such as `f"{type(exc).__name__}: {exc}"`, `f"dataset load failed: {exc}"` and
`f"subprocess ragas failed: {exc}"` could reach `data/evaluation/*.json` via `--output` or be copied
into the readiness gate, exposing provider messages in shared artifacts. The work recorded here
closes those sinks, including the RAGAS subprocess added by adversarial verification.

## Specs

- **S1 — The same rule, every producer.** Verbatim from the task: **"Aplica la misma regla
  (`redacted_error`) en los 3 call sites identificados (`EvaluationLayerResult.warnings`, runner RAGAS
  y re-emisión del gate) para cerrar de forma homogénea esa misma clase de fuga de información en
  artefactos generados."** `format_redacted_error()` is **called**, never reimplemented, for each
  exception string persisted by a report:
  - `evaluate_generation_layer_use_case.py`: the per-question failure tuple (`:242`) and the
    dataset-load rationale (`:76`);
  - `evaluate_retrieval_layer_use_case.py`: the dataset-load rationale (`:107`) and the
    RAGAS-unavailable warning, which forwards the runner's notes (`:159`);
  - `evaluate_resolution_layer_use_case.py`: the dataset-load rationale (`:47`). The spec above
    also claimed a counterpart RAGAS path here; **it does not exist** — the verification asked for
    the code to be checked rather than bent, and the claim is corrected here instead of being
    satisfied by inventing a sink.
  - `infrastructure/subprocess_ragas_runner.py`: the JSON-parse failure and subprocess-failure
    paths format their exception notes (`:137` and `:195` respectively), which the layers forward.
  - `scripts/run_ragas_evaluation.py`: the per-question `error` written to
    `evaluation_results.jsonl` and forwarded into `--json-output` and `--after-output` is redacted;
    `failed_questions` derives from that same value.
  The readiness gate needs **no code change** — it copies `result.warnings` into its own report
  (`readiness_gate_evaluator_use_case.py:267-269`). The strengthened test drives a marker-carrying
  exception through the real generation producer and verifies the serialized gate result contains
  no marker while retaining the class, length, question id and fingerprint.
- **S2 — The report must stay useful.** What replaces the text is the exception class name, the
  character count of the message, and the keyed fingerprint; the **question id** stays for
  per-question failures. A warning that only said "a question failed" would trade a confidentiality
  risk for an operational one: the operator must still be able to tell which question failed, of what
  class, and whether two runs failed the same way (the fingerprint).
- **S3 — One rendering, one place.** The rendering lives next to the rule it renders
  (`domain/checkpoint_models.py`, beside `redacted_error`) and every exception-string producer calls
  `format_redacted_error`, so those sites share one format. The strings remain `str` and the report
  schema does not change: `warnings` stay `tuple[str, ...]`, so no consumer has to move.
- **S4 — No key means no fingerprint, never the text.** Same rule and same consequence as R5: a
  missing, weak or invisible fingerprint key yields a rendering with the class and the length and no
  fingerprint, and never the raw message. An evaluation run is not aborted for want of a logging key.
- **S5 — The register stops grading this MEDIUM.** At task inception,
  `docs/spec/07-security-and-operational-risks.md` §5's R5 residual read
  "**MEDIUM, recorded as the next item** … evaluation and readiness reports still persist raw
  exception text". With this change that claim is false, so the residual moves to LOW for the
  evaluation/gate sinks too and names the code paths and the tests. The section's
  `- **Residual risk:**` bullet, and §6's acceptance criterion, are the places to correct — the same
  discipline R5 followed.
- **S6 — Nothing else moves.** Metric-derived warnings keep their numbers (the "RAGAS drop in
  faithfulness vs baseline" detail, the retrieval precision-threshold warning), the rationale strings
  keep their shape, and no exception text is added anywhere. A remaining producer that interpolates an
  exception into a persisted string is a defect to report, not to leave for the next reader.

## Tasks

- **T1 · S1, S2, S3, S4** — the rendering beside the rule, and the producers wired to it, test-first:
  a fake exception whose message contains a distinctive marker must leave no trace of that marker in
  the serialized evaluation report or the gate's warnings, while the class, the length, the fingerprint
  and the question id are all present and the fingerprint equals the independently recomputed HMAC.
  Route: worker (bounded; the review budget is the point). `commit: 13d35ef`
- **T2 · S1, S6** — the sweep the verifier did by hand, made repeatable: every remaining interpolation
  of an exception into a generated artifact string in `src/` is either converted or named in the
  review as a follow-up. Route: worker. `commit: 13d35ef`
- **T3 · S5** — the register's residual and §5/§6 wording, then the PR. Route: parent.
  `commit: c7303aa`
- **T4 · Adversarial follow-up** — formatter hardening, subprocess sinks and end-to-end/test
  isolation fixes. Route: parent. `commit: 406724b`

## Log

- **L1 (the task, verbatim, 2026-10-16):** "### 2. Alcance adicional: Redacción en reportes de
  evaluación y gate — **Aprobado para una tarea/PR inmediata.** Aplica la misma regla (`redacted_error`)
  en los 3 call sites identificados (`EvaluationLayerResult.warnings`, runner RAGAS y re-emisión del
  gate) para cerrar de forma homogénea esa misma clase de fuga de información en artefactos generados.
  Trátalo como un PR atómico y separado (`type:fix` o `type:security`) para no contaminar el scope de
  R5." The three call sites are the ones the R5 verification surfaced (`path:line` evidence in the PR
  #36 comment), and R2's PR #35 plus R5's PR #36 were both merged before this branch started
  (`main = ce98853`, one later commit, also on `main`).
- **L2 (implementation, 2026-10-17).** RED observed before any wiring: the marker
  `provider said 401 for api-key=secret-marker-xyz` appeared in the serialized report and in the
  runner output (`6 failed, 51 passed`). GREEN after: `72 passed`, and the full suite
  **2154 passed, 3 skipped, 0 failed** (the two Docker-dependent property tests deselected: the
  daemon is not running here and both fail identically on the base commit). Gates: `ruff`,
  `ruff format`, `mypy --strict` over 466 files, the architecture validator. The sweep for a
  remaining persisted interpolation found **none** in `src/`; two adjacent hits are deliberately
  left alone and classified: `infrastructure/mcp/mcp_server_adapter.py:795,800,811` puts exception
  and per-chunk strings in the `search_rag` **response** (returned to the caller, never written to
  an artifact, and the persisted log keeps only the error code) and
  `infrastructure/text2cypher_adapter.py:228,240` feeds the repair prompt in memory so the model can
  fix its own Cypher. One spec claim was wrong: the resolution layer has no RAGAS path, so S1 was
  corrected rather than the code bent to match it.

- **L3 (adversarial verification and follow-up).** Six claims were checked; two were falsified:
  the RAGAS subprocess was an unredacted error sink, and the gate test did not prove the real
  producer-to-serialized-report path claimed by the document. The four findings were:
  (1) **HIGH, code:** `scripts/run_ragas_evaluation.py` could persist and forward raw per-question
  errors; an `evaluation_results.jsonl` was found on disk in the checkout, produced by a test that
  invoked the real script, while the script's sink is also production-reachable. (2) **Code:** the
  renderer emitted the exception class and key id verbatim, and `str(error)` could raise. (3) **Test:**
  the gate test established warning propagation, not redaction through the real generation producer.
  (4) **Documentation:** the report overstated producer coverage and test transitivity, omitted the
  subprocess and resolution-metrics artifacts, and §6 lacked the promised evaluation-sink
  acceptance line.

  The code fixes were: harden `format_redacted_error` (both labels printable-only,
  whitespace-collapsed and capped at 80 characters; the exception class name reduced further to
  identifier characters, because a class name is an identifier by construction — the parent's own
  probe showed the bound alone left a label that carried a secret readable; `UnknownError` for an
  empty class; guarded `str(error)`, yielding length 0 if it raises); redact the script's per-question error before `evaluation_results.jsonl`,
  `--json-output` and `--after-output`, passing `MCP_HMAC_KEY_ID`/`MCP_HMAC_KEY` in the environment
  and never argv; and isolate the runner's fixed-name artifact in a per-run temporary working
  directory, use absolute script/dataset and output paths, strengthen the real-producer gate test,
  and run each script `CliRunner` case in an isolated working directory. The standalone script still
  writes its local results file, now redacted. The parent's end-to-end check caught the incomplete
  first fix: the artifact reappeared in the checkout root until the script tests were isolated; after
  that fix the parent confirmed the root stayed clean. The documentation overstatement was fixed
  here only; the subprocess, renderer and test findings were fixed in code.

  RED evidence recorded in L2: `provider said 401 for api-key=secret-marker-xyz` appeared in the
  serialized report and runner output before wiring.
  Parent verification: **2159 passed, 3 skipped, 0 failed**; `ruff`, `ruff format`, `mypy --strict`
  over 466 files, and the architecture validator were clean.
