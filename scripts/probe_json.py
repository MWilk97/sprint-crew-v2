#!/usr/bin/env python3
"""Preflight C / C': structured JSON on Planner and Judge lanes."""

from __future__ import annotations

import argparse
import json
import os
import sys

from openai import OpenAI

from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.ticket import TaskPlan

LANES = {
    "planner": ("http://127.0.0.1:8002/v1", "qwen3-14b", TaskPlan),
    "judge": ("http://127.0.0.1:8003/v1", "gemma-4-12b", ReviewOutcome),
}

PLANNER_PROMPT = """Produce a TaskPlan JSON for ticket DEMO-1.
Summary: add hello() to greeter.py and test it.
Include steps, files_to_touch, acceptance_tests with pytest, and out_of_scope."""

JUDGE_PROMPT = """Produce a ReviewOutcome JSON for ticket DEMO-1.
passed=true, tests_passed=true, summary='Looks good', findings=[]."""


def probe(lane: str) -> bool:
    base_url, model, schema_cls = LANES[lane]
    client = OpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY", "local"))
    schema = schema_cls.model_json_schema()
    prompt = PLANNER_PROMPT if lane == "planner" else JUDGE_PROMPT

    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_cls.__name__,
                "schema": schema,
                "strict": True,
            },
        },
    )
    raw = resp.choices[0].message.content or ""
    if raw.strip().startswith("```"):
        print(f"C {lane}: FAIL — markdown fence in output")
        print(raw[:300])
        return False
    try:
        data = json.loads(raw)
        schema_cls.model_validate(data)
    except Exception as exc:
        print(f"C {lane}: FAIL — {exc}")
        print(raw[:500])
        return False
    print(f"C {lane}: PASS ({schema_cls.__name__})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=LANES, required=True)
    args = parser.parse_args()
    return 0 if probe(args.lane) else 1


if __name__ == "__main__":
    sys.exit(main())
