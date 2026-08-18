"""Lightweight, deterministic evidence audit used before model review."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.source_quality import classify_source_grade


AuditStatus = Literal["pass", "pass_with_warnings", "fail"]
ReviewStage = Literal["initial", "final"]
_RESEARCH_ROLES = ("fundamental", "industry_competition", "market_catalyst")


class LogicAudit(BaseModel):
    logic_id: str
    submitted_by: str = ""
    supporting_fact_ids: list[str] = Field(default_factory=list)
    valid_fact_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    source_grades: list[str] = Field(default_factory=list)
    traceable: bool = False
    hard_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EvidenceAuditResult(BaseModel):
    audit_id: str
    run_id: str
    review_stage: ReviewStage
    status: AuditStatus
    checked_logic_ids: list[str] = Field(default_factory=list)
    eligible_logic_ids: list[str] = Field(default_factory=list)
    logic_checks: list[LogicAudit] = Field(default_factory=list)
    hard_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    high_severity_conflicts: list[str] = Field(default_factory=list)


class EvidenceAuditService:
    """Check the minimum S->F->L traceability required for report delivery."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def audit(
        self,
        run_id: str,
        review_stage: ReviewStage,
        *,
        logic_ids: list[str] | None = None,
        prior_review: dict[str, Any] | None = None,
        persist: bool = True,
        audit_label: str | None = None,
    ) -> EvidenceAuditResult:
        if not self._store.run_exists(run_id):
            raise KeyError(f"Unknown run_id: {run_id}")

        requested = _unique_strings(logic_ids)
        logic_records = self._store.query_records(
            run_id,
            record_types=("logic",),
            ids=requested,
            submitted_by=_RESEARCH_ROLES,
            limit=100,
        )
        logic_records.sort(key=lambda item: str((item.get("payload") or {}).get("logic_id") or ""))
        checks = [self._audit_logic(run_id, record) for record in logic_records]
        found_ids = {item.logic_id for item in checks}

        hard_failures: list[str] = []
        if requested:
            missing = [logic_id for logic_id in requested if logic_id not in found_ids]
            if missing:
                hard_failures.append("Requested logic IDs do not exist in the current Run: " + ", ".join(missing))

        eligible = [item.logic_id for item in checks if item.traceable]
        if len(eligible) < 3:
            hard_failures.append("Fewer than three candidate logics have a valid Fact-to-Source chain.")

        high_conflicts = _high_conflicts(prior_review)
        if high_conflicts:
            hard_failures.append("Unresolved high-severity conflicts require provisional delivery or human review.")

        warnings = [warning for item in checks for warning in item.warnings]
        first_three = [item for item in checks if item.traceable][:3]
        roles = {item.submitted_by for item in first_three if item.submitted_by}
        if len(first_three) == 3 and len(roles) == 1:
            warnings.append("The three traceable logics are concentrated in one research Agent.")
        invalid_count = sum(not item.traceable for item in checks)
        if invalid_count:
            warnings.append(f"{invalid_count} candidate logic(s) failed minimum traceability checks.")

        status: AuditStatus
        if hard_failures:
            status = "fail"
        elif warnings:
            status = "pass_with_warnings"
        else:
            status = "pass"

        audit_id = _audit_id(run_id, audit_label or review_stage)
        result = EvidenceAuditResult(
            audit_id=audit_id,
            run_id=run_id,
            review_stage=review_stage,
            status=status,
            checked_logic_ids=[item.logic_id for item in checks],
            eligible_logic_ids=eligible,
            logic_checks=checks,
            hard_failures=_unique_strings(hard_failures),
            warnings=_unique_strings(warnings),
            high_severity_conflicts=high_conflicts,
        )
        if persist:
            self._store.upsert_record(
                "artifact",
                audit_id,
                run_id,
                {
                    "artifact_id": audit_id,
                    "artifact_type": "evidence_audit",
                    "agent_id": "system",
                    "output": result.model_dump(mode="json"),
                },
                status=status,
                submitted_by="system",
            )
        return result

    def _audit_logic(self, run_id: str, record: dict[str, Any]) -> LogicAudit:
        payload = dict(record.get("payload") or {})
        logic_id = str(payload.get("logic_id") or "")
        supporting = _unique_strings(payload.get("supporting_fact_ids"))
        valid_facts: list[str] = []
        source_ids: list[str] = []
        grades: list[str] = []
        failures: list[str] = []
        warnings: list[str] = []

        if not supporting:
            failures.append("Logic has no supporting Fact ID.")
        for fact_id in supporting:
            fact_record = self._store.get_record(run_id, fact_id)
            if not fact_record or fact_record.get("record_type") != "fact":
                failures.append(f"Supporting Fact does not exist in the current Run: {fact_id}")
                continue
            fact = dict(fact_record.get("payload") or {})
            valid_sources: list[str] = []
            for source_id in _source_ids(fact):
                source_record = self._store.get_record(run_id, source_id)
                if not source_record or source_record.get("record_type") != "source":
                    continue
                source = dict(source_record.get("payload") or {})
                valid_sources.append(source_id)
                grade = classify_source_grade(
                    str(source.get("url_or_file") or ""),
                    title=str(source.get("title") or ""),
                    publisher=source.get("publisher"),
                    declared_grade=str(source.get("source_grade") or source_record.get("source_grade") or "C"),
                )
                grades.append(grade)
            if not valid_sources:
                failures.append(f"Fact has no valid Source in the current Run: {fact_id}")
                continue
            valid_facts.append(fact_id)
            source_ids.extend(valid_sources)

        if len(valid_facts) == 1:
            warnings.append(f"{logic_id} has only one traceable supporting Fact.")
        if valid_facts and not any(grade in {"A", "B"} for grade in grades):
            warnings.append(f"{logic_id} has no A/B-grade source; C-grade evidence remains usable with warning.")
        return LogicAudit(
            logic_id=logic_id,
            submitted_by=str(record.get("submitted_by") or payload.get("submitted_by") or ""),
            supporting_fact_ids=supporting,
            valid_fact_ids=_unique_strings(valid_facts),
            source_ids=_unique_strings(source_ids),
            source_grades=sorted(set(grades)),
            traceable=bool(valid_facts) and not failures,
            hard_failures=_unique_strings(failures),
            warnings=_unique_strings(warnings),
        )


def review_id_for(run_id: str, stage: str) -> str:
    safe = "".join(character for character in run_id if character.isalnum())[-18:]
    return f"REVIEW-{safe or 'UNKNOWN'}-{stage.upper()}-001"


def _audit_id(run_id: str, label: str) -> str:
    safe = "".join(character for character in run_id if character.isalnum())[-18:]
    safe_label = "".join(character for character in label if character.isalnum()).upper()
    return f"ART-EVIDENCE-AUDIT-{safe or 'UNKNOWN'}-{safe_label or 'AUDIT'}"


def _source_ids(payload: dict[str, Any]) -> list[str]:
    output = _unique_strings(payload.get("source_ids"))
    for item in payload.get("evidence_refs") or []:
        if isinstance(item, dict):
            output.extend(_unique_strings(item.get("source_ids")))
        elif str(item).startswith("S-"):
            output.append(str(item))
    return _unique_strings(output)


def _high_conflicts(review: dict[str, Any] | None) -> list[str]:
    output: list[str] = []
    for conflict in (review or {}).get("conflicts") or []:
        if isinstance(conflict, dict) and conflict.get("severity") == "high":
            output.append(str(conflict.get("conflict_id") or conflict.get("topic") or "high-conflict"))
    return _unique_strings(output)


def _unique_strings(values: Any) -> list[str]:
    output: list[str] = []
    for value in values or []:
        text = str(value)
        if text and text not in output:
            output.append(text)
    return output


__all__ = [
    "AuditStatus",
    "EvidenceAuditResult",
    "EvidenceAuditService",
    "LogicAudit",
    "ReviewStage",
    "review_id_for",
]
