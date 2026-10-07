#!/usr/bin/env python3
"""Minimal forbidden-pattern scanner for CI.

This is a lightweight check for the most critical patterns.
See vulnerability_scanner.py for the full 28-rule scanner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "build",
}

# Files that define forbidden patterns (they contain the patterns as data,
# not as executable code). These are the scanners themselves, not violations.
EXCLUDED_FILES = {
    "scripts/audit/forbidden_patterns.py",
    "scripts/vulnerability_scanner.py",
}

PYTHON_FILES = PROJECT_ROOT.rglob("*.py")


@dataclass(slots=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    description: str
    severity: str


RULES: list[Rule] = [
    Rule(
        name="subprocess_shell_true",
        pattern=re.compile(r"shell\s*=\s*True"),
        description="shell=True is forbidden",
        severity="CRITICAL",
    ),
    Rule(
        name="os_system",
        pattern=re.compile(r"os\.system\s*\("),
        description="os.system() is forbidden",
        severity="CRITICAL",
    ),
    Rule(
        name="subprocess_popen_string",
        pattern=re.compile(r"subprocess\.Popen\s*\(\s*[\"']"),
        description="subprocess.Popen with raw string command detected",
        severity="HIGH",
    ),
    Rule(
        name="dangerous_docker_rm",
        pattern=re.compile(r"docker\s+rm"),
        description="docker rm detected",
        severity="CRITICAL",
    ),
    Rule(
        name="dangerous_docker_stop",
        pattern=re.compile(r"docker\s+stop"),
        description="docker stop detected",
        severity="CRITICAL",
    ),
    Rule(
        name="dangerous_docker_kill",
        pattern=re.compile(r"docker\s+kill"),
        description="docker kill detected",
        severity="CRITICAL",
    ),
    Rule(
        name="dangerous_rm_rf",
        pattern=re.compile(r"rm\s+-rf"),
        description="rm -rf detected",
        severity="CRITICAL",
    ),
    Rule(
        name="raw_user_input_command",
        pattern=re.compile(r"run\s*\(\s*user_input"),
        description="raw user_input execution detected",
        severity="CRITICAL",
    ),
    Rule(
        name="raw_path_user_input",
        pattern=re.compile(r"Path\s*\(\s*user_input"),
        description="Path(user_input) detected",
        severity="HIGH",
    ),
]


def should_skip(path: Path) -> bool:
    """Skip excluded directories and files that define the patterns themselves."""
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return True
    # Skip files that define the scanner rules (they contain patterns as data)
    relative = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    if relative in EXCLUDED_FILES:
        return True
    # Skip test files — they test that validation rejects dangerous inputs
    return "test_" in path.name or path.name.startswith("test_")


def scan_file(path: Path) -> list[str]:
    violations: list[str] = []

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return violations

    for index, line in enumerate(content.splitlines(), start=1):
        for rule in RULES:
            if rule.pattern.search(line):
                violations.append(
                    f"[{rule.severity}] "
                    f"{rule.name} "
                    f"{path.relative_to(PROJECT_ROOT)}:{index}\n"
                    f"    -> {rule.description}\n"
                    f"    -> {line.strip()}"
                )

    return violations


def main() -> int:
    all_violations: list[str] = []

    for py_file in PYTHON_FILES:
        if should_skip(py_file):
            continue

        all_violations.extend(scan_file(py_file))

    if all_violations:
        print("\nFORBIDDEN PATTERNS DETECTED\n")  # noqa: T201

        for violation in all_violations:
            print(violation)  # noqa: T201
            print()  # noqa: T201

        return 1

    print("No forbidden patterns detected.")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
