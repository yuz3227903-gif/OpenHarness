"""Read-only, run-scoped evidence query tool."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from openharness.invest_research.evidence_store import EvidenceStore, RECORD_TABLES
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


RecordType = Literal[
    "parameter_card",
    "source",
    "fact",
    "logic",
    "catalyst",
    "risk",
    "artifact",
    "review",
    "issue",
    "report",
    "event",
]


class EvidenceQueryInput(BaseModel):
    record_types: list[RecordType] = Field(default_factory=lambda: ["source", "fact"])
    ids: list[str] = Field(default_factory=list, max_length=100)
    statuses: list[str] = Field(default_factory=list, max_length=20)
    submitted_by: list[str] = Field(default_factory=list, max_length=10)
    text_query: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=30, ge=1, le=100)


class EvidenceQueryTool(BaseTool):
    name = "evidence_query"
    description = (
        "Read structured records from the current research Run. The runtime enforces "
        "run isolation and role-specific authorization; arbitrary SQL is not accepted."
    )
    input_model = EvidenceQueryInput

    def __init__(self, evidence_store: EvidenceStore) -> None:
        self._store = evidence_store

    async def execute(
        self,
        arguments: EvidenceQueryInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        run_id = str(context.metadata.get("run_id") or "")
        scope = str(context.metadata.get("evidence_scope") or "none")
        if not run_id or not self._store.run_exists(run_id):
            return ToolResult(
                output="evidence_query failed: missing or unknown run context",
                is_error=True,
                metadata={"reason": "unknown_run"},
            )
        if scope == "none":
            return ToolResult(
                output="evidence_query denied: this Agent has no evidence access",
                is_error=True,
                metadata={"reason": "scope_denied"},
            )

        requested_ids = set(arguments.ids)
        effective_ids = requested_ids
        if scope == "authorized_records":
            allowed = _metadata_set(context.metadata, "authorized_refs")
            if requested_ids and not requested_ids.issubset(allowed):
                return _denied("requested IDs exceed the Agent's authorized input references")
            effective_ids = requested_ids or allowed
            if not effective_ids:
                return _empty_result(run_id, scope)
        elif scope == "approved_records_only":
            approved = _metadata_set(context.metadata, "approved_refs")
            if requested_ids and not requested_ids.issubset(approved):
                return _denied("ReportWriter may only query records approved by ReviewDecision")
            effective_ids = requested_ids or approved
            if not effective_ids:
                return _empty_result(run_id, scope)
        elif scope != "run_records_read_only":
            return _denied(f"unsupported evidence scope: {scope}")

        records = self._store.query_records(
            run_id,
            record_types=arguments.record_types,
            ids=sorted(effective_ids),
            statuses=arguments.statuses,
            submitted_by=arguments.submitted_by,
            text_query=arguments.text_query,
            limit=arguments.limit,
        )
        output = json.dumps(
            {
                "run_id": run_id,
                "scope": scope,
                "record_count": len(records),
                "records": records,
            },
            ensure_ascii=False,
        )
        if len(output) > 24_000:
            output = output[:24_000].rstrip() + "\n...[truncated]"
        return ToolResult(
            output=output,
            metadata={
                "run_id": run_id,
                "scope": scope,
                "record_count": len(records),
                "record_types": list(arguments.record_types),
            },
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        del arguments
        return True


def _metadata_set(metadata: dict[str, object], key: str) -> set[str]:
    value = metadata.get(key, set())
    if isinstance(value, set):
        return {str(item) for item in value}
    if isinstance(value, (list, tuple)):
        return {str(item) for item in value}
    return set()


def _denied(message: str) -> ToolResult:
    return ToolResult(
        output=f"evidence_query denied: {message}",
        is_error=True,
        metadata={"reason": "authorization_denied"},
    )


def _empty_result(run_id: str, scope: str) -> ToolResult:
    return ToolResult(
        output=json.dumps(
            {"run_id": run_id, "scope": scope, "record_count": 0, "records": []},
            ensure_ascii=False,
        ),
        metadata={"run_id": run_id, "scope": scope, "record_count": 0},
    )


__all__ = ["EvidenceQueryInput", "EvidenceQueryTool", "RecordType"]
