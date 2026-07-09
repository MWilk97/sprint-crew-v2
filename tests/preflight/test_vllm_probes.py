from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


@pytest.mark.preflight
def test_probe_vllm_tools(skip_unless_preflight_live) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "probe_vllm_tools.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.preflight
def test_probe_vllm_tools_work(skip_unless_preflight_live) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "probe_vllm_tools.py"), "--lane", "work"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.preflight
def test_probe_json(skip_unless_preflight_live) -> None:
    for lane in ("work", "work-review"):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "probe_json.py"), "--lane", lane],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
        assert proc.returncode == 0, f"lane={lane}\n{proc.stdout}\n{proc.stderr}"


@pytest.mark.preflight
def test_probe_backlog_plan(skip_unless_preflight_live) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "probe_json.py"), "--lane", "backlog"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
