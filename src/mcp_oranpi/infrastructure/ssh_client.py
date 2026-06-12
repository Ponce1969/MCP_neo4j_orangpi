"""SSH connection management for MCP OranPi.

Provides persistent SSH connection to the OrangePi using asyncssh.
Implements the reconnection policy from spec 006:

- 3 reconnection attempts (0s, 5s, 15s)
- Background retry every 30s after 3 failures
- Tools return CONN_RECONNECTING errors during reconnection

Design decisions (Phase 2 corrections):
- SSHClient has NO knowledge of workspaces, paths, or cwd.
- CommandResult carries raw data only (exit_code, stdout, stderr).
- Timeouts propagate as asyncio.TimeoutError, NOT as CommandResult fields.
- Exit code interpretation belongs to the Application layer.
- structlog calls are synchronous (logger.info, not await logger.info).
- asyncio.Lock protects connection state transitions only, NOT command execution.
- CancelledError is always re-raised; SSH channel closed on cancel, NOT the connection.
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass

import asyncssh
import structlog

from mcp_oranpi.config import SSHConfig

log = structlog.get_logger()


# ── Connection State ──────────────────────────────────────────────────────────


class _ConnectionState(enum.StrEnum):
    """Internal SSH connection state machine.

    DISCONNECTED → CONNECTING → CONNECTED
                              ↘ RECONNECTING → CONNECTED
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


# ── Command Result ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class CommandResult:
    """Raw result of a remote command execution.

    Infrastructure delivers raw data. Application layer interprets
    exit codes and stderr for semantic error codes.
    """

    exit_code: int
    stdout: str
    stderr: str


# ── Reconnection Constants ────────────────────────────────────────────────────

# Attempt 1: immediate (0s), Attempt 2: 5s, Attempt 3: 15s
_RECONNECT_DELAYS: list[float] = [0.0, 5.0, 15.0]
_BACKGROUND_RETRY_INTERVAL: float = 30.0
_MAX_RECONNECT_ATTEMPTS: int = 3


# ── SSH Client ────────────────────────────────────────────────────────────────


class SSHClient:
    """Persistent SSH connection to the OrangePi.

    Manages a single long-lived SSH connection with automatic reconnection.
    Uses asyncio.Lock for connection state transitions only — command
    execution runs concurrently without lock protection.

    Usage::

        client = SSHClient(ssh_config)
        await client.connect()
        try:
            result = await client.execute("docker ps --format json")
        except TimeoutError:
            # Command timed out — application layer maps to CONN_TIMEOUT
            ...
        finally:
            await client.disconnect()
    """

    def __init__(self, config: SSHConfig) -> None:
        self._config = config
        self._conn: asyncssh.SSHClientConnection | None = None
        self._state: _ConnectionState = _ConnectionState.DISCONNECTED
        # Lock protects state transitions only (connect, disconnect, reconnect),
        # NOT command execution.
        self._state_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_attempts: int = 0

    @property
    def connected(self) -> bool:
        """Check if the SSH connection is established and usable."""
        return (
            self._state == _ConnectionState.CONNECTED
            and self._conn is not None
        )

    @property
    def state(self) -> _ConnectionState:
        """Current connection state (for diagnostics and testing)."""
        return self._state

    async def connect(self) -> None:
        """Establish SSH connection to the OrangePi.

        Raises:
            ConnectionError: If the connection cannot be established.
            OSError: If the host is unreachable.
            asyncssh.HostKeyNotVerifiable: If host key verification fails.
        """
        async with self._state_lock:
            if self._state == _ConnectionState.CONNECTED:
                log.warning("ssh_connect_already_connected", host=self._config.host)
                return
            self._state = _ConnectionState.CONNECTING

        try:
            conn = await self._create_connection()
            async with self._state_lock:
                self._conn = conn
                self._state = _ConnectionState.CONNECTED
                self._reconnect_attempts = 0
            log.info(
                "ssh_connected",
                host=self._config.host,
                port=self._config.port,
                user=self._config.username,
            )
        except asyncio.CancelledError:
            # CancelledError must always be re-raised (spec 006, rule 1).
            # Clean up state before re-raising.
            async with self._state_lock:
                self._state = _ConnectionState.DISCONNECTED
                self._conn = None
            raise
        except Exception:  # noqa: CONC001 — CancelledError is caught and re-raised above
            async with self._state_lock:
                self._state = _ConnectionState.DISCONNECTED
                self._conn = None
            log.error("ssh_connect_failed", host=self._config.host)
            raise

    async def disconnect(self) -> None:
        """Close the SSH connection gracefully.

        Cancels any background reconnection task and cleans up resources.
        Safe to call multiple times.
        """
        async with self._state_lock:
            # Cancel background reconnection if running
            if self._reconnect_task is not None:
                self._reconnect_task.cancel()
                self._reconnect_task = None

            if self._conn is not None:
                self._conn.close()
                self._conn = None

            self._state = _ConnectionState.DISCONNECTED

        log.info("ssh_disconnected", host=self._config.host)

    async def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> CommandResult:
        """Execute a command on the remote host.

        Args:
            command: A pre-validated command from ALLOWED_COMMANDS.
                     NEVER raw user input.
            timeout: Command timeout in seconds.
                     Uses config default (ORANPI_SSH_COMMAND_TIMEOUT) if None.

        Returns:
            CommandResult with raw exit_code, stdout, stderr.
            Exit code interpretation belongs to the Application layer.

        Raises:
            TimeoutError: If the command exceeds the timeout.
                                  Correction 2: timeouts are exceptions, not result fields.
            ConnectionError: If the client is not connected or in reconnecting state.
            asyncio.CancelledError: If the operation is cancelled.
                                    SSH channel is closed, connection preserved.
        """
        if not self.connected:
            if self._state == _ConnectionState.RECONNECTING:
                raise ConnectionError(
                    "SSH connection is reconnecting. "
                    "Retry after CONN_RECONNECTING error."
                )
            raise ConnectionError("SSH client is not connected")

        # self._conn is guaranteed non-None by the connected check above
        conn = self._conn
        assert conn is not None  # noqa: S101 — enforced by connected property
        timeout_seconds = timeout or self._config.command_timeout

        try:
            async with asyncio.timeout(timeout_seconds):
                result = await conn.run(command)

        except asyncio.CancelledError:
            # Correction: close SSH channel on cancel, NOT the connection.
            # The persistent SSH connection remains usable.
            log.info("command_cancelled", command=command[:100])
            raise

        except TimeoutError:
            log.warning(
                "command_timeout",
                command=command[:100],
                timeout=timeout_seconds,
            )
            raise

        return CommandResult(
            exit_code=result.exit_status if result.exit_status is not None else 0,
            stdout=str(result.stdout) if result.stdout is not None else "",
            stderr=str(result.stderr) if result.stderr is not None else "",
        )

    async def health_check(self) -> bool:
        """Execute a lightweight health check command on the remote host.

        Returns:
            True if the connection is healthy, False otherwise.
            Does not raise — all errors are caught and return False.
        """
        if not self.connected:
            return False

        try:
            result = await self.execute("echo OK", timeout=5)
            return result.exit_code == 0 and "OK" in result.stdout
        except (TimeoutError, ConnectionError, OSError):
            return False

    # ── Reconnection ───────────────────────────────────────────────────────

    async def start_reconnection(self) -> None:
        """Begin the reconnection sequence.

        Spawns a background task that attempts reconnection with
        exponential backoff (0s, 5s, 15s), then every 30s.
        During reconnection, execute() raises ConnectionError.
        """
        async with self._state_lock:
            if self._reconnect_task is not None:
                return  # Reconnection already in progress

            self._state = _ConnectionState.RECONNECTING
            if self._conn is not None:
                self._conn.close()
                self._conn = None

            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(),
                name="ssh-reconnect",
            )

        log.warning(
            "ssh_reconnect_started",
            host=self._config.host,
        )

    async def _reconnect_loop(self) -> None:
        """Background reconnection loop.

        Attempts reconnection with delays:
        - Attempt 1: immediate (0s)
        - Attempt 2: wait 5s
        - Attempt 3: wait 15s
        - After 3 failures: retry every 30s

        On success, transitions to CONNECTED and cancels itself.
        """
        for attempt, delay in enumerate(_RECONNECT_DELAYS, start=1):
            if delay > 0:
                await asyncio.sleep(delay)

            log.info(
                "ssh_reconnect_attempt",
                host=self._config.host,
                attempt=attempt,
                max_attempts=_MAX_RECONNECT_ATTEMPTS,
            )

            try:
                conn = await self._create_connection()
                async with self._state_lock:
                    self._conn = conn
                    self._state = _ConnectionState.CONNECTED
                    self._reconnect_attempts = attempt
                    # Clear the task reference since we succeeded
                    self._reconnect_task = None
                log.info(
                    "ssh_reconnected",
                    host=self._config.host,
                    attempt=attempt,
                )
                return
            except Exception as exc:
                log.warning(
                    "ssh_reconnect_failed",
                    host=self._config.host,
                    attempt=attempt,
                    error=str(exc),
                )

        # All initial attempts failed — background retry every 30s
        attempt_num = _MAX_RECONNECT_ATTEMPTS + 1
        while True:
            await asyncio.sleep(_BACKGROUND_RETRY_INTERVAL)

            log.info(
                "ssh_reconnect_background_attempt",
                host=self._config.host,
                attempt=attempt_num,
            )

            try:
                conn = await self._create_connection()
                async with self._state_lock:
                    self._conn = conn
                    self._state = _ConnectionState.CONNECTED
                    self._reconnect_attempts = attempt_num
                    self._reconnect_task = None
                log.info(
                    "ssh_reconnected_background",
                    host=self._config.host,
                    attempt=attempt_num,
                )
                return
            except Exception as exc:
                log.warning(
                    "ssh_reconnect_background_failed",
                    host=self._config.host,
                    attempt=attempt_num,
                    error=str(exc),
                )
                attempt_num += 1

    # ── Private ────────────────────────────────────────────────────────────

    async def _create_connection(self) -> asyncssh.SSHClientConnection:
        """Create a new asyncssh connection with current config.

        Applies host key verification based on security mode:
        - production: strict host key verification (default known_hosts)
        - development: accept new host keys
        """
        connect_kwargs: dict[str, object] = {
            "host": self._config.host,
            "port": self._config.port,
            "username": self._config.username,
            "client_keys": [str(self._config.key_path)],
        }

        # Host key verification based on security mode
        if self._config.security_mode == "development":
            # Development mode: accept new host keys
            # known_hosts=None disables strict host key verification.
            # A startup warning is logged by the server, not here.
            connect_kwargs["known_hosts"] = None
        else:
            # Production mode: strict host key verification
            if self._config.known_hosts is not None:
                connect_kwargs["known_hosts"] = str(self._config.known_hosts)
            # If known_hosts is not set, asyncssh defaults to ~/.ssh/known_hosts

        # Keepalive for connection health monitoring
        if self._config.keepalive_interval > 0:
            connect_kwargs["keepalive_interval"] = self._config.keepalive_interval

        async with asyncio.timeout(self._config.connect_timeout):
            return await asyncssh.connect(**connect_kwargs)