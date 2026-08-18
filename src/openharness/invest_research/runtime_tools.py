"""Evidence-aware wrappers and runtime guards for research tools."""

from __future__ import annotations

import json
import asyncio
import hashlib
import re
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.research_budget import DEFAULT_RESEARCH_BUDGET_POLICY
from openharness.invest_research.source_quality import classify_source_grade
from openharness.invest_research.tavily_tool import TavilySearchInput, TavilySearchTool
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.tools.web_fetch_tool import WebFetchTool, WebFetchToolInput


_CACHE_LOCKS: dict[str, asyncio.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _cache_lock(run_id: str, tool_name: str, cache_key: str) -> asyncio.Lock:
    identity = f"{run_id}:{tool_name}:{cache_key}"
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(identity, asyncio.Lock())


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
                output=json.dumps(
                    {
                        "status": "tool_budget_exhausted",
                        "tool_name": self.name,
                        "used": used,
                        "limit": limit,
                        "instruction": (
                            "Do not call this tool again. Use the evidence already present "
                            "in the conversation and return a partial result with explicit gaps."
                        ),
                    },
                    ensure_ascii=False,
                ),
                # Budget exhaustion is an expected control signal, not a provider
                # failure. Treating it as an error caused models to keep retrying.
                is_error=False,
                metadata={
                    "reason": "tool_budget_exhausted",
                    "tool_name": self.name,
                    "used": used,
                    "limit": limit,
                },
            )
        counts[self.name] = used + 1
        result = await self._delegate.execute(arguments, context)
        if not _retryable_tool_result(result):
            return _with_budget_remaining(
                result,
                tool_name=self.name,
                remaining=max(0, limit - int(counts[self.name])),
            )

        # A retry is still charged against the same per-run budget. This keeps
        # the guard finite and prevents a broken provider from causing a loop.
        retry_used = int(counts.get(self.name, 0))
        if retry_used >= limit:
            return result
        await asyncio.sleep(0.25)
        counts[self.name] = retry_used + 1
        retry_result = await self._delegate.execute(arguments, context)
        combined = ToolResult(
            output=retry_result.output,
            is_error=retry_result.is_error,
            metadata={
                **retry_result.metadata,
                "retry_count": 1,
                "first_attempt_error": result.output[:240],
            },
        )
        return _with_budget_remaining(
            combined,
            tool_name=self.name,
            remaining=max(0, limit - int(counts[self.name])),
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        return self._delegate.is_read_only(arguments)


class EvidenceAwareTavilyTool(TavilySearchTool):
    """Register every Tavily result as a run-scoped discovered source."""

    def __init__(self, evidence_store: EvidenceStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._evidence_store = evidence_store

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolResult:
        run_id = str(context.metadata.get("run_id") or "")
        agent_id = str(context.metadata.get("agent_id") or "") or None
        if not run_id or not self._evidence_store.run_exists(run_id):
            return ToolResult(
                output="tavily_search failed: missing or unknown research Run",
                is_error=True,
                metadata={"reason": "unknown_run"},
            )
        bounded_arguments = arguments.model_copy(
            update={
                "max_results": min(
                    int(arguments.max_results),
                    DEFAULT_RESEARCH_BUDGET_POLICY.tavily_max_results,
                )
            }
        )
        request_payload = _normalized_search_payload(bounded_arguments)
        cache_key = _request_cache_key(request_payload)
        cached = self._evidence_store.get_cached_tool_result(
            run_id, self.name, cache_key
        )
        if cached is not None:
            for source_id in cached["source_ids"]:
                _authorize_ref(context.metadata, str(source_id))
            return ToolResult(
                output=str(cached["response_text"]),
                metadata={
                    **dict(cached["metadata"]),
                    "cache_hit": True,
                    "external_call": False,
                    "cache_key": cache_key,
                    "source_ids": list(cached["source_ids"]),
                },
            )

        async with _cache_lock(run_id, self.name, cache_key):
            cached = self._evidence_store.get_cached_tool_result(
                run_id, self.name, cache_key
            )
            if cached is not None:
                for source_id in cached["source_ids"]:
                    _authorize_ref(context.metadata, str(source_id))
                return ToolResult(
                    output=str(cached["response_text"]),
                    metadata={
                        **dict(cached["metadata"]),
                        "cache_hit": True,
                        "external_call": False,
                        "cache_key": cache_key,
                        "source_ids": list(cached["source_ids"]),
                    },
                )

            result = await super().execute(bounded_arguments, context)
            if result.is_error:
                return ToolResult(
                    output=result.output,
                    is_error=True,
                    metadata={
                        **result.metadata,
                        "cache_hit": False,
                        "external_call": True,
                        "cache_key": cache_key,
                    },
                )
            return self._register_and_cache_result(
                result=result,
                run_id=run_id,
                agent_id=agent_id,
                cache_key=cache_key,
                request_payload=request_payload,
                context=context,
            )

    def _register_and_cache_result(
        self,
        *,
        result: ToolResult,
        run_id: str,
        agent_id: str | None,
        cache_key: str,
        request_payload: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
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
            item = dict(item)
            item["content"] = str(item.get("content") or "")[
                : DEFAULT_RESEARCH_BUDGET_POLICY.tavily_snippet_chars
            ]
            url = str(item["url"])
            title = str(item.get("title") or item["url"])
            source_id = self._evidence_store.register_source(
                run_id,
                url_or_file=url,
                title=title,
                source_type="search_result",
                published_at=(str(item["published_date"]) if item.get("published_date") else None),
                source_grade=classify_source_grade(url, title=title),
                content=str(item.get("content") or ""),
                status="discovered",
                submitted_by=agent_id,
            )
            source_ids.append(source_id)
            registered_results.append({**item, "source_id": source_id})
            _authorize_ref(context.metadata, source_id)
        payload["results"] = registered_results
        payload["source_ids"] = source_ids
        output = json.dumps(payload, ensure_ascii=False)
        metadata = {
            **result.metadata,
            "source_ids": source_ids,
            "cache_hit": False,
            "external_call": True,
            "cache_key": cache_key,
        }
        if source_ids:
            self._evidence_store.cache_tool_result(
                run_id,
                self.name,
                cache_key,
                request_payload=request_payload,
                response_text=output,
                metadata=metadata,
                source_ids=source_ids,
            )
        return ToolResult(output=output, metadata=metadata)


class EvidenceAwareWebFetchTool(WebFetchTool):
    """Register a successfully fetched page and expose its stable S-ID."""

    def __init__(self, evidence_store: EvidenceStore) -> None:
        super().__init__()
        self._evidence_store = evidence_store

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolResult:
        run_id = str(context.metadata.get("run_id") or "")
        agent_id = str(context.metadata.get("agent_id") or "") or None
        if not run_id or not self._evidence_store.run_exists(run_id):
            return ToolResult(
                output="web_fetch failed: missing or unknown research Run",
                is_error=True,
                metadata={"reason": "unknown_run"},
            )
        url = _normalize_url(str(arguments.url))
        bounded_arguments = WebFetchToolInput(
            url=url,
            max_chars=min(
                int(arguments.max_chars),
                DEFAULT_RESEARCH_BUDGET_POLICY.web_fetch_max_chars,
            ),
        )
        request_payload = bounded_arguments.model_dump(mode="json")
        cache_key = _request_cache_key(request_payload)
        cached = self._evidence_store.get_cached_tool_result(
            run_id, self.name, cache_key
        )
        if cached is not None:
            for source_id in cached["source_ids"]:
                _authorize_ref(context.metadata, str(source_id))
            return ToolResult(
                output=str(cached["response_text"]),
                metadata={
                    **dict(cached["metadata"]),
                    "cache_hit": True,
                    "external_call": False,
                    "cache_key": cache_key,
                    "source_ids": list(cached["source_ids"]),
                    "source_id": (
                        str(cached["source_ids"][0]) if cached["source_ids"] else None
                    ),
                },
            )

        async with _cache_lock(run_id, self.name, cache_key):
            cached = self._evidence_store.get_cached_tool_result(
                run_id, self.name, cache_key
            )
            if cached is not None:
                for source_id in cached["source_ids"]:
                    _authorize_ref(context.metadata, str(source_id))
                return ToolResult(
                    output=str(cached["response_text"]),
                    metadata={
                        **dict(cached["metadata"]),
                        "cache_hit": True,
                        "external_call": False,
                        "cache_key": cache_key,
                        "source_ids": list(cached["source_ids"]),
                        "source_id": (
                            str(cached["source_ids"][0])
                            if cached["source_ids"]
                            else None
                        ),
                    },
                )
            result = await super().execute(bounded_arguments, context)
            if result.is_error:
                return ToolResult(
                    output=result.output,
                    is_error=True,
                    metadata={
                        **result.metadata,
                        "cache_hit": False,
                        "external_call": True,
                        "cache_key": cache_key,
                    },
                )
            return self._register_and_cache_result(
                result=result,
                run_id=run_id,
                agent_id=agent_id,
                url=url,
                cache_key=cache_key,
                request_payload=request_payload,
                context=context,
            )

    def _register_and_cache_result(
        self,
        *,
        result: ToolResult,
        run_id: str,
        agent_id: str | None,
        url: str,
        cache_key: str,
        request_payload: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        source_id = self._evidence_store.register_source(
            run_id,
            url_or_file=url,
            title=url,
            source_type="web_page",
            source_grade=classify_source_grade(url, title=url),
            content=result.output,
            status="fetched",
            submitted_by=agent_id,
        )
        _authorize_ref(context.metadata, source_id)
        output = f"Source-ID: {source_id}\n{result.output}"
        metadata = {
            **result.metadata,
            "source_id": source_id,
            "source_ids": [source_id],
            "cache_hit": False,
            "external_call": True,
            "cache_key": cache_key,
        }
        self._evidence_store.cache_tool_result(
            run_id,
            self.name,
            cache_key,
            request_payload=request_payload,
            response_text=output,
            metadata=metadata,
            source_ids=[source_id],
        )
        return ToolResult(output=output, metadata=metadata)


def _request_cache_key(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalized_search_payload(arguments: TavilySearchInput) -> dict[str, Any]:
    payload = arguments.model_dump(mode="json", exclude_none=True)
    payload["query"] = re.sub(r"\s+", " ", str(payload["query"]).strip()).casefold()
    for key in ("include_domains", "exclude_domains"):
        payload[key] = sorted(
            {str(item).strip().casefold() for item in payload.get(key, []) if str(item).strip()}
        )
    return payload


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    hostname = (parts.hostname or "").casefold()
    netloc = hostname
    if parts.port:
        netloc = f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme.casefold(), netloc, parts.path or "/", parts.query, ""))


def _authorize_ref(metadata: dict[str, Any], record_id: str) -> None:
    refs = metadata.get("authorized_refs")
    if isinstance(refs, set):
        refs.add(record_id)
    elif isinstance(refs, list):
        if record_id not in refs:
            refs.append(record_id)


def _retryable_tool_result(result: ToolResult) -> bool:
    if not result.is_error:
        return False
    text = result.output.lower()
    status = result.metadata.get("status_code")
    return (
        status == 429
        or (isinstance(status, int) and status >= 500)
        or any(marker in text for marker in ("network", "timeout", "connection", "temporarily"))
    )


def _with_budget_remaining(
    result: ToolResult,
    *,
    tool_name: str,
    remaining: int,
) -> ToolResult:
    metadata = {**result.metadata, "budget_remaining": remaining}
    if result.is_error or remaining:
        return ToolResult(
            output=result.output,
            is_error=result.is_error,
            metadata=metadata,
        )
    notice = (
        f"The {tool_name} budget is now exhausted. Do not call it again; "
        "finish with existing evidence and use status=partial for gaps."
    )
    output = result.output
    try:
        payload = json.loads(output)
    except (TypeError, ValueError, json.JSONDecodeError):
        output = f"{output}\n\n[Runtime budget notice] {notice}"
    else:
        if isinstance(payload, dict):
            payload["runtime_budget_notice"] = notice
            output = json.dumps(payload, ensure_ascii=False)
        else:
            output = f"{output}\n\n[Runtime budget notice] {notice}"
    return ToolResult(output=output, metadata=metadata)


__all__ = [
    "BudgetedTool",
    "EvidenceAwareTavilyTool",
    "EvidenceAwareWebFetchTool",
]
