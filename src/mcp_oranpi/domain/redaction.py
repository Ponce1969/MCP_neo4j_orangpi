"""Secret redaction for MCP OranPi tool output.

Applies pattern-based redaction to ALL text output before it leaves the MCP.
This is the security layer that prevents credential leaks from:

- Docker inspect Env (API keys, tokens, passwords, secrets)
- Cloudflare tunnel tokens and credentials
- Log files containing tokens
- Private keys
- URLs with embedded credentials

Design decisions:
- Redaction happens at the presentation layer, AFTER domain logic, BEFORE MCP output.
- This means domain code never sees redacted data — it works with real data.
- Only the agent sees redacted output.
- Pattern-based, not context-based: we don't parse semantics, we mask known patterns.
- Configurable via SENSITIVE_ENV_KEYWORDS for env var name matching.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ── Constants ──────────────────────────────────────────────────────────────────

REDACTED = "***REDACTED***"

# Environment variable names that indicate secret values.
# Matched case-insensitively against the part BEFORE the = sign.
SENSITIVE_ENV_KEYWORDS: tuple[str, ...] = (
    "PASSWORD",
    "PASSWD",
    "PASS",
    "SECRET",
    "TOKEN",
    "KEY",
    "CREDENTIAL",
    "AUTH",
    "API_KEY",
    "APIKEY",
    "PRIVATE",
    "CERT",
    "CLIENT_SECRET",
    "ACCESS_KEY",
    "ACCESS_SECRET",
    "CONNECTION_STRING",
    "DATABASE_URL",
    "DB_PASSWORD",
    "JWT_SECRET",
    "JWT_KEY",
    "MELI_CLIENT_SECRET",
    "MELI_ACCESS_TOKEN",
    "DEMETER_SECRET",
)

# Regex patterns for secret-like values in free text.
# Each pattern is (regex, group_to_redact).
# Ordered from most specific to least specific.
_SECRET_TEXT_PATTERNS: list[tuple[str, int]] = [
    # Private key blocks
    (
        r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"
        r"[\s\S]*?"
        r"-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
        0,
    ),
    # Cloudflare tunnel token (long hex string after "tunnel:" or in URLs)
    (r"tunnel[_-]?token[=:]\s*[a-fA-F0-9]{40,}", 0),
    # Generic long hex tokens (64+ chars, looks like a hash/key)
    # But NOT short hex like container IDs (12 chars)
    (r"(?<=[=:\"'\s])[a-fA-F0-9]{64,}(?=[\"'\s,\}]|$)", 0),
    # URL with credentials: protocol://user:password@host
    (r"(?<=://)([^:@\s]+):([^@\s]+)(?=@)", 0),
    # Bearer/token prefixes
    (r"(?:Bearer|bearer)\s+[a-zA-Z0-9\-._~+/]+=*", 0),
    # API key prefixes with underscore: sk_xxx, pk_xxx, rk_xxx
    (r'(?<=["\'])(?:sk|pk|rk)_[a-zA-Z0-9]{20,}(?:["\'])', 0),
    # OpenAI-style keys: sk-proj-...
    (r"(?:sk-proj|sk-None|sk-live|sk-test)-[a-zA-Z0-9]{20,}", 0),
]

# Paths that should NEVER be readable via logs_file.
# These contain credentials, tokens, or private keys.
BLOCKED_LOG_PATHS: tuple[str, ...] = (
    ".cloudflared",
    ".cloudflare",
    ".ssh",
    ".gnupg",
    ".aws",
    ".config/gcloud",
    "credentials",
    ".env",  # noqa: CONF-002 — this is a blocked path name, not a config reference
    ".htpasswd",
    ".netrc",
    "shadow",
    "passwd",
)

# File extensions that are commonly credentials.
CREDENTIAL_EXTENSIONS: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)

# Compiled regex patterns (compiled once for performance).
_COMPILED_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(p, re.IGNORECASE), group)
    for p, group in _SECRET_TEXT_PATTERNS
]


# ── Text Redaction ────────────────────────────────────────────────────────────


def redact_text(text: str) -> str:
    """Redact known secret patterns from free-form text.

    Applies pattern-based redaction to catch:
    - Private keys
    - Long hex tokens
    - URLs with credentials
    - Bearer tokens
    - Common API key prefixes

    This is a BEST-EFFORT filter. It does NOT guarantee complete redaction
    of all possible secret formats. It catches the most common patterns.
    """
    if not text:
        return text

    result = text
    for _, (pattern, group) in enumerate(_COMPILED_PATTERNS):
        if group == 0:
            # Redact the entire match
            result = pattern.sub(REDACTED, result)
        else:
            # Redact only the specific group - for URL credentials
            # Capture group index for closure (avoids B023)
            captured_group = group

            def _make_redactor(g: int) -> Callable[[re.Match[str]], str]:
                def _redact_group(m: re.Match[str]) -> str:
                    start = m.start(g)
                    end = m.end(g)
                    prefix = m.group(0)[: start - m.start(0)]
                    suffix = m.group(0)[end - m.start(0) :]
                    return prefix + REDACTED + suffix
                return _redact_group

            result = pattern.sub(_make_redactor(captured_group), result)

    return result


# ── Environment Variable Redaction ────────────────────────────────────────────


def _is_sensitive_env_var(name: str) -> bool:
    """Check if an environment variable name indicates a secret value.

    Matches case-insensitively against SENSITIVE_ENV_KEYWORDS.
    A variable is sensitive if its name CONTAINS any keyword.
    Examples:
    - POSTGRES_PASSWORD -> True (contains PASSWORD)
    - MELI_CLIENT_SECRET -> True (contains SECRET)
    - JWT_SECRET_KEY -> True (contains SECRET and KEY)
    - PORT -> False (no keyword match)
    - HOST -> False (no keyword match)
    - PATH -> False (no keyword match, KEY is only a substring of PATH)
    """
    name_upper = name.upper()
    for keyword in SENSITIVE_ENV_KEYWORDS:
        # Use word boundary-ish check: keyword must appear as a
        # distinct part of the name, separated by _ or at start/end.
        # "PASS" should not match "PASSWORD" (it's redundant),
        # but "PASS" SHOULD match "DB_PASS" or "MY_PASS_VAR".
        # "KEY" should not match "MONKEY" but should match "API_KEY".
        # Check that keyword is at a boundary (_ separated)
        if keyword in name_upper and (
            name_upper.startswith(keyword + "_")
            or name_upper.endswith("_" + keyword)
            or ("_" + keyword + "_") in name_upper
            or name_upper == keyword
        ):
            return True
    return False


def redact_env_vars(env_list: list[str]) -> list[str]:
    """Redact values of sensitive environment variables.

    Takes a list of "NAME=VALUE" strings and returns the same list
    with sensitive values replaced by REDACTED.

    A variable is sensitive if its name matches any of the
    SENSITIVE_ENV_KEYWORDS (case-insensitive, underscore-delimited).
    """
    result: list[str] = list()
    for entry in env_list:
        if "=" not in entry:
            result.append(entry)
            continue

        name, _, value = entry.partition("=")
        if _is_sensitive_env_var(name):
            result.append(f"{name}={REDACTED}")
        else:
            result.append(entry)

    return result


def redact_env_vars_in_json_dict(env_dict: dict[str, str]) -> dict[str, str]:
    """Redact values of sensitive keys in a JSON environment dict.

    Takes a dict like {"POSTGRES_PASSWORD": "abc123"} and returns
    {"POSTGRES_PASSWORD": "***REDACTED***"} for sensitive keys.
    """
    result = dict()
    for key, value in env_dict.items():
        if _is_sensitive_env_var(key):
            result[key] = REDACTED
        else:
            result[key] = value
    return result


# ── Path Blocking ──────────────────────────────────────────────────────────────


def is_log_path_blocked(path: str) -> tuple[bool, str]:
    """Check if a log file path should be blocked from reading.

    Returns (blocked: bool, reason: str).
    If blocked, the reason explains why. If not blocked, reason is empty.

    Blocks paths that:
    - Contain credential directory names (.cloudflared, .ssh, .aws, etc.)
    - Have credential file extensions (.pem, .key, .p12, etc.)
    - Contain sensitive filenames (credentials, .env, .htpasswd, etc.)
    """
    # Normalize path separators
    normalized = path.replace("\\", "/").lower()

    # Check for blocked directory names in the path
    for blocked_dir in BLOCKED_LOG_PATHS:
        # Match as a directory component (with / before/after or at start)
        dir_pattern = f"/{blocked_dir.lower()}/"
        if dir_pattern in f"/{normalized}/":
            return True, f"Path contains restricted directory: {blocked_dir}"

        # Also match if the path ends with the blocked dir
        if normalized.endswith("/" + blocked_dir.lower()) or normalized == blocked_dir.lower():
            return True, f"Path is a restricted directory: {blocked_dir}"

    # Check for credential file extensions
    for ext in CREDENTIAL_EXTENSIONS:
        if normalized.endswith(ext):
            return True, f"File has credential extension: {ext}"

    # Check for sensitive filenames
    path_parts = normalized.split("/")
    filename = path_parts[-1] if path_parts else normalized
    for blocked_name in BLOCKED_LOG_PATHS:
        if filename == blocked_name.lower():
            return True, f"File has restricted name: {blocked_name}"

    # Block any JSON files inside .cloudflared or similar dirs
    # Even though .json is not in CREDENTIAL_EXTENSIONS,
    # credential JSON files inside these directories are sensitive.
    for blocked_dir in (".cloudflared", ".cloudflare", ".aws", ".ssh"):
        if f"/{blocked_dir}/" in f"/{normalized}/":
            return True, f"Path inside restricted directory: {blocked_dir}"

    return False, ""


# ── JSON Output Redaction ─────────────────────────────────────────────────────


JsonType = str | int | float | bool | None


def redact_json_response(
    data: dict[str, object] | list[object] | JsonType,
) -> dict[str, object] | list[object] | JsonType:
    """Recursively redact known secret patterns in JSON-serializable data.

    Walks the data structure and applies redaction to all string values.
    Does NOT modify the structure — returns a new structure with redacted strings.

    Special handling:
    - Dict keys named "Env" or "env" with list values: redact the env var entries
    - String values: apply text pattern redaction
    - Nested structures: recurse
    """
    if data is None or isinstance(data, (int, float, bool)):
        return data

    if isinstance(data, str):
        return redact_text(data)

    if isinstance(data, list):
        return [redact_json_response(item) for item in data]  # type: ignore[arg-type]

    if isinstance(data, dict):
        result: dict[str, object] = dict()
        for key, value in data.items():
            # Special case: environment variable lists
            if key in ("Env", "env", "environment") and isinstance(value, list):
                # Check if list items look like NAME=VALUE strings
                if value and isinstance(value[0], str) and "=" in value[0]:
                    result[key] = redact_env_vars(value)
                    continue
                # Also handle dicts with secret keys
                if value and isinstance(value[0], dict):
                    result[key] = [redact_json_response(v) for v in value]
                    continue

            # Special case: dicts that might be env var maps
            if key in ("Env", "env", "environment") and isinstance(value, dict):
                result[key] = redact_env_vars_in_json_dict(value)
                continue

            result[key] = redact_json_response(value)  # type: ignore[arg-type]
        return result

    return data