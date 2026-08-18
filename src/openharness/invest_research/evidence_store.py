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

                CREATE TABLE IF NOT EXISTS tool_result_cache (
                    run_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, tool_name, cache_key),
                    FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS ix_tool_cache_run
                    ON tool_result_cache(run_id, tool_name);
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

    def get_cached_tool_result(
        self,
        run_id: str,
        tool_name: str,
        cache_key: str,
    ) -> dict[str, Any] | None:
        """Return one successful, Run-scoped tool result without cross-Run reuse."""

        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_json, response_text, metadata_json,
                       source_ids_json, created_at, updated_at
                FROM tool_result_cache
                WHERE run_id=? AND tool_name=? AND cache_key=?
                """,
                (run_id, tool_name, cache_key),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        item["metadata"] = json.loads(item.pop("metadata_json"))
        item["source_ids"] = json.loads(item.pop("source_ids_json"))
        return item

    def cache_tool_result(
        self,
        run_id: str,
        tool_name: str,
        cache_key: str,
        *,
        request_payload: dict[str, Any],
        response_text: str,
        metadata: dict[str, Any] | None = None,
        source_ids: Iterable[str] = (),
    ) -> None:
        """Persist a successful read-only tool response for the current Run."""

        if not self.run_exists(run_id):
            raise KeyError(f"Unknown run_id: {run_id}")
        if not response_text.strip():
            raise ValueError("Empty tool responses cannot be cached")
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_result_cache(
                    run_id, tool_name, cache_key, request_json, response_text,
                    metadata_json, source_ids_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, tool_name, cache_key) DO UPDATE SET
                    request_json=excluded.request_json,
                    response_text=excluded.response_text,
                    metadata_json=excluded.metadata_json,
                    source_ids_json=excluded.source_ids_json,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    tool_name,
                    cache_key,
                    _json(request_payload),
                    response_text,
                    _json(metadata or {}),
                    _json(list(dict.fromkeys(source_ids))),
                    timestamp,
                    timestamp,
                ),
            )

    def source_catalog(self, run_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
        """Return compact source rows ordered by quality and verification state."""

        grade_order = "CASE source_grade WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 ELSE 1 END"
        status_order = "CASE status WHEN 'verified' THEN 3 WHEN 'fetched' THEN 2 ELSE 1 END"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT source_id, title, publisher, published_at, url_or_file,
                       source_grade, status, submitted_by, accessed_at
                FROM source_records
                WHERE run_id=?
                ORDER BY {grade_order} DESC, {status_order} DESC, updated_at DESC
                LIMIT ?
                """,
                (run_id, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_grade_counts(self, run_id: str) -> dict[str, int]:
        """Count source records by deterministic quality grade for one Run."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_grade, COUNT(*) AS count
                FROM source_records
                WHERE run_id=?
                GROUP BY source_grade
                """,
                (run_id,),
            ).fetchall()
        counts = {grade: 0 for grade in ("A", "B", "C", "D")}
        for row in rows:
            grade = str(row["source_grade"] or "C")
            counts[grade if grade in counts else "C"] += int(row["count"])
        return counts

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
        # A partial result can contain useful, source-backed material without
        # satisfying the full role contract.  Normalize that material before
        # writing the artifact so downstream audit/reporting can still see a
        # real S -> F -> L chain.  These records are deliberately marked as
        # system-recovered candidates and can only lead to provisional
        # delivery until a Reviewer approves them.
        recovered = self._recover_partial_evidence(run_id, agent_id, payload)
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
        for key in (
            "financial_facts",
            "operating_facts",
            "counter_evidence",
            "_recovered_facts",
        ):
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
            if payload.get("audit_status") != "fail":
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
            "recovered_evidence": recovered,
        }

    def _recover_partial_evidence(
        self,
        run_id: str,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Promote source-backed partial outputs into auditable candidates.

        OpenHarness may return ``status=partial`` after a tool budget is
        reached.  In that case the model often has already produced a
        structured comparison row or catalyst event, but has not had enough
        room to repeat the same information as a ``Fact`` and ``Logic``.
        Dropping those objects made a usable run look empty and forced the
        local fallback report.  This recovery only uses IDs already present in
        the current Run; it never invents numeric values or source IDs.
        """

        recovered_facts: list[dict[str, Any]] = []
        recovered_logics: list[dict[str, Any]] = []
        source_ids_seen: set[str] = set()

        def valid_sources(values: Any) -> list[str]:
            output: list[str] = []
            for value in values or []:
                source_id = str(value)
                record = self.get_record(run_id, source_id)
                if record and record.get("record_type") == "source":
                    if source_id not in output:
                        output.append(source_id)
            source_ids_seen.update(output)
            return output

        if agent_id == "industry_competition":
            rows = payload.get("peer_comparison") or []
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    continue
                sources = valid_sources(row.get("source_ids"))
                values = row.get("values")
                if not sources or not isinstance(values, dict):
                    continue
                comparable_values = {
                    str(key): value
                    for key, value in values.items()
                    if value is not None and str(value).strip() != ""
                }
                if not comparable_values:
                    continue
                company = str(row.get("company_name") or "unknown company")
                fact_id = _stable_id(
                    "F",
                    run_id,
                    agent_id,
                    company,
                    _json(comparable_values),
                )
                recovered_facts.append(
                    {
                        "fact_id": fact_id,
                        "statement": (
                            f"{company} 的可比指标记录：{_json(comparable_values)}。"
                            "该记录来自本 Run 已登记来源，具体口径需结合比较字典复核。"
                        ),
                        "period": "ParameterCard-defined comparison period",
                        "source_ids": sources,
                        "statement_class": "disclosed_fact",
                        "entity": company,
                        "metric": "peer_comparison",
                        "submitted_by": agent_id,
                        "recovery_used": True,
                        "recovery_reason": "partial_peer_comparison",
                    }
                )

            if not payload.get("logic_candidates") and recovered_facts:
                strengths = [str(item) for item in payload.get("relative_strengths") or [] if item]
                weaknesses = [str(item) for item in payload.get("relative_weaknesses") or [] if item]
                observation = (strengths or weaknesses or [
                    str(payload.get("competition_structure") or "已登记的三家公司比较结果")
                ])[0]
                fact_id = str(recovered_facts[0]["fact_id"])
                recovered_logics.append(
                    {
                        "logic_id": _stable_id("L", run_id, agent_id, fact_id),
                        "title": "行业与竞争位置的暂定判断",
                        "mechanism": (
                            f"{observation}；该判断通过已登记的可比事实影响对公司的相对竞争位置判断，"
                            "但尚未完成完整 Reviewer 复核。"
                        ),
                        "supporting_fact_ids": [fact_id],
                        "counter_evidence_ids": [],
                        "falsification_conditions": [
                            "后续官方披露或统一口径数据否定当前比较结果"
                        ],
                        "tracking_indicators": ["三家公司统一口径的业务、规模和盈利指标"],
                        "submitted_by": agent_id,
                        "provisional": True,
                        "recovery_used": True,
                    }
                )

        if agent_id == "fundamental":
            # Fundamental is the most likely role to hit the response-size
            # limit because it carries many financial fields.  If the model
            # returned complete fact arrays but a truncated tail, retain those
            # facts and create one clearly provisional logic candidate.
            fact_candidates = [
                item
                for key in ("financial_facts", "operating_facts")
                for item in (payload.get(key) or [])
                if isinstance(item, dict) and item.get("fact_id")
            ]
            for item in fact_candidates:
                sources = valid_sources(item.get("source_ids"))
                if sources:
                    item["source_ids"] = sources
            if not payload.get("logic_candidates") and fact_candidates:
                fact_id = str(fact_candidates[0]["fact_id"])
                recovered_logics.append(
                    {
                        "logic_id": _stable_id("L", run_id, agent_id, fact_id),
                        "title": "公司基本面变化的暂定判断",
                        "mechanism": (
                            "已登记的财务或经营事实显示公司基本面存在可研究变化；"
                            "具体驱动和持续性尚待完整审查。"
                        ),
                        "supporting_fact_ids": [fact_id],
                        "counter_evidence_ids": [],
                        "falsification_conditions": [
                            "后续定期报告或原始披露无法支持该变化，或变化被证明为一次性因素"
                        ],
                        "tracking_indicators": ["后续定期报告中的收入、利润、现金流及经营指标"],
                        "submitted_by": agent_id,
                        "provisional": True,
                        "recovery_used": True,
                    }
                )

        if agent_id == "market_catalyst":
            events = payload.get("events") or []
            event_fact_ids: list[str] = []
            for event in events:
                if not isinstance(event, dict):
                    continue
                sources = valid_sources(event.get("source_ids"))
                if not sources:
                    continue
                title = str(event.get("title") or "未命名催化事件")
                catalyst_id = str(event.get("catalyst_id") or title)
                fact_id = _stable_id("F", run_id, agent_id, catalyst_id)
                event_fact_ids.append(fact_id)
                event["related_fact_ids"] = [fact_id]
                recovered_facts.append(
                    {
                        "fact_id": fact_id,
                        "statement": (
                            f"未来半年候选事件：{title}；事件类型={event.get('event_type') or '未说明'}；"
                            f"证据分类={event.get('evidence_class') or '未说明'}；"
                            f"传导路径={event.get('transmission_path') or '未说明'}。"
                        ),
                        "period": "未来半年催化窗口",
                        "source_ids": sources,
                        "statement_class": str(event.get("evidence_class") or "market_expectation"),
                        "entity": "target_company",
                        "metric": "catalyst_event",
                        "submitted_by": agent_id,
                        "recovery_used": True,
                        "recovery_reason": "partial_catalyst_event",
                    }
                )

            if not payload.get("logic_candidates"):
                for event, fact_id in zip(events, event_fact_ids):
                    if not isinstance(event, dict):
                        continue
                    title = str(event.get("title") or "未命名催化事件")
                    signals = [
                        str(item)
                        for item in event.get("failure_signals") or []
                        if item
                    ]
                    metrics = [
                        str(item)
                        for item in event.get("affected_metrics") or []
                        if item
                    ]
                    recovered_logics.append(
                        {
                            "logic_id": _stable_id("L", run_id, agent_id, title, fact_id),
                            "title": f"{title}带来的暂定催化逻辑",
                            "mechanism": (
                                f"若该事件在研究窗口内落地，可通过"
                                f"{event.get('transmission_path') or '公司经营或市场预期'}"
                                "影响相关表现；该判断仅作为候选逻辑。"
                            ),
                            "supporting_fact_ids": [fact_id],
                            "counter_evidence_ids": [],
                            "falsification_conditions": signals[:3]
                            or ["事件延期、落空或实际影响低于预期"],
                            "tracking_indicators": metrics[:3]
                            or ["事件是否按窗口落地及后续公告验证"],
                            "submitted_by": agent_id,
                            "provisional": True,
                            "recovery_used": True,
                        }
                    )

            # Keep the recovery bounded.  Three catalysts are enough to unlock
            # provisional delivery; the remaining events stay available in the
            # catalyst records and report context.
            recovered_logics = recovered_logics[:3]

        if recovered_facts:
            existing = payload.setdefault("_recovered_facts", [])
            existing.extend(recovered_facts)
        if recovered_logics:
            existing = payload.setdefault("logic_candidates", [])
            existing.extend(recovered_logics)
            payload.setdefault("unverified_items", []).append(
                {
                    "item": f"{len(recovered_logics)} 条逻辑由系统从部分成功结果暂定恢复",
                    "reason": "原始研究结果包含已登记来源支持的比较行或催化事件，但未完成完整逻辑字段输出。",
                    "required_evidence": "由 Reviewer 或人工复核暂定逻辑及其来源口径。",
                }
            )

        return {
            "fact_ids": [str(item["fact_id"]) for item in recovered_facts],
            "logic_ids": [str(item["logic_id"]) for item in recovered_logics],
            "source_ids": sorted(source_ids_seen),
        }

    def persist_provisional_parameter_card(
        self,
        *,
        run_id: str,
        parameter_card_id: str,
        payload: dict[str, Any],
        reason: str,
    ) -> str:
        """Persist a backend-recovered Planner card without hiding recovery.

        The record remains provisional and carries the recovery reason.  It is
        therefore usable for downstream task routing but cannot be mistaken
        for a normal Gate 1 confirmation.
        """

        parameter_payload = {
            "parameter_card_id": parameter_card_id,
            "version": "1.0",
            "run_id": run_id,
            "recovery_used": True,
            "recovery_reason": reason,
            **dict(payload),
        }
        self.upsert_record(
            "parameter_card",
            parameter_card_id,
            run_id,
            parameter_payload,
            status="provisional",
            submitted_by="system",
        )
        return parameter_card_id

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
