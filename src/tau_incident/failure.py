"""Execution failure categories, separate from review judgments."""

import asyncio
import sqlite3
from typing import Literal

import httpx

FailureCategory = Literal[
    "transport", "output", "context", "cancelled", "persistence", "commit_unknown", "execution"
]


def classify_failure(error: BaseException) -> FailureCategory:
    from tau_incident.context import ContextInsufficient
    from tau_incident.coordinator import LocalRecordingError
    from tau_incident.executor import OutputInvalid
    from tau_incident.store import CommitUnknown

    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, CommitUnknown):
        return "commit_unknown"
    if isinstance(error, (httpx.TransportError, ConnectionError, TimeoutError)):
        return "transport"
    if isinstance(error, ContextInsufficient):
        return "context"
    if isinstance(error, OutputInvalid):
        return "output"
    if isinstance(error, (LocalRecordingError, sqlite3.Error, OSError)):
        return "persistence"
    return "execution"
