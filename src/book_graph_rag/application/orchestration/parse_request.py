"""Parse untrusted orchestration payloads into the domain request contract.

Lives in the application layer (not the domain module) because parsing a raw
wire ``Mapping`` is a boundary concern: the spec routes T1 as *"contract +
request parsing in the application layer, outside the MCP"*. The architecture
guard permits application → domain imports, so this stays a thin, pure
function with no infrastructure dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from book_graph_rag.domain.orchestration_models import OrchestrationRequest


def parse_orchestration_request(payload: Mapping[str, Any]) -> OrchestrationRequest:
    """Validate ``payload`` into an :class:`OrchestrationRequest`.

    Fails closed on typos: unknown keys raise pydantic's ``ValidationError``
    (a ``ValueError`` subclass) naming the offending key. Blank questions,
    unknown modes or intents, and empty or duplicate scopes are rejected the
    same way, with the offending field named in the message. Validation errors
    are never swallowed or rewritten.
    """
    return OrchestrationRequest.model_validate(dict(payload))
