"""Helper for the two cancellation tests that assert a child dies with its run.

Both need the same shape: a command whose process *tree* is deeper than one level, so a
kill that signals only the direct pid leaves something alive to observe. Acceptance
commands used to get a shell (``sleep N; touch marker``) which provided that fan-out for
free; they no longer do, so the spawner is written in Python instead.
"""

from __future__ import annotations

from pathlib import Path


def write_grandchild_spawner(script_path: Path, *, lifetime_s: float) -> Path:
    """Write a script that spawns a grandchild which sleeps, then touches ``sys.argv[1]``.

    Invoke as ``python <script_path> <marker_path>``. If the process group is killed the
    marker never appears; if only the direct child is signalled, the grandchild survives
    and creates it.
    """
    script_path.write_text(
        "import subprocess, sys\n"
        "grandchild = (\n"
        "    'import pathlib, sys, time; '\n"
        f"    'time.sleep({lifetime_s}); '\n"
        "    'pathlib.Path(sys.argv[1]).touch()'\n"
        ")\n"
        "subprocess.run([sys.executable, '-c', grandchild, sys.argv[1]])\n",
        encoding="utf-8",
    )
    return script_path
