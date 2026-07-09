"""SMTP adapter for outbound handoff."""

from __future__ import annotations


class SmtpAdapter:
    def send(self, recipient: str, body: str) -> bool:
        return bool(recipient and body)
