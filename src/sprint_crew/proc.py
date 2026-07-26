"""Subprocess execution that a cancelled run can actually stop.

``asyncio.to_thread(subprocess.run, ...)`` frees the event loop but is not cancellable:
cancelling the awaiting task raises inside the coroutine while the worker thread keeps
running the child to completion. A run therefore reported ``cancelled`` within
CANCEL_GRACE_S while its pytest kept burning CPU for up to ACCEPTANCE_TEST_TIMEOUT_S —
possibly alongside the *next* run admitted into the single slot.

Two details make the kill actually work:

- ``start_new_session=True`` puts the child in its own process group. Acceptance commands
  run through a shell, so the process we spawn is ``sh`` and pytest is its *child*;
  signalling the shell's pid alone leaves pytest orphaned. We signal the group.
- SIGTERM first, then SIGKILL after a grace period, so a test runner gets the chance to
  clean up its own temporary state before being destroyed.

POSIX only (``os.killpg``). The deployment target is Linux/GX10; on Windows there is no
process-group equivalent and this module would need a Job Object instead.
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

    Every failure mode here is benign — the process may have exited between the check and
    the signal — so they are suppressed rather than masking the cancellation that got us
    here.
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


async def run_shell(command: str, *, cwd: Path, timeout: float | None = None) -> ProcResult:
    """Run ``command`` through a shell, killable by cancelling the awaiting task."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    return await _communicate(proc, timeout=timeout)


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
