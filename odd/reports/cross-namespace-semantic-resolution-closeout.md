# Close-out — `cross-namespace-semantic-resolution`

- **Branch**: `feat/cross-namespace-semantic-resolution` (48 commits ahead of `main`, clean fast-forward)
- **Closed**: 2026-10-06
- **Feature document**: `odd/tasks/cross-namespace-semantic-resolution.md` (specs, tasks and the full log)
- **Production**: `bookgraph-neo4j` on the OrangePi (all mutations under the AGENTS.md §7.2 gate)

## 1. Why this feature existed

An earlier one-off script (`scripts-ops/resolve_cross_namespace.py`) merged entities that shared
a name across different books, bypassing the mandatory quarantine (`band=EXACT` + no S3 scoring).
302 merges landed that way (31% of the ledger at the time), and the merge path had no guard to
stop it happening again. On top of that, the audit was blind to the intra-namespace case-only
duplicates (R5b grouped on the exact name and found zero groups) and the review loop had no place
to record a human decision.

## 2. Before → after (measured; every number reproducible from the linked evidence)

| Metric | Before | After |
|---|---|---|
| Ledger entries | 958 (2026-10-03) | **1051** |
| Cross-namespace merges applied | 302 | **274** (30 pairs compensated: 29 entries, 1 partial) |
| Cross-namespace suspicious pairs | 194 (retro-audit, 2026-10-04) | **145** (272 stratified) |
| R5a — active cross-namespace duplicate groups | 456 at baseline; the 25 revivals alone pushed it to **477** | **456** (the `separate` decisions discount the revived pairs) |
| R5b — intra-namespace case-only groups | **0 (blind)** — the exact-name grouping found nothing | **64 found, then 64 merged → 0** |
| Active entities | 20492 (pre-T10) | **20428** |
| `merged_into` entities | 901 | **965** |
| MENTIONS / RELATED | 42890 / 42196 | 42890 / **42176** (the re-point collapses, modelled and verified) |
| Audit warnings / blocking | 520 / 0 (after the R5b fix) | **456 / 0** |
| Human decisions recorded | none existed | **50** (30 `separate`, 20 `keep`) |

Global audit `passed` (0 blocking), `expose-mcp` gate `passed`, self-loops `0`, ledger chain
verified with 0 hash inconsistencies.

## 3. What was built

- **`ledger rollback`** — read-only by default, §7.2 gated apply, direction inference from two
  rules (geometry, then the loser's provenance), a predicted edge census and a plan fingerprint
  that binds the reviewed plan to the mutation.
- **`ledger rollback --candidate`** (T8f) — partial reversal of a multi-candidate entry, with the
  non-selected candidates left merged; fail-closed on an overlapping request (a candidate already
  compensated) and on a folded-alias value shared with a non-selected candidate.
- **Candidate-aware retro-audit** (T8f.2) — `rolled_back` per candidate and a guard on the ORIGINAL
  population plus a compensation reference check (unknown target, canonical mismatch, unknown
  candidate, duplicate compensation).
- **Decision registry** (T9a) — `decisions record|list`, append-only
  `data/resolution/cross_namespace_decisions.jsonl`, latest-wins per entity; pre-commitments
  (unknown seq, non-candidate, compensating entry) refused before any append. Consumed by R5a
  (through one shared clause, also used by the enqueue path so both counts agree) and by the
  consolidated queue, which now discounts the decided keeps.
- **R5b case-insensitive grouping** (T9b) — `toLower(trim(name))`; measured equal to the Python
  `normalize_key` on this corpus (64 groups over 128 entities, both), so the Cypher grouping is a
  corpus-verified equivalent.
- **Intra-namespace merge batch** (T10) — `scripts-ops/resolve_intra_ns.py`: plan derived from the
  audit rule through an imported shared grouping expression, canonical chosen by **richness**
  (mentions → degree → shortest id → lexicographic), `--apply` requiring a fresh backup, an
  approval file and the reviewed fingerprint, and a census that **simulates the plan order** to
  predict every edge the re-point collapses or deletes.
- **Review surface** — `quarantine enqueue|list|render|approve|reject` with the namespace guard in
  `ApplyMergeUseCase`, plus the MCP Streamable HTTP transport and server self-description.

## 4. How it was applied (all under the §7.2 gate, all verified)

- **Lote 1 (T8c/T8d)**: 10 cross-namespace merges reversed (pilot 305/501, clean subset
  536/391/307/360/404, final 584/342/443).
- **Lote 2A**: 375/489/503/480 reversed, 413/475 kept.
- **Lote 2B**: 2B-A 9 reversed (492/455/423/583/532/491/347/537/451) and 2B-B the **first partial
  rollback** (508, one candidate reverted while its sibling stayed merged).
- **Lote 2C**: 2C 590/319 reversed + 403/512 kept; 2C-B 371/415 reversed + 355/332/551/526 kept;
  2C-C1 502 reversed + 323/350/382/543/544/431 kept.
- **T10**: the 64 intra-namespace case-only groups merged in three per-namespace batches
  (7 + 28 + 29), 0 failures.

Verification used the same battery every time: predicted vs measured census (with drift failing
the command), shared-neighbour check (0 on every reverted pair), ledger chain, scoped + global
audits, and `repoint_merged_endpoint_edges --apply` whenever a revival put an edge on a merged
endpoint.

## 5. Known limits (deliberate, documented, not debts left silent)

- **145 cross-namespace suspicious pairs remain, on purpose.** The tier sweep showed diminishing
  returns (median cosine 0.506; 96% of the pairs above 0.40 are `ambiguous`/`none_rest`) and the
  last three tiers produced 11 keeps against 6 reverts. The governance tooling is in place to
  intervene on demand.
- **`R5b = 0` does not mean "the graph is clean"**: it means no same-name duplicates remain inside
  a namespace. The cross-namespace rule (R5a) still reports 456 undecided groups.
- A folded alias VALUE shared by two candidates of one entry cannot be partially rolled back (the
  adapter removes aliases by value): such an entry can only be reverted whole.
- An `unknown` fallback restores both directions and creates one mirror; it needs explicit
  maintainer acceptance (accepted twice: 480 and 455).
- **Cosine orders, it does not decide**: 25 of the reviewed pairs with a low cosine turned out to
  be the same concept and were kept.
- The pre-commit hook stashes the tracked working tree, so commits were run with
  `pre-commit run --files` by hand and `--no-verify` (declared in each message).
- `ruff format --check .` reports 152 pre-existing files unrelated to this feature; the touched
  files are clean.

## 6. Evidence index (`evidence-bundles/`)

`cosine-all-pairs-20261004.json` · `batch1..batch2cc1-decision-sheets-*.txt` ·
`cross-namespace-decisions-20261005.jsonl` (50 decisions) · `lote2a-rollback-20261004.json` ·
`lote2b-plan-20261004.json` · `applied-cross-namespace-audit-consolidated-*.json` (after 2A, 508,
2B, 2C, 2C-B, 2C-C1, T9a) · `t10-lote1-plan-20261005.json` ·
`merged-endpoint-edges-*.json` (the four repoints) · `audit-after-t10-lote1-20261005.json` ·
`audit-after-t10-complete-20261006.json`.

Backups (OrangePi, `~/backups_neo4j/`): one fresh backup per apply — the last three are
`bookgraph_backup_20261005T154049Z.json`, `…T195449Z.json`, `…T20261006T000524Z.json`.
