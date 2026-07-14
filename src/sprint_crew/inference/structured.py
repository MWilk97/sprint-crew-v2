from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from sprint_crew.config import Role, lane_for_role

T = TypeVar("T", bound=BaseModel)

_MAX_RETRIES = 3
_RAW_FRAGMENT_CHARS = 2000


@lru_cache(maxsize=8)
def _client(base_url: str, timeout_seconds: float) -> OpenAI:
    return OpenAI(base_url=base_url, api_key="local", timeout=timeout_seconds)


def _extract_json_object(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def structured_completion(
    role: Role,
    *,
    system_prompt: str,
    user_prompt: str,
    output_type: type[T],
    temperature: float = 0,
    timeout_seconds: float = 600,
    max_retries: int = _MAX_RETRIES,
    max_tokens: int | None = None,
) -> T:
    lane = lane_for_role(role)
    client = _client(lane.base_url, timeout_seconds)
    schema = output_type.model_json_schema()
    repair_hint = ""
    last_error: Exception | None = None

    for attempt in range(max_retries):
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if repair_hint:
            messages.append({"role": "user", "content": repair_hint})

        create_kwargs: dict[str, object] = {
            "model": lane.served_name,
            "messages": messages,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_type.__name__,
                    "schema": schema,
                    "strict": True,
                },
            },
        }
        if max_tokens is not None:
            create_kwargs["max_tokens"] = max_tokens

        resp = client.chat.completions.create(**create_kwargs)
        raw = resp.choices[0].message.content or ""
        try:
            parsed = json.loads(_extract_json_object(raw))
            return output_type.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            if attempt + 1 >= max_retries:
                break
            fragment = raw[:_RAW_FRAGMENT_CHARS]
            repair_hint = (
                f"Your previous response was invalid JSON for {output_type.__name__}. "
                f"Error: {exc}. Return ONLY valid JSON matching the schema. "
                f"Do not use markdown fences. Previous output fragment:\n{fragment}"
            )

    assert last_error is not None
    raise last_error
