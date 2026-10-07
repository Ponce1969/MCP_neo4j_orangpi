"""Resolución quirúrgica intra-namespace del libro 2 COMPLETO (GraphRAG agéntico).

Lee /tmp/resolve_redo_groups.json (generado por gen_resolve_redo_groups.py sobre el
grafo completo: 657 chunks, 136 grupos, canonical = id más corto) y fusiona los
duplicados lógicos del source knowledge:graphrag-agentic con ApplyMergeUseCase:
soft-delete merged_into + re-point MENTIONS/RELATED + fold aliases + ledger
chained con approver human:gonzalo. Solo toca este namespace.

Uso (AGENTS.md §7.2: backup -> dry-run -> aprobación -> apply):
  uv run python resolve_full_book2_intra.py --dry-run   # read-only: valida ids activos + resumen
  uv run python resolve_full_book2_intra.py --apply     # aplica los 136 merges, ledger seq 167+
"""

import argparse
import asyncio
import json
import sys
from typing import Any, cast

from neo4j import GraphDatabase

from book_graph_rag.application.resolve_entities_use_case import MergeGroup
from book_graph_rag.config import Settings
from book_graph_rag.domain.models import EntityType
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.infrastructure.resolution_wiring import build_apply_merge_use_case

NS = "knowledge:graphrag-agentic"
GROUPS_JSON = "/tmp/resolve_redo_groups.json"


def load_groups() -> list[dict[str, Any]]:
    with open(GROUPS_JSON, encoding="utf-8") as f:
        return cast(list[dict[str, Any]], json.load(f))


def build_group(entry: dict[str, Any]) -> MergeGroup:
    canonical_id: str = entry["canonical"]
    dup_ids: list[str] = entry["candidates"]
    kind: EntityType = entry["kind"]

    def _s0(entity_id: str) -> S0NormalizedForm:
        raw = entity_id.rsplit(":", 1)[-1].replace("-", " ")
        return S0NormalizedForm(
            original=raw,
            nfkc=raw,
            casefold=raw.lower(),
            compact="".join(raw.lower().split()),
            tokens=tuple(raw.lower().split()),
        )

    evidence = [
        ResolutionEvidence(
            anchor_id=canonical_id,
            candidate_id=d,
            anchor_type=kind,
            candidate_type=kind,
            anchor_namespace=NS,
            candidate_namespace=NS,
            anchor_normalized=_s0(canonical_id),
            candidate_normalized=_s0(d),
            s0_matched_field="id",
            band=ConfidenceBand.EXACT,
            cross_namespace=False,
            cross_type=False,
            composite_score=1.0,
        )
        for d in dup_ids
    ]
    return MergeGroup(
        canonical_id=canonical_id,
        duplicate_ids=dup_ids,
        band=ConfidenceBand.EXACT,
        evidence=evidence,
    )


def dry_run() -> int:
    groups = load_groups()
    all_ids = [x["canonical"] for x in groups] + [c for x in groups for c in x["candidates"]]
    settings = Settings.model_validate({})
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    active_ns = 0
    try:
        with driver.session() as s:
            active_ns_row = s.run(
                "MATCH (n:Entity) WHERE n.id STARTS WITH $ns "
                "AND (n.merged_into IS NULL OR n.merged_into = '') RETURN count(*) AS n",
                ns=f"{NS}:",
            ).single()
            assert active_ns_row is not None
            active_ns = active_ns_row["n"]
            rows = list(
                s.run(
                    "MATCH (n:Entity) WHERE n.id IN $ids RETURN n.id AS id, n.merged_into AS mi",
                    ids=all_ids,
                )
            )
    finally:
        driver.close()

    found = {r["id"]: r["mi"] for r in rows}
    missing = [i for i in all_ids if i not in found]
    inactive = [i for i in all_ids if found.get(i)]
    n_dups = sum(len(x["candidates"]) for x in groups)

    print(f"groups={len(groups)} canonical={len(groups)} duplicates={n_dups}")
    print(f"entities_active_namespace={active_ns} -> after={active_ns - n_dups}")
    print(
        f"ids_checked={len(all_ids)} found={len(found)} "
        f"missing={len(missing)} inactive={len(inactive)}"
    )
    if missing:
        print("MISSING(no existe):")
        for i in missing:
            print("  ", i)
    if inactive:
        print("INACTIVE(ya merged_into):")
        for i in inactive:
            print("  ", i)
    if missing or inactive:
        print("DRY_RUN BLOCKED")
        return 1
    print("DRY_RUN OK - listo para --apply")
    return 0


async def apply() -> int:
    groups = load_groups()
    settings = Settings.model_validate({})
    use_case, closables = await build_apply_merge_use_case(settings)
    results: list[tuple[str, int, int]] = []
    try:
        for entry in groups:
            group = build_group(entry)
            res = await use_case.apply(group, approver="human:gonzalo")
            results.append((group.canonical_id, res.seq, len(group.duplicate_ids)))
    finally:
        for closer in closables:
            await closer.close()
    for cid, seq, ndup in results:
        print(f"MERGE_OK seq={seq} canon={cid} dups={ndup}")
    print(f"APPLY_DONE groups={len(results)}")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read-only validation")
    mode.add_argument("--apply", action="store_true", help="apply merges (ledger)")
    args = parser.parse_args()

    if args.dry_run:
        return dry_run()
    return await apply()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
