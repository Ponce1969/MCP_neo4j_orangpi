# 009 - Tech Debt: Mypy and Tests Refactoring

## Context
During the pre-production audit (Judgment Day preparation), it was discovered that while the production code is clean and passes the vulnerability scanner, the tests and scripts are severely outdated regarding type definitions.

Mypy (`uv run mypy .`) reports 296 errors, primarily located in `tests/` and `scripts/`. 

## Root Cause
- Test mocks do not match current domain models (e.g., `CommandRunner` mock missing `set_response` method).
- Fixtures and test initializations are missing required arguments that were added to the typed models later (e.g., `AppConfig` missing `ssh_host`, `ssh_user`, `ssh_key_path`, and `root_workspace_dir`).

## Action Plan
- Refactor the test suite to comply with the updated strict typing rules.
- Update `CommandRunner` mocks to properly implement the expected interfaces.
- Update all instances of `AppConfig` initialization in tests to include the required arguments.
- Ensure `uv run mypy .` passes with 0 errors across the entire repository.

*Deferred for tomorrow as per human decision.*
