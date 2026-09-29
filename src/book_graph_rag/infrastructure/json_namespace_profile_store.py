"""Local JSON artifact store for namespace routing profiles."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from book_graph_rag.domain.routing_models import NamespaceProfile
from book_graph_rag.ports.namespace_profile_store_port import NamespaceProfileStorePort


class NamespaceProfileStoreError(RuntimeError):
    """Raised when a profile artifact cannot be read or validated."""


class JsonNamespaceProfileStore(NamespaceProfileStorePort):
    """Store profiles as one deterministic JSON document on local disk.

    Writes are atomic (temp file in the same directory, then replace) so an
    interrupted build never leaves a truncated artifact on the Orange Pi.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def save_all(self, profiles: tuple[NamespaceProfile, ...]) -> None:
        """Persist ``profiles`` atomically and deterministically."""
        payload = json.dumps(
            [profile.model_dump(mode="json") for profile in profiles],
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise

    async def load_all(self) -> tuple[NamespaceProfile, ...]:
        """Return the persisted profiles, or an empty tuple when absent."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NamespaceProfileStoreError(f"invalid profile artifact JSON: {exc.msg}") from exc
        if not isinstance(data, list):
            raise NamespaceProfileStoreError("profile artifact must be a JSON list")
        try:
            return tuple(NamespaceProfile.model_validate(item) for item in data)
        except ValidationError as exc:
            raise NamespaceProfileStoreError("profile artifact failed validation") from exc
