"""Outbound message ferry — central dispatch pipeline."""

from __future__ import annotations

from messaging.adapters.smtp import SmtpAdapter
from messaging.adapters.webhook import WebhookAdapter
from messaging.models import DeliveryStatus, Message


class OutboundHandoff:
    """Hands outbound messages to channel adapters."""

    def __init__(self) -> None:
        self._smtp = SmtpAdapter()
        self._webhook = WebhookAdapter()

    def dispatch(self, message: Message) -> DeliveryStatus:
        if message.channel == "webhook":
            ok = self._webhook.send(message.recipient, message.body)
        else:
            ok = self._smtp.send(message.recipient, message.body)
        return DeliveryStatus.DELIVERED if ok else DeliveryStatus.FAILED


class MessageFerry:
    """Thin facade over OutboundHandoff (in-memory only until queue story lands)."""

    def __init__(self) -> None:
        self._handoff = OutboundHandoff()

    def send_now(self, message: Message) -> DeliveryStatus:
        return self._handoff.dispatch(message)
