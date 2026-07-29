#!/usr/bin/env python3
"""Preflight E: Interpreter clarify on the Work lane, with optional image input.

Three things this checks that the JSON probe does not:
  * the model returns useful clarify questions with a defensible recommendation
  * a clear request yields ZERO questions (the model does not interrogate the user)
  * the lane accepts image content parts at all (vision tower actually loaded)
  * an image survives strict guided JSON, which is what clarify actually sends (M11)

Usage:
  scripts/probe_interpreter.py                  # text probes + latency
  scripts/probe_interpreter.py --image PATH     # also probe vision, raw and via clarify
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import mimetypes
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

from sprint_crew.agents.interpreter import run_interpreter, to_clarify_questions
from sprint_crew.config import Role, lane_for_role
from sprint_crew.inference.router import thinking_chat_template_kwargs
from sprint_crew.schemas.attachment import AttachmentKind, AttachmentPayload

VAGUE = "make the backlog endpoints faster"
CLEAR = (
    "In src/sprint_crew/paths.py, add a module-level docstring explaining what "
    "resolve_safe_path rejects. Do not change any behaviour."
)


async def _probe(
    label: str, prompt: str, *, expect_questions: bool, thinking: bool | None = None
) -> bool:
    started = time.monotonic()
    analysis = await run_interpreter(user_prompt=prompt, thinking=thinking)
    elapsed = time.monotonic() - started
    questions = to_clarify_questions(analysis, limit=4)

    print(f"\n--- {label} ({elapsed:.1f}s) ---")
    print(f"goal:       {analysis.restated_goal}")
    print(f"confidence: {analysis.confidence}")
    for assumption in analysis.assumptions:
        print(f"assumes:    {assumption}")
    for question in questions:
        print(f"  Q: {question.text}")
        print(f"     why: {question.why_asked}")
        for suggestion in question.suggestions:
            mark = "*" if suggestion.suggestion_id == question.recommended_suggestion_id else " "
            print(f"    {mark} {suggestion.label} — {suggestion.rationale}")
        if question.recommended_suggestion_id is None:
            print("     FAIL — no recommendation")
            return False

    if expect_questions and not questions:
        print("FAIL — vague request produced no questions")
        return False
    if not expect_questions and questions:
        print(f"WARN — clear request still produced {len(questions)} question(s)")
    print(f"E {label}: PASS ({elapsed:.1f}s)")
    return True


def _probe_image(image_path: Path) -> bool:
    """Minimal vision round-trip: the lane must accept an image_url content part."""
    lane = lane_for_role(Role.WORK)
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode()
    client = OpenAI(
        base_url=lane.base_url,
        api_key=os.environ.get("OPENAI_API_KEY", "local"),
        timeout=300,
    )
    started = time.monotonic()
    resp = client.chat.completions.create(
        model=lane.served_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in one sentence."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }
        ],
        max_tokens=512,
        temperature=0,
        # Without this the reasoning trace eats the whole token budget and `content`
        # comes back empty — the qwen3 parser moves the trace to reasoning_content.
        extra_body=thinking_chat_template_kwargs(False),
    )
    elapsed = time.monotonic() - started
    message = resp.choices[0].message
    text = (message.content or "").strip()
    if not text:
        reasoning = (getattr(message, "reasoning_content", None) or "").strip()
        print(f"E vision: FAIL — empty content (reasoning_content: {len(reasoning)} chars)")
        return False
    print(f"\n--- vision ({elapsed:.1f}s) ---\n{text[:400]}")
    print(f"E vision: PASS ({elapsed:.1f}s)")
    return True


async def _probe_image_clarify(image_path: Path) -> bool:
    """The combination M11 actually ships: an image *and* strict guided JSON.

    ``_probe_image`` above deliberately uses a raw client and free text, so it says nothing
    about whether the vision path survives ``response_format: json_schema, strict: true``.
    That is the question the milestone turns on — if this fails, images have to be a
    separate free-text describe call whose output feeds the normal text-only IntentAnalysis.
    """
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode()
    payload = AttachmentPayload(
        filename=image_path.name,
        media_type=mime,
        kind=AttachmentKind.IMAGE,
        data_url=f"data:{mime};base64,{encoded}",
    )
    started = time.monotonic()
    try:
        analysis = await run_interpreter(
            user_prompt="This screenshot shows the bug I hit. What do you need to know?",
            attachments=[payload],
        )
    except Exception as exc:
        # Broad on purpose: reporting how this fails *is* the probe. A guided-JSON refusal
        # and a timeout are different answers to the open question, and both matter.
        print(f"E vision+json: FAIL — {type(exc).__name__}: {exc}")
        return False
    elapsed = time.monotonic() - started
    print(f"\n--- vision + guided JSON ({elapsed:.1f}s) ---")
    print(f"goal:       {analysis.restated_goal}")
    print(f"confidence: {analysis.confidence}")
    for question in analysis.questions[:3]:
        print(f"  Q: {question.text}")
    print(f"E vision+json: PASS ({elapsed:.1f}s)")
    return True


async def _main_async(image: Path | None, compare_thinking: bool) -> int:
    if compare_thinking:
        # Same prompt both ways: clarify is interactive, so the reasoning trace is paid
        # for while a human waits. Decide enable_thinking on measured latency.
        ok = await _probe("vague thinking=off", VAGUE, expect_questions=True, thinking=False)
        ok = await _probe("vague thinking=on", VAGUE, expect_questions=True, thinking=True) and ok
        return 0 if ok else 1

    ok = await _probe("vague", VAGUE, expect_questions=True)
    ok = await _probe("clear", CLEAR, expect_questions=False) and ok
    if image is not None:
        ok = _probe_image(image) and ok
        ok = await _probe_image_clarify(image) and ok
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="image file to probe vision with")
    parser.add_argument(
        "--compare-thinking",
        action="store_true",
        help="run the vague prompt with thinking off and on, and report both latencies",
    )
    args = parser.parse_args()
    if args.image is not None and not args.image.is_file():
        print(f"no such image: {args.image}")
        return 2
    return asyncio.run(_main_async(args.image, args.compare_thinking))


if __name__ == "__main__":
    sys.exit(main())
