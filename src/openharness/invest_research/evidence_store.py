"""Run-scoped SQLite evidence storage for investment-research Agents."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


DEFAULT_DATABASE = Path(".openharness/data/investment-research.sqlite3")
DEFAULT_UPLOAD_ROOT = Path(".openharness/data/uploads")


RECORD_TABLES: dict[str, tuple[str, str]] = {
    "parameter_card": ("parameter_cards", "parameter_card_id"),
    "source": ("source_records", "source_id"),
    "fact": ("fact_records", "fact_id"),
    "logic": ("logic_candidates", "logic_id"),
    "catalyst": ("catalyst_events", "catalyst_id"),
    "risk": ("risk_items", "risk_id"),
    "artifact": ("agent_artifacts", "artifact_id"),
    "review": ("review_decisions", "review_id"),
    "issue": ("review_issues", "issue_id"),
    "report": ("report_artifacts", "report_id"),
    "event": ("run_events", "event_id"),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest.upper()}"


class EvidenceStore:
    """Small, thread-safe SQLite repository with strict run isolation."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        upload_root: str | Path | None = None,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self.upload_root = Path(upload_root or DEFAULT_UPLOAD_ROOT).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.ensure_schema()

    @classmethod
    def for_project(cls, project_root: str | Path) -> "EvidenceStore":
        root = Path(project_root).resolve()
        return cls(root / DEFAULT_DATABASE, upload_root=root / DEFAULT_UPLOAD_ROOT)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def ensure_schema(self) -> None:
        generic_tables = [
            ("parameter_cards", "parameter_card_id"),
            ("fact_records", "fact_id"),
            ("logic_candidates", "logic_id"),
            ("catalyst_events", "catalyst_id"),
            ("risk_items", "risk_id"),
            ("agent_artifacts", "artifact_id"),
            ("review_decisions", "review_id"),
            ("review_issues", "issue_id"),
            ("report_artifacts", "report_id"),
            ("run_events", "event_id"),
        ]
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY,
                    company_query TEXT,
                    status TEXT NOT NULL,
                    schema_version TEXT NOT NULL DEFAULT '1.0',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_records (
                    source_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    publisher TEXT,
                    published_at TEXT,
                    accessed_at TEXT NOT NULL,
                    url_or_file TEXT NOT NULL,
                    location TEXT,
                    source_grade TEXT NOT NULL,
                    content_hash TEXT,
                    status TEXT NOT NULL,
                    submitted_by TEXT,
                    payload_json TEXT NOT NULL,
                    schema_version TEXT NOT NULL DEFAULT '1.0',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS ix_source_run ON source_records(run_id);

                CREATE TABLE IF NOT EXISTS uploaded_files (
                    file_ref TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    absolute_path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    media_type TEXT,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS ix_upload_run ON uploaded_files(run_id);
                """
            )
            for table, id_column in generic_tables:
                self._migrate_generic_table_if_needed(connection, table, id_column)
            for table, id_column in generic_tables:
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        {id_column} TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        submitted_by TEXT,
                        payload_json TEXT NOT NULL,
                        schema_version TEXT NOT NULL DEFAULT '1.0',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (run_id, {id_column}),
                        FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
                    )
                    """
                )
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_run ON {table}(run_id)"
                )

    @staticmethod
    def _migrate_generic_table_if_needed(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
    ) -> None:
        """Migrate the first draft's global ID key to a Run-scoped key."""

        existing = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if not existing:
            return
        primary_key_columns = [str(row[1]) for row in existing if int(row[5])]
        if primary_key_columns != [id_column]:
            return
        legacy = f"{table}__legacy"
        connection.execute(f"DROP INDEX IF EXISTS ix_{table}_run")
        connection.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        connection.execute(
            f"""
            CREATE TABLE {table} (
                {id_column} TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                submitted_by TEXT,
                payload_json TEXT NOT NULL,
                schema_version TEXT NOT NULL DEFAULT '1.0',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, {id_column}),
                FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO {table}(
                {id_column}, run_id, status, submitted_by, payload_json,
                schema_version, created_at, updated_at
            )
            SELECT {id_column}, run_id, status, submitted_by, payload_json,
                   schema_version, created_at, updated_at
            FROM {legacy}
            """
        )
        connection.execute(f"DROP TABLE {legacy}")
        connection.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_run ON {table}(run_id)")

    def create_run(self, run_id: str, company_query: str, status: str = "created") -> None:
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_runs(run_id, company_query, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    company_query=excluded.company_query,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (run_id, company_query, status, timestamp, timestamp),
            )

    def update_run_status(self, run_id: str, status: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE research_runs SET status=?, updated_at=? WHERE run_id=?",
                (status, _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown run_id: {run_id}")

    def run_exists(self, run_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM research_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return row is not None

    def upsert_record(
        self,
        record_type: str,
        record_id: str,
        run_id: str,
        payload: dict[str, Any],
        *,
        status: str,
        submitted_by: str | None = None,
    ) -> None:
        table_info = RECORD_TABLES.get(record_type)
        if table_info is None or record_type == "source":
            raise ValueError(f"Unsupported generic record type: {record_type}")
        table, id_column = table_info
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO {table}(
                    {id_column}, run_id, status, submitted_by, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, {id_column}) DO UPDATE SET
                    status=excluded.status,
                    submitted_by=excluded.submitted_by,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    record_id,
                    run_id,
                    status,
                    submitted_by,
                    _json(payload),
                    timestamp,
                    timestamp,
                ),
            )

    def register_source(
        self,
        run_id: str,
        *,
        url_or_file: str,
        title: str,
        source_type: str = "web_page",
        publisher: str | None = None,
        published_at: str | None = None,
        location: str | None = None,
        source_grade: str = "C",
        content: str | bytes | None = None,
        status: str = "discovered",
        submitted_by: str | None = None,
    ) -> str:
        normalized_location = url_or_file.strip()
        source_id = _stable_id("S", run_id, normalized_location)
        if isinstance(content, str):
            content_bytes = content.encode("utf-8", errors="replace")
        else:
            content_bytes = content
        content_hash = (
            "sha256:" + hashlib.sha256(content_bytes).hexdigest()
            if content_bytes is not None
            else None
        )
        timestamp = _now()
        payload = {
            "source_id": source_id,
            "run_id": run_id,
            "source_type": source_type,
            "title": title,
            "publisher": publisher,
            "published_at": published_at,
            "accessed_at": timestamp,
            "url_or_file": normalized_location,
            "location": location,
            "source_grade": source_grade,
            "content_hash": content_hash,
            "status": status,
            "submitted_by": submitted_by,
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_records(
                    source_id, run_id, source_type, title, publisher, published_at,
                    accessed_at, url_or_file, location, source_grade, content_hash,
                    status, submitted_by, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    title=excluded.title,
                    publisher=COALESCE(excluded.publisher, source_records.publisher),
                    published_at=COALESCE(excluded.published_at, source_records.published_at),
                    accessed_at=excluded.accessed_at,
                    location=COALESCE(excluded.location, source_records.location),
                    source_grade=excluded.source_grade,
                    content_hash=COALESCE(excluded.content_hash, source_records.content_hash),
                    status=CASE
                        WHEN source_records.status='verified' THEN source_records.status
                        ELSE excluded.status
                    END,
                    submitted_by=COALESCE(excluded.submitted_by, source_records.submitted_by),
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    run_id,
                    source_type,
                    title or normalized_location,
                    publisher,
                    published_at,
                    timestamp,
                    normalized_location,
                    location,
                    source_grade,
                    content_hash,
                    status,
                    submitted_by,
                    _json(payload),
                    timestamp,
                    timestamp,
                ),
            )
        return source_id

    def register_uploaded_file(
        self,
        run_id: str,
        path: str | Path,
        *,
        display_name: str | None = None,
        media_type: str | None = None,
    ) -> str:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        allowed_root = (self.upload_root / run_id).resolve()
        try:
            resolved.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError("Uploaded files must be inside the current Run upload directory") from exc
        raw = resolved.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        file_ref = _stable_id("UPLOAD", run_id, resolved.name, digest)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO uploaded_files(
                    file_ref, run_id, absolute_path, display_name, media_type,
                    byte_size, sha256, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'registered', ?)
                ON CONFLICT(file_ref) DO UPDATE SET
                    absolute_path=excluded.absolute_path,
                    display_name=excluded.display_name,
                    media_type=excluded.media_type,
                    byte_size=excluded.byte_size,
                    sha256=excluded.sha256,
                    status='registered'
                """,
                (
                    file_ref,
                    run_id,
                    str(resolved),
                    display_name or resolved.name,
                    media_type,
                    len(raw),
                    digest,
                    _now(),
                ),
            )
        return file_ref

    def get_uploaded_file(self, run_id: str, file_ref: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM uploaded_files WHERE run_id=? AND file_ref=?",
                (run_id, file_ref),
            ).fetchone()
        return dict(row) if row is not None else None

    def query_records(
        self,
        run_id: str,
        *,
        record_types: Iterable[str],
        ids: Iterable[str] = (),
        statuses: Iterable[str] = (),
        submitted_by: Iterable[str] = (),
        text_query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        requested_ids = tuple(dict.fromkeys(ids))
        requested_statuses = tuple(dict.fromkeys(statuses))
        requested_agents = tuple(dict.fromkeys(submitted_by))
        records: list[dict[str, Any]] = []
        with self._lock, self._connect() as connection:
            for record_type in dict.fromkeys(record_types):
                table_info = RECORD_TABLES.get(record_type)
                if table_info is None:
                    raise ValueError(f"Unknown record_type: {record_type}")
                table, id_column = table_info
                clauses = ["run_id=?"]
                params: list[Any] = [run_id]
                if requested_ids:
                    clauses.append(f"{id_column} IN ({','.join('?' for _ in requested_ids)})")
                    params.extend(requested_ids)
                if requested_statuses:
                    clauses.append(f"status IN ({','.join('?' for _ in requested_statuses)})")
                    params.extend(requested_statuses)
                if requested_agents:
                    clauses.append(
                        f"submitted_by IN ({','.join('?' for _ in requested_agents)})"
                    )
                    params.extend(requested_agents)
                if text_query:
                    clauses.append("payload_json LIKE ?")
                    params.append(f"%{text_query}%")
                params.append(max(1, min(limit - len(records), 100)))
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
                    f"ORDER BY updated_at DESC LIMIT ?",
                    params,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    payload = json.loads(item.pop("payload_json"))
                    item["record_type"] = record_type
                    item["payload"] = payload
                    records.append(item)
                    if len(records) >= limit:
                        return records
        return records

    def record_exists(self, run_id: str, record_id: str) -> bool:
        with self._lock, self._connect() as connection:
            for table, id_column in RECORD_TABLES.values():
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE run_id=? AND {id_column}=?",
                    (run_id, record_id),
                ).fetchone()
                if row is not None:
                    return True
        return False

    def get_record(self, run_id: str, record_id: str) -> dict[str, Any] | None:
        """Return one record by ID without permitting caller-supplied SQL."""

        with self._lock, self._connect() as connection:
            for record_type, (table, id_column) in RECORD_TABLES.items():
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE run_id=? AND {id_column}=?",
                    (run_id, record_id),
                ).fetchone()
                if row is None:
                    continue
                item = dict(row)
                item["record_type"] = record_type
                item["payload"] = json.loads(item.pop("payload_json"))
                return item
        return None

    def update_record_status(
        self,
        run_id: str,
        record_type: str,
        record_ids: Iterable[str],
        status: str,
    ) -> None:
        table_info = RECORD_TABLES.get(record_type)
        if table_info is None:
            raise ValueError(f"Unknown record_type: {record_type}")
        table, id_column = table_info
        identifiers = tuple(dict.fromkeys(record_ids))
        if not identifiers:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE {table} SET status=?, updated_at=? "
                f"WHERE run_id=? AND {id_column} IN ({','.join('?' for _ in identifiers)})",
                (status, _now(), run_id, *identifiers),
            )

    def persist_agent_output(
        self,
        *,
        run_id: str,
        task_id: str,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        parameter_card_id: str | None = None
        if agent_id == "planner" and payload.get("gate_1_payload"):
            parameter_card_id = _stable_id("PC", run_id, "gate-1")
            parameter_payload = {
                "parameter_card_id": parameter_card_id,
                "version": "1.0",
                "run_id": run_id,
                **dict(payload["gate_1_payload"]),
            }
            self.upsert_record(
                "parameter_card",
                parameter_card_id,
                run_id,
                parameter_payload,
                status="awaiting_gate_1",
                submitted_by=agent_id,
            )

        artifact_id = f"ART-{agent_id.upper().replace('_', '-')}-{uuid4().hex[:10].upper()}"
        artifact_payload = {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "output": payload,
        }
        self.upsert_record(
            "artifact",
            artifact_id,
            run_id,
            artifact_payload,
            status=payload.get("status", "candidate"),
            submitted_by=agent_id,
        )

        fact_ids: list[str] = []
        for key in ("financial_facts", "operating_facts", "counter_evidence"):
            for item in payload.get(key, []) or []:
                if isinstance(item, dict) and item.get("fact_id"):
                    fact_id = str(item["fact_id"])
                    self.upsert_record(
                        "fact", fact_id, run_id, item, status="candidate", submitted_by=agent_id
                    )
                    fact_ids.append(fact_id)

        logic_ids: list[str] = []
        for item in payload.get("logic_candidates", []) or []:
            if isinstance(item, dict) and item.get("logic_id"):
                logic_id = str(item["logic_id"])
                self.upsert_record(
                    "logic", logic_id, run_id, item, status="candidate", submitted_by=agent_id
                )
                logic_ids.append(logic_id)

        catalyst_ids: list[str] = []
        for item in payload.get("events", []) or []:
            if isinstance(item, dict) and item.get("catalyst_id"):
                catalyst_id = str(item["catalyst_id"])
                self.upsert_record(
                    "catalyst",
                    catalyst_id,
                    run_id,
                    item,
                    status="candidate",
                    submitted_by=agent_id,
                )
                catalyst_ids.append(catalyst_id)

        risk_ids: list[str] = []
        for item in payload.get("risk_items", []) or []:
            if isinstance(item, dict) and item.get("risk_id"):
                risk_id = str(item["risk_id"])
                self.upsert_record(
                    "risk", risk_id, run_id, item, status="candidate", submitted_by=agent_id
                )
                risk_ids.append(risk_id)

        review_id = payload.get("review_id")
        if agent_id == "reviewer_arbiter" and review_id:
            self.upsert_record(
                "review",
                str(review_id),
                run_id,
                payload,
                status=str(payload.get("decision") or "reviewed"),
                submitted_by=agent_id,
            )
            for issue in payload.get("issues", []) or []:
                if isinstance(issue, dict) and issue.get("issue_id"):
                    self.upsert_record(
                        "issue",
                        str(issue["issue_id"]),
                        run_id,
                        issue,
                        status="open",
                        submitted_by=agent_id,
                    )
            self.update_record_status(
                run_id, "fact", payload.get("approved_fact_ids", []), "verified"
            )
            self.update_record_status(
                run_id, "logic", payload.get("approved_logic_ids", []), "approved"
            )
            self.update_record_status(
                run_id, "catalyst", payload.get("approved_catalyst_ids", []), "approved"
            )
            self.update_record_status(
                run_id, "risk", payload.get("approved_risk_ids", []), "approved"
            )

        report_id: str | None = None
        if agent_id == "report_writer":
            report_id = f"REPORT-{uuid4().hex[:10].upper()}"
            self.upsert_record(
                "report",
                report_id,
                run_id,
                payload,
                status=payload.get("status", "candidate"),
                submitted_by=agent_id,
            )

        return {
            "parameter_card_id": parameter_card_id,
            "artifact_id": artifact_id,
            "fact_ids": sorted(set(fact_ids)),
            "logic_ids": sorted(set(logic_ids)),
            "catalyst_ids": sorted(set(catalyst_ids)),
            "risk_ids": sorted(set(risk_ids)),
            "review_id": str(review_id) if review_id else None,
            "report_id": report_id,
        }

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        submitted_by: str | None = None,
    ) -> str:
        event_id = f"EVENT-{uuid4().hex[:12].upper()}"
        event_payload = {"event_type": event_type, **payload}
        self.upsert_record(
            "event",
            event_id,
            run_id,
            event_payload,
            status="recorded",
            submitted_by=submitted_by,
        )
        return event_id


__all__ = ["DEFAULT_DATABASE", "DEFAULT_UPLOAD_ROOT", "EvidenceStore", "RECORD_TABLES"]
