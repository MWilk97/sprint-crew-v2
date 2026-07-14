#!/usr/bin/env python3
"""Aggregate benchmark JSON reports from benchmarks/results/ into a scorecard."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "benchmarks" / "results"


def _load_reports(results_dir: Path) -> list[dict]:
    reports: list[dict] = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            data["_source_file"] = path.name
            reports.append(data)
    return reports


def _tier_of(report: dict) -> str:
    tier = report.get("tier")
    if isinstance(tier, str) and tier:
        return tier
    name = report.get("_source_file", "")
    if name.startswith("capability_"):
        return "capability"
    if name.startswith("from_prompt_integration"):
        return "integration"
    if (
        name.startswith("trap_")
        or name.startswith("from_prompt_vector_trap")
        or name.startswith("from_prompt_vector_e2e_")
    ):
        return "trap"
    if name.startswith("vector_ab"):
        return "ab"
    return "unknown"


def _passed(report: dict) -> bool:
    tier = _tier_of(report)
    failure = report.get("failure")

    if tier == "trap":
        # SOFT tier: report-only unless failure is absent
        return failure in (None, "")

    if tier == "integration":
        if "pipeline_ok" in report:
            if failure:
                return False
            pipeline_ok = bool(report.get("pipeline_ok"))
            post_check_ok = report.get("post_check_ok")
            return pipeline_ok and post_check_ok is not False
        if failure:
            return False
        return report.get("backlog_status") == "completed"

    if failure:
        return False

    if tier == "capability":
        return report.get("session_status") == "awaiting_human"

    if tier == "ab":
        return bool(report.get("merge_gate_ok"))

    if report.get("backlog_status") == "completed":
        return True
    if report.get("merge_gate_ok") is True:
        return True
    if report.get("session_status") == "awaiting_human":
        return True
    if report.get("collect_exit_code") not in (None, 0):
        return False
    return failure in (None, "")


def _pipeline_only_ok(report: dict) -> bool:
    if _tier_of(report) != "integration":
        return False
    return bool(report.get("pipeline_ok")) and report.get("post_check_ok") is False


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 2)


def build_scorecard(reports: list[dict]) -> dict:
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for report in reports:
        by_tier[_tier_of(report)].append(report)

    tiers: dict[str, dict] = {}
    for tier, items in sorted(by_tier.items()):
        passed = sum(1 for r in items if _passed(r))
        durations = [
            float(r["duration_s"]) for r in items if isinstance(r.get("duration_s"), (int, float))
        ]
        semantic = [
            float(r["semantic_tool_calls"])
            for r in items
            if isinstance(r.get("semantic_tool_calls"), (int, float))
        ]
        tools = [
            float(r["total_tool_calls"])
            for r in items
            if isinstance(r.get("total_tool_calls"), (int, float))
        ]
        failure_classes = Counter(
            str(r.get("failure_class") or "unknown") for r in items if not _passed(r)
        )
        pipeline_only = sum(1 for r in items if _pipeline_only_ok(r))
        tiers[tier] = {
            "count": len(items),
            "passed": passed,
            "pass_rate": round(passed / len(items), 3) if items else 0.0,
            "pipeline_only_ok": pipeline_only if tier == "integration" else None,
            "median_duration_s": _median(durations),
            "median_semantic_tool_calls": _median(semantic),
            "median_total_tool_calls": _median(tools),
            "failure_histogram": dict(failure_classes.most_common(10)),
            "latest_files": [r.get("_source_file") for r in items[-5:]],
        }

    return {
        "report_count": len(reports),
        "tiers": tiers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate agent benchmark JSON reports")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS_DIR,
        help="Directory with capability_*, trap_*, from_prompt_* JSON files",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON scorecard")
    args = parser.parse_args()

    reports = _load_reports(args.results_dir)
    scorecard = build_scorecard(reports)

    if args.json:
        print(json.dumps(scorecard, indent=2))
    else:
        print(f"Reports loaded: {scorecard['report_count']}")
        for tier, stats in scorecard["tiers"].items():
            print(
                f"  [{tier}] n={stats['count']} pass_rate={stats['pass_rate']} "
                f"median_duration_s={stats['median_duration_s']}"
            )
            if stats["failure_histogram"]:
                print(f"    failures: {stats['failure_histogram']}")
            if stats.get("pipeline_only_ok"):
                print(f"    pipeline_only_ok (post-check fail): {stats['pipeline_only_ok']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
