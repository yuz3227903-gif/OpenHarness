"""Deterministic report-delivery decisions after model review."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from openharness.invest_research.evidence_audit import EvidenceAuditService
from openharness.invest_research.evidence_store import EvidenceStore

DeliveryMode = Literal["formal", "provisional", "fallback"]
_RESEARCH_ROLES = (
    "fundamental",
    "industry_competition",
    "market_catalyst",
)
_GRADE_SCORE = {"A": 4, "B": 3, "C": 2, "D": 1}


class DeliveryDecision(BaseModel):
    """Auditable authorization for the material passed to ReportWriter."""

    delivery_mode: DeliveryMode
    selected_logic_ids: list[str] = Field(default_factory=list, max_length=3)
    review_id: str | None = None
    recovery_used: bool = False
    recovery_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    audit_status: str | None = None
    audit_artifact_id: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> DeliveryDecision:
        if (
            self.delivery_mode in {"formal", "provisional"}
            and len(self.selected_logic_ids) != 3
        ):
            raise ValueError(
                f"{self.delivery_mode} delivery requires exactly three logic IDs"
            )
        if self.delivery_mode == "formal" and self.recovery_used:
            raise ValueError("formal delivery cannot be marked as recovered")
        if self.delivery_mode == "provisional" and not self.recovery_used:
            raise ValueError("provisional delivery must record recovery_used=true")
        return self


class DeliveryDecisionBuilder:
    """Prefer a formal review, otherwise select three evidence-backed logics."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def build(
        self,
        run_id: str,
        review_output: dict[str, Any] | None,
        *,
        recovery_reason: str | None = None,
        audit_result: dict[str, Any] | None = None,
    ) -> DeliveryDecision:
        review = dict(review_output or {})
        audit = dict(audit_result or {})
        if not audit:
            audit = EvidenceAuditService(self._store).audit(
                run_id,
                "final",
                logic_ids=_unique_strings(review.get("approved_logic_ids")) or None,
                prior_review=review,
                persist=False,
            ).model_dump(mode="json")
        audit_status = str(audit.get("status") or "fail")
        eligible_logic_ids = set(_unique_strings(audit.get("eligible_logic_ids")))
        audit_warnings = _unique_strings(
            [*(audit.get("warnings") or []), *(audit.get("hard_failures") or [])]
        )
        approved = _unique_strings(review.get("approved_logic_ids"))
        if (
            review.get("decision") in {"approve_for_report", "approve_with_warnings"}
            and len(approved) == 3
            and audit_status in {"pass", "pass_with_warnings"}
            and set(approved).issubset(eligible_logic_ids)
            and all(self._logic_exists(run_id, logic_id) for logic_id in approved)
        ):
            return DeliveryDecision(
                delivery_mode="formal",
                selected_logic_ids=approved,
                review_id=_review_id(review),
                warnings=_unique_strings([*_review_warnings(review), *audit_warnings]),
                audit_status=audit_status,
                audit_artifact_id=str(audit.get("audit_id") or "") or None,
            )

        candidates = self._eligible_candidates(run_id)
        selected = self._select_diverse(candidates)
        reason = recovery_reason or _infer_recovery_reason(review)
        if len(selected) != 3:
            return DeliveryDecision(
                delivery_mode="fallback",
                selected_logic_ids=[],
                review_id=_review_id(review),
                recovery_used=True,
                recovery_reason=reason,
                warnings=[
                    *_review_warnings(review),
                    *audit_warnings,
                    "Fewer than three candidate logics have traceable facts and sources.",
                ],
                audit_status=audit_status,
                audit_artifact_id=str(audit.get("audit_id") or "") or None,
            )

        review_id = _review_id(review) or _recovery_review_id(run_id)
        return DeliveryDecision(
            delivery_mode="provisional",
            selected_logic_ids=[item["logic_id"] for item in selected],
            review_id=review_id,
            recovery_used=True,
            recovery_reason=reason,
            warnings=[
                *_review_warnings(review),
                *audit_warnings,
                (
                    "The three investment logics were selected deterministically from "
                    "traceable candidate evidence and still require human review."
                ),
            ],
            audit_status=audit_status,
            audit_artifact_id=str(audit.get("audit_id") or "") or None,
        )

    def _logic_exists(self, run_id: str, logic_id: str) -> bool:
        record = self._store.get_record(run_id, logic_id)
        return bool(record and record.get("record_type") == "logic")

    def _eligible_candidates(self, run_id: str) -> list[dict[str, Any]]:
        records = self._store.query_records(
            run_id,
            record_types=("logic",),
            submitted_by=_RESEARCH_ROLES,
            limit=100,
        )
        candidates: list[dict[str, Any]] = []
        for record in records:
            payload = dict(record.get("payload") or {})
            logic_id = str(payload.get("logic_id") or "")
            if not logic_id.startswith("L-"):
                continue
            fact_ids = _unique_strings(payload.get("supporting_fact_ids"))
            valid_facts: list[dict[str, Any]] = []
            grade_score = 0
            for fact_id in fact_ids:
                fact_record = self._store.get_record(run_id, fact_id)
                if not fact_record or fact_record.get("record_type") != "fact":
                    continue
                fact = dict(fact_record.get("payload") or {})
                source_records = []
                for source_id in _source_ids(fact):
                    source = self._store.get_record(run_id, source_id)
                    if source and source.get("record_type") == "source":
                        source_records.append(source)
                if not source_records:
                    continue
                valid_facts.append(fact)
                grade_score += max(
                    _GRADE_SCORE.get(
                        str((source.get("payload") or {}).get("source_grade") or source.get("source_grade") or "D"),
                        1,
                    )
                    for source in source_records
                )
            if not valid_facts:
                continue
            candidates.append(
                {
                    "logic_id": logic_id,
                    "submitted_by": str(
                        record.get("submitted_by")
                        or payload.get("submitted_by")
                        or ""
                    ),
                    "valid_fact_count": len(valid_facts),
                    "grade_score": grade_score,
                    "counter_evidence_count": len(
                        _unique_strings(payload.get("counter_evidence_ids"))
                    ),
                }
            )
        return sorted(
            candidates,
            key=lambda item: (
                -item["valid_fact_count"],
                -item["grade_score"],
                item["counter_evidence_count"],
                item["logic_id"],
            ),
        )

    @staticmethod
    def _select_diverse(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        used_roles: set[str] = set()
        for candidate in candidates:
            role = candidate["submitted_by"]
            if role in used_roles:
                continue
            selected.append(candidate)
            used_roles.add(role)
            if len(selected) == 3:
                return selected
        for candidate in candidates:
            if candidate in selected:
                continue
            selected.append(candidate)
            if len(selected) == 3:
                break
        return selected


def register_recovery_review(
    store: EvidenceStore,
    run_id: str,
    delivery: DeliveryDecision,
    review_output: dict[str, Any] | None,
) -> None:
    """Persist a provisional audit record without promoting research records."""

    if delivery.delivery_mode != "provisional" or not delivery.review_id:
        return
    review = dict(review_output or {})
    store.upsert_record(
        "review",
        delivery.review_id,
        run_id,
        {
            "review_id": delivery.review_id,
            "decision": "system_provisional_delivery",
            "approved_fact_ids": [],
            "approved_logic_ids": [],
            "provisional_logic_ids": delivery.selected_logic_ids,
            "approved_catalyst_ids": [],
            "approved_risk_ids": [],
            "recovery_used": True,
            "recovery_reason": delivery.recovery_reason,
            "warnings": delivery.warnings,
            "prior_review_id": _review_id(review),
            "issues": list(review.get("issues") or []),
            "limitations": list(review.get("limitations") or []),
        },
        status="provisional",
        submitted_by="system",
    )


def _source_ids(payload: dict[str, Any]) -> list[str]:
    output = _unique_strings(payload.get("source_ids"))
    for item in payload.get("evidence_refs") or []:
        if isinstance(item, dict):
            output.extend(_unique_strings(item.get("source_ids")))
        elif str(item).startswith("S-"):
            output.append(str(item))
    return _unique_strings(output)


def _review_id(review: dict[str, Any]) -> str | None:
    value = str(review.get("review_id") or "")
    return value if value.startswith("REVIEW-") else None


def _recovery_review_id(run_id: str) -> str:
    safe = "".join(character for character in run_id if character.isalnum())[-16:]
    return f"REVIEW-RECOVERY-{safe or 'UNKNOWN'}"


def _review_warnings(review: dict[str, Any]) -> list[str]:
    output = [str(item) for item in review.get("limitations") or [] if item]
    output.extend(str(item) for item in review.get("unverified_items") or [] if item)
    for issue in review.get("issues") or []:
        if isinstance(issue, dict) and issue.get("problem_statement"):
            output.append(str(issue["problem_statement"]))
    return _unique_strings(output)


def _infer_recovery_reason(review: dict[str, Any]) -> str:
    if not review:
        return "reviewer_unavailable"
    approved = _unique_strings(review.get("approved_logic_ids"))
    if len(approved) != 3:
        return "reviewer_did_not_select_three_logics"
    return f"reviewer_route_{review.get('decision') or 'unknown'}"


def _unique_strings(values: Any) -> list[str]:
    output: list[str] = []
    for value in values or []:
        text = str(value)
        if text and text not in output:
            output.append(text)
    return output


__all__ = [
    "DeliveryDecision",
    "DeliveryDecisionBuilder",
    "DeliveryMode",
    "register_recovery_review",
]
