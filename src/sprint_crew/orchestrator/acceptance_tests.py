from __future__ import annotations

import shlex
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.exec_policy import (
    ALLOWED_COMMANDS,
    CommandPolicyError,
    check_command_allowed,
    resolve_argv,
    sandbox_env,
)
from sprint_crew.orchestrator.pytest_cmd import normalize_test_command
from sprint_crew.proc import run_argv

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
    """One allowlisted executable per command, no prose, no shell syntax (AGENTS.md §3.1)."""
    if not commands:
        raise AcceptanceTestsValidationError(["(empty list)"])

    invalid: list[str] = []
    for cmd in commands:
        stripped = cmd.strip()
        if not stripped:
            invalid.append("(empty command)")
            continue
        if check_command_allowed(stripped) is not None:
            invalid.append(stripped)
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
    """Run each acceptance command; returns combined output and whether all passed.

    Async so a cancelled run kills the child, exec-not-shell, and sandboxed env — see
    AGENTS.md §3.1. Revalidated here because this is what spawns the process, so a caller
    that skipped the plan-time gate fails closed.
    """
    validate_acceptance_tests(commands)
    timeout_s = get_settings().acceptance_test_timeout_s
    lines: list[str] = []
    all_passed = True
    for cmd in commands:
        # Validate the raw string first (above): normalize rewrites argv[0] into an
        # absolute .venv/bin/pytest path that no longer matches the allowlist.
        normalized = normalize_test_command(cmd, workspace_root)
        lines.append(f"$ {normalized}")
        try:
            result = await run_argv(
                resolve_argv(shlex.split(normalized)),
                cwd=workspace_root,
                env=sandbox_env(),
                timeout=timeout_s,
            )
        except (CommandPolicyError, FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            # A shell reported a missing interpreter as exit 127; exec raises instead. Still
            # a failed acceptance test, not a crashed run.
            all_passed = False
            lines.append(f"could not start command: {exc}")
            lines.append("")
            continue
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


def tool_log_shows_passing_acceptance(
    tool_log: list[dict], commands: list[str], workspace_root: Path
) -> bool:
    """True when the agent's own tool log contains a successful acceptance-test run.

    The orchestrator records every ``run_command`` with its ok flag, so a claim of green
    tests is checkable against what the agent actually observed. A model that reports
    ``tests_passed=True`` while its own last test run errored is not making a judgement
    call — it is stating something its tools contradict.
    """
    if not commands:
        return False
    wanted = {normalize_test_command(cmd.strip(), workspace_root) for cmd in commands}
    for entry in tool_log:
        if entry.get("tool") != "run_command" or not entry.get("ok"):
            continue
        raw = (entry.get("args") or {}).get("command")
        if not isinstance(raw, str) or not raw.strip():
            continue
        if normalize_test_command(raw.strip(), workspace_root) in wanted:
            return True
    return False
