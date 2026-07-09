"""Webhook adapter for outbound handoff."""

from __future__ import annotations


class WebhookAdapter:
    def send(self, url: str, body: str) -> bool:
        return bool(url.startswith("http") and body)
