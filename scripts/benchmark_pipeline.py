#!/usr/bin/env python3
"""Run benchmark scenarios from benchmarks/scenarios.yaml against live vLLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.helpers.ac_targets import pytest_target_from_ac  # noqa: E402
from tests.helpers.cycle_assertions import (  # noqa: E402
    assert_cycle_passed,
    count_tool_call_events,
    pytest_passes,
)
from tests.helpers.session_metrics import planning_mode_from_session  # noqa: E402
from tests.helpers.vector_fixtures import apply_vector_overlay  # noqa: E402

from sprint_crew.orchestrator.session import create_and_run_cycle, prepare_workspace  # noqa: E402
from sprint_crew.schemas.ticket import JiraTicket  # noqa: E402


def _resolve_test_target(ticket: JiraTicket, session) -> str | None:
    target = pytest_target_from_ac(ticket.acceptance_criteria)
    if target:
        return target
    plan = session.task_plan
    if plan is not None:
        for cmd in plan.acceptance_tests:
            target = pytest_target_from_ac(cmd)
            if target:
                return target
    return None


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(data.get("scenarios", []))


async def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    ticket_data = scenario["ticket"]
    ticket = JiraTicket(
        key=ticket_data.get("key", "DEMO-1"),
        summary=ticket_data["summary"],
        description=ticket_data.get("description", ""),
        status="To Do",
        issue_type="Story",
        acceptance_criteria=ticket_data.get("acceptance_criteria", "pytest -q"),
    )
    fixture = ROOT / scenario["fixture"]
    overlay = scenario.get("fixture_overlay")
    if overlay:
        staging = Path(tempfile.mkdtemp(prefix="bench-fixture-"))
        shutil.copytree(fixture, staging, dirs_exist_ok=True)
        apply_vector_overlay(staging, overlay)
        source = staging
    else:
        source = fixture
    workspace = prepare_workspace(f"bench-{scenario['id']}-{uuid4().hex[:8]}", source=source)

    t0 = time.perf_counter()
    session = await create_and_run_cycle(
        ticket=ticket,
        workspace=workspace,
        use_real_ship=False,
    )
    elapsed = time.perf_counter() - t0

    test_target = _resolve_test_target(ticket, session)

    passed = True
    error = ""
    try:
        assert_cycle_passed(session, workspace=workspace, test_target=test_target)
    except AssertionError as exc:
        passed = False
        error = str(exc)

    return {
        "id": scenario["id"],
        "complexity": scenario.get("complexity", "unknown"),
        "passed": passed,
        "error": error,
        "elapsed_seconds": round(elapsed, 2),
        "attempt": session.attempt,
        "tool_call_events": count_tool_call_events(session),
        "pytest_green": pytest_passes(workspace, test_target=test_target)
        if workspace.exists()
        else False,
        "test_target": test_target,
        "planning_mode": planning_mode_from_session(session),
        "session_id": session.session_id,
        "status": session.status.value,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=ROOT / "benchmarks" / "scenarios.yaml",
    )
    parser.add_argument(
        "--ids",
        nargs="*",
        help="Run only these scenario ids (default: all)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results",
    )
    args = parser.parse_args()

    scenarios = load_scenarios(args.scenarios)
    if args.ids:
        scenarios = [s for s in scenarios if s["id"] in args.ids]
    if not scenarios:
        print("No scenarios to run")
        return 1

    results = []
    for scenario in scenarios:
        print(f"Running scenario: {scenario['id']}")
        results.append(await run_scenario(scenario))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.output_dir / f"{stamp}.json"
    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "scenarios_file": str(args.scenarios),
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {out_path}")

    passed = sum(1 for r in results if r["passed"])
    print(f"Summary: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
