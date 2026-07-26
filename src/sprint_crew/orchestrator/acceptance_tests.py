from __future__ import annotations

import shlex
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.pytest_cmd import normalize_test_command
from sprint_crew.proc import run_shell
from sprint_crew.tools.run_command import ALLOWED_COMMANDS

# Prose tokens that must not appear as bare pytest arguments (not after -k/-m/etc.).
_PROSE_STOP_WORDS: frozenset[str] = frozenset(
    {
        "passes",
        "pass",
        "green",
        "succeeds",
        "succeed",
        "ok",
        "clean",
        "must",
        "should",
        "via",
        "user",
        "all",
        "tests",
        "test",
    }
)

# pytest flags that consume the next token as an argument.
_PYTEST_FLAGS_WITH_ARG: frozenset[str] = frozenset(
    {
        "-k",
        "-m",
        "--maxfail",
        "--tb",
        "-W",
        "--durations",
        "--ignore",
        "--rootdir",
        "--cov",
        "--cov-report",
    }
)


class AcceptanceTestsValidationError(ValueError):
    """Raised when TaskPlan.acceptance_tests contains non-executable commands."""

    def __init__(self, invalid: list[str]) -> None:
        self.invalid = invalid
        joined = "; ".join(invalid)
        super().__init__(
            f"acceptance_tests must be allowlisted shell commands, not prose. Invalid: {joined}"
        )


def _looks_like_path(token: str) -> bool:
    if "/" in token or token.endswith((".py", ".js", ".ts", ".tsx", ".jsx")):
        return True
    return token.startswith("tests") or token.startswith("test_")


def _pytest_has_prose_arguments(argv: list[str]) -> bool:
    expect_arg = False
    for token in argv[1:]:
        if expect_arg:
            expect_arg = False
            continue
        if token in _PYTEST_FLAGS_WITH_ARG:
            expect_arg = True
            continue
        if token.startswith("-") or _looks_like_path(token):
            continue
        if token.lower() in _PROSE_STOP_WORDS:
            return True
    return False


def validate_acceptance_tests(commands: list[str]) -> list[str]:
    """Ensure each command starts with an allowlisted executable and has no prose tokens."""
    if not commands:
        raise AcceptanceTestsValidationError(["(empty list)"])

    invalid: list[str] = []
    for cmd in commands:
        stripped = cmd.strip()
        if not stripped:
            invalid.append("(empty command)")
            continue
        try:
            argv = shlex.split(stripped)
        except ValueError:
            invalid.append(stripped)
            continue
        if not argv or argv[0] not in ALLOWED_COMMANDS:
            invalid.append(stripped)
            continue
        if argv[0] == "pytest" and _pytest_has_prose_arguments(argv):
            invalid.append(stripped)

    if invalid:
        raise AcceptanceTestsValidationError(invalid)
    return commands


async def run_acceptance_tests(workspace_root: Path, commands: list[str]) -> tuple[str, bool]:
    """Run each acceptance command, returning combined output and whether all passed.

    Async rather than sync-in-a-thread so a cancelled run actually kills the child. See
    sprint_crew.proc: a to_thread'd subprocess.run survives cancellation and keeps a pytest
    alive for up to ACCEPTANCE_TEST_TIMEOUT_S after the user pressed Stop.
    """
    timeout_s = get_settings().acceptance_test_timeout_s
    lines: list[str] = []
    all_passed = True
    for cmd in commands:
        normalized = normalize_test_command(cmd, workspace_root)
        lines.append(f"$ {normalized}")
        result = await run_shell(normalized, cwd=workspace_root, timeout=timeout_s)
        if result.timed_out:
            # A hung command is a failed acceptance test, not a crashed run.
            all_passed = False
            lines.append(f"timed out after {timeout_s:.0f}s")
            lines.append("")
            continue
        passed = result.returncode == 0
        all_passed = all_passed and passed
        lines.append(f"exit_code={result.returncode}")
        if result.stdout.strip():
            lines.append(result.stdout.strip()[-2000:])
        if result.stderr.strip():
            lines.append("stderr: " + result.stderr.strip()[-1000:])
        lines.append("")
    return "\n".join(lines), all_passed
