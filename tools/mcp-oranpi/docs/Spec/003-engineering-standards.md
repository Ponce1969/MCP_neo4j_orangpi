# 003 - Engineering Standards

## Python Version

Required:

* Python 3.13+

---

# Dependency Management

Mandatory:

* UV

Forbidden:

* pip install
* requirements.txt
* pipenv
* poetry

Dependencies must be declared only in:

* pyproject.toml

Environment synchronization:

```bash
uv sync
```

---

# Typing Standards

Typing is STRICT.

Mandatory:

* mypy strict
* complete type annotations
* explicit return types

Forbidden:

* Any
* untyped defs
* partially typed APIs

---

# Linting

Mandatory:

* Ruff

The repository must remain Ruff-clean at all times.

---

# Async Standards

The system is async-first.

Mandatory:

* asyncio
* asyncssh
* async-compatible libraries

Forbidden:

* blocking IO inside async runtime
* sync subprocess execution in async flows

---

# Testing Standards

Mandatory:

* pytest
* pytest-asyncio

Requirements:

* isolated tests
* deterministic tests
* no flaky tests

---

# Logging Standards

Mandatory:

* structured logging
* contextual logs
* explicit errors

Forbidden:

* print debugging
* silent exception swallowing

---

# Error Handling

Errors must:

* be explicit
* be typed
* preserve context
* avoid silent failures

Forbidden:

```python
except Exception:
    pass
```

---

# Code Quality Philosophy

Priority order:

1. correctness
2. readability
3. maintainability
4. observability
5. performance

---

# Architecture Standards

Preferred:

* clean architecture
* domain-oriented structure
* explicit boundaries
* dependency inversion

Avoid:

* god objects
* tightly coupled services
* implicit shared state

---

# Configuration Standards

Configuration must:

* use environment variables
* use typed settings
* avoid hardcoded secrets

Mandatory:

* .env.example

Forbidden:

* committing secrets
* hardcoded credentials
