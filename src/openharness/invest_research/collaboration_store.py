"""SQLite-backed records for the local Slack-style research workbench."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.invest_research.agent_registry import iter_agent_entries


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class CollaborationStore:
    """Persist channels, messages, tasks and visible events in the project DB."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.database_path = (
            self.project_root / ".openharness" / "data" / "investment-research.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()
        self._seed_defaults()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workbench_workspaces (
                    workspace_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_channels (
                    channel_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'project', project_company TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_messages (
                    message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, thread_id TEXT,
                    author_id TEXT NOT NULL, author_type TEXT NOT NULL, message_kind TEXT NOT NULL,
                    body TEXT NOT NULL, mentions_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_workbench_messages_channel
                    ON workbench_messages(channel_id, created_at);
                CREATE TABLE IF NOT EXISTS workbench_tasks (
                    task_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, run_id TEXT,
                    created_by TEXT NOT NULL, assignee_id TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_workbench_tasks_channel
                    ON workbench_tasks(channel_id, updated_at);
                CREATE TABLE IF NOT EXISTS workbench_events (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    channel_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_artifacts (
                    artifact_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, run_id TEXT,
                    task_id TEXT, agent_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
                    title TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
                    refs_json TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )

    def _seed_defaults(self) -> None:
        # Keep seed values ASCII here; the UI supplies the Chinese display labels.
        with self._lock, self._connect() as connection:
            timestamp = _now()
            connection.execute(
                "INSERT OR IGNORE INTO workbench_workspaces(workspace_id, name, created_at) VALUES (?, ?, ?)",
                ("default", "AI Investment Research", timestamp),
            )
            connection.execute(
                """INSERT OR IGNORE INTO workbench_channels(
                    channel_id, workspace_id, name, kind, project_company, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                ("research-room", "default", "research-room", "project", "科大讯飞", timestamp),
            )
            exists = connection.execute(
                "SELECT 1 FROM workbench_messages WHERE channel_id=? LIMIT 1", ("research-room",)
            ).fetchone()
            if exists is None:
                self._insert_message(
                    connection, channel_id="research-room", author_id="system", author_type="system",
                    message_kind="system_message",
                    body="欢迎来到科大讯飞研究室。可以在这里通过 @Agent 发起研究任务。",
                    metadata={"demo": True},
                )
                self._insert_message(
                    connection, channel_id="research-room", author_id="planner", author_type="agent",
                    message_kind="agent_message",
                    body="我已准备好接收研究任务。你可以发送：@Planner 请研究科大讯飞。",
                    metadata={"demo": True},
                )

    @staticmethod
    def _insert_message(
        connection: sqlite3.Connection, *, channel_id: str, author_id: str, author_type: str,
        message_kind: str, body: str, mentions: list[str] | None = None,
        thread_id: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = f"MSG-{uuid4().hex[:12].upper()}"
        created_at = _now()
        connection.execute(
            """INSERT INTO workbench_messages(
                message_id, channel_id, thread_id, author_id, author_type, message_kind,
                body, mentions_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, channel_id, thread_id, author_id, author_type, message_kind, body,
             _json(mentions or []), _json(metadata or {}), created_at),
        )
        return {
            "message_id": message_id, "channel_id": channel_id, "thread_id": thread_id,
            "author_id": author_id, "author_type": author_type, "message_kind": message_kind,
            "body": body, "mentions": mentions or [], "metadata": metadata or {},
            "created_at": created_at,
        }

    def list_channels(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM workbench_channels ORDER BY created_at"
            ).fetchall()]

    def list_agents(self) -> list[dict[str, Any]]:
        return [
            {
                "agent_id": entry.agent_id, "name": entry.display_name,
                "role": entry.role_title_zh, "type": "system", "status": "online",
                "model": "deepseek-v4-flash", "allowed_tools": list(entry.allowed_tools),
                "runtime_agent_name": entry.runtime_agent_name,
            }
            for entry in iter_agent_entries()
        ]

    def add_message(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            return self._insert_message(connection, **kwargs)

    def list_messages(
        self,
        channel_id: str,
        limit: int = 100,
        include_replies: bool = True,
    ) -> list[dict[str, Any]]:
        """List channel messages, optionally excluding thread replies.

        The main Slack-like timeline should contain conversation roots only.
        Replies are queried through :meth:`get_thread` and rendered in the
        right pane, so a busy Agent handoff does not flood the channel.
        """

        where = "channel_id=?"
        if not include_replies:
            where += " AND thread_id IS NULL"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM workbench_messages WHERE {where} ORDER BY created_at DESC LIMIT ?",
                (channel_id, max(1, min(limit, 500))),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["mentions"] = json.loads(item.pop("mentions_json"))
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def get_thread(self, message_id: str, channel_id: str | None = None) -> dict[str, Any] | None:
        """Return a root message and its direct replies for the workbench.

        A message can be opened either from the channel timeline or from an
        existing reply.  In the latter case ``thread_id`` points at the root.
        Keeping this lookup in the store makes the UI independent from a
        truncated channel history and prevents it from querying SQLite itself.
        """

        with self._lock, self._connect() as connection:
            selected = connection.execute(
                "SELECT * FROM workbench_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if selected is None:
                return None
            selected_item = dict(selected)
            root_id = selected_item.get("thread_id") or selected_item["message_id"]
            root = connection.execute(
                "SELECT * FROM workbench_messages WHERE message_id=?", (root_id,)
            ).fetchone()
            if root is None:
                return None
            root_item = dict(root)
            if channel_id and root_item["channel_id"] != channel_id:
                return None
            rows = connection.execute(
                """SELECT * FROM workbench_messages
                   WHERE channel_id=? AND thread_id=?
                   ORDER BY created_at ASC""",
                (root_item["channel_id"], root_id),
            ).fetchall()

        def decode(row: dict[str, Any]) -> dict[str, Any]:
            row["mentions"] = json.loads(row.pop("mentions_json"))
            row["metadata"] = json.loads(row.pop("metadata_json"))
            return row

        return {
            "root": decode(root_item),
            "replies": [decode(dict(row)) for row in rows],
        }

    def create_task(self, *, channel_id: str, created_by: str, assignee_id: str, title: str,
                    task_id: str | None = None,
                    run_id: str | None = None, status: str = "queued",
                    metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        task_id = task_id or f"TASK-WB-{uuid4().hex[:10].upper()}"
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO workbench_tasks(
                    task_id, channel_id, run_id, created_by, assignee_id, title, status,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, channel_id, run_id, created_by, assignee_id, title, status,
                 _json(metadata or {}), timestamp, timestamp),
            )
        return self.get_task(task_id) or {}

    def update_task(self, task_id: str, status: str, **fields: Any) -> dict[str, Any] | None:
        assignments = ["status=?", "updated_at=?"]
        values: list[Any] = [status, _now()]
        for field in ("run_id", "metadata_json"):
            if field in fields:
                assignments.append(f"{field}=?")
                values.append(fields[field])
        values.append(task_id)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE workbench_tasks SET {', '.join(assignments)} WHERE task_id=?", values)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM workbench_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def list_tasks(self, channel_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workbench_tasks"
        values: tuple[Any, ...] = ()
        if channel_id:
            query += " WHERE channel_id=?"
            values = (channel_id,)
        query += " ORDER BY updated_at DESC LIMIT 100"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def upsert_artifact(self, *, artifact_id: str, channel_id: str, agent_id: str,
                        title: str, summary: str, artifact_type: str = "research",
                        status: str = "submitted", task_id: str | None = None,
                        run_id: str | None = None, refs: list[str] | None = None,
                        metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workbench_artifacts(
                    artifact_id, channel_id, run_id, task_id, agent_id, artifact_type,
                    title, summary, status, refs_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    status=excluded.status, summary=excluded.summary,
                    refs_json=excluded.refs_json, metadata_json=excluded.metadata_json""",
                (artifact_id, channel_id, run_id, task_id, agent_id, artifact_type,
                 title, summary, status, _json(refs or []), _json(metadata or {}), timestamp),
            )
        return self.get_artifact(artifact_id) or {}

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["refs"] = json.loads(item.pop("refs_json"))
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def list_artifacts(self, channel_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workbench_artifacts"
        values: tuple[Any, ...] = ()
        if channel_id:
            query += " WHERE channel_id=?"
            values = (channel_id,)
        query += " ORDER BY created_at DESC LIMIT 100"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["refs"] = json.loads(item.pop("refs_json"))
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def add_event(self, *, channel_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = f"EVENT-WB-{uuid4().hex[:12].upper()}"
        created_at = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO workbench_events(event_id, channel_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (event_id, channel_id, event_type, _json(payload), created_at),
            )
            event_seq = int(cursor.lastrowid)
        return {"event_seq": event_seq, "event_id": event_id, "channel_id": channel_id,
                "event_type": event_type, "payload": payload, "created_at": created_at}

    def events_since(self, channel_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workbench_events WHERE channel_id=? AND event_seq>? ORDER BY event_seq LIMIT 100",
                (channel_id, max(0, after_seq)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def latest_event_seq(self, channel_id: str) -> int:
        """Return the current channel cursor so a new page skips stale backlog."""

        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(event_seq), 0) AS event_seq "
                "FROM workbench_events WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
        return int(row["event_seq"] if row is not None else 0)

    def has_flow_event(self, channel_id: str, flow_event_id: str) -> bool:
        """Return whether a CrewAI Flow event was already projected to this channel."""
        marker = f'"flow_event_id": "{flow_event_id}"'
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM workbench_events WHERE channel_id=? AND event_type=? "
                "AND payload_json LIKE ? LIMIT 1",
                (channel_id, "flow_event", f"%{marker}%"),
            ).fetchone()
        return row is not None


__all__ = ["CollaborationStore"]
