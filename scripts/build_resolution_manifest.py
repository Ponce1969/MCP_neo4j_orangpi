"""Generate the resolution dataset manifest from pairs.yaml.

The manifest records pair counts per stratum and a SHA-256 of the pairs file so
that any drift is caught at load time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

from book_graph_rag.domain.dataset_models import DatasetManifest, LabeledPair


def build_manifest(pairs_path: Path, manifest_path: Path | None = None) -> DatasetManifest:
    """Compute counts and SHA-256 for pairs.yaml and return a DatasetManifest.

    If ``manifest_path`` is provided, write the manifest as JSON.  Refuses to
    overwrite an existing manifest unless ``--force`` is used (handled by the
    CLI layer).
    """
    content = pairs_path.read_bytes()
    raw_pairs = yaml.safe_load(content)
    if raw_pairs is None:
        raw_pairs = []
    pairs = [LabeledPair.model_validate(item) for item in raw_pairs]

    same_count = sum(1 for p in pairs if p.label.value == "same")
    different_count = len(pairs) - same_count
    hard_count = sum(1 for p in pairs if p.hard)
    multilingual_count = sum(1 for p in pairs if p.multilingual)

    manifest = DatasetManifest(
        pairs_file_sha256=hashlib.sha256(content).hexdigest(),
        pair_count=len(pairs),
        same_count=same_count,
        different_count=different_count,
        hard_count=hard_count,
        multilingual_count=multilingual_count,
    )

    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )

    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build resolution dataset manifest")
    parser.add_argument(
        "--pairs",
        type=Path,
        default=Path("tests/fixtures/resolution/pairs.yaml"),
        help="Path to pairs.yaml",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/fixtures/resolution/manifest.json"),
        help="Path to write manifest.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing manifest even if it differs",
    )
    args = parser.parse_args(argv)

    if not args.pairs.exists():
        print(f"pairs file not found: {args.pairs}", file=sys.stderr)
        return 1

    if args.manifest.exists() and not args.force:
        existing = DatasetManifest.model_validate_json(args.manifest.read_text())
        current_content = args.pairs.read_bytes()
        if existing.pairs_file_sha256 != hashlib.sha256(current_content).hexdigest():
            print(
                "manifest drift detected: regenerate with --force to overwrite",
                file=sys.stderr,
            )
            return 2

    build_manifest(args.pairs, args.manifest)
    print(f"manifest written to {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
