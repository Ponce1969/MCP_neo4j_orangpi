"""Read-only risk probe for the optional cross-namespace debt review.

Categorizes the remaining merged groups (concept/component/pattern/risk/llmops)
into 'generic' (single-word names, theoretical homonymy risk) vs 'specific'
(multi-word or hyphenated names) so the maintainer can judge whether any
generic concept lost semantic nuance after the phase-2 cross merge.
"""

from __future__ import annotations

import json
from collections import Counter

GROUPS = "/tmp/cross_groups_rest.json"


def main() -> None:
    with open(GROUPS, encoding="utf-8") as f:
        groups = json.load(f)
    generic = [x for x in groups if x["name"] and " " not in x["name"] and "-" not in x["name"]]
    specific = [x for x in groups if x not in generic]

    print(f"TOTAL: {len(groups)}")
    print(f"GENERICOS (1 palabra): {len(generic)} | ESPECIFICOS: {len(specific)}")
    print("GENERICOS por kind:", dict(Counter(x["kind"] for x in generic)))
    print()
    print("--- 30 primeros genericos (name, kind, libros) ---")
    for x in sorted(generic, key=lambda c: c["name"].lower())[:30]:
        print(f"  {x['name']!r:28s} [{x['kind']:9s}] {x['namespaces']}")
    print()
    print("--- muestra de especificos ---")
    for x in specific[:10]:
        print(f"  {x['name']!r:48s} [{x['kind']:9s}] {x['namespaces']}")


if __name__ == "__main__":
    main()
