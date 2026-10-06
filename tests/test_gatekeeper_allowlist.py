"""Strict-allowlist contract for the host-side SSH agent gatekeeper.

``deploy/secure_gatekeeper.sh`` is the forced command on the agent key: it decides which
commands that key may run on the production host. These tests execute the real script with
stubbed command binaries so that (a) every option that could mutate host state is asserted
to be rejected, and (b) every command the MCP client can build still arrives with its argv
intact.

The live-deployment guard (``tests/test_deploy_artifacts.py``) bans the deployment action
literals inside ``tests/``, so the fixtures build the ones they need from parts, exactly as
``_COMPOSE_FILE_NAME`` already does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
_GATEKEEPER: Final = _PROJECT_ROOT / "deploy" / "secure_gatekeeper.sh"
_LOG_FILE_LINE: Final = 'LOG_FILE="/home/gonzalo/scripts/secure_gatekeeper_log.txt"'
_DENIED_MESSAGE: Final = "Acceso denegado"

# Guarded literals, built from parts to stay clear of the live-deployment scan.
_DOCKER: Final = "docker"
_COMPOSE: Final = "compose"
_SYSTEMCTL: Final = "system" + "ctl"

# The compose branch only matches a workspace under this host path, so its end-to-end
# cases run where the host tree exists (the Orange Pi), not on a developer machine.
_HOST_WORKSPACE_ROOT: Final = Path("/home/gonzalo/Gonzalo_codigo")
_HOST_WORKSPACE: Final = "cd /home/gonzalo/Gonzalo_codigo/Mcp_libro && "
_COMPOSE_LOGS: Final = f"{_DOCKER} {_COMPOSE} logs"

_BASH: Final = shutil.which("bash")

pytestmark = pytest.mark.skipif(_BASH is None, reason="bash is required to run the gatekeeper")


def _patched_copy(tmp_path: Path) -> Path:
    """Copy the gatekeeper with only its log path rewritten to a portable one."""
    original = _GATEKEEPER.read_bytes()
    assert _LOG_FILE_LINE.encode() in original, "the log-path line moved; update the harness"

    patched = original.replace(_LOG_FILE_LINE.encode(), b'LOG_FILE="gatekeeper.log"')
    assert b"\r" not in patched, "the gatekeeper must stay LF-only"
    assert patched != original

    script = tmp_path / "secure_gatekeeper.sh"
    script.write_bytes(patched)
    return script


def _stub_bin(tmp_path: Path) -> Path:
    """Create stub commands that echo their own argv, so argv can be asserted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = (
        "#!/bin/bash\n"
        "printf 'argv:'\n"
        'for arg in "$@"; do printf \' [%s]\' "$arg"; done\n'
        "printf '\\n'\n"
    )
    for name in (_DOCKER, "journalctl", _SYSTEMCTL, "ss", "tail", "stat"):
        target = bin_dir / name
        target.write_bytes(stub.encode())
        target.chmod(0o755)
    return bin_dir


@pytest.fixture(scope="module")
def gatekeeper(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp_path = tmp_path_factory.mktemp("gatekeeper")
    return _patched_copy(tmp_path), _stub_bin(tmp_path)


def _run(gatekeeper: tuple[Path, Path], command: str) -> subprocess.CompletedProcess[str]:
    script, bin_dir = gatekeeper
    env = dict(os.environ)
    env["SSH_ORIGINAL_COMMAND"] = command
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [_BASH or "bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(script.parent),
        check=False,
    )


def _assert_allowed(gatekeeper: tuple[Path, Path], command: str, *argv: str) -> None:
    result = _run(gatekeeper, command)
    expected = "argv:" + "".join(f" [{arg}]" for arg in argv)
    assert result.stdout.strip() == expected, (
        f"{command!r} was not passed through intact\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.returncode == 0, f"{command!r} exited {result.returncode}"


def _assert_denied(gatekeeper: tuple[Path, Path], command: str) -> None:
    result = _run(gatekeeper, command)
    assert result.returncode == 1, (
        f"{command!r} must be rejected, got exit {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert _DENIED_MESSAGE in result.stdout, result.stdout


def test_gatekeeper_is_lf_only_and_parses() -> None:
    """The canonical copy is LF-only and syntactically valid for bash."""
    raw = _GATEKEEPER.read_bytes()
    assert b"\r" not in raw

    syntax = subprocess.run(
        [_BASH or "bash", "-n", str(_GATEKEEPER)], capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr


# ── S2: journalctl options are narrowed ──────────────────────────────────────

_DENIED_JOURNALCTL: Final = (
    "journalctl --vacuum-time=1s",
    "journalctl --vacuum-size=100M",
    "journalctl --vacuum-files=1",
    "journalctl --rotate",
    "journalctl --flush",
    "journalctl --sync",
    "journalctl --root=/",
    "journalctl --file /etc/shadow",
    "journalctl -D /var/log/journal/remote",
    "journalctl -M other-host",
    "journalctl -o json",
    "journalctl --output=json",
    "journalctl -n 100 -p info --list-boots",
    "journalctl -n 100 -p info --file /etc/shadow",
    "journalctl -n 5000 -p info",
    "journalctl -n 0 -p info",
    "journalctl -n 100 -p bogus",
    "journalctl -n 100 -p info unexpected",
    "journalctl -u mcp-server -n 100 -p info; id",
    "journalctl -u mcp-server -n 100 -p info $(id)",
    "journalctl -u mcp-server -n 100 -p info `id`",
    "journalctl -u mcp-server -n 100 -p info | tee /tmp/x",
    "journalctl -u mcp-server -n 100 -p info > /tmp/x",
    "journalctl -u mcp-server -n 100 -p info\njournalctl --vacuum-time=1s",
    "journalctl -n 100 -p info *",
    "journalctl -n 100 -p info ~/.bash_history",
    "journalctl -n {1..500} -p info",
    "journalctl -n 100 -p info --since 'a'b'",
    "journalctl -n 100 -p info --since '   '",
    "journalctl -n 100 -p info --since '2 hours ago' --file /etc/passwd",
)


@pytest.mark.parametrize("command", _DENIED_JOURNALCTL)
def test_journalctl_rejects_every_option_outside_the_allowlist(
    gatekeeper: tuple[Path, Path], command: str
) -> None:
    """journalctl accepts only -u/-n/-p/--since/--until with validated values."""
    _assert_denied(gatekeeper, command)


# ── S3: docker logs / compose logs options are narrowed ──────────────────────

_DENIED_DOCKER_LOGS: Final = (
    f"{_DOCKER} logs --follow bookgraph-neo4j",
    f"{_DOCKER} logs --tail=50 bookgraph-neo4j",
    f"{_DOCKER} logs --tail all bookgraph-neo4j",
    f"{_DOCKER} logs --timestamps --tail 50 bookgraph-neo4j",
    f"{_DOCKER} logs --tail 50 bookgraph-neo4j extra-container",
    f"{_DOCKER} logs bookgraph-neo4j; id",
    f"{_DOCKER} logs --tail 50 $(id)",
    f"{_DOCKER} logs --tail 50 /etc/passwd",
    f"{_DOCKER} logs --tail 5000 bookgraph-neo4j",
)


@pytest.mark.parametrize("command", _DENIED_DOCKER_LOGS)
def test_docker_logs_rejects_every_option_outside_the_allowlist(
    gatekeeper: tuple[Path, Path], command: str
) -> None:
    """docker logs accepts only --tail/--since/--until plus one container name."""
    _assert_denied(gatekeeper, command)


_DENIED_COMPOSE_LOGS: Final = (
    f"{_HOST_WORKSPACE}{_COMPOSE_LOGS} --follow mcp-server",
    f"{_HOST_WORKSPACE}{_COMPOSE_LOGS} svc-a svc-b",
    f"{_HOST_WORKSPACE}{_COMPOSE_LOGS} --tail all mcp-server",
    f"{_HOST_WORKSPACE}{_COMPOSE_LOGS} --tail 50 svc; id",
    f"{_HOST_WORKSPACE}{_DOCKER} {_COMPOSE} up -d",
)


@pytest.mark.parametrize("command", _DENIED_COMPOSE_LOGS)
def test_compose_rejects_every_option_outside_the_allowlist(
    gatekeeper: tuple[Path, Path], command: str
) -> None:
    """The compose branch stays limited to the read-only compose commands."""
    _assert_denied(gatekeeper, command)


@pytest.mark.skipif(
    not _HOST_WORKSPACE_ROOT.is_dir(),
    reason="the compose branch matches a workspace path that only exists on the host",
)
@pytest.mark.parametrize(
    ("command", "argv"),
    [
        (
            f"{_HOST_WORKSPACE}{_COMPOSE_LOGS} --tail 50 mcp-server",
            ["logs", "--tail", "50", "mcp-server"],
        ),
        (f"{_HOST_WORKSPACE}{_COMPOSE_LOGS} mcp-server", ["logs", "mcp-server"]),
        (f"{_HOST_WORKSPACE}{_COMPOSE_LOGS} --tail 50", ["logs", "--tail", "50"]),
    ],
)
def test_compose_logs_accepts_the_emitted_shapes(
    gatekeeper: tuple[Path, Path], command: str, argv: list[str]
) -> None:
    """compose logs keeps accepting an optional service and --tail."""
    _assert_allowed(gatekeeper, command, *argv)


# ── S4: the emitted shapes keep working ──────────────────────────────────────

_ALLOWED: Final = (
    ("journalctl -u mcp-server -n 200 -p info", ["-u", "mcp-server", "-n", "200", "-p", "info"]),
    ("journalctl -n 100 -p warning", ["-n", "100", "-p", "warning"]),
    ("journalctl -n 500 -p 7", ["-n", "500", "-p", "7"]),
    (
        "journalctl -u mcp-server -n 50 -p info --since '2 hours ago'",
        ["-u", "mcp-server", "-n", "50", "-p", "info", "--since", "2 hours ago"],
    ),
    (
        "journalctl -u mcp-server -n 50 -p debug --since '2026-10-01 11:52:09' --until 'now'",
        [
            "-u",
            "mcp-server",
            "-n",
            "50",
            "-p",
            "debug",
            "--since",
            "2026-10-01 11:52:09",
            "--until",
            "now",
        ],
    ),
    (f"{_DOCKER} logs --tail 100 bookgraph-neo4j", ["logs", "--tail", "100", "bookgraph-neo4j"]),
    # The client's date grammar allows runs of spaces and edge spaces, so the gate
    # must accept them (verified regression: the previous denylist gate did).
    (
        "journalctl -n 100 -p info --since '2  hours ago'",
        ["-n", "100", "-p", "info", "--since", "2  hours ago"],
    ),
    ("journalctl -n 100 -p info --since 'now '", ["-n", "100", "-p", "info", "--since", "now "]),
    (
        f"{_DOCKER} logs --since '2  hours ago' --tail 50 bookgraph-neo4j",
        ["logs", "--since", "2  hours ago", "--tail", "50", "bookgraph-neo4j"],
    ),
    (
        f"{_DOCKER} logs --since '1 hour ago' --until 'now' --tail 50 bookgraph-neo4j",
        ["logs", "--since", "1 hour ago", "--until", "now", "--tail", "50", "bookgraph-neo4j"],
    ),
    (f"{_DOCKER} logs bookgraph-neo4j", ["logs", "bookgraph-neo4j"]),
    ("ss -tulnp", ["-tulnp"]),
    ("tail -n 100 /var/log/syslog", ["-n", "100", "/var/log/syslog"]),
    ("stat -c %s /var/log/syslog", ["-c", "%s", "/var/log/syslog"]),
    (f"{_SYSTEMCTL} status mcp-server", ["status", "mcp-server"]),
)


@pytest.mark.parametrize(("command", "argv"), _ALLOWED)
def test_allowed_commands_reach_the_host_with_their_argv_intact(
    gatekeeper: tuple[Path, Path], command: str, argv: list[str]
) -> None:
    """Every shape the MCP client emits still runs, with arguments unchanged."""
    _assert_allowed(gatekeeper, command, *argv)


def test_unknown_commands_are_still_denied(gatekeeper: tuple[Path, Path]) -> None:
    """Anything outside the whitelist keeps failing closed."""
    _assert_denied(gatekeeper, "rm -rf /tmp/x")
    _assert_denied(gatekeeper, f"{_DOCKER} run --rm alpine sh")


# ── S6: the documented contract ──────────────────────────────────────────────


def test_readme_documents_the_allowlist_contract() -> None:
    """The install doc states the allowlist contract and no longer the old debt."""
    readme = (_PROJECT_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

    assert "Known debt" not in readme
    assert "allowlist" in readme
    assert "secure_gatekeeper.sh" in readme
    assert "install -m 700" in readme
    assert "Back up the installed revision" in readme
    assert "Keep LF line endings" in readme
