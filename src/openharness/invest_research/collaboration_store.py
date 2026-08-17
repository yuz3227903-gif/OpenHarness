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
from openharness.invest_research.workbench_models import DEFAULT_MODEL


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
                    kind TEXT NOT NULL DEFAULT 'project', project_company TEXT, topic TEXT,
                    description TEXT, root_task_id TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_agents (
                    agent_id TEXT PRIMARY KEY, name TEXT NOT NULL, profile TEXT NOT NULL,
                    role TEXT NOT NULL, system_prompt TEXT NOT NULL, model TEXT NOT NULL,
                    avatar_path TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workbench_channel_members (
                    channel_id TEXT NOT NULL, agent_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(channel_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS workbench_agent_overrides (
                    agent_id TEXT PRIMARY KEY, model TEXT NOT NULL, updated_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS workbench_files (
                    file_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, message_id TEXT,
                    task_id TEXT, run_id TEXT, owner_id TEXT NOT NULL, owner_type TEXT NOT NULL,
                    source TEXT NOT NULL, filename TEXT NOT NULL, stored_name TEXT NOT NULL,
                    media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_workbench_files_channel
                    ON workbench_files(channel_id, created_at);
                CREATE TABLE IF NOT EXISTS workbench_agent_skills (
                    skill_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL,
                    stored_name TEXT NOT NULL, media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_workbench_agent_skills_agent
                    ON workbench_agent_skills(agent_id, created_at);
                -- Only what managing a local session needs. The conversation
                -- itself stays on the user's machine; copying it here would
                -- move their code and files onto the platform.
                CREATE TABLE IF NOT EXISTS workbench_local_sessions (
                    session_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                    provider TEXT NOT NULL, bridge_id TEXT, workspace TEXT,
                    local_session_ref TEXT, channel_id TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_workbench_local_sessions_agent
                    ON workbench_local_sessions(agent_id, updated_at);
                """
            )
            channel_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(workbench_channels)")
            }
            for name, definition in (
                ("topic", "TEXT"), ("description", "TEXT"), ("root_task_id", "TEXT")
            ):
                if name not in channel_columns:
                    connection.execute(
                        f"ALTER TABLE workbench_channels ADD COLUMN {name} {definition}"
                    )
            # Built-in Agents have no row in workbench_agents, so their avatar
            # lives beside their model override.
            override_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(workbench_agent_overrides)")
            }
            if "avatar_path" not in override_columns:
                connection.execute(
                    "ALTER TABLE workbench_agent_overrides ADD COLUMN avatar_path TEXT"
                )
            # A built-in Agent is defined by the plugin, so removing it from the
            # workspace is a flag here rather than a row delete — that keeps the
            # removal reversible.
            if "removed" not in override_columns:
                connection.execute(
                    "ALTER TABLE workbench_agent_overrides "
                    "ADD COLUMN removed INTEGER NOT NULL DEFAULT 0"
                )
            # An Agent is either hosted by this platform or a local one the
            # bridge connects to. Existing rows predate the distinction, so
            # they default to hosted and keep working untouched.
            agent_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(workbench_agents)")
            }
            for name, definition in (
                ("agent_type", "TEXT NOT NULL DEFAULT 'hosted'"),
                ("provider", "TEXT"),
                ("bridge_id", "TEXT"),
                ("workspace", "TEXT"),
                ("capabilities_json", "TEXT"),
                ("connection_config_json", "TEXT"),
                ("connection_status", "TEXT"),
            ):
                if name not in agent_columns:
                    connection.execute(
                        f"ALTER TABLE workbench_agents ADD COLUMN {name} {definition}"
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
            channels = [dict(row) for row in connection.execute(
                "SELECT * FROM workbench_channels ORDER BY created_at"
            ).fetchall()]
            for channel in channels:
                channel["member_ids"] = [
                    row["agent_id"] for row in connection.execute(
                        "SELECT agent_id FROM workbench_channel_members WHERE channel_id=? ORDER BY rowid",
                        (channel["channel_id"],),
                    ).fetchall()
                ]
            return channels

    def list_agents(self, include_removed: bool = False) -> list[dict[str, Any]]:
        built_in = [
            {
                "agent_id": entry.agent_id, "name": entry.display_name,
                "role": entry.role_title_zh, "type": "system", "status": "online",
                "model": DEFAULT_MODEL, "allowed_tools": list(entry.allowed_tools),
                "runtime_agent_name": entry.runtime_agent_name, "profile": entry.role_title_zh,
                "avatar_path": None, "enabled": True,
                # Built-in roles are hosted here, so they answer the same
                # provider/runtime questions a local Agent does. The UI then
                # renders one kind of Agent card.
                "agent_type": "hosted", "provider": "openharness", "runtime": "hosted",
                "workspace": None, "bridge_id": None, "connection_config": {},
                "capabilities": {
                    "chat": True, "streaming": True, "shell": False,
                    "file_read": "read_uploaded_file" in entry.allowed_tools,
                    "file_write": False, "diff": False, "mcp": False,
                    "skills": True, "resume_session": False, "approval": False,
                },
            }
            for entry in iter_agent_entries()
        ]
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workbench_agents ORDER BY created_at"
            ).fetchall()
        with self._lock, self._connect() as connection:
            overrides = {
                row["agent_id"]: dict(row)
                for row in connection.execute(
                    "SELECT agent_id, model, avatar_path, removed FROM workbench_agent_overrides"
                ).fetchall()
            }
        for item in built_in:
            override = overrides.get(item["agent_id"]) or {}
            item["model"] = override.get("model") or item["model"]
            item["avatar_path"] = override.get("avatar_path") or item["avatar_path"]
            item["removed"] = bool(override.get("removed"))
        if not include_removed:
            built_in = [item for item in built_in if not item["removed"]]
        custom = []
        for row in rows:
            item = dict(row)
            agent_type = str(item.get("agent_type") or "hosted")
            item["type"] = "local" if agent_type == "local" else "custom"
            item["agent_type"] = agent_type
            item["status"] = "online" if item.pop("enabled") else "disabled"
            item["enabled"] = item["status"] == "online"
            item["allowed_tools"] = []
            item["removed"] = False
            item["capabilities"] = json.loads(item.pop("capabilities_json", None) or "{}")
            item["connection_config"] = json.loads(
                item.pop("connection_config_json", None) or "{}"
            )
            if agent_type == "local":
                # A local Agent's runtime is the user's machine, so its state
                # is whatever the bridge last reported — never assumed online.
                item["runtime"] = "local"
                item["status"] = str(item.get("connection_status") or "unknown")
            else:
                item["runtime"] = "hosted"
                item["provider"] = item.get("provider") or "openharness"
            custom.append(item)
        return built_in + custom

    def set_builtin_agent_removed(self, agent_id: str, removed: bool) -> bool:
        """Remove a built-in Agent from the workspace, or put it back.

        The plugin definition on disk is untouched, so this is reversible; the
        Agent simply stops appearing and stops accepting work.
        """

        known = {item["agent_id"] for item in self.list_agents(include_removed=True)}
        if agent_id not in known:
            return False
        timestamp = _now()
        with self._lock, self._connect() as connection:
            custom = connection.execute(
                "SELECT 1 FROM workbench_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if custom is not None:
                return False
            existing = connection.execute(
                "SELECT model FROM workbench_agent_overrides WHERE agent_id=?", (agent_id,)
            ).fetchone()
            connection.execute(
                """INSERT INTO workbench_agent_overrides(agent_id, model, removed, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(agent_id) DO UPDATE SET
                     removed=excluded.removed, updated_at=excluded.updated_at""",
                (agent_id, (existing["model"] if existing else DEFAULT_MODEL),
                 1 if removed else 0, timestamp),
            )
        return True

    def removed_builtin_agents(self) -> list[dict[str, Any]]:
        return [
            item for item in self.list_agents(include_removed=True)
            if item.get("removed")
        ]

    def update_agent_model(self, agent_id: str, model: str) -> dict[str, Any] | None:
        if self.get_agent(agent_id) is None:
            return None
        timestamp = _now()
        with self._lock, self._connect() as connection:
            custom = connection.execute(
                "SELECT 1 FROM workbench_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if custom is not None:
                connection.execute(
                    "UPDATE workbench_agents SET model=?, updated_at=? WHERE agent_id=?",
                    (model, timestamp, agent_id),
                )
            else:
                connection.execute(
                    """INSERT INTO workbench_agent_overrides(agent_id, model, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         model=excluded.model, updated_at=excluded.updated_at""",
                    (agent_id, model, timestamp),
                )
        return self.get_agent(agent_id)

    def update_agent_avatar(self, agent_id: str, avatar_path: str) -> dict[str, Any] | None:
        """Set an Agent avatar, including the built-in roles.

        Built-in Agents are defined in code and have no ``workbench_agents``
        row, so their avatar is stored in the override table next to the model.
        """

        agent = self.get_agent(agent_id)
        if agent is None:
            return None
        timestamp = _now()
        with self._lock, self._connect() as connection:
            custom = connection.execute(
                "SELECT 1 FROM workbench_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            if custom is not None:
                connection.execute(
                    "UPDATE workbench_agents SET avatar_path=?, updated_at=? WHERE agent_id=?",
                    (avatar_path, timestamp, agent_id),
                )
            else:
                connection.execute(
                    """INSERT INTO workbench_agent_overrides(agent_id, model, avatar_path, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         avatar_path=excluded.avatar_path, updated_at=excluded.updated_at""",
                    (agent_id, str(agent.get("model") or ""), avatar_path, timestamp),
                )
        return self.get_agent(agent_id)

    def ensure_direct_channel(self, agent_id: str, agent_name: str) -> dict[str, Any]:
        """Return the 1:1 channel for one Agent, creating it on first use."""

        channel_id = f"dm-{agent_id}"
        with self._lock, self._connect() as connection:
            timestamp = _now()
            connection.execute(
                """INSERT OR IGNORE INTO workbench_channels(
                    channel_id, workspace_id, name, kind, project_company, topic,
                    description, root_task_id, created_at
                ) VALUES (?, 'default', ?, 'direct', NULL, ?, ?, NULL, ?)""",
                (channel_id, agent_name, f"与 {agent_name} 的单独对话",
                 "只有你和这个 Agent 可见的一对一频道。", timestamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO workbench_channel_members(channel_id, agent_id, created_at) VALUES (?, ?, ?)",
                (channel_id, agent_id, timestamp),
            )
        return next(
            item for item in self.list_channels() if item["channel_id"] == channel_id
        )

    def create_agent(
        self, *, name: str, profile: str, role: str, system_prompt: str, model: str,
        avatar_path: str | None = None, agent_type: str = "hosted",
        provider: str | None = None, bridge_id: str | None = None,
        workspace: str | None = None, capabilities: dict[str, Any] | None = None,
        connection_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an Agent of either kind.

        A hosted Agent is described by its model and prompt. A local one is
        described by which provider on which bridge, in which workspace — its
        prompt and model belong to the CLI on the user's machine, not here.
        """

        prefix = "local" if agent_type == "local" else "custom"
        agent_id = f"{prefix}-{uuid4().hex[:10]}"
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workbench_agents(
                    agent_id, name, profile, role, system_prompt, model, avatar_path,
                    enabled, created_at, updated_at, agent_type, provider, bridge_id,
                    workspace, capabilities_json, connection_config_json, connection_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, name, profile, role, system_prompt, model, avatar_path,
                 timestamp, timestamp, agent_type, provider, bridge_id, workspace,
                 _json(capabilities or {}), _json(connection_config or {}),
                 "unknown" if agent_type == "local" else None),
            )
        return self.get_agent(agent_id) or {}

    def record_local_session(
        self, *, session_id: str, agent_id: str, provider: str, bridge_id: str | None,
        workspace: str | None, channel_id: str | None = None, status: str = "idle",
        local_session_ref: str | None = None,
    ) -> dict[str, Any]:
        """Track one local session's metadata, not its contents."""

        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workbench_local_sessions(
                    session_id, agent_id, provider, bridge_id, workspace,
                    local_session_ref, channel_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status=excluded.status,
                    local_session_ref=COALESCE(excluded.local_session_ref, local_session_ref),
                    updated_at=excluded.updated_at""",
                (session_id, agent_id, provider, bridge_id, workspace,
                 local_session_ref, channel_id, status, timestamp, timestamp),
            )
        return self.get_local_session(session_id) or {}

    def get_local_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_local_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_local_sessions(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workbench_local_sessions"
        values: tuple[Any, ...] = ()
        if agent_id:
            query += " WHERE agent_id=?"
            values = (agent_id,)
        query += " ORDER BY updated_at DESC LIMIT 100"
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(query, values).fetchall()]

    def set_agent_connection_status(self, agent_id: str, status: str) -> dict[str, Any] | None:
        """Record what the bridge last said about a local Agent."""

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE workbench_agents SET connection_status=?, updated_at=? "
                "WHERE agent_id=? AND agent_type='local'",
                (status, _now(), agent_id),
            )
        return self.get_agent(agent_id) if cursor.rowcount else None

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_agents() if item["agent_id"] == agent_id), None)

    def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"name", "profile", "role", "system_prompt", "model", "avatar_path", "enabled"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_agent(agent_id)
        assignments = [f"{key}=?" for key in updates]
        values = [int(value) if key == "enabled" else value for key, value in updates.items()]
        assignments.append("updated_at=?")
        values.extend([_now(), agent_id])
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE workbench_agents SET {', '.join(assignments)} WHERE agent_id=?", values
            )
        return self.get_agent(agent_id) if cursor.rowcount else None

    def delete_agent(self, agent_id: str) -> bool:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM workbench_channel_members WHERE agent_id=?", (agent_id,)
            )
            cursor = connection.execute(
                "DELETE FROM workbench_agents WHERE agent_id=?", (agent_id,)
            )
        return bool(cursor.rowcount)

    def delete_custom_agent_cascade(self, agent_id: str) -> dict[str, Any] | None:
        """Remove a user-created Agent and its private workspace footprint.

        Shared-channel history intentionally stays in place: it is part of the
        project's audit trail.  The Agent's one-to-one channel, queued work,
        local-session metadata and uploaded Skill records are private to that
        Agent and can safely be removed together.
        """

        agent = self.get_agent(agent_id)
        if agent is None or agent.get("type") not in {"custom", "local"}:
            return None
        direct_channel_id = f"dm-{agent_id}"
        skills = self.list_agent_skills(agent_id)
        removed: dict[str, int] = {}
        with self._lock, self._connect() as connection:
            for table in (
                "workbench_messages", "workbench_tasks", "workbench_events",
                "workbench_artifacts", "workbench_files", "workbench_channel_members",
            ):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE channel_id=?", (direct_channel_id,)
                )
                removed[table] = cursor.rowcount
            cursor = connection.execute(
                "DELETE FROM workbench_channels WHERE channel_id=?", (direct_channel_id,)
            )
            removed["workbench_channels"] = cursor.rowcount
            for table in ("workbench_agent_skills", "workbench_local_sessions", "workbench_channel_members"):
                cursor = connection.execute(f"DELETE FROM {table} WHERE agent_id=?", (agent_id,))
                removed[table] = removed.get(table, 0) + cursor.rowcount
            cursor = connection.execute("DELETE FROM workbench_agents WHERE agent_id=?", (agent_id,))
            removed["workbench_agents"] = cursor.rowcount
        return {
            "agent": agent,
            "direct_channel_id": direct_channel_id,
            "skills": skills,
            "removed": removed,
        }

    def create_channel(
        self, *, name: str, topic: str, description: str, member_ids: list[str],
        workspace_id: str = "default",
    ) -> dict[str, Any]:
        channel_id = f"channel-{uuid4().hex[:10]}"
        timestamp = _now()
        root_task_id = f"TASK-WB-{uuid4().hex[:10].upper()}"
        unique_members = list(dict.fromkeys(member_ids))
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workbench_channels(
                    channel_id, workspace_id, name, kind, project_company, topic,
                    description, root_task_id, created_at
                ) VALUES (?, ?, ?, 'project', ?, ?, ?, ?, ?)""",
                (channel_id, workspace_id, name, topic, topic, description, root_task_id, timestamp),
            )
            for agent_id in unique_members:
                connection.execute(
                    "INSERT INTO workbench_channel_members(channel_id, agent_id, created_at) VALUES (?, ?, ?)",
                    (channel_id, agent_id, timestamp),
                )
            connection.execute(
                """INSERT INTO workbench_tasks(
                    task_id, channel_id, run_id, created_by, assignee_id, title, status,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, NULL, 'owner', 'unassigned', ?, 'queued', ?, ?, ?)""",
                (root_task_id, channel_id, topic,
                 _json({"root_research_task": True, "description": description}),
                 timestamp, timestamp),
            )
            self._insert_message(
                connection, channel_id=channel_id, author_id="system", author_type="system",
                message_kind="system_message",
                body=f"研究频道已创建：{topic}。请明确启动研究或 @Agent 下发任务。",
                metadata={"root_task_id": root_task_id},
            )
        return next(item for item in self.list_channels() if item["channel_id"] == channel_id)

    def update_channel(self, channel_id: str, **fields: Any) -> dict[str, Any] | None:
        """Rename a channel or restate its topic."""

        allowed = {"name", "topic", "description"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return next(
                (item for item in self.list_channels() if item["channel_id"] == channel_id), None
            )
        assignments = ", ".join(f"{key}=?" for key in updates)
        values = [*updates.values(), channel_id]
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE workbench_channels SET {assignments} WHERE channel_id=?", values
            )
        if not cursor.rowcount:
            return None
        return next(
            (item for item in self.list_channels() if item["channel_id"] == channel_id), None
        )

    def delete_channel(self, channel_id: str) -> dict[str, int]:
        """Delete a channel and everything recorded inside it.

        Leaving messages, tasks or files behind would keep them visible in the
        workspace-wide panes with no channel to open, so the delete cascades.
        Returns the row counts removed so the caller can report them.
        """

        removed: dict[str, int] = {}
        with self._lock, self._connect() as connection:
            for table in (
                "workbench_messages", "workbench_tasks", "workbench_events",
                "workbench_artifacts", "workbench_files", "workbench_channel_members",
            ):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE channel_id=?", (channel_id,)
                )
                removed[table] = cursor.rowcount
            cursor = connection.execute(
                "DELETE FROM workbench_channels WHERE channel_id=?", (channel_id,)
            )
            removed["workbench_channels"] = cursor.rowcount
        return removed

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

    def delete_task(self, task_id: str) -> dict[str, Any] | None:
        """Remove one task record without deleting its messages or artefacts."""

        task = self.get_task(task_id)
        if task is None:
            return None
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM workbench_tasks WHERE task_id=?", (task_id,))
        return task

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

    def add_file(
        self, *, channel_id: str, owner_id: str, owner_type: str, source: str,
        filename: str, stored_name: str, media_type: str, size_bytes: int,
        file_id: str | None = None, message_id: str | None = None,
        task_id: str | None = None, run_id: str | None = None, summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register one channel file.

        ``file_id`` may be supplied so an Agent artefact that is re-synced on
        every refresh keeps a stable identity instead of duplicating a row.
        """

        file_id = file_id or f"FILE-{uuid4().hex[:12].upper()}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workbench_files(
                    file_id, channel_id, message_id, task_id, run_id, owner_id, owner_type,
                    source, filename, stored_name, media_type, size_bytes, summary,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    size_bytes=excluded.size_bytes, summary=excluded.summary,
                    metadata_json=excluded.metadata_json,
                    -- A file is uploaded before the message that carries it
                    -- exists, so the link arrives on this second write. Without
                    -- it the row never learns which message it belongs to.
                    -- COALESCE keeps the link when a later re-sync omits it.
                    message_id=COALESCE(excluded.message_id, workbench_files.message_id),
                    task_id=COALESCE(excluded.task_id, workbench_files.task_id),
                    run_id=COALESCE(excluded.run_id, workbench_files.run_id)""",
                (file_id, channel_id, message_id, task_id, run_id, owner_id, owner_type,
                 source, filename, stored_name, media_type, int(size_bytes), summary,
                 _json(metadata or {}), _now()),
            )
        return self.get_file(file_id) or {}

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_files WHERE file_id=?", (file_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def list_files(self, channel_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workbench_files"
        values: tuple[Any, ...] = ()
        if channel_id:
            query += " WHERE channel_id=?"
            values = (channel_id,)
        query += " ORDER BY created_at DESC LIMIT 200"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def delete_file(self, file_id: str) -> dict[str, Any] | None:
        """Forget one file, and detach it from the message it arrived with.

        A message keeps a copy of its attachments in metadata so the timeline
        can render them without a second query. Leaving that copy behind would
        show a deleted file as still attached, with a download that 404s.
        """

        record = self.get_file(file_id)
        if record is None:
            return None
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM workbench_files WHERE file_id=?", (file_id,))
            # Prefer the recorded link, but fall back to searching the channel:
            # rows written before the link was stored would otherwise keep
            # showing a deleted file with a download that 404s.
            rows = connection.execute(
                "SELECT message_id, metadata_json FROM workbench_messages WHERE message_id=?"
                if record.get("message_id") else
                "SELECT message_id, metadata_json FROM workbench_messages WHERE channel_id=?",
                (record.get("message_id") or record.get("channel_id"),),
            ).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                attachments = metadata.get("attachments") or []
                remaining = [
                    item for item in attachments
                    if isinstance(item, dict) and item.get("file_id") != file_id
                ]
                if len(remaining) == len(attachments):
                    continue
                metadata["attachments"] = remaining
                connection.execute(
                    "UPDATE workbench_messages SET metadata_json=? WHERE message_id=?",
                    (_json(metadata), row["message_id"]),
                )
        return record

    def add_agent_skill(
        self, *, agent_id: str, name: str, description: str, filename: str,
        stored_name: str, media_type: str, size_bytes: int,
    ) -> dict[str, Any]:
        """Attach one uploaded Skill plugin to an Agent."""

        skill_id = f"SKILL-{uuid4().hex[:12].upper()}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO workbench_agent_skills(
                    skill_id, agent_id, name, description, filename, stored_name,
                    media_type, size_bytes, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (skill_id, agent_id, name, description, filename, stored_name,
                 media_type, int(size_bytes), _now()),
            )
        return self.get_agent_skill(skill_id) or {}

    def get_agent_skill(self, skill_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_agent_skills WHERE skill_id=?", (skill_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    def list_agent_skills(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM workbench_agent_skills"
        values: tuple[Any, ...] = ()
        if agent_id:
            query += " WHERE agent_id=?"
            values = (agent_id,)
        query += " ORDER BY created_at DESC LIMIT 100"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    def set_agent_skill_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE workbench_agent_skills SET enabled=? WHERE skill_id=?",
                (1 if enabled else 0, skill_id),
            )
        return self.get_agent_skill(skill_id) if cursor.rowcount else None

    def delete_agent_skill(self, skill_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM workbench_agent_skills WHERE skill_id=?", (skill_id,)
            )
        return bool(cursor.rowcount)

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
