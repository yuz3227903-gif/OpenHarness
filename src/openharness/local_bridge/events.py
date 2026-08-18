"""The event vocabulary every Agent is translated into.

A Codex event and an OpenClaw event look nothing alike. Each adapter converts
its own protocol into these, so the chat UI renders one shape and never learns
which CLI produced it. Adding an Agent means writing a translation, not adding
a branch to the frontend.

Events are deliberately small and JSON-safe: they cross a process boundary and
are replayed from a buffer after a reconnect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal[
    "session.created", "session.updated",
    "message.start", "message.delta", "message.end",
    "thinking",
    "tool.start", "tool.result",
    "command.start", "command.output", "command.end",
    "file.read", "file.write", "file.diff",
    "approval.required", "approval.result",
    "agent.status",
    "error",
]

#: Every type the protocol defines. The bridge refuses to emit anything else,
#: so a typo in an adapter surfaces as a failure rather than as an event the
#: UI silently ignores.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session.created", "session.updated",
        "message.start", "message.delta", "message.end",
        "thinking",
        "tool.start", "tool.result",
        "command.start", "command.output", "command.end",
        "file.read", "file.write", "file.diff",
        "approval.required", "approval.result",
        "agent.status",
        "error",
    }
)


class UnknownEventType(ValueError):
    """An adapter produced a type outside the protocol."""


@dataclass
class AgentEvent:
    """One thing that happened inside a session."""

    type: str
    session_id: str
    seq: int = 0
    created_at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise UnknownEventType(
                f"{self.type!r} is not part of the agent event protocol"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "seq": self.seq,
            "created_at": self.created_at,
            "data": self.data,
        }


def message_delta(session_id: str, text: str) -> AgentEvent:
    return AgentEvent("message.delta", session_id, data={"text": text})


def command_output(session_id: str, line: str, *, stream: str = "stdout") -> AgentEvent:
    return AgentEvent("command.output", session_id, data={"line": line, "stream": stream})


def agent_status(session_id: str, status: str, *, detail: str = "") -> AgentEvent:
    return AgentEvent("agent.status", session_id, data={"status": status, "detail": detail})


def error(session_id: str, message: str, *, kind: str = "error") -> AgentEvent:
    return AgentEvent("error", session_id, data={"message": message, "kind": kind})


__all__ = [
    "EVENT_TYPES",
    "AgentEvent",
    "EventType",
    "UnknownEventType",
    "agent_status",
    "command_output",
    "error",
    "message_delta",
]
