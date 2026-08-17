"""Codex CLI, installed from npm as ``@openai/codex``.

One turn is run headless with ``codex exec``, which prints a stream of JSON
events. Those events are translated into the bridge's own protocol, so the
workbench renders a Codex turn the same way it renders any other local Agent.

The event names have moved between Codex versions, so the translation matches
on families rather than exact strings, and anything unrecognised falls back to
assistant text. A new Codex release should degrade to "the answer still shows
up", never to a blank turn.
"""

from __future__ import annotations

import json
import re
from typing import Any

from openharness.local_bridge.adapters.base import AgentCapabilities, LocalAgentAdapter
from openharness.local_bridge.events import (
    AgentEvent,
    command_output,
    message_delta,
)

_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?[\w.-]*")
#: Lines of raw command output are the noisiest thing a turn produces; keep the
#: transcript readable.
_MAX_OUTPUT_CHARS = 2000


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Some versions carry content as a list of {type, text} parts.
        parts = [
            str(item.get("text") or "") for item in value if isinstance(item, dict)
        ]
        return "\n".join(part for part in parts if part)
    return ""


class CodexAdapter(LocalAgentAdapter):
    provider = "codex"
    display_name = "Codex"
    # npm installs a `codex.cmd` shim on Windows; shutil.which resolves either.
    executables = ("codex",)

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            chat=True,
            streaming=True,
            shell=True,
            file_read=True,
            file_write=True,
            diff=True,
            mcp=True,
            skills=False,
            resume_session=True,
            approval=True,
        )

    # ------------------------------------------------------------- running

    def run_args(self, *, session_ref: str | None) -> list[str]:
        """Run one turn headless, as JSON events.

        ``--skip-git-repo-check`` is deliberate: a workspace the user chose is
        not always a git repository, and refusing to run there would be a
        surprising restriction coming from us rather than from them.
        """

        args = ["exec", "--json", "--skip-git-repo-check"]
        if session_ref:
            # Continue Codex's own conversation rather than starting over.
            args += ["--resume", session_ref]
        return args

    def parse_version(self, output: str) -> str | None:
        """Codex prints a banner such as ``codex-cli 0.5.1``.

        Pull just the number so the UI shows a version rather than a sentence.
        """

        match = _VERSION.search(output)
        if match:
            return match.group(0)
        return super().parse_version(output)

    # --------------------------------------------------------- translation

    def translate(self, line: str, session_id: str) -> list[AgentEvent]:
        cleaned = (line or "").strip()
        if not cleaned:
            return []
        if not cleaned.startswith("{"):
            # Not JSON: an older Codex, or a plain banner line. It is still what
            # the Agent said, so it is shown rather than dropped.
            return [message_delta(session_id, cleaned)]
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            return [message_delta(session_id, cleaned)]
        if not isinstance(payload, dict):
            return [message_delta(session_id, cleaned)]

        body = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        kind = str(body.get("type") or payload.get("type") or "")
        return self._events_for(kind, body, session_id)

    def _events_for(self, kind: str, body: dict[str, Any], session_id: str) -> list[AgentEvent]:
        if not kind:
            return []
        if "session" in kind:
            # Carries the id the next turn resumes from. Emitted rather than
            # dropped so session_reference can find it; the UI renders a
            # session.updated as nothing.
            reference = body.get("session_id") or body.get("id")
            if not reference:
                return []
            return [AgentEvent("session.updated", session_id, data={
                "session_id": str(reference),
            })]
        if "reasoning" in kind or "thinking" in kind:
            text = _text(body.get("text") or body.get("message") or body.get("content"))
            return [AgentEvent("thinking", session_id, data={"text": text})] if text else []
        if "command_begin" in kind or kind.endswith("command.start"):
            command = body.get("command")
            rendered = " ".join(command) if isinstance(command, list) else str(command or "")
            return [AgentEvent("command.start", session_id, data={
                "command": rendered, "cwd": str(body.get("cwd") or ""),
            })]
        if "output_delta" in kind or "command_output" in kind:
            chunk = _text(body.get("chunk") or body.get("output") or body.get("text"))
            return [command_output(session_id, chunk[:_MAX_OUTPUT_CHARS])] if chunk else []
        if "command_end" in kind:
            return [AgentEvent("command.end", session_id, data={
                "exit_code": body.get("exit_code", body.get("exitCode", 0)),
            })]
        if "patch" in kind or "file_change" in kind or "apply" in kind:
            return self._file_events(body, session_id)
        if "mcp_tool_call_begin" in kind or kind.endswith("tool.start"):
            return [AgentEvent("tool.start", session_id, data={
                "name": str(body.get("tool") or body.get("name") or "工具"),
            })]
        if "mcp_tool_call_end" in kind or kind.endswith("tool.result"):
            return [AgentEvent("tool.result", session_id, data={
                "name": str(body.get("tool") or body.get("name") or "工具"),
            })]
        if "error" in kind:
            message = _text(body.get("message") or body.get("error")) or "Codex 报告了一个错误"
            return [AgentEvent("error", session_id, data={"message": message})]
        if "task_complete" in kind or "turn_complete" in kind:
            # The final answer sometimes rides on the completion event.
            text = _text(body.get("last_agent_message") or body.get("message"))
            return [message_delta(session_id, text)] if text else []
        if "message" in kind or "agent" in kind or "assistant" in kind or "delta" in kind:
            text = _text(body.get("message") or body.get("text") or body.get("content")
                         or body.get("delta"))
            return [message_delta(session_id, text)] if text else []
        return []

    def _file_events(self, body: dict[str, Any], session_id: str) -> list[AgentEvent]:
        changes = body.get("changes") or body.get("files") or {}
        paths: list[str] = []
        if isinstance(changes, dict):
            paths = [str(key) for key in changes]
        elif isinstance(changes, list):
            paths = [
                str(item.get("path") if isinstance(item, dict) else item) for item in changes
            ]
        single = body.get("path")
        if not paths and single:
            paths = [str(single)]
        return [AgentEvent("file.write", session_id, data={"path": path}) for path in paths]

    def session_reference(self, events: list[AgentEvent]) -> str | None:
        """Codex reports its own session id, which lets the next turn resume."""

        for event in events:
            candidate = event.data.get("session_id") or event.data.get("session_ref")
            if candidate:
                return str(candidate)
        return None


__all__ = ["CodexAdapter"]
