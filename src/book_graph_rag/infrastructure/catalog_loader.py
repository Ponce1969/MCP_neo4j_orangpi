"""Load the versioned ``catalog.yaml`` into the domain ``Catalog`` model.

YAML parsing is an infrastructure concern: the domain ``Catalog`` model stays
pure (stdlib + pydantic). This module is the only place that touches ``yaml``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from book_graph_rag.domain.namespaces import Catalog


class CatalogLoadError(Exception):
    """Raised when the catalog file is missing, unparsable, or malformed."""


class CatalogLoader:
    """Load ``catalog.yaml`` into a validated domain ``Catalog``."""

    def __init__(self, catalog_path: Path) -> None:
        self._catalog_path = catalog_path

    def load(self) -> Catalog:
        try:
            text = self._catalog_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CatalogLoadError(f"Cannot read catalog {self._catalog_path}: {exc}") from exc

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise CatalogLoadError(f"Invalid YAML in {self._catalog_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise CatalogLoadError(f"Catalog {self._catalog_path} must be a YAML mapping")

        try:
            return Catalog.model_validate(data)
        except ValidationError as exc:
            raise CatalogLoadError(f"Malformed catalog {self._catalog_path}: {exc}") from exc
