"""Local Slack-style workbench for the investment-research flow.

This first web milestone intentionally uses the Python standard library.  The
HTTP and SSE shapes are kept small and FastAPI-compatible so the UI can later
move behind the project's planned API layer without changing the interaction.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import webbrowser
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4

from openharness.invest_research.collaboration_store import CollaborationStore
from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.workbench_agent_tasks import (
    DIRECT_AGENT_IDS,
    DirectAgentTaskError,
    build_direct_agent_input,
    direct_task_budget,
    direct_task_prompt,
)
from openharness.invest_research.workbench_models import (
    ARK_PLAN_BASE_URL,
    DEFAULT_MODEL,
    MODEL_CATALOG,
    model_ids,
)


log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = PROJECT_ROOT / ".openharness" / "plugins" / "investment-research" / "workbench"
MAX_REQUEST_BYTES = 3 * 1024 * 1024
MAX_AVATAR_BYTES = 2 * 1024 * 1024
# Channel attachments travel as base64 data URLs, which inflate the payload by
# roughly a third.  Keep the raw file cap and the request cap separate so a
# 20 MB document is not rejected by the JSON body guard.
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_REQUEST_BYTES = 28 * 1024 * 1024
AVATAR_ROOT = PROJECT_ROOT / ".openharness" / "data" / "uploads" / "avatars"
FILE_ROOT = PROJECT_ROOT / ".openharness" / "data" / "uploads" / "files"
SKILL_ROOT = PROJECT_ROOT / ".openharness" / "data" / "agent-skills"
#: Where the page looks for the user's Local Agent Bridge. The browser talks to
#: it directly; this server never proxies to the user's machine.
LOCAL_BRIDGE_PORT = 18789
MAX_SKILL_BYTES = 5 * 1024 * 1024
# A Skill plugin is text or a packaged folder; refuse anything executable.
SKILL_SUFFIXES = {".md", ".markdown", ".json", ".yaml", ".yml", ".txt", ".zip"}
FLOW_IDLE_TIMEOUT_SECONDS = 360
STORE = CollaborationStore(PROJECT_ROOT)
EVIDENCE_STORE = EvidenceStore.for_project(PROJECT_ROOT)
RUN_LOCK = threading.Lock()
RUN_STATE_LOCK = threading.RLock()
DIRECT_TASK_LOCK = threading.RLock()
# One FIFO queue and one worker per Agent. A thread-per-task would let the same
# Agent run several jobs at once; replies to it are a queue it works through in
# order, so a second request waits for the first to finish.
AGENT_QUEUES: dict[str, queue.Queue[dict[str, Any]]] = {}
AGENT_WORKERS: dict[str, threading.Thread] = {}
# How many times work may be handed on Agent to Agent before the chain stops.
# Without a ceiling two Agents could @ each other indefinitely.
MAX_AGENT_HANDOFF_DEPTH = 2
RUN_RESUME_EVENT = threading.Event()
RUN_RESUME_EVENT.set()
RUN_STATE: dict[str, Any] = {
    "running": False,
    "run_id": None,
    "task_id": None,
    "company": None,
    "as_of_date": None,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
    "last_progress": None,
    "paused": False,
    "pause_requested": False,
    "flow_orphaned": False,
}

AGENT_IDS = {
    "planner", "fundamental", "industry_competition", "market_catalyst",
    "risk", "reviewer_arbiter", "report_writer",
}
MENTION_ALIASES = {
    "planner": "planner", "fundamental": "fundamental", "industrycompetition": "industry_competition",
    "industry_competition": "industry_competition", "marketcatalyst": "market_catalyst",
    "market_catalyst": "market_catalyst", "risk": "risk", "reviewerarbiter": "reviewer_arbiter",
    "reviewer_arbiter": "reviewer_arbiter", "reportwriter": "report_writer", "report_writer": "report_writer",
}


def _resolve_agent_model(agent_id: str) -> str:
    agent = STORE.get_agent(agent_id)
    return str((agent or {}).get("model") or DEFAULT_MODEL)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _save_avatar(data_url: str) -> str | None:
    if not data_url:
        return None
    match = re.fullmatch(
        r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)", data_url
    )
    if not match:
        raise ValueError("avatar must be a PNG, JPEG, WEBP or GIF data URL")
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("avatar image is invalid") from exc
    if not payload or len(payload) > MAX_AVATAR_BYTES:
        raise ValueError("avatar must be between 1 byte and 2 MB")
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
    AVATAR_ROOT.mkdir(parents=True, exist_ok=True)
    filename = f"avatar-{int(time.time() * 1000)}-{len(payload)}{extensions[match.group(1)]}"
    (AVATAR_ROOT / filename).write_bytes(payload)
    return f"uploads/avatars/{filename}"


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9一-鿿._-]+")
_DATA_URL = re.compile(r"data:([\w.+-]+/[\w.+-]+)?(;charset=[\w-]+)?;base64,([A-Za-z0-9+/=\r\n]+)")


def _safe_filename(name: str) -> str:
    """Reduce a browser-supplied name to a short, path-free display name."""

    cleaned = _SAFE_FILENAME.sub("_", Path(str(name or "")).name).strip("._") or "file"
    return cleaned[:120]


def _store_channel_upload(
    *, channel_id: str, filename: str, data_url: str, message_id: str | None = None,
    owner_id: str = "owner", owner_type: str = "human", summary: str = "",
) -> dict[str, Any]:
    """Decode one base64 attachment onto disk and register it in the channel."""

    match = _DATA_URL.fullmatch(str(data_url or "").strip())
    if not match:
        raise ValueError("attachment must be a base64 data URL")
    try:
        payload = base64.b64decode(match.group(3), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("attachment payload is not valid base64") from exc
    if not payload:
        raise ValueError("attachment is empty")
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("attachment must be 20 MB or smaller")

    display_name = _safe_filename(filename)
    channel_dir = FILE_ROOT / _safe_filename(channel_id)
    channel_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{int(time.time() * 1000)}-{uuid4().hex[:8]}-{display_name}"
    (channel_dir / stored_name).write_bytes(payload)
    return STORE.add_file(
        channel_id=channel_id,
        message_id=message_id,
        owner_id=owner_id,
        owner_type=owner_type,
        source="upload",
        filename=display_name,
        stored_name=f"{_safe_filename(channel_id)}/{stored_name}",
        media_type=match.group(1) or "application/octet-stream",
        size_bytes=len(payload),
        summary=summary,
    )


def _store_agent_skill(
    *, agent_id: str, filename: str, data_url: str, name: str, description: str,
) -> dict[str, Any]:
    """Persist one uploaded Skill plugin for an Agent.

    A Skill is instruction text or a packaged folder, never a program the
    workbench executes, so the accepted suffixes stay deliberately narrow.
    """

    match = _DATA_URL.fullmatch(str(data_url or "").strip())
    if not match:
        raise ValueError("skill must be a base64 data URL")
    try:
        payload = base64.b64decode(match.group(3), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("skill payload is not valid base64") from exc
    if not payload:
        raise ValueError("skill file is empty")
    if len(payload) > MAX_SKILL_BYTES:
        raise ValueError("skill file must be 5 MB or smaller")

    display_name = _safe_filename(filename)
    if Path(display_name).suffix.lower() not in SKILL_SUFFIXES:
        allowed = "、".join(sorted(SKILL_SUFFIXES))
        raise ValueError(f"skill file type is not supported; allowed: {allowed}")

    agent_dir = SKILL_ROOT / _safe_filename(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{int(time.time() * 1000)}-{uuid4().hex[:8]}-{display_name}"
    (agent_dir / stored_name).write_bytes(payload)
    return STORE.add_agent_skill(
        agent_id=agent_id,
        name=(name.strip() or Path(display_name).stem)[:120],
        description=description.strip()[:500],
        filename=display_name,
        stored_name=f"{_safe_filename(agent_id)}/{stored_name}",
        media_type=match.group(1) or "text/markdown",
        size_bytes=len(payload),
    )


def _resolve_stored_file(record: dict[str, Any]) -> Path | None:
    """Map a file record onto a real path, refusing anything outside the roots."""

    stored = str(record.get("stored_name") or "")
    if not stored:
        return None
    root = FILE_ROOT if record.get("source") == "upload" else PROJECT_ROOT
    candidate = (root / stored).resolve()
    resolved_root = root.resolve()
    if resolved_root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


_AGENT_FILE_SOURCES = (
    ("report.md", "agent_report", "研究报告"),
    ("summary.json", "agent_intermediate", "运行摘要"),
    ("report-context.json", "agent_intermediate", "报告材料包"),
)


def _sync_agent_files(channel_id: str, run_id: str | None) -> None:
    """Register the artefacts a Run wrote on disk as channel files.

    ``file_id`` is derived from the Run and file name so repeated syncs update
    the same row instead of appending a duplicate on every workspace refresh.
    """

    if not run_id:
        return
    run_directory = PROJECT_ROOT / ".openharness" / "validation" / run_id
    if not run_directory.is_dir():
        return
    for name, source, title in _AGENT_FILE_SOURCES:
        candidate = run_directory / name
        if not candidate.is_file():
            continue
        try:
            relative = candidate.resolve().relative_to(PROJECT_ROOT)
        except ValueError:
            continue
        STORE.add_file(
            file_id=f"FILE-{run_id}-{name.replace('.', '-').upper()}",
            channel_id=channel_id,
            run_id=run_id,
            owner_id="report_writer" if source == "agent_report" else "system",
            owner_type="agent" if source == "agent_report" else "system",
            source=source,
            filename=name,
            stored_name=relative.as_posix(),
            media_type="text/markdown" if name.endswith(".md") else "application/json",
            size_bytes=candidate.stat().st_size,
            summary=f"{title} · Run {run_id}",
        )


_SEARCH_SCOPES = ("all", "message", "file", "task", "agent", "channel")


def _search_workspace(
    *, query: str, scope: str, sender: str, channel_id: str, since_days: int, sort: str,
) -> dict[str, Any]:
    """Search every record the workbench owns: messages, files, tasks, members.

    Matching is a case-insensitive substring over the fields a person would
    actually recall.  Results carry the channel and timestamp so a hit can be
    opened where it happened.
    """

    needle = query.strip().lower()
    scope = scope if scope in _SEARCH_SCOPES else "all"
    sender = sender.strip()
    cutoff: str | None = None
    if since_days > 0:
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    channels = STORE.list_channels()
    channel_names = {item["channel_id"]: item["name"] for item in channels}
    searched = [item for item in channels if not channel_id or item["channel_id"] == channel_id]
    agents = {item["agent_id"]: item for item in STORE.list_agents()}

    status_labels = {
        "queued": "排队中", "pending": "待办", "running": "执行中", "completed": "已交付",
        "complete": "已交付", "failed": "失败", "blocked": "已阻塞", "review": "待确认",
        "awaiting_review": "待确认", "partial": "部分完成",
    }
    member_labels = {"owner": "你", "system": "工作台", "unassigned": "未指派"}

    def display(member_id: str) -> str:
        agent = agents.get(member_id)
        if agent is not None:
            return str(agent.get("name") or member_id)
        return member_labels.get(member_id, member_id)

    def hit(kind: str, *, title: str, body: str, owner: str, created_at: str,
            channel: str, ref: dict[str, Any], score: int) -> dict[str, Any]:
        return {
            "kind": kind, "title": title, "body": body[:280], "owner": owner,
            "owner_name": display(owner), "created_at": created_at,
            "channel_id": channel, "channel_name": channel_names.get(channel, channel),
            "ref": ref, "score": score,
        }

    results: list[dict[str, Any]] = []

    def keep(text: str) -> bool:
        return not needle or needle in text.lower()

    for channel in searched:
        cid = channel["channel_id"]
        if scope in ("all", "message"):
            for message in STORE.list_messages(cid, limit=500):
                if sender and message.get("author_id") != sender:
                    continue
                if cutoff and str(message.get("created_at") or "") < cutoff:
                    continue
                if not keep(str(message.get("body") or "")):
                    continue
                results.append(hit(
                    "message", title=kind_title(message), body=str(message.get("body") or ""),
                    owner=str(message.get("author_id") or ""),
                    created_at=str(message.get("created_at") or ""), channel=cid,
                    ref={"message_id": message["message_id"],
                         "thread_id": message.get("thread_id")},
                    score=3 if message.get("thread_id") is None else 2,
                ))
        if scope in ("all", "task"):
            for task in STORE.list_tasks(cid):
                if sender and sender not in {task.get("created_by"), task.get("assignee_id")}:
                    continue
                if cutoff and str(task.get("updated_at") or "") < cutoff:
                    continue
                if not keep(str(task.get("title") or "")):
                    continue
                results.append(hit(
                    "task", title=str(task.get("title") or ""),
                    body=(
                        f"负责人 {display(str(task.get('assignee_id') or ''))}"
                        f" · {status_labels.get(str(task.get('status') or ''), str(task.get('status') or ''))}"
                    ),
                    owner=str(task.get("assignee_id") or ""),
                    created_at=str(task.get("updated_at") or ""), channel=cid,
                    ref={"task_id": task["task_id"], "status": task.get("status")}, score=3,
                ))
        if scope in ("all", "file"):
            for record in STORE.list_files(cid):
                if sender and record.get("owner_id") != sender:
                    continue
                if cutoff and str(record.get("created_at") or "") < cutoff:
                    continue
                if not keep(f"{record.get('filename')} {record.get('summary')}"):
                    continue
                results.append(hit(
                    "file", title=str(record.get("filename") or ""),
                    body=str(record.get("summary") or ""),
                    owner=str(record.get("owner_id") or ""),
                    created_at=str(record.get("created_at") or ""), channel=cid,
                    ref={"file_id": record["file_id"], "source": record.get("source")}, score=2,
                ))

    # A member and a channel are workspace-level records: they have no author
    # and no owning channel, so a sender or channel filter excludes them rather
    # than letting every one of them through unfiltered.
    if scope in ("all", "agent") and not channel_id:
        for agent in agents.values():
            if sender and agent["agent_id"] != sender:
                continue
            if not keep(f"{agent.get('name')} {agent.get('role')} {agent.get('profile')}"):
                continue
            results.append(hit(
                "agent", title=str(agent.get("name") or agent["agent_id"]),
                body=str(agent.get("role") or ""), owner=agent["agent_id"],
                created_at="", channel="", ref={"agent_id": agent["agent_id"]}, score=1,
            ))
    if scope in ("all", "channel") and not sender:
        for channel in searched:
            if not keep(f"{channel.get('name')} {channel.get('topic')}"):
                continue
            results.append(hit(
                "channel", title=str(channel.get("name") or ""),
                body=str(channel.get("topic") or ""), owner="", created_at="",
                channel=channel["channel_id"],
                ref={"channel_id": channel["channel_id"], "kind": channel.get("kind")}, score=1,
            ))

    if sort == "recent":
        results.sort(key=lambda item: item["created_at"], reverse=True)
    else:
        results.sort(key=lambda item: (item["score"], item["created_at"]), reverse=True)

    counts: dict[str, int] = {}
    for item in results:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {
        "query": query, "scope": scope, "total": len(results),
        "counts": counts, "results": results[:100],
        "senders": [
            {"id": agent_id, "name": str(agent.get("name") or agent_id)}
            for agent_id, agent in agents.items()
        ] + [{"id": "owner", "name": "你"}],
        "channels": [
            {"id": item["channel_id"], "name": item["name"], "kind": item.get("kind")}
            for item in channels
        ],
    }


def kind_title(message: dict[str, Any]) -> str:
    labels = {
        "system_message": "工作台状态", "user_message": "用户消息", "agent_message": "Agent 消息",
        "task_dispatch": "任务派发", "progress_update": "流程状态", "task_update": "任务状态",
        "review_issue": "审查 / 返工", "artifact_delivery": "成果交付",
        "report_delivery": "报告交付", "run_failed": "运行失败",
    }
    return labels.get(str(message.get("message_kind") or ""), "消息")


def _builtin_role_prompt(agent_id: str) -> str:
    """Read a built-in Agent's role prompt from its plugin definition.

    Only a custom Agent stores its prompt in the database; a built-in role
    defines it in ``agents/<id>.md`` after the YAML front matter. Without this
    the graph would show an empty prompt for every built-in role.
    """

    candidate = PROJECT_ROOT / ".openharness" / "plugins" / "investment-research" / "agents" / f"{agent_id}.md"
    if not candidate.is_file():
        return ""
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


def _relationship_graph(channel_id: str) -> dict[str, Any]:
    """Build the who-works-with-whom graph, for one channel or the workspace.

    An empty ``channel_id`` aggregates every channel, which is what the graph
    pane shows by default — collaboration crosses channels, so scoping it to
    the one open in the chat pane would hide most of the relationships.

    Two real collaboration records become edges: a task, whose creator handed
    work to its assignee, and a message, whose author addressed the Agents it
    mentioned.  Nothing is inferred — a pair with no record has no edge.
    """

    # Include removed Agents in the identity lookup. Their past tasks and
    # mentions are real records, so dropping the node would leave dangling
    # edges — and an unknown id would be mislabelled as the human owner. The
    # node is marked instead, so the UI can show it as no longer in the
    # workspace.
    agents = {item["agent_id"]: item for item in STORE.list_agents(include_removed=True)}
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}

    def touch(member_id: str) -> str | None:
        member_id = str(member_id or "").strip()
        if not member_id or member_id == "unassigned":
            return None
        if member_id not in nodes:
            agent = agents.get(member_id)
            if agent is not None:
                kind = "agent"
                name = str(agent.get("name") or member_id)
                role = str(agent.get("role") or "Agent")
                avatar_path = agent.get("avatar_path")
            elif member_id == "system":
                kind, name, role, avatar_path = "system", "工作台", "系统事件", None
            else:
                kind, name, role, avatar_path = "human", "你", "频道所有者", None
            # Carry the profile the operator configured so clicking a node can
            # show it without a second round trip. Read-only in the graph.
            nodes[member_id] = {
                "id": member_id, "name": name, "role": role, "type": kind,
                "avatar_path": avatar_path, "out_degree": 0, "in_degree": 0,
                "connections": 0,
                "profile": str((agent or {}).get("profile") or ""),
                "system_prompt": str(
                    (agent or {}).get("system_prompt")
                    or (_builtin_role_prompt(member_id) if agent else "")
                ),
                "model": str((agent or {}).get("model") or ""),
                "agent_type": str((agent or {}).get("type") or ""),
                "status": str((agent or {}).get("status") or ""),
                "removed": bool((agent or {}).get("removed")),
                "allowed_tools": list((agent or {}).get("allowed_tools") or []),
                "skills": [
                    {
                        "skill_id": skill["skill_id"], "name": skill["name"],
                        "description": skill.get("description", ""),
                        "filename": skill["filename"], "enabled": skill["enabled"],
                    }
                    for skill in (STORE.list_agent_skills(member_id) if agent else [])
                ],
            }
        return member_id

    def link(source: str, target: str, relation: str, ref: str | None = None) -> None:
        source_id, target_id = touch(source), touch(target)
        if not source_id or not target_id or source_id == target_id:
            return
        key = (source_id, target_id)
        edge = edges.setdefault(
            key,
            {
                "source": source_id, "target": target_id, "weight": 0,
                "relations": [], "task_ids": [], "message_ids": [],
            },
        )
        edge["weight"] += 1
        if relation not in edge["relations"]:
            edge["relations"].append(relation)
        # Keep what produced the edge so clicking it can show the real records.
        if ref:
            bucket = "task_ids" if relation == "task" else "message_ids"
            if ref not in edge[bucket]:
                edge[bucket].append(ref)

    all_channels = STORE.list_channels()
    scanned = (
        [item["channel_id"] for item in all_channels] if not channel_id else [channel_id]
    )
    task_index: dict[str, dict[str, Any]] = {}
    for task in STORE.list_tasks(channel_id or None):
        link(
            str(task.get("created_by") or ""), str(task.get("assignee_id") or ""),
            "task", str(task.get("task_id") or ""),
        )
        task_index[str(task.get("task_id"))] = {
            "task_id": task.get("task_id"), "title": task.get("title"),
            "status": task.get("status"), "assignee_id": task.get("assignee_id"),
            "created_by": task.get("created_by"), "channel_id": task.get("channel_id"),
            "updated_at": task.get("updated_at"),
        }
    # list_messages needs a concrete channel, so walk them rather than passing
    # an empty id, which would match nothing.
    message_index: dict[str, dict[str, Any]] = {}
    for scanned_id in scanned:
        for message in STORE.list_messages(scanned_id, limit=500):
            mentions = message.get("mentions") or []
            if not mentions:
                continue
            message_index[str(message.get("message_id"))] = {
                "message_id": message.get("message_id"),
                "author_id": message.get("author_id"),
                "message_kind": message.get("message_kind"),
                "body": str(message.get("body") or "")[:240],
                "channel_id": message.get("channel_id"),
                "created_at": message.get("created_at"),
            }
            for mention in mentions:
                link(
                    str(message.get("author_id") or ""), str(mention),
                    "mention", str(message.get("message_id") or ""),
                )

    for edge in edges.values():
        nodes[edge["source"]]["out_degree"] += edge["weight"]
        nodes[edge["target"]]["in_degree"] += edge["weight"]
    for node in nodes.values():
        node["connections"] = node["out_degree"] + node["in_degree"]

    channel_sizes = []
    for channel in all_channels:
        if channel.get("kind") == "direct":
            continue
        message_count = len(STORE.list_messages(channel["channel_id"], limit=500))
        channel_sizes.append({
            "channel_id": channel["channel_id"], "name": channel["name"],
            "member_count": len(channel.get("member_ids") or []),
            "message_count": message_count,
        })
    channel_sizes.sort(key=lambda item: item["message_count"], reverse=True)

    ranked = sorted(nodes.values(), key=lambda item: item["connections"], reverse=True)
    return {
        "channel_id": channel_id,
        "nodes": ranked,
        "edges": sorted(edges.values(), key=lambda item: item["weight"], reverse=True),
        "stats": {
            "humans": sum(1 for node in nodes.values() if node["type"] == "human"),
            "agents": sum(1 for node in nodes.values() if node["type"] == "agent"),
            "connections": len(edges),
        },
        "top_members": ranked[:5],
        "channels": channel_sizes[:5],
        # Lookups so clicking an edge can name the records behind it.
        "tasks": task_index,
        "messages": message_index,
    }


#: Credential stores. Refused wherever they appear in the path, because
#: nesting a "project" under one is exactly how a workspace boundary gets
#: sidestepped.
CREDENTIAL_DIR_NAMES = frozenset({
    ".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker",
    ".mozilla", ".thunderbird", "keychains", ".password-store",
})

#: Broad containers holding both sensitive and ordinary data. Refused only when
#: the workspace *is* one of them or sits directly inside — matching them
#: anywhere would reject every path under AppData, including the system temp
#: directory, which is not a security win.
BROAD_SYSTEM_DIR_NAMES = frozenset({
    "appdata", "library", ".config", "system32", "windows",
    "system volume information", "program files", "program files (x86)",
})


def _validate_workspace(raw: str) -> str:
    """Check the workspace a local Agent will be confined to.

    The bridge enforces the boundary at execution time; this refuses the
    obviously unsafe choices before an Agent is ever created.
    """

    text = str(raw or "").strip()
    if not text:
        raise ValueError("workspace is required")
    if len(text) > 500:
        raise ValueError("workspace path is too long")

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError("workspace 必须是绝对路径")

    resolved = candidate.resolve()
    lowered = [part.lower() for part in resolved.parts]

    credential_hit = set(lowered) & CREDENTIAL_DIR_NAMES
    if credential_hit:
        raise ValueError(f"该目录涉及敏感数据，不能作为工作区：{sorted(credential_hit)[0]}")

    # A drive or filesystem root would hand over the whole machine.
    if resolved.parent == resolved:
        raise ValueError("不能把整个磁盘根目录作为工作区")
    if str(resolved) == str(Path.home()):
        raise ValueError("不能把用户主目录作为工作区，请选择具体项目目录")

    # Only the container itself and its immediate children are refused.
    for name in (lowered[-1], lowered[-2] if len(lowered) >= 2 else ""):
        if name in BROAD_SYSTEM_DIR_NAMES:
            raise ValueError(f"该目录属于系统区域，不能作为工作区：{name}")
    return str(resolved)


def _create_local_agent(body: dict[str, Any], avatar_path: str | None) -> dict[str, Any]:
    """Register an Agent that runs on the user's machine.

    The provider must be one the bridge actually knows; accepting an arbitrary
    string would create an Agent nothing can ever run.
    """

    from openharness.local_bridge.adapters import available_providers, get_adapter

    provider = str(body.get("provider") or "").strip()
    if provider not in available_providers():
        raise ValueError(
            f"未知的本地 Agent 类型：{provider or '(缺失)'}；"
            f"当前支持：{'、'.join(available_providers())}"
        )
    adapter = get_adapter(provider)
    workspace = _validate_workspace(body.get("workspace", ""))

    bridge_id = str(body.get("bridge_id") or "").strip()[:120] or "local"
    permission_mode = str(body.get("permission_mode") or "ask").strip()
    if permission_mode not in {"ask", "accept_edits", "read_only"}:
        raise ValueError("permission_mode 必须是 ask、accept_edits 或 read_only")

    # Trust the adapter for capabilities rather than whatever the page sent:
    # the client cannot grant an Agent an ability it does not have.
    capabilities = adapter.capabilities().to_dict() if adapter else {}

    return STORE.create_agent(
        name=_required_text(body, "name", 80),
        profile=str(body.get("profile") or f"运行在本机的 {provider} Agent").strip()[:500],
        role=str(body.get("role") or (adapter.display_name if adapter else provider))[:120],
        # The prompt and model belong to the local CLI, not to this record.
        system_prompt="",
        model="",
        avatar_path=avatar_path,
        agent_type="local",
        provider=provider,
        bridge_id=bridge_id,
        workspace=workspace,
        capabilities=capabilities,
        connection_config={
            "permission_mode": permission_mode,
            "session_mode": str(body.get("session_mode") or "new")[:40],
        },
    )


def _channel_attachments(channel_id: str, raw_ids: Any) -> list[dict[str, Any]]:
    """Resolve client-supplied attachment IDs to files this channel really owns."""

    if not isinstance(raw_ids, list):
        return []
    attachments: list[dict[str, Any]] = []
    for value in raw_ids[:10]:
        record = STORE.get_file(str(value))
        if record is None or record.get("channel_id") != channel_id:
            continue
        attachments.append({
            "file_id": record["file_id"], "filename": record["filename"],
            "stored_name": record["stored_name"], "media_type": record["media_type"],
            "size_bytes": record["size_bytes"], "summary": record.get("summary", ""),
        })
    return attachments


def _required_text(body: dict[str, Any], field: str, limit: int) -> str:
    value = str(body.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} is too long")
    return value


def _model_settings_payload() -> dict[str, Any]:
    return {
        "provider": "ark-plan",
        "base_url": ARK_PLAN_BASE_URL,
        "default_model": DEFAULT_MODEL,
        "api_key_configured": bool(
            os.environ.get("ARK_API_KEY") or os.environ.get("OPENAI_API_KEY")
        ),
        "models": MODEL_CATALOG,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _validation_path(name: str) -> Path:
    return PROJECT_ROOT / ".openharness" / "validation" / name


def _report_path(run_id: str | None = None) -> Path:
    if run_id:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
        candidate = PROJECT_ROOT / ".openharness" / "validation" / safe / "report.md"
        if safe == run_id and candidate.is_file():
            return candidate
    return _validation_path("full-chain-report.md")


def _snapshot() -> dict[str, Any]:
    with RUN_STATE_LOCK:
        state = dict(RUN_STATE)
    # Never show the previous compatibility summary/report while a new run is
    # still waiting for its own run_id.  Otherwise the workbench can display
    # an old ReportWriter failure as if it belonged to the current run.
    is_running = bool(state.get("running"))
    summary = state.get("summary")
    if summary is None and not is_running:
        summary = _read_json(_validation_path("full-chain.json"))
    report = None if is_running and not state.get("run_id") else _report_path(state.get("run_id"))
    return {
        **state,
        "summary": summary,
        "report_available": bool(report and report.is_file()),
        "report_path": str(report) if report and report.is_file() else None,
        "report_bytes": report.stat().st_size if report and report.is_file() else 0,
    }


def _workspace_agents(channel_id: str) -> list[dict[str, Any]]:
    """Project the latest real task state onto the Agent roster for the UI."""

    agents = STORE.list_agents()
    tasks = STORE.list_tasks(channel_id)
    by_agent: dict[str, dict[str, Any]] = {}
    for task in tasks:
        agent_id = str(task.get("assignee_id") or "")
        if not agent_id or agent_id in by_agent:
            continue
        by_agent[agent_id] = task
    for agent in agents:
        task = by_agent.get(str(agent.get("agent_id") or ""))
        if not task:
            continue
        metadata = task.get("metadata") or {}
        status = str(task.get("status") or "")
        agent["status"] = status or agent.get("status") or "online"
        agent["current_task_id"] = task.get("task_id")
        agent["task_phase"] = metadata.get("phase")
        agent["last_activity_at"] = metadata.get("last_activity_at")
        agent["elapsed_seconds"] = metadata.get("elapsed_seconds")
    return agents


def _thread_payload(message_id: str, channel_id: str | None = None) -> dict[str, Any] | None:
    """Build safe, structured thread data for the right-hand workbench pane.

    The payload deliberately exposes only collaboration artifacts, task state
    and evidence identifiers.  It never includes model hidden reasoning or a
    raw tool response.
    """

    thread = STORE.get_thread(message_id, channel_id)
    if thread is None:
        return None
    messages = [thread["root"], *thread["replies"]]
    task_ids = {
        str((message.get("metadata") or {}).get("task_id"))
        for message in messages
        if (message.get("metadata") or {}).get("task_id")
    }
    run_ids = {
        str((message.get("metadata") or {}).get("run_id"))
        for message in messages
        if (message.get("metadata") or {}).get("run_id")
    }
    artifact_ids = {
        str((message.get("metadata") or {}).get("artifact_id"))
        for message in messages
        if (message.get("metadata") or {}).get("artifact_id")
    }
    tasks = [task for task_id in sorted(task_ids) if (task := STORE.get_task(task_id))]
    artifacts = [
        artifact for artifact in STORE.list_artifacts(thread["root"]["channel_id"])
        if artifact.get("artifact_id") in artifact_ids
        or artifact.get("task_id") in task_ids
        or (artifact.get("run_id") and artifact.get("run_id") in run_ids)
    ]
    return {**thread, "tasks": tasks, "artifacts": artifacts}


def _direct_task_heartbeat(
    *,
    channel_id: str,
    task_id: str,
    agent_id: str,
    run_id: str,
    started_at: float,
    stop_event: threading.Event,
) -> None:
    """Publish honest liveness signals while one model/tool call is in flight."""

    while not stop_event.wait(5.0):
        elapsed = int(max(0, time.monotonic() - started_at))
        last_activity = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        metadata = {
            "direct_agent_task": True,
            "phase": "agent_execution",
            "last_activity_at": last_activity,
            "elapsed_seconds": elapsed,
        }
        STORE.update_task(
            task_id,
            "running",
            run_id=run_id,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )
        STORE.add_event(
            channel_id=channel_id,
            event_type="direct_agent_progress",
            payload={
                "task_id": task_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "phase": "agent_execution",
                "elapsed_seconds": elapsed,
                "last_activity_at": last_activity,
            },
        )


def _progress_event(channel_id: str, progress: dict[str, Any]) -> None:
    key = (progress.get("stage"), progress.get("message"))
    with RUN_STATE_LOCK:
        if RUN_STATE.get("last_progress") == key:
            return
        RUN_STATE["last_progress"] = key
    stage = str(progress.get("stage") or "progress")
    if "review" in stage:
        kind = "review_issue"
    elif "report" in stage:
        kind = "report_delivery"
    elif "planner" in stage:
        kind = "task_update"
    else:
        kind = "progress_update"
    body = f"[{stage}] {progress.get('message') or '研究流程继续运行'}"
    message = STORE.add_message(
        channel_id=channel_id, author_id="system", author_type="system",
        message_kind=kind, body=body, metadata={"progress": progress},
    )
    STORE.add_event(channel_id=channel_id, event_type="progress", payload={"message": message, "progress": progress})


def _wait_if_paused() -> None:
    """Cooperatively pause the flow at the next Agent/stage boundary."""
    with RUN_STATE_LOCK:
        requested = bool(RUN_STATE.get("pause_requested"))
        if requested:
            RUN_STATE["paused"] = True
    if not requested:
        return
    RUN_RESUME_EVENT.wait()
    with RUN_STATE_LOCK:
        RUN_STATE["paused"] = False
        RUN_STATE["pause_requested"] = False


def _set_pause(paused: bool) -> tuple[int, dict[str, Any]]:
    with RUN_STATE_LOCK:
        if not RUN_STATE.get("running"):
            return HTTPStatus.CONFLICT, {"error": "当前没有正在运行的研究任务", "status": _snapshot()}
        RUN_STATE["pause_requested"] = paused
        RUN_STATE["paused"] = paused
    if paused:
        RUN_RESUME_EVENT.clear()
        STORE.add_event(
            channel_id="research-room", event_type="run_paused",
            payload={"run_id": RUN_STATE.get("run_id"), "message": "已请求暂停，当前 Agent 完成当前调用后暂停。"},
        )
    else:
        RUN_RESUME_EVENT.set()
        STORE.add_event(
            channel_id="research-room", event_type="run_resumed",
            payload={"run_id": RUN_STATE.get("run_id"), "message": "已继续研究流程。"},
        )
    return HTTPStatus.OK, {"status": "paused" if paused else "running", "run": _snapshot()}


def _replay_flow_events(
    channel_id: str,
    run_id: str | None,
    summary: dict[str, Any],
    root_message_id: str | None = None,
) -> None:
    """Turn CrewAI's auditable flow events into visible channel records."""
    for flow_event in summary.get("events") or []:
        if not isinstance(flow_event, dict):
            continue
        event_id = str(flow_event.get("event_id") or "")
        event_type = str(flow_event.get("event_type") or "progress_update")
        actor_id = str(flow_event.get("actor_id") or "system")
        task_id = flow_event.get("task_id")
        artifact_id = flow_event.get("artifact_id")
        targets = list(flow_event.get("target_agent_ids") or [])
        # Keep the orchestration event contract unchanged while presenting the
        # next real collaborator in the channel instead of an abstract system.
        if targets == ["system"]:
            targets = {
                "risk": ["reviewer_arbiter"],
                "reviewer_arbiter": ["report_writer"],
                "report_writer": [],
            }.get(actor_id, [])
        if event_id and STORE.has_flow_event(channel_id, event_id):
            continue
        message_kind = "progress_update"
        if event_type in {
            "task_started", "task_handoff", "report_section_started", "report_section_retrying",
        }:
            message_kind = "task_dispatch"
            assignee = targets[0] if targets else (actor_id if actor_id in AGENT_IDS else "planner")
            if task_id:
                STORE.create_task(
                    task_id=str(task_id), channel_id=channel_id, created_by=actor_id,
                    assignee_id=assignee, title=str(flow_event.get("message") or "Agent 研究任务"),
                    run_id=run_id, status="running", metadata={"targets": targets, "flow_event_id": event_id},
                )
                STORE.update_task(str(task_id), "running", run_id=run_id)
        elif event_type in {"artifact_delivered", "report_generated"}:
            message_kind = "artifact_delivery"
            if artifact_id:
                if task_id:
                    STORE.create_task(
                        task_id=str(task_id), channel_id=channel_id, created_by=actor_id,
                        assignee_id=actor_id if actor_id in AGENT_IDS else "report_writer",
                        title=str(flow_event.get("message") or "报告或研究成果交付"),
                        run_id=run_id, status="running", metadata={"flow_event_id": event_id},
                    )
                result_projection = (
                    flow_event.get("result_projection")
                    or summary.get("agent_results", {}).get(actor_id, {})
                )
                refs = [
                    *result_projection.get("source_ids", []),
                    *result_projection.get("fact_ids", []),
                    *result_projection.get("logic_ids", []),
                    *result_projection.get("catalyst_ids", []),
                    *result_projection.get("risk_ids", []),
                ]
                STORE.upsert_artifact(
                    artifact_id=str(artifact_id), channel_id=channel_id, run_id=run_id,
                    task_id=str(task_id) if task_id else None, agent_id=actor_id,
                    title=("研究报告" if event_type == "report_generated" else f"{actor_id} 研究交付"),
                    summary=str(flow_event.get("message") or "已提交研究成果"),
                    artifact_type=("report" if event_type == "report_generated" else "research"),
                    status="submitted", refs=list(dict.fromkeys(refs)),
                    metadata={"flow_event_id": event_id, "targets": targets},
                )
                if task_id:
                    STORE.update_task(str(task_id), "completed", run_id=run_id)
        elif event_type in {"task_failed", "report_failed", "report_section_failed"}:
            message_kind = "run_failed"
            if task_id:
                STORE.update_task(str(task_id), "failed", run_id=run_id)
        elif "review" in event_type or event_type in {"issue_created", "supplement_requested"}:
            message_kind = "review_issue"
        elif event_type in {
            "report_section_started", "report_section_retrying", "report_section_completed",
            "report_section_partial", "report_assembled",
        }:
            message_kind = "report_delivery"
        body = str(flow_event.get("message") or event_type)
        body_prefix = " ".join(f"@{item}" for item in targets if item in AGENT_IDS)
        if body_prefix and message_kind in {
            "task_dispatch", "review_issue", "artifact_delivery", "report_delivery",
        }:
            body = f"{body_prefix} {body}"
        metadata = {
            "flow_event_id": event_id, "event_type": event_type,
            "target_agent_ids": targets, "task_id": task_id, "artifact_id": artifact_id,
            "run_id": run_id,
            "result_projection": flow_event.get("result_projection") or {},
        }
        message = STORE.add_message(
            channel_id=channel_id,
            thread_id=root_message_id,
            author_id=actor_id if actor_id in AGENT_IDS else "system",
            author_type="agent" if actor_id in AGENT_IDS else "system",
            message_kind=message_kind, body=body, mentions=targets,
            metadata=metadata,
        )
        STORE.add_event(channel_id=channel_id, event_type="flow_event", payload={"message": message, "flow_event": flow_event})


def _direct_failure_message(result: Any) -> str:
    """Explain a failed direct task in terms the operator can act on.

    The raw runtime error names an internal condition. Say what ran out and
    what to do about it, while still carrying the original text so the cause
    stays auditable.
    """

    error = str(getattr(result, "error", "") or "")
    failure_class = str(getattr(result, "failure_class", "") or "unknown")
    retry_count = getattr(result, "retry_count", 0)

    advice = ""
    if "MaxTurnsExceeded" in error or "maximum turn limit" in error:
        advice = "这次的问题超出了该角色单次直接任务的回合预算。把问题拆小一点再派，或改用 @Planner 走完整研究。"
    elif failure_class == "timeout":
        advice = "模型或工具调用超时。稍后重试；若反复超时，缩小问题范围。"
    elif failure_class in {"network", "rate_limit"}:
        advice = "与模型服务的连接不稳定或触发限流，稍后重试。"
    elif failure_class == "provider_auth":
        advice = "模型服务认证失败，请检查 Provider 配置。"
    elif failure_class == "empty_response":
        advice = "模型返回了空回复，已自动重试仍未成功。可以再派一次。"

    detail = f"（失败分类：{failure_class}，已重试：{retry_count} 次）"
    if advice:
        return f"这项直接任务未完成：{advice}\n原始错误：{error or result.status} {detail}"
    return f"这项直接任务未完成：{error or result.status}。{detail}"


def _agent_handoff_targets(
    body: str, from_agent_id: str, structured_output: Any = None,
) -> list[str]:
    """Return the Agents this delivery hands work to.

    The delivery message is a summary assembled from a couple of chosen output
    fields, so a mention the model wrote elsewhere would never appear in it.
    The Agent's own structured output is scanned too — that is where its prose
    actually lives.

    Only an Agent that accepts direct tasks qualifies, and an Agent never hands
    work to itself.
    """

    scanned = [body]
    if isinstance(structured_output, dict):
        scanned.append(json.dumps(structured_output, ensure_ascii=False))
    mentions: list[str] = []
    for text in scanned:
        mentions.extend(_parse_mentions(text, None))
    return list(dict.fromkeys(
        agent_id for agent_id in mentions
        if agent_id in DIRECT_AGENT_IDS and agent_id != from_agent_id
    ))


def _dispatch_agent_handoffs(
    *,
    channel_id: str,
    root_message: dict[str, Any],
    from_agent_id: str,
    targets: list[str],
    objective: str,
    as_of_date: date,
    handoff_depth: int,
) -> None:
    """Turn one Agent's @mentions into queued tasks for those Agents.

    The chain stops at ``MAX_AGENT_HANDOFF_DEPTH``. When it does, the refusal is
    posted to the channel rather than dropped, so a truncated chain is visible
    instead of looking like the Agent simply never replied.
    """

    if not targets:
        return
    if handoff_depth >= MAX_AGENT_HANDOFF_DEPTH:
        blocked = STORE.add_message(
            channel_id=channel_id,
            thread_id=root_message.get("thread_id") or root_message["message_id"],
            author_id="system", author_type="system", message_kind="task_update",
            body=(
                f"{from_agent_id} 想继续转派给 {'、'.join(targets)}，但本次交接已达上限"
                f"（{MAX_AGENT_HANDOFF_DEPTH} 层），没有再创建任务。需要继续请手动 @。"
            ),
            metadata={"handoff_blocked": True, "targets": targets},
        )
        STORE.add_event(
            channel_id=channel_id, event_type="handoff_blocked",
            payload={"message": blocked, "targets": targets},
        )
        return

    tasks = _start_direct_agent_tasks(
        channel_id=channel_id,
        root_message=root_message,
        agent_ids=targets,
        objective=objective,
        as_of_date=as_of_date,
        created_by=from_agent_id,
        handoff_depth=handoff_depth + 1,
    )
    if not tasks:
        return
    dispatched = STORE.add_message(
        channel_id=channel_id,
        thread_id=root_message.get("thread_id") or root_message["message_id"],
        author_id=from_agent_id, author_type="agent", message_kind="task_dispatch",
        body=" ".join(f"@{item}" for item in targets) + " 已按上面的结论为你们创建任务。",
        mentions=targets,
        metadata={
            "agent_handoff": True, "from_agent_id": from_agent_id,
            "task_ids": [task["task_id"] for task in tasks],
            "handoff_depth": handoff_depth + 1,
        },
    )
    STORE.add_event(
        channel_id=channel_id, event_type="agent_handoff",
        payload={"message": dispatched, "tasks": tasks, "from_agent_id": from_agent_id},
    )


def _run_direct_agent_task(
    *,
    channel_id: str,
    root_message_id: str,
    task_id: str,
    agent_id: str,
    objective: str,
    as_of_date: date,
    summary: dict[str, Any] | None,
    handoff_depth: int = 0,
) -> None:
    """Execute one non-Planner Agent without starting the full CrewAI flow."""

    run_id: str | None = None
    try:
        from openharness.invest_research.orchestration.research_flow import (
            build_agent_delivery_message,
        )
        from openharness.invest_research.orchestration.runtime_gateway import (
            OpenHarnessRuntimeGateway,
        )

        input_payload, context_package = build_direct_agent_input(
            agent_id=agent_id,
            objective=objective,
            task_id=task_id,
            summary=summary,
            evidence_store=EVIDENCE_STORE,
            as_of_date=as_of_date,
        )
        run_id = str(input_payload["run_id"])
        started_at = time.monotonic()
        stop_heartbeat = threading.Event()
        STORE.update_task(
            task_id,
            "running",
            run_id=run_id,
            metadata_json=json.dumps(
                {
                    "direct_agent_task": True,
                    "root_message_id": root_message_id,
                    "objective": objective,
                    "phase": "preparing_context",
                    "last_activity_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "elapsed_seconds": 0,
                },
                ensure_ascii=False,
            ),
        )
        accepted = STORE.add_message(
            channel_id=channel_id,
            thread_id=root_message_id,
            author_id=agent_id,
            author_type="agent",
            message_kind="agent_message",
            body=(
                "已收到直接任务。我会复用本频道最近一次研究的参数卡和证据，"
                "按本角色权限完成分析；证据不足的部分会明确标记为待验证。"
            ),
            metadata={
                "task_id": task_id,
                "run_id": run_id,
                "direct_agent_task": True,
                "root_message_id": root_message_id,
            },
        )
        STORE.add_event(
            channel_id=channel_id,
            event_type="direct_agent_started",
            payload={"message": accepted, "task_id": task_id, "agent_id": agent_id},
        )

        heartbeat = threading.Thread(
            target=_direct_task_heartbeat,
            kwargs={
                "channel_id": channel_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "run_id": run_id,
                "started_at": started_at,
                "stop_event": stop_heartbeat,
            },
            daemon=True,
            name=f"workbench-heartbeat-{task_id}",
        )
        heartbeat.start()

        budget = direct_task_budget(agent_id)
        selected_agent = STORE.get_agent(agent_id) or {}
        selected_model = str(selected_agent.get("model") or DEFAULT_MODEL)
        result = asyncio.run(
            OpenHarnessRuntimeGateway().execute(
                agent_id=agent_id,
                input_payload=input_payload,
                task_prompt=direct_task_prompt(agent_id, objective),
                context_package=context_package,
                max_turns=budget["max_turns"],
                max_output_tokens=budget["max_output_tokens"],
                tool_call_limits=budget["tool_call_limits"],
                timeout_seconds=180,
                retry_transient=True,
                model_override=selected_model,
            )
        )
        projection = {
            "execution_status": result.status,
            "output_status": (result.structured_output or {}).get("status"),
            "failure_class": result.failure_class,
            "retry_count": result.retry_count,
            "degraded": result.degraded,
            "tool_names": list(dict.fromkeys(call.tool_name for call in result.tool_calls)),
            "total_tokens": result.usage.total_tokens,
            "source_ids": result.source_ids,
            "fact_ids": result.fact_ids,
            "logic_ids": result.logic_ids,
            "catalyst_ids": result.catalyst_ids,
            "risk_ids": result.risk_ids,
            "warnings": result.warnings[:5],
            "model": result.model,
        }
        if result.status == "succeeded":
            refs = list(
                dict.fromkeys(
                    [
                        *result.source_ids,
                        *result.fact_ids,
                        *result.logic_ids,
                        *result.catalyst_ids,
                        *result.risk_ids,
                    ]
                )
            )
            if result.artifact_id:
                STORE.upsert_artifact(
                    artifact_id=result.artifact_id,
                    channel_id=channel_id,
                    run_id=run_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    artifact_type="direct_research",
                    title=f"{agent_id} 直接任务交付",
                    summary=build_agent_delivery_message(result),
                    status="submitted",
                    refs=refs,
                    metadata={
                        "direct_agent_task": True,
                        "root_message_id": root_message_id,
                        "result_projection": projection,
                    },
                )
            STORE.update_task(task_id, "completed", run_id=run_id)
            delivery_body = build_agent_delivery_message(result)
            handoffs = _agent_handoff_targets(
                delivery_body, agent_id, result.structured_output
            )
            delivered = STORE.add_message(
                channel_id=channel_id,
                thread_id=root_message_id,
                author_id=agent_id,
                author_type="agent",
                message_kind="artifact_delivery",
                body=delivery_body,
                mentions=handoffs,
                metadata={
                    "task_id": task_id,
                    "run_id": run_id,
                    "artifact_id": result.artifact_id,
                    "direct_agent_task": True,
                    "root_message_id": root_message_id,
                    "result_projection": projection,
                },
            )
            STORE.add_event(
                channel_id=channel_id,
                event_type="direct_agent_completed",
                payload={"message": delivered, "task_id": task_id, "agent_id": agent_id},
            )
            _dispatch_agent_handoffs(
                channel_id=channel_id,
                root_message=delivered,
                from_agent_id=agent_id,
                targets=handoffs,
                objective=delivery_body,
                as_of_date=as_of_date,
                handoff_depth=handoff_depth,
            )
        else:
            STORE.update_task(
                task_id,
                "failed",
                run_id=run_id,
                metadata_json=json.dumps(
                    {
                        "direct_agent_task": True,
                        "root_message_id": root_message_id,
                        "failure_class": result.failure_class,
                        "error": result.error,
                    },
                    ensure_ascii=False,
                ),
            )
            failed = STORE.add_message(
                channel_id=channel_id,
                thread_id=root_message_id,
                author_id=agent_id,
                author_type="agent",
                message_kind="task_update",
                body=_direct_failure_message(result),
                metadata={
                    "task_id": task_id,
                    "run_id": run_id,
                    "direct_agent_task": True,
                    "root_message_id": root_message_id,
                    "result_projection": projection,
                },
            )
            STORE.add_event(
                channel_id=channel_id,
                event_type="direct_agent_failed",
                payload={"message": failed, "task_id": task_id, "agent_id": agent_id},
            )
    except DirectAgentTaskError as exc:
        STORE.update_task(
            task_id,
            "blocked",
            run_id=run_id,
            metadata_json=json.dumps(
                {
                    "direct_agent_task": True,
                    "root_message_id": root_message_id,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
        )
        blocked = STORE.add_message(
            channel_id=channel_id,
            thread_id=root_message_id,
            author_id=agent_id,
            author_type="agent",
            message_kind="task_update",
            body=f"暂时无法执行这项任务：{exc}",
            metadata={
                "task_id": task_id,
                "direct_agent_task": True,
                "root_message_id": root_message_id,
            },
        )
        STORE.add_event(
            channel_id=channel_id,
            event_type="direct_agent_blocked",
            payload={"message": blocked, "task_id": task_id, "agent_id": agent_id},
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        STORE.update_task(
            task_id,
            "failed",
            run_id=run_id,
            metadata_json=json.dumps(
                {
                    "direct_agent_task": True,
                    "root_message_id": root_message_id,
                    "error": error,
                },
                ensure_ascii=False,
            ),
        )
        failed = STORE.add_message(
            channel_id=channel_id,
            thread_id=root_message_id,
            author_id=agent_id,
            author_type="agent",
            message_kind="task_update",
            body=f"直接任务执行失败：{error}",
            metadata={
                "task_id": task_id,
                "direct_agent_task": True,
                "root_message_id": root_message_id,
            },
        )
        STORE.add_event(
            channel_id=channel_id,
            event_type="direct_agent_failed",
            payload={"message": failed, "task_id": task_id, "agent_id": agent_id},
        )
    finally:
        # The per-Agent worker owns the job's lifetime now; there is no
        # per-task thread registry left to clean up.
        if "stop_heartbeat" in locals():
            stop_heartbeat.set()


def _start_direct_agent_tasks(
    *,
    channel_id: str,
    root_message: dict[str, Any],
    agent_ids: list[str],
    objective: str,
    as_of_date: date,
    created_by: str = "owner",
    handoff_depth: int = 0,
) -> list[dict[str, Any]]:
    """Queue one task for every mentioned Agent.

    ``created_by`` is the requester — the human, or another Agent handing work
    on. ``handoff_depth`` counts Agent-to-Agent hops so a chain terminates.
    """

    summary = _snapshot().get("summary")
    # DIRECT_AGENT_IDS is the static role list; an Agent removed from this
    # workspace must stop accepting work even though its role still exists.
    available = {item["agent_id"] for item in STORE.list_agents()}
    tasks: list[dict[str, Any]] = []
    for agent_id in dict.fromkeys(agent_ids):
        if agent_id not in DIRECT_AGENT_IDS or agent_id not in available:
            continue
        task = STORE.create_task(
            channel_id=channel_id,
            created_by=created_by,
            assignee_id=agent_id,
            title=objective[:160] or f"直接 @{agent_id} 任务",
            status="queued",
            metadata={
                "direct_agent_task": True,
                "root_message_id": root_message["message_id"],
                "objective": objective,
                "handoff_depth": handoff_depth,
            },
        )
        position = _enqueue_agent_task(
            agent_id=agent_id,
            job={
                "channel_id": channel_id,
                "root_message_id": root_message["message_id"],
                "task_id": task["task_id"],
                "agent_id": agent_id,
                "objective": objective,
                "as_of_date": as_of_date,
                "summary": summary,
                "handoff_depth": handoff_depth,
            },
        )
        STORE.update_task(
            task["task_id"],
            "queued",
            metadata_json=json.dumps(
                {**(task.get("metadata") or {}), "queue_position": position},
                ensure_ascii=False,
            ),
        )
        STORE.add_event(
            channel_id=channel_id,
            event_type="direct_agent_queued",
            payload={
                "task_id": task["task_id"], "agent_id": agent_id,
                "queue_position": position, "created_by": created_by,
            },
        )
        tasks.append(STORE.get_task(task["task_id"]) or task)
    return tasks


def _enqueue_agent_task(*, agent_id: str, job: dict[str, Any]) -> int:
    """Queue one job for an Agent and return its 1-based place in that queue."""

    with DIRECT_TASK_LOCK:
        pending = AGENT_QUEUES.setdefault(agent_id, queue.Queue())
        worker = AGENT_WORKERS.get(agent_id)
        if worker is None or not worker.is_alive():
            worker = threading.Thread(
                target=_agent_worker, args=(agent_id,), daemon=True,
                name=f"workbench-agent-{agent_id}",
            )
            AGENT_WORKERS[agent_id] = worker
            worker.start()
        pending.put(job)
        return pending.qsize()


def _agent_worker(agent_id: str) -> None:
    """Run one Agent's queued jobs strictly one at a time."""

    pending = AGENT_QUEUES[agent_id]
    while True:
        job = pending.get()
        try:
            _run_direct_agent_task(**job)
        except Exception:
            # One bad job must not kill the worker, or the Agent's whole queue
            # would stall behind it.
            log.exception("direct agent task failed outside its own handler")
        finally:
            pending.task_done()
            _publish_queue_positions(agent_id)


def _publish_queue_positions(agent_id: str) -> None:
    """Refresh the waiting count so the UI does not show a stale position."""

    with DIRECT_TASK_LOCK:
        pending = AGENT_QUEUES.get(agent_id)
        waiting = pending.qsize() if pending else 0
    for task in STORE.list_tasks():
        if task.get("assignee_id") != agent_id or task.get("status") != "queued":
            continue
        metadata = {**(task.get("metadata") or {}), "queue_waiting": waiting}
        STORE.update_task(
            task["task_id"], "queued",
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )


def _run_background(
    *,
    company: str,
    as_of_date: date,
    channel_id: str,
    task_id: str,
    root_message_id: str | None = None,
) -> None:
    run_id = None
    try:
        # Import only in the worker: the static page can start even if the
        # optional model/runtime dependencies are not importable yet.
        from openharness.invest_research.orchestration.flow_runner import run
        from openharness.invest_research.orchestration.runtime_gateway import (
            OpenHarnessRuntimeGateway,
        )

        runtime_gateway = OpenHarnessRuntimeGateway(
            model_resolver=_resolve_agent_model,
        )

        with RUN_STATE_LOCK:
            RUN_STATE["started_at"] = RUN_STATE.get("started_at")
        validation = _validation_path("full-chain-progress.json")
        previous: tuple[Any, Any] | None = None
        last_activity = time.monotonic()
        result_box: dict[str, Any] = {}

        def on_flow_event(flow_event: dict[str, Any]) -> None:
            """Project real CrewAI Flow events into the visible channel."""
            nonlocal last_activity
            last_activity = time.monotonic()
            event_run_id = str(flow_event.get("run_id") or "") or None
            with RUN_STATE_LOCK:
                if event_run_id and not RUN_STATE.get("run_id"):
                    RUN_STATE["run_id"] = event_run_id
                RUN_STATE["last_progress"] = [
                    str(flow_event.get("event_type") or "flow_event"),
                    str(flow_event.get("message") or ""),
                ]
            _replay_flow_events(
                channel_id,
                event_run_id,
                {"events": [flow_event], "agent_results": {}},
                root_message_id,
            )

        def invoke_flow() -> None:
            try:
                result_box["value"] = asyncio.run(
                    run(
                        company,
                        as_of_date,
                        complete_report=True,
                        pause_callback=_wait_if_paused,
                        event_callback=on_flow_event,
                        gateway=runtime_gateway,
                    )
                )
            except Exception as exc:  # pass the original failure to the worker
                result_box["error"] = exc

        flow_thread = threading.Thread(target=invoke_flow, name="investment-research-flow", daemon=True)
        flow_thread.start()
        timed_out = False
        while flow_thread.is_alive():
            progress = _read_json(validation)
            if progress:
                current = (progress.get("stage"), progress.get("message"))
                if current != previous:
                    _progress_event(channel_id, progress)
                    previous = current
                    last_activity = time.monotonic()
            if time.monotonic() - last_activity > FLOW_IDLE_TIMEOUT_SECONDS:
                timed_out = True
                result_box["error"] = TimeoutError(
                    f"工作台连续 {FLOW_IDLE_TIMEOUT_SECONDS} 秒没有收到研究流程进度，已安全停止等待。"
                )
                break
            time.sleep(0.8)
        if timed_out and flow_thread.is_alive():
            reason = str(result_box["error"])
            STORE.update_task(
                task_id,
                "failed",
                metadata_json=json.dumps({"error": reason, "orphaned_flow": True}, ensure_ascii=False),
            )
            message = STORE.add_message(
                channel_id=channel_id,
                author_id="system",
                author_type="system",
                message_kind="run_failed",
                body=f"研究流程未能在限定时间内返回：{reason} 请重启工作台后再试。",
                metadata={"task_id": task_id, "orphaned_flow": True},
            )
            STORE.add_event(channel_id=channel_id, event_type="run_failed", payload={"message": message})
            with RUN_STATE_LOCK:
                RUN_STATE.update({
                    "running": False,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "error": reason,
                    "flow_orphaned": True,
                    "paused": False,
                    "pause_requested": False,
                })
            RUN_RESUME_EVENT.set()
            return
        flow_thread.join()
        if "error" in result_box:
            raise result_box["error"]
        future = result_box.get("value")
        summary = future[0] if isinstance(future, tuple) else future
        run_id = summary.get("run_id") if isinstance(summary, dict) else None
        report = _report_path(run_id)
        report_generated = bool(summary.get("report_generated")) if isinstance(summary, dict) else False
        report_available = report.is_file()
        if not report_generated and not report_available:
            reason = (summary.get("error") if isinstance(summary, dict) else None) or "流程结束，但没有生成报告文件"
            STORE.update_task(task_id, "failed", run_id=run_id, metadata_json=json.dumps({"summary": summary, "error": reason}, ensure_ascii=False))
            message = STORE.add_message(
                channel_id=channel_id, author_id="system", author_type="system",
                message_kind="run_failed", body=f"研究流程已结束，但报告没有生成：{reason}",
                metadata={"run_id": run_id, "summary": summary},
            )
            STORE.add_event(channel_id=channel_id, event_type="run_failed", payload={"message": message, "run_id": run_id, "summary": summary})
            with RUN_STATE_LOCK:
                RUN_STATE.update({"running": False, "run_id": run_id, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": summary, "error": str(reason)})
            return
        if isinstance(summary, dict):
            _replay_flow_events(channel_id, run_id, summary, root_message_id)
        STORE.update_task(task_id, "completed", run_id=run_id, metadata_json=json.dumps({"summary": summary}, ensure_ascii=False))
        message = STORE.add_message(
            channel_id=channel_id, author_id="report_writer", author_type="agent",
            thread_id=root_message_id,
            message_kind="report_delivery",
            body=("ReportWriter 已完成正式报告交付。点击右侧报告按钮查看 Markdown。"
                  if report_generated else
                  "正式 ReportWriter 未完整完成，系统已生成可查看的降级报告。"),
            metadata={"run_id": run_id, "report_path": str(report), "summary": summary, "fallback": not report_generated},
        )
        STORE.add_event(channel_id=channel_id, event_type="report_delivered", payload={"message": message, "run_id": run_id, "report_available": report_available, "fallback": not report_generated})
        with RUN_STATE_LOCK:
            RUN_STATE.update({"running": False, "run_id": run_id, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": summary, "error": None})
    except Exception as exc:
        STORE.update_task(task_id, "failed", run_id=run_id, metadata_json=json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        message = STORE.add_message(
            channel_id=channel_id, author_id="system", author_type="system",
            thread_id=root_message_id,
            message_kind="run_failed", body=f"研究运行失败：{type(exc).__name__}: {exc}",
            metadata={"run_id": run_id},
        )
        STORE.add_event(channel_id=channel_id, event_type="run_failed", payload={"message": message})
        with RUN_STATE_LOCK:
            RUN_STATE.update({"running": False, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "error": f"{type(exc).__name__}: {exc}"})
    finally:
        # Do not leave a paused worker blocked after the run finishes or fails.
        RUN_RESUME_EVENT.set()
        RUN_LOCK.release()


def _start_research(
    company: str,
    as_of_text: str,
    channel_id: str,
    created_by: str = "owner",
    root_message_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    try:
        as_of_date = date.fromisoformat(as_of_text)
    except ValueError:
        return HTTPStatus.BAD_REQUEST, {"error": "as_of_date must use YYYY-MM-DD"}
    if not company or len(company) > 120:
        return HTTPStatus.BAD_REQUEST, {"error": "company is required"}
    with RUN_STATE_LOCK:
        if RUN_STATE.get("flow_orphaned"):
            return HTTPStatus.CONFLICT, {"error": "上一次流程没有正常退出，请先重启工作台", "status": _snapshot()}
    if not RUN_LOCK.acquire(blocking=False):
        return HTTPStatus.CONFLICT, {"error": "A research run is already running", "status": _snapshot()}
    # The progress file is generated output, not durable history.  Reset it so
    # the new worker cannot replay a previous run's final progress message.
    try:
        _validation_path("full-chain-progress.json").write_text(
            json.dumps({"stage": "starting", "message": "正在启动当前研究任务"}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    task = STORE.create_task(channel_id=channel_id, created_by=created_by, assignee_id="planner", title=f"研究 {company}", status="running", metadata={"company": company, "as_of_date": as_of_text})
    STORE.update_task(task["task_id"], "running")
    STORE.add_event(channel_id=channel_id, event_type="run_started", payload={"task": task, "company": company, "as_of_date": as_of_text})
    message = STORE.add_message(
        channel_id=channel_id, author_id="planner", author_type="agent", message_kind="task_dispatch",
        body=f"@Fundamental @IndustryCompetition @MarketCatalyst 已收到研究任务：{company}（基准日 {as_of_text}）。先规划范围，再启动并行研究。",
        mentions=["fundamental", "industry_competition", "market_catalyst"], metadata={"task_id": task["task_id"], "company": company},
    )
    with RUN_STATE_LOCK:
        RUN_STATE.update({"running": True, "run_id": None, "task_id": task["task_id"], "company": company, "as_of_date": as_of_text, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "finished_at": None, "summary": None, "error": None, "last_progress": None, "paused": False, "pause_requested": False, "flow_orphaned": False})
    RUN_RESUME_EVENT.set()
    thread = threading.Thread(
        target=_run_background,
        kwargs={
            "company": company,
            "as_of_date": as_of_date,
            "channel_id": channel_id,
            "task_id": task["task_id"],
            "root_message_id": root_message_id,
        },
        daemon=True,
        name="investment-research-workbench",
    )
    thread.start()
    return HTTPStatus.ACCEPTED, {"status": "started", "task": task, "message": message}


def _parse_mentions(body: str, provided: Any) -> list[str]:
    result: list[str] = []
    if isinstance(provided, list):
        result.extend(str(item).lower().lstrip("@").replace("-", "_") for item in provided)
    for raw in re.findall(r"@([A-Za-z][A-Za-z0-9_-]*)", body):
        normalized = MENTION_ALIASES.get(raw.lower())
        if normalized:
            result.append(normalized)
    return list(dict.fromkeys(item for item in result if item in AGENT_IDS))


def _ensure_flow_events_projected(channel_id: str) -> dict[str, Any]:
    """Self-heal the UI if a worker finished before event projection was enabled."""
    snapshot = _snapshot()
    summary = snapshot.get("summary")
    if isinstance(summary, dict) and summary.get("run_id") and summary.get("events"):
        _replay_flow_events(channel_id, str(summary["run_id"]), summary)
    return snapshot


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "OpenHarnessWorkbench/0.1"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        data = payload if isinstance(payload, bytes) else _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > max_bytes:
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path == "/api/workspace":
            channel_id = query.get("channel_id", ["research-room"])[0]
            # The files pane is workspace wide, so it asks for every channel's
            # files and filters client-side.
            files_scope = None if query.get("files", [""])[0] == "all" else channel_id
            snapshot = _ensure_flow_events_projected("research-room")
            _sync_agent_files(channel_id, snapshot.get("run_id") or RUN_STATE.get("run_id"))
            self._send(200, {"workspace": {"workspace_id": "default", "name": "AI帮投研助手"}, "channels": STORE.list_channels(), "agents": _workspace_agents("research-room"), "tasks": STORE.list_tasks(), "artifacts": STORE.list_artifacts(), "files": STORE.list_files(files_scope), "removed_agents": STORE.removed_builtin_agents(), "bridge_port": LOCAL_BRIDGE_PORT, "run": snapshot, "event_seq": STORE.latest_event_seq("research-room"), "model_settings": _model_settings_payload()})
        elif path == "/api/channels":
            self._send(200, STORE.list_channels())
        elif path.startswith("/api/channels/") and path.endswith("/messages"):
            channel_id = path.split("/")[3]
            self._send(200, STORE.list_messages(channel_id, include_replies=False))
        elif path.startswith("/api/threads/"):
            message_id = path.split("/")[3]
            payload = _thread_payload(message_id, query.get("channel_id", [None])[0])
            if payload is None:
                self._send(404, {"error": "thread not found"})
            else:
                self._send(200, payload)
        elif path == "/api/agents":
            self._send(200, STORE.list_agents())
        elif path.startswith("/api/agents/") and path.endswith("/skills"):
            agent_id = path.split("/")[3]
            if STORE.get_agent(agent_id) is None:
                self._send(404, {"error": "Agent not found"})
                return
            self._send(200, STORE.list_agent_skills(agent_id))
        elif path.startswith("/api/agents/") and path.endswith("/messages"):
            agent_id = path.split("/")[3]
            agent = STORE.get_agent(agent_id)
            if agent is None:
                self._send(404, {"error": "Agent not found"})
                return
            channel = STORE.ensure_direct_channel(agent_id, str(agent.get("name") or agent_id))
            self._send(200, {
                "channel": channel,
                "agent": agent,
                "messages": STORE.list_messages(channel["channel_id"]),
            })
        elif path == "/api/local-providers":
            # The catalogue of local Agent types this platform can register.
            # Detection of what is actually installed happens on the bridge.
            from openharness.local_bridge.adapters import available_providers, get_adapter

            catalogue = []
            for provider in available_providers():
                adapter = get_adapter(provider)
                if adapter is None:
                    continue
                catalogue.append({
                    "provider": adapter.provider,
                    "display_name": adapter.display_name,
                    "capabilities": adapter.capabilities().to_dict(),
                })
            self._send(200, {"providers": catalogue, "bridge_port": LOCAL_BRIDGE_PORT})
        elif path == "/api/models":
            self._send(200, _model_settings_payload())
        elif path == "/api/tasks":
            self._send(200, STORE.list_tasks(query.get("channel_id", [None])[0]))
        elif path == "/api/artifacts":
            self._send(200, STORE.list_artifacts(query.get("channel_id", [None])[0]))
        elif path == "/api/search":
            try:
                since_days = int(query.get("since", ["0"])[0])
            except ValueError:
                since_days = 0
            self._send(200, _search_workspace(
                query=query.get("q", [""])[0],
                scope=query.get("scope", ["all"])[0],
                sender=query.get("sender", [""])[0],
                channel_id=query.get("channel_id", [""])[0],
                since_days=since_days,
                sort=query.get("sort", ["relevance"])[0],
            ))
        elif path == "/api/graph":
            self._send(200, _relationship_graph(query.get("channel_id", [""])[0]))
        elif path == "/api/files":
            # No channel_id means every channel: the files pane is workspace
            # wide and filters client-side.
            channel_id = query.get("channel_id", [""])[0]
            _sync_agent_files(channel_id or "research-room", RUN_STATE.get("run_id"))
            self._send(200, STORE.list_files(channel_id or None))
        elif path.startswith("/api/files/") and path.endswith("/download"):
            self._serve_file(path.split("/")[3])
        elif path == "/api/research/status":
            self._send(200, _snapshot())
        elif path == "/api/research/report":
            report = _report_path(query.get("run_id", [None])[0])
            if not report.is_file():
                self._send(404, {"error": "report not available"})
            else:
                self._send(200, report.read_bytes(), "text/markdown; charset=utf-8")
        elif path == "/api/events":
            self._send_events(query)
        elif path.startswith("/uploads/avatars/"):
            self._serve_avatar(path)
        else:
            self._serve_static(path)

    def _send_events(self, query: dict[str, list[str]]) -> None:
        channel_id = query.get("channel_id", ["research-room"])[0]
        try:
            after = int(query.get("after", ["0"])[0])
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Establish the stream immediately.  Waiting for the first event before
        # sending headers makes EventSource look stuck and delays every update.
        try:
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

        deadline = time.time() + 20
        next_heartbeat = time.time() + 5
        cursor = after
        try:
            while time.time() < deadline:
                events = STORE.events_since(channel_id, cursor)
                for event in events:
                    # Always use the default SSE message event. The original
                    # type remains in the JSON payload so one listener handles
                    # every current and future message kind.
                    self.wfile.write(f"id: {event['event_seq']}\ndata: ".encode("utf-8"))
                    self.wfile.write(_json_bytes(event))
                    self.wfile.write(b"\n\n")
                    cursor = max(cursor, int(event["event_seq"]))
                if events:
                    self.wfile.flush()
                if time.time() >= next_heartbeat:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    next_heartbeat = time.time() + 5
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Normal when the browser reconnects, reloads or closes the tab.
            return
        finally:
            self.close_connection = True

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in candidate.parents and candidate != WEB_ROOT:
            self._send(403, {"error": "forbidden"})
            return
        if not candidate.is_file():
            self._send(404, {"error": "not found"})
            return
        content_types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}
        self._send(200, candidate.read_bytes(), content_types.get(candidate.suffix, "application/octet-stream"))

    def _serve_file(self, file_id: str) -> None:
        record = STORE.get_file(file_id)
        if record is None:
            self._send(404, {"error": "file not found"})
            return
        candidate = _resolve_stored_file(record)
        if candidate is None:
            self._send(410, {"error": "file is no longer available on disk"})
            return
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", str(record.get("media_type") or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # RFC 5987 keeps Chinese file names intact across browsers.
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(str(record.get('filename') or 'file'))}",
        )
        self.end_headers()
        self.wfile.write(data)

    def _serve_avatar(self, path: str) -> None:
        filename = Path(path).name
        candidate = (AVATAR_ROOT / filename).resolve()
        if candidate.parent != AVATAR_ROOT.resolve() or not candidate.is_file():
            self._send(404, {"error": "avatar not found"})
            return
        content_types = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
        self._send(200, candidate.read_bytes(), content_types.get(candidate.suffix.lower(), "application/octet-stream"))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        is_upload = path.startswith("/api/channels/") and path.endswith("/files")
        body = self._body(MAX_FILE_REQUEST_BYTES if is_upload else MAX_REQUEST_BYTES)
        if body is None:
            self._send(400, {"error": "invalid JSON request"})
            return
        if is_upload:
            channel_id = path.split("/")[3]
            try:
                record = _store_channel_upload(
                    channel_id=channel_id,
                    filename=str(body.get("filename") or ""),
                    data_url=str(body.get("data_url") or ""),
                    summary=str(body.get("summary") or "").strip()[:500],
                )
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            STORE.add_event(
                channel_id=channel_id, event_type="file_created", payload={"file": record}
            )
            self._send(201, record)
            return
        if path == "/api/research/pause":
            status, payload = _set_pause(True)
            self._send(status, payload)
            return
        if path == "/api/research/resume":
            status, payload = _set_pause(False)
            self._send(status, payload)
            return
        if path == "/api/research/run":
            status, payload = _start_research(str(body.get("company") or "").strip(), str(body.get("as_of_date") or "").strip(), str(body.get("channel_id") or "research-room"), "owner")
            self._send(status, payload)
            return
        if path.startswith("/api/agents/") and path.endswith("/restore"):
            agent_id = path.split("/")[3]
            if not STORE.set_builtin_agent_removed(agent_id, False):
                self._send(404, {"error": "removable built-in Agent not found"})
                return
            self._send(200, STORE.get_agent(agent_id))
            return
        if path.startswith("/api/agents/") and path.endswith("/skills"):
            agent_id = path.split("/")[3]
            if STORE.get_agent(agent_id) is None:
                self._send(404, {"error": "Agent not found"})
                return
            try:
                skill = _store_agent_skill(
                    agent_id=agent_id,
                    filename=str(body.get("filename") or ""),
                    data_url=str(body.get("data_url") or ""),
                    name=str(body.get("name") or ""),
                    description=str(body.get("description") or ""),
                )
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(201, skill)
            return
        if path.startswith("/api/agents/") and path.endswith("/messages"):
            agent_id = path.split("/")[3]
            agent = STORE.get_agent(agent_id)
            if agent is None:
                self._send(404, {"error": "Agent not found"})
                return
            text = str(body.get("body") or "").strip()
            if not text:
                self._send(400, {"error": "message body is required"})
                return
            channel = STORE.ensure_direct_channel(agent_id, str(agent.get("name") or agent_id))
            channel_id = channel["channel_id"]
            message = STORE.add_message(
                channel_id=channel_id, author_id="owner", author_type="human",
                message_kind="user_message", body=text, mentions=[agent_id],
                metadata={"direct_message": True},
            )
            STORE.add_event(
                channel_id=channel_id, event_type="message_created", payload={"message": message}
            )
            if agent_id not in DIRECT_AGENT_IDS:
                self._send(201, {
                    "message": message, "channel": channel, "status": "recorded",
                    "notice": f"{agent.get('name') or agent_id} 不接受单独任务；请在项目频道 @Planner 启动完整研究。",
                })
                return
            try:
                as_of = date.fromisoformat(
                    str(body.get("as_of_date") or time.strftime("%Y-%m-%d"))
                )
            except ValueError:
                self._send(400, {"error": "as_of_date must use YYYY-MM-DD"})
                return
            tasks = _start_direct_agent_tasks(
                channel_id=channel_id, root_message=message, agent_ids=[agent_id],
                objective=text, as_of_date=as_of,
            )
            self._send(HTTPStatus.ACCEPTED, {
                "message": message, "channel": channel, "status": "agent_started",
                "agent_ids": [agent_id], "tasks": tasks,
            })
            return
        if path == "/api/agents":
            # A local Agent is described by which provider, on which bridge, in
            # which workspace. Its model and prompt live in the CLI on the
            # user's machine, so the hosted validation does not apply to it.
            if str(body.get("agent_type") or "hosted") == "local":
                try:
                    avatar_path = _save_avatar(str(body.get("avatar_data_url") or ""))
                    agent = _create_local_agent(body, avatar_path)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                self._send(201, agent)
                return
            try:
                avatar_path = _save_avatar(str(body.get("avatar_data_url") or ""))
                model = _required_text(body, "model", 120)
                if model not in model_ids():
                    raise ValueError("model is not available in the Ark Plan catalog")
                agent = STORE.create_agent(
                    name=_required_text(body, "name", 80),
                    profile=_required_text(body, "profile", 500),
                    role=_required_text(body, "role", 120),
                    system_prompt=_required_text(body, "system_prompt", 12000),
                    model=model,
                    avatar_path=avatar_path,
                )
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._send(201, agent)
            return
        if path == "/api/channels":
            try:
                name = _required_text(body, "name", 80)
                topic = _required_text(body, "topic", 240)
                description = str(body.get("description") or "").strip()
                raw_members = body.get("member_ids")
                if not isinstance(raw_members, list) or not raw_members:
                    raise ValueError("member_ids must contain at least one Agent")
                known_agents = {item["agent_id"] for item in STORE.list_agents() if item.get("enabled", True)}
                member_ids = [str(item) for item in raw_members]
                if any(item not in known_agents for item in member_ids):
                    raise ValueError("member_ids contains an unknown or disabled Agent")
                channel = STORE.create_channel(
                    name=name, topic=topic, description=description, member_ids=member_ids
                )
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            STORE.add_event(channel_id=channel["channel_id"], event_type="channel_created", payload={"channel": channel})
            self._send(201, channel)
            return
        if path.startswith("/api/channels/") and path.endswith("/messages"):
            channel_id = path.split("/")[3]
            requested_thread_id = str(body.get("thread_id") or "").strip() or None
            if requested_thread_id and STORE.get_thread(requested_thread_id, channel_id) is None:
                self._send(404, {"error": "thread not found"})
                return
            attachments = _channel_attachments(channel_id, body.get("attachment_ids"))
            message = STORE.add_message(
                channel_id=channel_id, author_id="owner", author_type="human", message_kind="user_message",
                body=str(body.get("body") or "").strip(), mentions=_parse_mentions(str(body.get("body") or ""), body.get("mentions")), thread_id=requested_thread_id,
                metadata={"demo": False, "attachments": attachments},
            )
            for attachment in attachments:
                STORE.add_file(
                    file_id=attachment["file_id"], channel_id=channel_id,
                    message_id=message["message_id"], owner_id="owner", owner_type="human",
                    source="upload", filename=attachment["filename"],
                    stored_name=attachment["stored_name"], media_type=attachment["media_type"],
                    size_bytes=attachment["size_bytes"], summary=attachment.get("summary", ""),
                )
            STORE.add_event(
                channel_id=channel_id,
                event_type="message_created",
                payload={"message": message},
            )
            mentions = message["mentions"]
            # Planner starts a new complete research run only from the channel
            # timeline.  Inside an existing thread, the research Agents handle
            # targeted follow-up work without accidentally restarting a run.
            if message["body"] and "planner" in mentions and not requested_thread_id:
                company = str(body.get("company") or "科大讯飞").strip()
                as_of = str(body.get("as_of_date") or time.strftime("%Y-%m-%d"))
                status, payload = _start_research(
                    company,
                    as_of,
                    channel_id,
                    "owner",
                    root_message_id=message["message_id"],
                )
                payload["message"] = message
                self._send(status, payload)
            elif message["body"] and any(agent_id in DIRECT_AGENT_IDS for agent_id in mentions):
                as_of_text = str(body.get("as_of_date") or time.strftime("%Y-%m-%d"))
                try:
                    as_of = date.fromisoformat(as_of_text)
                except ValueError:
                    self._send(400, {"error": "as_of_date must use YYYY-MM-DD"})
                    return
                direct_agents = [
                    agent_id for agent_id in mentions if agent_id in DIRECT_AGENT_IDS
                ]
                tasks = _start_direct_agent_tasks(
                    channel_id=channel_id,
                    root_message={
                        **message,
                        "message_id": requested_thread_id or message["message_id"],
                    },
                    agent_ids=direct_agents,
                    objective=message["body"],
                    as_of_date=as_of,
                )
                if not tasks:
                    # Every mentioned Agent was removed from the workspace.
                    self._send(201, {
                        "message": message, "status": "recorded",
                        "notice": "被 @ 的 Agent 已移出工作区，没有创建任务。可在左侧「已移除」里恢复。",
                    })
                    return
                self._send(
                    HTTPStatus.ACCEPTED,
                    {
                        "message": message,
                        "status": "agents_started",
                        "agent_ids": [task["assignee_id"] for task in tasks],
                        "tasks": tasks,
                    },
                )
            else:
                self._send(
                    201,
                    {
                        "message": message,
                        "status": "recorded",
                        "notice": "消息已记录；@任意 Agent 可创建直接任务，@Planner 可启动完整研究。",
                    },
                )
            return
        self._send(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        body = self._body()
        if body is None:
            self._send(400, {"error": "invalid JSON request"})
            return
        if path.startswith("/api/channels/") and len(path.split("/")) == 4:
            channel_id = path.split("/")[3]
            if not any(item["channel_id"] == channel_id for item in STORE.list_channels()):
                self._send(404, {"error": "channel not found"})
                return
            try:
                updates = {
                    "name": _required_text(body, "name", 80),
                    "topic": str(body.get("topic") or "").strip()[:240],
                    "description": str(body.get("description") or "").strip()[:1000],
                }
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            updated = STORE.update_channel(channel_id, **updates)
            STORE.add_event(
                channel_id=channel_id, event_type="channel_updated",
                payload={"channel": updated},
            )
            self._send(200, updated)
            return
        if path.startswith("/api/skills/"):
            skill_id = path.split("/")[3]
            if STORE.get_agent_skill(skill_id) is None:
                self._send(404, {"error": "skill not found"})
                return
            if "enabled" not in body:
                self._send(400, {"error": "enabled is required"})
                return
            self._send(200, STORE.set_agent_skill_enabled(skill_id, bool(body["enabled"])))
            return
        if path.startswith("/api/agents/"):
            agent_id = path.split("/")[3]
            agent = STORE.get_agent(agent_id)
            if not agent:
                self._send(404, {"error": "Agent not found"})
                return
            updates = {key: body[key] for key in ("name", "profile", "role", "system_prompt", "model", "enabled") if key in body}
            if "model" in updates and str(updates["model"]) not in model_ids():
                self._send(400, {"error": "model is not available in the Ark Plan catalog"})
                return
            if agent.get("type") != "custom":
                # A built-in role's prompt and contract stay fixed, but the
                # operator still owns its model choice and its avatar.
                if set(updates) - {"model"}:
                    self._send(400, {"error": "built-in Agents only allow model and avatar changes"})
                    return
                if not updates and not body.get("avatar_data_url"):
                    self._send(400, {"error": "nothing to update"})
                    return
                updated = agent
                if "model" in updates:
                    updated = STORE.update_agent_model(agent_id, str(updates["model"])) or agent
                if body.get("avatar_data_url"):
                    try:
                        avatar_path = _save_avatar(str(body["avatar_data_url"]))
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                        return
                    if avatar_path:
                        updated = STORE.update_agent_avatar(agent_id, avatar_path) or updated
                self._send(200, updated)
                return
            if body.get("avatar_data_url"):
                try:
                    updates["avatar_path"] = _save_avatar(str(body["avatar_data_url"]))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
            updated = STORE.update_agent(agent_id, **updates)
            self._send(200, updated)
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/channels/") and len(path.split("/")) == 4:
            channel_id = path.split("/")[3]
            channels = STORE.list_channels()
            channel = next(
                (item for item in channels if item["channel_id"] == channel_id), None
            )
            if channel is None:
                self._send(404, {"error": "channel not found"})
                return
            if any(
                task.get("status") == "running"
                for task in STORE.list_tasks(channel_id)
            ):
                self._send(409, {"error": "频道内还有正在执行的任务，无法删除。"})
                return
            # A workspace with no project channel has nowhere to send a message.
            projects = [item for item in channels if item.get("kind") != "direct"]
            if channel.get("kind") != "direct" and len(projects) <= 1:
                self._send(409, {"error": "这是最后一个项目频道，删除后无处发送消息。"})
                return
            removed = STORE.delete_channel(channel_id)
            uploads = (FILE_ROOT / _safe_filename(channel_id)).resolve()
            if FILE_ROOT.resolve() in uploads.parents and uploads.is_dir():
                shutil.rmtree(uploads, ignore_errors=True)
            self._send(200, {"deleted": True, "channel_id": channel_id, "removed": removed})
            return
        if path.startswith("/api/skills/"):
            skill_id = path.split("/")[3]
            record = STORE.get_agent_skill(skill_id)
            if record is None:
                self._send(404, {"error": "skill not found"})
                return
            stored = (SKILL_ROOT / str(record.get("stored_name") or "")).resolve()
            if SKILL_ROOT.resolve() in stored.parents and stored.is_file():
                stored.unlink()
            STORE.delete_agent_skill(skill_id)
            self._send(200, {"deleted": True, "skill_id": skill_id})
            return
        if path.startswith("/api/agents/"):
            agent_id = path.split("/")[3]
            agent = STORE.get_agent(agent_id)
            if not agent:
                self._send(404, {"error": "Agent not found"})
                return
            if any(
                task.get("assignee_id") == agent_id and task.get("status") == "running"
                for task in STORE.list_tasks()
            ):
                self._send(409, {"error": "该 Agent 还有正在执行的任务，无法删除。"})
                return
            if agent.get("type") == "custom":
                STORE.delete_agent(agent_id)
                self._send(200, {"deleted": True, "agent_id": agent_id, "restorable": False})
                return
            # A built-in Agent belongs to the plugin. Removing it from the
            # workspace is reversible; deleting its definition is not ours to do.
            STORE.set_builtin_agent_removed(agent_id, True)
            self._send(200, {
                "deleted": True, "agent_id": agent_id, "restorable": True,
                "notice": f"{agent.get('name') or agent_id} 已移出工作区，可随时恢复。",
            })
            return
        self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def reconcile_orphaned_tasks() -> list[str]:
    """Close out tasks left ``running`` by a previous process.

    Task execution lives in this process, so nothing is working on them after a
    restart. Left alone they claim to be running forever: the board shows a
    phantom in-flight task and the Agent can never be removed, because removal
    refuses while a task is running.
    """

    interrupted: list[str] = []
    for task in STORE.list_tasks():
        if task.get("status") != "running":
            continue
        metadata = {
            **(task.get("metadata") or {}),
            "interrupted": True,
            "failure_class": "interrupted",
            "error": "工作台重启，这条任务的执行进程已不存在。",
        }
        STORE.update_task(
            task["task_id"], "failed",
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )
        interrupted.append(task["task_id"])
        channel_id = str(task.get("channel_id") or "")
        if channel_id:
            STORE.add_event(
                channel_id=channel_id, event_type="task_interrupted",
                payload={"task_id": task["task_id"], "agent_id": task.get("assignee_id")},
            )
    return interrupted


def build_server(port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), WorkbenchHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    interrupted = reconcile_orphaned_tasks()
    if interrupted:
        print(
            f"已将 {len(interrupted)} 条上次遗留的运行中任务标记为中断: "
            f"{', '.join(interrupted)}",
            flush=True,
        )
    server = build_server(args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"OpenHarness workbench: {url}", flush=True)
    if args.open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_server", "main"]
