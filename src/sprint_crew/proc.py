"""Subprocess execution that a cancelled run can actually stop. See AGENTS.md §3.1.

``start_new_session=True`` puts the child in its own process group, because test runners
spawn their own children and signalling the direct pid alone orphans them. SIGTERM first,
then SIGKILL, so a runner can clean up its temporary state.

POSIX only (``os.killpg``); on Windows this would need a Job Object.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path

#: Seconds a process group gets to honour SIGTERM before it is SIGKILLed.
_TERM_GRACE_S = 5.0


@dataclass(frozen=True)
class ProcResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the child's process group, then SIGKILL anything still alive.

    Failures are suppressed: the process may exit between the check and the signal, and
    that must not mask the cancellation that got us here.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    with contextlib.suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_S)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_S)


async def _communicate(proc: asyncio.subprocess.Process, *, timeout: float | None) -> ProcResult:
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # A hung command is a failed step, not a crashed run — but the group still has to
        # die, or the "timeout" only stops us waiting for it.
        await _terminate_group(proc)
        return ProcResult(stdout="", stderr="", returncode=-1, timed_out=True)
    except asyncio.CancelledError:
        await _terminate_group(proc)
        raise
    return ProcResult(
        stdout=(stdout or b"").decode("utf-8", errors="replace"),
        stderr=(stderr or b"").decode("utf-8", errors="replace"),
        returncode=proc.returncode if proc.returncode is not None else -1,
    )


async def run_argv(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> ProcResult:
    """Run ``argv`` without a shell, killable by cancelling the awaiting task."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    return await _communicate(proc, timeout=timeout)
