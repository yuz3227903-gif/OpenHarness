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
import os
import re
import threading
import time
import webbrowser
from datetime import date
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
FLOW_IDLE_TIMEOUT_SECONDS = 360
STORE = CollaborationStore(PROJECT_ROOT)
EVIDENCE_STORE = EvidenceStore.for_project(PROJECT_ROOT)
RUN_LOCK = threading.Lock()
RUN_STATE_LOCK = threading.RLock()
DIRECT_TASK_LOCK = threading.RLock()
DIRECT_TASK_THREADS: dict[str, threading.Thread] = {}
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


def _run_direct_agent_task(
    *,
    channel_id: str,
    root_message_id: str,
    task_id: str,
    agent_id: str,
    objective: str,
    as_of_date: date,
    summary: dict[str, Any] | None,
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
            delivered = STORE.add_message(
                channel_id=channel_id,
                thread_id=root_message_id,
                author_id=agent_id,
                author_type="agent",
                message_kind="artifact_delivery",
                body=build_agent_delivery_message(result),
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
                body=(
                    f"这项直接任务未完成：{result.error or result.status}。"
                    f"（失败分类：{result.failure_class or 'unknown'}，"
                    f"已重试：{result.retry_count} 次）"
                ),
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
        if "stop_heartbeat" in locals():
            stop_heartbeat.set()
        with DIRECT_TASK_LOCK:
            DIRECT_TASK_THREADS.pop(task_id, None)


def _start_direct_agent_tasks(
    *,
    channel_id: str,
    root_message: dict[str, Any],
    agent_ids: list[str],
    objective: str,
    as_of_date: date,
) -> list[dict[str, Any]]:
    """Create and asynchronously start one task for every mentioned Agent."""

    summary = _snapshot().get("summary")
    tasks: list[dict[str, Any]] = []
    for agent_id in dict.fromkeys(agent_ids):
        if agent_id not in DIRECT_AGENT_IDS:
            continue
        task = STORE.create_task(
            channel_id=channel_id,
            created_by="owner",
            assignee_id=agent_id,
            title=objective[:160] or f"直接 @{agent_id} 任务",
            status="queued",
            metadata={
                "direct_agent_task": True,
                "root_message_id": root_message["message_id"],
                "objective": objective,
            },
        )
        thread = threading.Thread(
            target=_run_direct_agent_task,
            kwargs={
                "channel_id": channel_id,
                "root_message_id": root_message["message_id"],
                "task_id": task["task_id"],
                "agent_id": agent_id,
                "objective": objective,
                "as_of_date": as_of_date,
                "summary": summary,
            },
            daemon=True,
            name=f"workbench-{agent_id}-{task['task_id']}",
        )
        with DIRECT_TASK_LOCK:
            DIRECT_TASK_THREADS[task["task_id"]] = thread
        thread.start()
        tasks.append(task)
    return tasks


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
            snapshot = _ensure_flow_events_projected("research-room")
            _sync_agent_files(channel_id, snapshot.get("run_id") or RUN_STATE.get("run_id"))
            self._send(200, {"workspace": {"workspace_id": "default", "name": "AI帮投研助手"}, "channels": STORE.list_channels(), "agents": _workspace_agents("research-room"), "tasks": STORE.list_tasks(), "artifacts": STORE.list_artifacts(), "files": STORE.list_files(channel_id), "run": snapshot, "event_seq": STORE.latest_event_seq("research-room"), "model_settings": _model_settings_payload()})
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
        elif path == "/api/models":
            self._send(200, _model_settings_payload())
        elif path == "/api/tasks":
            self._send(200, STORE.list_tasks(query.get("channel_id", [None])[0]))
        elif path == "/api/artifacts":
            self._send(200, STORE.list_artifacts(query.get("channel_id", [None])[0]))
        elif path == "/api/files":
            channel_id = query.get("channel_id", ["research-room"])[0]
            _sync_agent_files(channel_id, RUN_STATE.get("run_id"))
            self._send(200, STORE.list_files(channel_id))
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
        if path == "/api/agents":
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
                self._send(
                    HTTPStatus.ACCEPTED,
                    {
                        "message": message,
                        "status": "agents_started",
                        "agent_ids": direct_agents,
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
                if set(updates) != {"model"} or body.get("avatar_data_url"):
                    self._send(400, {"error": "built-in Agents only allow model changes"})
                    return
                self._send(200, STORE.update_agent_model(agent_id, str(updates["model"])))
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
        if path.startswith("/api/agents/"):
            agent_id = path.split("/")[3]
            agent = STORE.get_agent(agent_id)
            if not agent or agent.get("type") != "custom":
                self._send(404, {"error": "custom Agent not found"})
                return
            if any(task.get("assignee_id") == agent_id and task.get("status") == "running" for task in STORE.list_tasks()):
                self._send(409, {"error": "running Agent cannot be deleted"})
                return
            STORE.delete_agent(agent_id)
            self._send(200, {"deleted": True, "agent_id": agent_id})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_server(port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), WorkbenchHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
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
