"""Sessions: running a turn and streaming what happens.

One session is one conversation with one local Agent, in one workspace. A turn
starts a process, reads its output line by line, and publishes protocol events
as they arrive — a caller sees the Agent working rather than waiting for it to
finish.

Events are buffered per session so a browser that reconnects can replay from
where it left off instead of losing the middle of an answer.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openharness.local_bridge.adapters.base import LocalAgentAdapter
from openharness.local_bridge.events import AgentEvent, agent_status, error

log = logging.getLogger(__name__)

#: How many events one session keeps for replay. Enough to cover a reconnect
#: mid-answer without letting a long session grow without bound.
EVENT_BUFFER = 2000
#: A turn that has produced nothing for this long is treated as wedged. Without
#: it a hung CLI would hold the session open forever.
TURN_IDLE_TIMEOUT = 300.0

#: Substrings that make a command worth stopping to ask about. Matching text is
#: coarse, so this is a prompt to confirm — never a claim to have understood
#: the command.
DANGEROUS_PATTERNS: tuple[str, ...] = (
    "rm -rf", "rmdir /s", "del /f", "format ",
    "git push", "git reset --hard", "git clean -fd",
    "npm publish", "yarn publish", "pip upload", "twine upload",
    "shutdown", "reboot", "mkfs", "dd if=",
    "chmod 777", "curl | sh", "wget | sh", "iwr | iex",
)


class SessionError(Exception):
    """A session request could not be honoured. Safe to show a user."""


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    action: str
    detail: str
    created_at: float = field(default_factory=time.time)
    decision: str | None = None
    event = threading.Event()


@dataclass
class Session:
    session_id: str
    agent_id: str
    provider: str
    workspace: str
    permission_mode: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "idle"
    session_ref: str | None = None
    events: deque[AgentEvent] = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER))
    seq: int = 0
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _process: subprocess.Popen[str] | None = None
    #: A turn starts in a worker thread, so a cancel can land before the
    #: process exists. Recording the intent lets the worker honour it instead
    #: of reporting "nothing to cancel" and then running anyway.
    _cancel_requested: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "provider": self.provider,
            "workspace": self.workspace,
            "permission_mode": self.permission_mode,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "event_count": self.seq,
            "has_session_ref": bool(self.session_ref),
        }

    def publish(self, event: AgentEvent) -> AgentEvent:
        with self._lock:
            self.seq += 1
            event.seq = self.seq
            self.events.append(event)
            self.updated_at = time.time()
        return event

    def events_since(self, after: int) -> list[AgentEvent]:
        with self._lock:
            return [event for event in self.events if event.seq > after]


def looks_dangerous(command: str) -> str | None:
    """Return the pattern that makes this command worth confirming."""

    lowered = " ".join(str(command or "").lower().split())
    for pattern in DANGEROUS_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def within_workspace(path: str | Path, workspace: str | Path) -> bool:
    """Is this path inside the workspace the Agent was confined to?

    Both sides are resolved first, so ``workspace/../etc`` does not pass by
    looking like a prefix match.
    """

    try:
        resolved = Path(path).resolve()
        root = Path(workspace).resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


class SessionManager:
    """Every session this bridge is running."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- lifecycle

    def create(self, *, agent_id: str, provider: str, workspace: str,
               permission_mode: str = "ask") -> Session:
        try:
            resolved_workspace = Path(workspace).resolve()
        except OSError as exc:
            raise SessionError(f"工作目录不可用：{workspace}") from exc
        if not resolved_workspace.is_dir():
            raise SessionError(f"工作目录不存在：{workspace}")
        session = Session(
            session_id=f"S-{uuid.uuid4().hex[:12].upper()}",
            agent_id=agent_id, provider=provider,
            workspace=str(resolved_workspace),
            permission_mode=permission_mode,
        )
        session.publish(AgentEvent("session.created", session.session_id, data=session.public()))
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionError("会话不存在或已结束")
        return session

    def list(self, *, agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [
            session.public() for session in sorted(sessions, key=lambda s: s.created_at, reverse=True)
            if agent_id is None or session.agent_id == agent_id
        ]

    def cancel(self, session_id: str) -> bool:
        """Stop whatever the session is running, or is about to run.

        A turn spawns in a worker thread, so a cancel can arrive before the
        process exists. The intent is recorded either way and the worker
        honours it, rather than this reporting failure while the turn goes on
        to run.
        """

        session = self.get(session_id)
        with session._lock:
            if session.status not in {"running", "awaiting_approval"}:
                return False
            session._cancel_requested = True
            process = session._process
            pending = list(session.approvals.values())

        # A turn blocked on a question is released by denying it.
        for approval in pending:
            approval.decision = "deny"
            approval.event.set()

        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        session.publish(agent_status(session.session_id, "cancelled", detail="用户取消"))
        return True

    # ------------------------------------------------------------------ turns

    def send(self, session_id: str, prompt: str, adapter: LocalAgentAdapter) -> Session:
        """Start one turn. Returns immediately; events stream as they arrive."""

        session = self.get(session_id)
        if session.status == "running":
            raise SessionError("该会话正在执行，请等待或取消当前任务")
        if not str(prompt or "").strip():
            raise SessionError("消息不能为空")

        with session._lock:
            session._cancel_requested = False
        session.status = "running"
        session.publish(AgentEvent("message.start", session.session_id,
                                   data={"role": "user", "text": prompt}))
        thread = threading.Thread(
            target=self._run_turn, args=(session, prompt, adapter),
            daemon=True, name=f"bridge-turn-{session.session_id}",
        )
        thread.start()
        return session

    def _run_turn(self, session: Session, prompt: str, adapter: LocalAgentAdapter) -> None:
        try:
            command = adapter.build_command(
                prompt=prompt, workspace=session.workspace, session_ref=session.session_ref,
            )
        except FileNotFoundError as exc:
            session.publish(error(session.session_id, str(exc), kind="agent_not_found"))
            session.publish(agent_status(session.session_id, "agent_not_found"))
            session.status = "idle"
            return

        blocked = looks_dangerous(" ".join(command))
        if blocked and session.permission_mode != "accept_edits":
            approved = self._await_approval(
                session, action=" ".join(command),
                detail=f"命令中包含 {blocked}",
            )
            if not approved:
                session.publish(AgentEvent("approval.result", session.session_id,
                                           data={"approved": False, "action": " ".join(command)}))
                session.publish(agent_status(session.session_id, "denied", detail="用户拒绝执行"))
                session.status = "idle"
                return

        session.publish(AgentEvent("command.start", session.session_id,
                                   data={"command": " ".join(command), "cwd": session.workspace}))
        turn_events: list[AgentEvent] = []
        try:
            # The Agent runs inside its workspace, with no shell between us and
            # it, so neither the prompt nor the path can become a command.
            process = subprocess.Popen(  # noqa: S603
                command, cwd=session.workspace, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, shell=False,
                env={**os.environ, "OPENHARNESS_BRIDGE_SESSION": session.session_id},
            )
        except OSError as exc:
            session.publish(error(session.session_id, f"无法启动 Agent：{exc}", kind="spawn_failed"))
            session.status = "idle"
            return

        with session._lock:
            session._process = process
            # A cancel that arrived while the process was starting is honoured
            # here; otherwise it would be lost between the two states.
            if session._cancel_requested:
                try:
                    process.terminate()
                except OSError:
                    pass

        deadline = time.time() + TURN_IDLE_TIMEOUT
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    if time.time() > deadline:
                        process.terminate()
                        session.publish(error(session.session_id,
                                              "Agent 长时间没有输出，已终止本轮。", kind="timeout"))
                        break
                    deadline = time.time() + TURN_IDLE_TIMEOUT
                    text = line.rstrip("\n")
                    for event in adapter.translate(text, session.session_id):
                        session.publish(self._mark_outside_workspace(session, event))
                        turn_events.append(event)
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = -1
        except Exception as exc:  # noqa: BLE001 - a turn must not kill the bridge
            log.exception("bridge turn failed")
            session.publish(error(session.session_id, f"执行异常：{type(exc).__name__}: {exc}"))
            returncode = -1
        finally:
            with session._lock:
                session._process = None

        session.publish(AgentEvent("command.end", session.session_id,
                                   data={"exit_code": returncode}))
        session.publish(AgentEvent("message.end", session.session_id,
                                   data={"role": "assistant", "exit_code": returncode}))
        # A CLI that reports its own session id lets the next turn continue
        # rather than start over.
        reference = adapter.session_reference(turn_events)
        if reference:
            session.session_ref = reference
            session.publish(AgentEvent("session.updated", session.session_id,
                                       data={"session_ref": reference}))
        session.status = "idle"
        session.publish(agent_status(session.session_id, "idle"))

    def _mark_outside_workspace(self, session: Session, event: AgentEvent) -> AgentEvent:
        """Flag a file the Agent touched outside the directory it was given.

        The bridge cannot stop a CLI from reaching outside its workspace — the
        CLI owns its own file access. What it can do is refuse to let that pass
        unremarked, so the page shows it rather than rendering it like any other
        edit.
        """

        if event.type not in {"file.read", "file.write", "file.diff"}:
            return event
        path = str(event.data.get("path") or "").strip()
        if not path or within_workspace(path, session.workspace):
            return event
        event.data["outside_workspace"] = True
        return event

    # -------------------------------------------------------------- approvals

    def _await_approval(self, session: Session, *, action: str, detail: str) -> bool:
        """Ask, then block this turn until the user answers or time runs out.

        A silent timeout denies. Defaulting to "allow" would turn an unattended
        tab into blanket permission.
        """

        approval = PendingApproval(
            approval_id=f"A-{uuid.uuid4().hex[:10].upper()}",
            session_id=session.session_id, action=action, detail=detail,
        )
        approval.event = threading.Event()
        with session._lock:
            session.approvals[approval.approval_id] = approval
        session.publish(AgentEvent("approval.required", session.session_id, data={
            "approval_id": approval.approval_id, "action": action, "detail": detail,
        }))
        session.status = "awaiting_approval"
        granted = approval.event.wait(timeout=300)
        session.status = "running"
        with session._lock:
            session.approvals.pop(approval.approval_id, None)
        if not granted:
            session.publish(error(session.session_id, "审批超时，已按拒绝处理。", kind="approval_timeout"))
            return False
        return approval.decision in {"allow_once", "allow_session"}

    def resolve_approval(self, session_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"allow_once", "allow_session", "deny"}:
            raise SessionError("decision 必须是 allow_once、allow_session 或 deny")
        session = self.get(session_id)
        with session._lock:
            approval = session.approvals.get(approval_id)
        if approval is None:
            raise SessionError("该审批请求不存在或已处理")
        approval.decision = decision
        # Allowing for the whole session is remembered by relaxing the mode, so
        # later commands in this session do not ask again.
        if decision == "allow_session":
            session.permission_mode = "accept_edits"
        approval.event.set()
        session.publish(AgentEvent("approval.result", session.session_id, data={
            "approval_id": approval_id, "approved": decision != "deny", "decision": decision,
        }))
        return {"approval_id": approval_id, "decision": decision}


_MANAGER: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = SessionManager()
    return _MANAGER


__all__ = [
    "DANGEROUS_PATTERNS",
    "EVENT_BUFFER",
    "PendingApproval",
    "Session",
    "SessionError",
    "SessionManager",
    "get_session_manager",
    "looks_dangerous",
    "within_workspace",
]
