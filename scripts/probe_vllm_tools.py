#!/usr/bin/env python3
"""Preflight A+B+C: vLLM must emit structured tool_calls (Coder or Work lane)."""

from __future__ import annotations

import argparse
import os
import sys

from openai import OpenAI

from sprint_crew.config import Role, lane_for_role

# Read lanes from models.yaml so a model swap does not strand the probes.
_CODER = lane_for_role(Role.CODING)
_WORK = lane_for_role(Role.WORK)

LANES = {
    "coder": (_CODER.base_url, _CODER.served_name),
    "work": (_WORK.base_url, _WORK.served_name),
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

MULTI_TOOLS = [
    READ_FILE_TOOL,
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List directory entries.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff patch.",
            "parameters": {
                "type": "object",
                "properties": {"patch": {"type": "string"}},
                "required": ["patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run an allowlisted shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show git diff.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show recent git log.",
            "parameters": {
                "type": "object",
                "properties": {"n": {"type": "integer"}},
            },
        },
    },
]

CODER_SYSTEM = """You are a coding agent. Use tools to inspect and modify the repo.
Always prefer tools over guessing. One tool call per turn when possible."""

TECHLEAD_READONLY_TOOLS = [
    READ_FILE_TOOL,
    MULTI_TOOLS[2],  # grep
    MULTI_TOOLS[3],  # list_directory
    MULTI_TOOLS[6],  # git_status
    MULTI_TOOLS[8],  # git_log
]

TECHLEAD_SYSTEM = """You are a Tech Lead planning agent. Use read-only tools to explore the repo.
Prefer list_directory, read_file, grep, and git_* before answering. One tool call per turn when possible."""


def client_for(lane: str) -> tuple[OpenAI, str]:
    base_url, model = LANES[lane]
    return OpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY", "local")), model


def probe_a(lane: str) -> bool:
    client, model = client_for(lane)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "read package.json"}],
        tools=[READ_FILE_TOOL],
        temperature=0,
    )
    msg = resp.choices[0].message
    calls = msg.tool_calls or []
    ok = bool(calls) and calls[0].function.name == "read_file"
    if lane == "coder":
        ok = ok and not (msg.content or "").strip()
    print(f"A tool_probe: {'PASS' if ok else 'FAIL'}")
    if calls:
        print(f"  tool_calls[0].name = {calls[0].function.name!r}")
    else:
        print(f"  content = {(msg.content or '')[:200]!r}")
    return ok


def probe_b(lane: str) -> bool:
    client, model = client_for(lane)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CODER_SYSTEM},
            {
                "role": "user",
                "content": (
                    "Fix the failing test in this repo. Start by listing the project root "
                    "and reading package.json and the test file."
                ),
            },
        ],
        tools=MULTI_TOOLS,
        temperature=0,
    )
    msg = resp.choices[0].message
    calls = msg.tool_calls or []
    ok = len(calls) >= 1
    print(f"B multi_tool_probe: {'PASS' if ok else 'FAIL'} ({len(calls)} tool_calls)")
    for i, call in enumerate(calls[:3]):
        print(f"  [{i}] {call.function.name}")
    return ok


def probe_c(lane: str) -> bool:
    """TechLead-like read-only exploration (Work lane)."""
    client, model = client_for(lane)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": TECHLEAD_SYSTEM},
            {
                "role": "user",
                "content": (
                    "Explore this repo for planning: list the project root, read README if present, "
                    "and grep for validate_email."
                ),
            },
        ],
        tools=TECHLEAD_READONLY_TOOLS,
        temperature=0,
    )
    msg = resp.choices[0].message
    calls = msg.tool_calls or []
    names = {call.function.name for call in calls}
    ok = len(calls) >= 1 and names & {
        "list_directory",
        "read_file",
        "grep",
        "git_status",
        "git_log",
    }
    print(f"C techlead_probe: {'PASS' if ok else 'FAIL'} ({len(calls)} tool_calls)")
    for i, call in enumerate(calls[:3]):
        print(f"  [{i}] {call.function.name}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=LANES, default="coder")
    parser.add_argument("--test", choices=["a", "b", "c", "all"], default="all")
    args = parser.parse_args()

    results: list[bool] = []
    if args.test in ("a", "all"):
        results.append(probe_a(args.lane))
    if args.test in ("b", "all"):
        results.append(probe_b(args.lane))
    if args.test in ("c", "all"):
        results.append(probe_c(args.lane))

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
