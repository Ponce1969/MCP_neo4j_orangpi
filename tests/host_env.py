"""Machine-independence helpers for configuration-reading tests.

The suite must give the same verdict on a laptop, in CI, and on a host that ships a real
``.env`` and a live database. ``Settings`` is a pydantic-settings ``BaseSettings``: on top
of its init data it reads, in priority order, process environment variables named after
the fields and a dotenv file resolved against the current working directory. Any test
that asserts a *default* (or drives the composition root with explicit values) must cut
those two ambient channels first — otherwise it asserts about the machine, not the code.

Two mechanisms live here, matching how the project already isolates config tests
(``tests/test_config.py``: empty working directory + deleted env vars):

* :func:`clear_ambient_settings_env` deletes every process env var that maps to a
  ``Settings`` field. Combined with a ``monkeypatch.chdir`` into an empty directory it
  makes a construction see only its explicit data — the pattern for tests that do not
  need the checkout's working directory.
* :func:`write_hostile_dotenv` materialises a dotenv shaped like the production host's,
  so a test can *construct the hostile environment that broke it* and prove its
  isolation instead of relying on a green run on a clean machine. Tests that must keep
  the checkout as their working directory (the composition-root tests read
  ``catalog.yaml`` relative to it) pass ``_env_file=None`` to the ``Settings``
  constructor, which disables the dotenv channel without moving the working directory.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings

#: A factory that builds ``Settings`` from explicit values only (see
#: ``tests/conftest.py:isolated_settings_factory``).
SettingsFactory = Callable[..., Settings]

#: A key shaped like the production one that broke the suite on the host (its ``.env``
#: carried a real ``mcp_hmac_key`` and the default-asserting test reported it). Long
#: enough to survive ``effective_token``'s 32-character floor, so tests that embed it
#: fail (or pass) for their own reasons and never because the ambient key happened to
#: be missing, short, or present.
HOSTILE_HMAC_KEY = "a4c9377220e3" + "hostile-dotenv-key-0123456789abcdef"

#: The per-test fingerprint key for MCP adapter tests. R5 makes the MCP refuse to log
#: without a real key, so an adapter built by a test carries this one explicitly: the
#: fingerprint path is exercised without any dependence on the machine's ambient
#: ``MCP_HMAC_KEY``.
TEST_HMAC_KEY = SecretStr("integration-hmac-key-0123456789abcdefghij")


def clear_ambient_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every process env var that maps to a ``Settings`` field.

    Field-driven, so a new setting cannot silently reintroduce the coupling: the host
    that broke the suite flipped ``skill_gate_enabled`` and ``mcp_hmac_key`` through
    variables this list did not know about yet.
    """
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


def write_hostile_dotenv(directory: Path, **values: str) -> Path:
    """Materialise a ``.env`` like the production host's, for tests that need one.

    ``Settings`` resolves its dotenv against the working directory, so a test that
    chdirs into ``directory`` reproduces exactly the contamination the host saw.
    """
    dotenv = directory / ".env"
    dotenv.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    return dotenv
