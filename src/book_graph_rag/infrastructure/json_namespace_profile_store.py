"""Local JSON artifact store for namespace routing profiles.

The artifact carries the v2 profile shape (prototypes, ``k``,
``prototype_sources``, plus the passive diagnostic ``centroid``). Reads are
gated: an entry missing the ``prototypes`` field is a v1 artifact and is
rejected, prototype block invariants are enforced by the domain model with a
field-naming message, and optional ``expected_profile_version`` /
``expected_model_id`` arguments turn any version or model drift into a hard
failure instead of silently scoring the wrong geometry.
"""

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

    def __init__(
        self,
        path: Path,
        *,
        expected_profile_version: str | None = None,
        expected_model_id: str | None = None,
    ) -> None:
        self._path = path
        self._expected_profile_version = expected_profile_version
        self._expected_model_id = expected_model_id

    @property
    def path(self) -> Path:
        """Return the artifact path."""
        return self._path

    async def save_all(self, profiles: tuple[NamespaceProfile, ...]) -> None:
        """Persist ``profiles`` atomically and deterministically.

        Refuses to write a profile without a prototype block: the reader
        rejects such entries by design, so writing one would produce an
        artifact this very store cannot load.
        """
        for profile in profiles:
            if not profile.prototypes or profile.k < 1:
                raise NamespaceProfileStoreError(
                    f"refusing to write profile for {profile.namespace.source_id}: "
                    "prototypes must be non-empty with k >= 1 (artifact shape v2); "
                    "rebuild with scripts/build_namespace_profiles.py"
                )
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
            profiles = tuple(NamespaceProfile.model_validate(item) for item in data)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise NamespaceProfileStoreError(
                f"profile artifact failed validation: {details}"
            ) from exc
        # v1 shape gate (S4): validation alone would accept a legacy profile
        # (empty prototype block), so the raw field presence is checked here.
        for index, (item, profile) in enumerate(zip(data, profiles, strict=True)):
            if isinstance(item, dict) and "prototypes" not in item:
                raise NamespaceProfileStoreError(
                    f"profile artifact entry {index} ({profile.namespace.source_id}) is "
                    "missing field 'prototypes' — that is profile shape v1; expected "
                    "profile_version 2.0.0. Rebuild with scripts/build_namespace_profiles.py"
                )
        self._check_expectations(profiles)
        return profiles

    def _check_expectations(self, profiles: tuple[NamespaceProfile, ...]) -> None:
        """Reject artifacts built for a different version or model (S4)."""
        for profile in profiles:
            if (
                self._expected_profile_version is not None
                and profile.profile_version != self._expected_profile_version
            ):
                raise NamespaceProfileStoreError(
                    f"profile_version mismatch for {profile.namespace.source_id}: "
                    f"expected {self._expected_profile_version!r}, "
                    f"got {profile.profile_version!r}"
                )
            if self._expected_model_id is not None and profile.model_id != self._expected_model_id:
                raise NamespaceProfileStoreError(
                    f"model_id mismatch for {profile.namespace.source_id}: "
                    f"expected {self._expected_model_id!r}, got {profile.model_id!r}"
                )
