"""Evidence-aware wrappers and runtime guards for research tools."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.tavily_tool import TavilySearchTool
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.tools.web_fetch_tool import WebFetchTool


class BudgetedTool(BaseTool):
    """Apply a fail-closed per-tool call budget around one implementation."""

    def __init__(self, delegate: BaseTool) -> None:
        self._delegate = delegate
        self.name = delegate.name
        self.description = delegate.description
        self.input_model = delegate.input_model

    async def execute(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> ToolResult:
        limits = context.metadata.get("tool_limits")
        counts = context.metadata.get("tool_counts")
        if not isinstance(limits, dict) or not isinstance(counts, dict):
            return ToolResult(
                output=f"{self.name} denied: runtime tool budget is missing",
                is_error=True,
                metadata={"reason": "missing_tool_budget"},
            )
        limit = int(limits.get(self.name, 0))
        used = int(counts.get(self.name, 0))
        if limit < 1 or used >= limit:
            return ToolResult(
                output=f"{self.name} denied: per-run call limit reached ({used}/{limit})",
                is_error=True,
                metadata={
                    "reason": "tool_budget_exhausted",
                    "tool_name": self.name,
                    "used": used,
                    "limit": limit,
                },
            )
        counts[self.name] = used + 1
        return await self._delegate.execute(arguments, context)

    def is_read_only(self, arguments: BaseModel) -> bool:
        return self._delegate.is_read_only(arguments)


class EvidenceAwareTavilyTool(TavilySearchTool):
    """Register every Tavily result as a run-scoped discovered source."""

    def __init__(self, evidence_store: EvidenceStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._evidence_store = evidence_store

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolResult:
        result = await super().execute(arguments, context)
        if result.is_error:
            return result
        run_id = str(context.metadata.get("run_id") or "")
        agent_id = str(context.metadata.get("agent_id") or "") or None
        if not run_id or not self._evidence_store.run_exists(run_id):
            return ToolResult(
                output="tavily_search failed: missing or unknown research Run",
                is_error=True,
                metadata={"reason": "unknown_run"},
            )
        try:
            payload = json.loads(result.output)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ToolResult(
                output="tavily_search failed: could not register malformed search output",
                is_error=True,
                metadata={"reason": "invalid_search_output"},
            )

        source_ids: list[str] = []
        registered_results: list[dict[str, Any]] = []
        for item in payload.get("results", []):
            if not isinstance(item, dict) or not str(item.get("url") or "").strip():
                continue
            source_id = self._evidence_store.register_source(
                run_id,
                url_or_file=str(item["url"]),
                title=str(item.get("title") or item["url"]),
                source_type="search_result",
                published_at=(str(item["published_date"]) if item.get("published_date") else None),
                source_grade="C",
                content=str(item.get("content") or ""),
                status="discovered",
                submitted_by=agent_id,
            )
            source_ids.append(source_id)
            registered_results.append({**item, "source_id": source_id})
            _authorize_ref(context.metadata, source_id)
        payload["results"] = registered_results
        payload["source_ids"] = source_ids
        return ToolResult(
            output=json.dumps(payload, ensure_ascii=False),
            metadata={**result.metadata, "source_ids": source_ids},
        )


class EvidenceAwareWebFetchTool(WebFetchTool):
    """Register a successfully fetched page and expose its stable S-ID."""

    def __init__(self, evidence_store: EvidenceStore) -> None:
        super().__init__()
        self._evidence_store = evidence_store

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolResult:
        result = await super().execute(arguments, context)
        if result.is_error:
            return result
        run_id = str(context.metadata.get("run_id") or "")
        agent_id = str(context.metadata.get("agent_id") or "") or None
        if not run_id or not self._evidence_store.run_exists(run_id):
            return ToolResult(
                output="web_fetch failed: missing or unknown research Run",
                is_error=True,
                metadata={"reason": "unknown_run"},
            )
        source_id = self._evidence_store.register_source(
            run_id,
            url_or_file=str(arguments.url),
            title=str(arguments.url),
            source_type="web_page",
            source_grade="C",
            content=result.output,
            status="fetched",
            submitted_by=agent_id,
        )
        _authorize_ref(context.metadata, source_id)
        return ToolResult(
            output=f"Source-ID: {source_id}\n{result.output}",
            metadata={**result.metadata, "source_id": source_id},
        )


def _authorize_ref(metadata: dict[str, Any], record_id: str) -> None:
    refs = metadata.get("authorized_refs")
    if isinstance(refs, set):
        refs.add(record_id)
    elif isinstance(refs, list):
        if record_id not in refs:
            refs.append(record_id)


__all__ = [
    "BudgetedTool",
    "EvidenceAwareTavilyTool",
    "EvidenceAwareWebFetchTool",
]
