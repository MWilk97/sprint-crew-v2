"""Messaging domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"
    DELIVERED = "delivered"


@dataclass
class Message:
    id: str
    recipient: str
    body: str
    channel: str = "smtp"
    status: DeliveryStatus = DeliveryStatus.PENDING
