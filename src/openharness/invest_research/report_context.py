"""Build a compact, deterministic, read-only package for ReportWriter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openharness.invest_research.evidence_store import EvidenceStore


DEFAULT_MAX_CHARS = 48_000


class ReportContextError(ValueError):
    """Raised when approved records cannot form a report-writing context."""


class ReportContextBuilder:
    """Convert one reviewed Run into a bounded ReportWriter context package."""

    def __init__(self, store: EvidenceStore, *, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        if max_chars < 12_000:
            raise ValueError("report context max_chars must be at least 12000")
        self._store = store
        self._max_chars = max_chars

    def build(self, run_id: str, review_output: dict[str, Any]) -> dict[str, Any]:
        if not self._store.run_exists(run_id):
            raise ReportContextError(f"unknown research Run: {run_id}")
        decision = str(review_output.get("decision") or "")
        if decision not in {"approve_for_report", "approve_with_warnings"}:
            raise ReportContextError(
                "ReportWriter context requires approve_for_report or approve_with_warnings"
            )

        approved_logic_ids = _unique_strings(review_output.get("approved_logic_ids"))
        if len(approved_logic_ids) != 3:
            raise ReportContextError("report context requires exactly three approved logic IDs")

        parameter = self._first_payload(run_id, "parameter_card")
        logics = self._approved_records(run_id, "logic", approved_logic_ids, {"approved"})
        if len(logics) != 3:
            raise ReportContextError("one or more approved logic records are missing")

        supporting_fact_ids = _unique_strings(
            fact_id
            for logic in logics
            for fact_id in logic.get("supporting_fact_ids", [])
        )
        approved_fact_ids = _unique_strings(review_output.get("approved_fact_ids"))
        selected_fact_ids = _unique_strings([*supporting_fact_ids, *approved_fact_ids])[:16]
        facts = self._approved_records(
            run_id,
            "fact",
            selected_fact_ids,
            {"verified", "approved"},
        )
        catalysts = self._approved_records(
            run_id,
            "catalyst",
            _unique_strings(review_output.get("approved_catalyst_ids"))[:8],
            {"approved"},
        )
        risks = self._approved_records(
            run_id,
            "risk",
            _unique_strings(review_output.get("approved_risk_ids"))[:6],
            {"approved"},
        )
        peer_comparison, peer_limitations, peer_refs = self._peer_material(
            run_id, review_output
        )

        source_ids = _collect_source_ids(
            [*facts, *logics, *catalysts, *risks, *peer_comparison]
        )
        source_ids = _unique_strings(
            [*source_ids, *_source_refs(review_output.get("evidence_refs", []))]
        )[:24]
        sources = self._source_records(run_id, source_ids)

        context: dict[str, Any] = {
            "context_version": "1.0",
            "run_id": run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "source_policy": (
                "Only the records in this package may be used. Missing information must be "
                "labelled as unverified; no new fact or source ID may be created."
            ),
            "company": parameter.get("company_identity") or {},
            "research_period": parameter.get("research_period"),
            "catalyst_window": parameter.get("catalyst_window"),
            "competitors": parameter.get("recommended_competitors") or [],
            "operating_changes": facts,
            "investment_logics": [
                {
                    **logic,
                    "supporting_facts": [
                        fact
                        for fact in facts
                        if fact.get("fact_id") in set(logic.get("supporting_fact_ids", []))
                    ][:6],
                }
                for logic in logics
            ],
            "peer_comparison": peer_comparison,
            "peer_comparison_limitations": peer_limitations,
            "catalysts": catalysts,
            "risks": risks,
            "review": {
                "review_id": review_output.get("review_id"),
                "decision": decision,
                "decision_rationale": review_output.get("decision_rationale"),
                "issues": _compact_dicts(
                    review_output.get("issues", []),
                    ("issue_id", "severity", "problem_statement", "input_refs"),
                    limit=8,
                ),
                "unverified_items": list(review_output.get("unverified_items") or [])[:8],
                "limitations": list(review_output.get("limitations") or [])[:8],
                "rejected_items": list(review_output.get("rejected_items") or [])[:8],
            },
            "sources": sources,
            "authorized_record_ids": {
                "fact_ids": [item.get("fact_id") for item in facts],
                "logic_ids": approved_logic_ids,
                "catalyst_ids": [item.get("catalyst_id") for item in catalysts],
                "risk_ids": [item.get("risk_id") for item in risks],
                "source_ids": [item.get("source_id") for item in sources],
                "artifact_ids": peer_refs,
            },
        }
        return self._fit_budget(context)

    def write(self, context: dict[str, Any], output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2),
            encoding="utf-8-sig",
        )
        return output_path

    def _first_payload(self, run_id: str, record_type: str) -> dict[str, Any]:
        records = self._store.query_records(
            run_id, record_types=(record_type,), limit=1
        )
        return dict(records[0].get("payload") or {}) if records else {}

    def _approved_records(
        self,
        run_id: str,
        record_type: str,
        record_ids: list[str],
        allowed_statuses: set[str],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record_id in record_ids:
            record = self._store.get_record(run_id, record_id)
            if not record or record.get("record_type") != record_type:
                continue
            if str(record.get("status")) not in allowed_statuses:
                continue
            records.append(dict(record.get("payload") or {}))
        return records

    def _source_records(self, run_id: str, source_ids: list[str]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for source_id in source_ids:
            record = self._store.get_record(run_id, source_id)
            if not record or record.get("record_type") != "source":
                continue
            payload = dict(record.get("payload") or {})
            output.append(
                {
                    key: payload.get(key)
                    for key in (
                        "source_id",
                        "title",
                        "publisher",
                        "published_at",
                        "accessed_at",
                        "source_grade",
                        "url_or_file",
                    )
                    if payload.get(key) is not None
                }
            )
        return output

    def _peer_material(
        self, run_id: str, review_output: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[Any], list[str]]:
        allowed_artifacts = {
            item
            for item in _unique_strings(review_output.get("evidence_refs"))
            if item.startswith("ART-")
        }
        records = self._store.query_records(
            run_id,
            record_types=("artifact",),
            submitted_by=("industry_competition",),
            limit=10,
        )
        for record in records:
            payload = dict(record.get("payload") or {})
            artifact_id = str(payload.get("artifact_id") or "")
            if allowed_artifacts and artifact_id not in allowed_artifacts:
                continue
            output = payload.get("output") or {}
            comparison = list(output.get("peer_comparison") or [])[:3]
            if comparison:
                return (
                    comparison,
                    list(output.get("not_comparable_fields") or [])[:8]
                    + list(output.get("source_limitations") or [])[:8],
                    [artifact_id] if artifact_id else [],
                )
        return [], [], []

    def _fit_budget(self, context: dict[str, Any]) -> dict[str, Any]:
        def size() -> int:
            return len(json.dumps(context, ensure_ascii=False, separators=(",", ":")))

        if size() <= self._max_chars:
            context["context_size_chars"] = size()
            context["truncated"] = False
            return context

        context["sources"] = context["sources"][:12]
        context["operating_changes"] = context["operating_changes"][:12]
        context["catalysts"] = context["catalysts"][:6]
        context["risks"] = context["risks"][:5]
        context["review"]["unverified_items"] = context["review"]["unverified_items"][:5]
        context["review"]["limitations"] = context["review"]["limitations"][:5]
        context["review"]["rejected_items"] = context["review"]["rejected_items"][:5]
        for logic in context["investment_logics"]:
            logic["supporting_facts"] = logic.get("supporting_facts", [])[:4]

        serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > self._max_chars:
            context["peer_comparison_limitations"] = context[
                "peer_comparison_limitations"
            ][:4]
            context["review"]["issues"] = context["review"]["issues"][:4]
            serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > self._max_chars:
            raise ReportContextError(
                f"essential report context exceeds {self._max_chars} characters"
            )
        context["context_size_chars"] = len(serialized)
        context["truncated"] = True
        return context


def _unique_strings(values: Any) -> list[str]:
    output: list[str] = []
    for value in values or []:
        text = str(value)
        if text and text not in output:
            output.append(text)
    return output


def _collect_source_ids(values: Any) -> list[str]:
    found: list[str] = []
    if isinstance(values, dict):
        for key, value in values.items():
            if key == "source_ids":
                for source_id in _unique_strings(value):
                    if source_id.startswith("S-") and source_id not in found:
                        found.append(source_id)
            else:
                for source_id in _collect_source_ids(value):
                    if source_id not in found:
                        found.append(source_id)
    elif isinstance(values, list):
        for value in values:
            for source_id in _collect_source_ids(value):
                if source_id not in found:
                    found.append(source_id)
    return found


def _source_refs(values: Any) -> list[str]:
    return [item for item in _unique_strings(values) if item.startswith("S-")]


def _compact_dicts(values: Any, fields: tuple[str, ...], *, limit: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for value in list(values or [])[:limit]:
        if isinstance(value, dict):
            output.append({field: value.get(field) for field in fields if field in value})
    return output


__all__ = ["DEFAULT_MAX_CHARS", "ReportContextBuilder", "ReportContextError"]
