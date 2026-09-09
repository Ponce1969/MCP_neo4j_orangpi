"""Load the versioned ``gates.yaml`` into the domain ``GatePolicy`` model.

YAML parsing is an infrastructure concern: the domain ``GatePolicy`` model stays
pure (stdlib + pydantic). This module mirrors ``catalog_loader.py``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from book_graph_rag.domain.gate_models import GatePolicy


class GatePolicyLoadError(Exception):
    """Raised when the gate policy file is missing, unparsable, or malformed."""


class GatePolicyLoader:
    """Load ``gates.yaml`` into a validated domain ``GatePolicy``."""

    def __init__(self, policy_path: Path) -> None:
        self._policy_path = policy_path

    def load(self) -> GatePolicy:
        try:
            text = self._policy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GatePolicyLoadError(
                f"Cannot read gate policy {self._policy_path}: {exc}"
            ) from exc

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise GatePolicyLoadError(
                f"Invalid YAML in {self._policy_path}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise GatePolicyLoadError(
                f"Gate policy {self._policy_path} must be a YAML mapping"
            )

        raw_gates = data.get("gates")
        if isinstance(raw_gates, list):
            data["gates"] = tuple(raw_gates)

        try:
            return GatePolicy.model_validate(data)
        except ValidationError as exc:
            raise GatePolicyLoadError(
                f"Malformed gate policy {self._policy_path}: {exc}"
            ) from exc
