"""MCP OranPi server entry point.

This module provides the stdio-based MCP server lifecycle.
It is the entry point called by ``uv run mcp-oranpi``.

All logs go to stderr (per spec 006) so that stdout remains
exclusively for MCP JSON-RPC messages.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog to output JSON to stderr.

    Args:
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def main() -> None:
    """Start the MCP OranPi server.

    Phase 2: Configures structlog to stderr, loads config,
    and prepares SSH connection lifecycle.
    Full MCP tool registration and stdio serve come in Phase 5.
    """
    configure_logging()

    log = structlog.get_logger()
    log.info("mcp_oranpi_starting", version="0.1.0", phase="2")


if __name__ == "__main__":
    main()