"""One policy for every model-authored command this system executes. See AGENTS.md §3.1.

Imported by both the ``run_command`` tool and ``orchestrator/acceptance_tests`` so their
rules cannot drift apart again.
"""

from __future__ import annotations

import os
import shlex
import shutil

ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "npm",
        "node",
        "ruff",
        "mypy",
        "pip",
        "uv",
    }
)

#: What shlex reports as standalone operator tokens in punctuation_chars mode.
_PUNCTUATION_CHARS: frozenset[str] = frozenset("();<>|&")

_ENV_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
    }
)


class CommandPolicyError(ValueError):
    """Raised when a command cannot be made safe to run."""


def sandbox_env() -> dict[str, str]:
    """The only environment a model-authored subprocess is allowed to inherit."""
    return {k: v for k, v in os.environ.items() if k in _ENV_PASSTHROUGH}


def shell_operators(command: str) -> list[str]:
    """Unquoted shell operator tokens (``;``, ``|``, ``&&``, ``>``, ``(``…).

    Unbalanced quotes raise rather than guess.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise CommandPolicyError(f"could not parse command: {exc}") from exc
    return [t for t in tokens if t and set(t) <= _PUNCTUATION_CHARS]


def check_command_allowed(command: str) -> str | None:
    """Validate a raw command. Returns an error message, or None when it is fine.

    Must see the *raw* string: `shlex.split` flattens operators into ordinary tokens.
    """
    stripped = command.strip()
    if not stripped:
        return "empty command"
    try:
        operators = shell_operators(stripped)
    except CommandPolicyError as exc:
        return str(exc)
    if operators:
        rendered = " ".join(repr(op) for op in sorted(set(operators)))
        return (
            f"command contains shell operators ({rendered}); commands run without a shell, "
            "so write one plain command per entry"
        )
    if "`" in stripped:
        return "command contains a backtick substitution; commands run without a shell"
    return None


def resolve_executable(argv0: str) -> str:
    """Resolve a bare command name to an absolute path on PATH."""
    if os.path.sep in argv0 or (os.path.altsep and os.path.altsep in argv0):
        raise CommandPolicyError(f"Executable must be a bare command name, got {argv0!r}.")
    resolved = shutil.which(argv0)
    if resolved is None and argv0 == "python":
        resolved = shutil.which("python3")
    if resolved is None:
        raise CommandPolicyError(f"Command not found on PATH: {argv0!r}.")
    return resolved


def resolve_argv(argv: list[str]) -> list[str]:
    """Resolve argv[0] to an absolute path, leaving an already-resolved one alone.

    `create_subprocess_exec` does its own PATH lookup but not the `python` → `python3`
    fallback, so both call sites resolve here instead.
    """
    if not argv:
        raise CommandPolicyError("Empty command.")
    if os.path.sep in argv[0] or (os.path.altsep and os.path.altsep in argv[0]):
        return argv
    return [resolve_executable(argv[0]), *argv[1:]]


def check_argv_allowlisted(argv: list[str]) -> None:
    """Raise unless argv[0] is an allowlisted command name."""
    if not argv:
        raise CommandPolicyError("Empty command.")
    if argv[0] not in ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(ALLOWED_COMMANDS))
        raise CommandPolicyError(f"Command {argv[0]!r} not allowlisted. Allowed: {allowed}.")
