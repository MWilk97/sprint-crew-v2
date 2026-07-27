"""One policy for every externally-authored command this system executes.

Two call sites run strings the model wrote: the ``run_command`` tool and
``orchestrator/acceptance_tests``. They used to disagree about everything that matters —
the tool filtered the environment down to seven variables and refused a shell, while
acceptance commands went through ``shell=True`` with the full parent environment, tokens
included. Same untrusted source, opposite threat models.

Both now import from here, so the rules cannot drift apart again:

- ``ALLOWED_COMMANDS`` — argv[0] must be one of these, checked before anything runs.
- ``check_command_allowed`` — rejects shell *operators*. Neither path uses a shell any
  more, so an operator is inert at execution time; rejecting it keeps the contract honest
  (a plan entry is one command, not a pipeline) and gives the model a clear error instead
  of a silently-literal argument. Detection is by ``shlex`` in ``punctuation_chars`` mode
  rather than a substring scan, so a quoted argument that merely *contains* an operator —
  ``python -c "import os; print(x)"``, ``pytest -k "a or b"`` — is left alone.
- ``sandbox_env()`` — the passthrough allowlist. HF_TOKEN, JIRA_API_TOKEN, GITHUB_TOKEN
  and CONSOLE_API_TOKEN are in this process's environment; a subprocess the model asked
  for has no business seeing them.
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

#: Characters shlex treats as standalone operator tokens in ``punctuation_chars`` mode.
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
    """Unquoted shell operator tokens in ``command`` (``;``, ``|``, ``&&``, ``>``, ``(``…).

    Raises ``CommandPolicyError`` when the string cannot be tokenised at all (unbalanced
    quotes), since that is not something to guess at either.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise CommandPolicyError(f"could not parse command: {exc}") from exc
    return [t for t in tokens if t and set(t) <= _PUNCTUATION_CHARS]


def check_command_allowed(command: str) -> str | None:
    """Validate a raw command string. Returns an error message, or None when it is fine.

    Takes the *raw* string on purpose — it has to see operators before ``shlex.split``
    flattens them into ordinary tokens, and before ``normalize_test_command`` rewrites
    argv[0] into an absolute interpreter path that no longer matches the allowlist.
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

    ``create_subprocess_exec`` does its own PATH lookup, but not the ``python`` →
    ``python3`` fallback, and not against the sandboxed PATH — so resolving here is what
    makes ``python -m pytest`` behave the same for acceptance commands as it does for the
    ``run_command`` tool. An argv[0] that already contains a separator has been resolved
    by ``normalize_test_command`` and is passed through.
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
