"""Smoke tests for the namespace profile builder CLI."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from click.testing import CliRunner

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "build_namespace_profiles",
    _ROOT / "scripts/build_namespace_profiles.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_namespace_profiles = _MODULE.build_namespace_profiles


def test_cli_help_lists_options_and_exits_zero() -> None:
    runner = CliRunner()

    result = runner.invoke(build_namespace_profiles, ["--help"])

    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--graph-snapshot" in result.output


def test_cli_rejects_unknown_option() -> None:
    runner = CliRunner()

    result = runner.invoke(build_namespace_profiles, ["--nope"])

    assert result.exit_code != 0


def test_cli_module_has_build_entrypoint() -> None:
    assert callable(build_namespace_profiles)
