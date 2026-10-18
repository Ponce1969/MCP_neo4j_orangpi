"""Shared fixtures for the suite.

``tests/conftest.py`` is loaded before any test module (it is part of the ``tests``
package), which is what puts the checkout root on ``sys.path``; the
``isolated_settings_factory`` fixture is the ready-made form of the isolation in
``tests/host_env.py`` for tests that only need to build ``Settings``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from tests.host_env import SettingsFactory, clear_ambient_settings_env


@pytest.fixture
def isolated_settings_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SettingsFactory:
    """Build ``Settings`` from explicit values only, whatever machine runs the test.

    Each call cuts both ambient channels before constructing: the working directory
    moves to an empty ``tmp_path`` (the checkout's ``.env`` is invisible from there)
    and every field-named env var is deleted. A test that passes a value explicitly
    keeps it; everything else observes the documented default.
    """

    def build(**overrides: Any) -> Settings:
        monkeypatch.chdir(tmp_path)
        clear_ambient_settings_env(monkeypatch)
        base: dict[str, Any] = {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "fake",
        }
        return Settings.model_validate({**base, **overrides})

    return build
