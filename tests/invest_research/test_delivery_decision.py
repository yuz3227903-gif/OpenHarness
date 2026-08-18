from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from openharness.invest_research.delivery_decision import (
    DeliveryDecisionBuilder,
    register_recovery_review,
)
from openharness.invest_research.evidence_store import EvidenceStore


def _store() -> tuple[Path, EvidenceStore, str]:
    root = (
        Path(__file__).resolve().parents[2]
        / ".openharness"
        / "data"
        / "tests"
        / f"delivery-decision-{uuid4().hex}"
    )
    root.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore(root / "evidence.sqlite3", upload_root=root / "uploads")
    run_id = "RUN-DELIVERY-001"
    store.create_run(run_id, "示例公司")
    return root, store, run_id


def _add_logic(
    store: EvidenceStore,
    run_id: str,
    *,
    logic_id: str,
    role: str,
    grade: str = "B",
    with_fact: bool = True,
    with_source: bool = True,
) -> None:
    fact_id = logic_id.replace("L-", "F-", 1)
    source_ids: list[str] = []
    if with_source:
        source_ids.append(
            store.register_source(
                run_id,
                url_or_file=f"https://example.com/{logic_id}",
                title=f"Source for {logic_id}",
                source_grade=grade,
                status="verified",
                submitted_by=role,
            )
        )
    if with_fact:
        store.upsert_record(
            "fact",
            fact_id,
            run_id,
            {
                "fact_id": fact_id,
                "metric_name": logic_id,
                "value": 1,
                "source_ids": source_ids,
            },
            status="candidate",
            submitted_by=role,
        )
    store.upsert_record(
        "logic",
        logic_id,
        run_id,
        {
            "logic_id": logic_id,
            "title": logic_id,
            "mechanism": "fixture",
            "supporting_fact_ids": [fact_id],
            "counter_evidence_ids": [],
            "submitted_by": role,
        },
        status="candidate",
        submitted_by=role,
    )


def test_provisional_selection_prefers_role_diversity():
    root, store, run_id = _store()
    try:
        _add_logic(store, run_id, logic_id="L-FUND-001", role="fundamental", grade="A")
        _add_logic(store, run_id, logic_id="L-FUND-002", role="fundamental", grade="A")
        _add_logic(
            store,
            run_id,
            logic_id="L-INDUSTRY-001",
            role="industry_competition",
            grade="B",
        )
        _add_logic(
            store,
            run_id,
            logic_id="L-MARKET-001",
            role="market_catalyst",
            grade="C",
        )

        decision = DeliveryDecisionBuilder(store).build(
            run_id, {}, recovery_reason="reviewer_timeout"
        )

        assert decision.delivery_mode == "provisional"
        assert decision.recovery_used is True
        assert decision.recovery_reason == "reviewer_timeout"
        assert set(decision.selected_logic_ids) == {
            "L-FUND-001",
            "L-INDUSTRY-001",
            "L-MARKET-001",
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_candidate_without_fact_or_source_is_not_eligible():
    root, store, run_id = _store()
    try:
        _add_logic(store, run_id, logic_id="L-VALID-001", role="fundamental")
        _add_logic(
            store,
            run_id,
            logic_id="L-NO-FACT-001",
            role="industry_competition",
            with_fact=False,
        )
        _add_logic(
            store,
            run_id,
            logic_id="L-NO-SOURCE-001",
            role="market_catalyst",
            with_source=False,
        )

        decision = DeliveryDecisionBuilder(store).build(run_id, {})

        assert decision.delivery_mode == "fallback"
        assert decision.selected_logic_ids == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_formal_review_remains_formal_and_does_not_use_recovery():
    root, store, run_id = _store()
    try:
        for index, role in enumerate(
            ("fundamental", "industry_competition", "market_catalyst"), start=1
        ):
            _add_logic(store, run_id, logic_id=f"L-FORMAL-00{index}", role=role)
        review = {
            "review_id": "REVIEW-DELIVERY-001",
            "decision": "approve_with_warnings",
            "approved_logic_ids": [
                "L-FORMAL-001",
                "L-FORMAL-002",
                "L-FORMAL-003",
            ],
        }

        decision = DeliveryDecisionBuilder(store).build(run_id, review)

        assert decision.delivery_mode == "formal"
        assert decision.recovery_used is False
        assert decision.selected_logic_ids == review["approved_logic_ids"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_review_with_only_two_logics_is_recovered_to_three():
    root, store, run_id = _store()
    try:
        for index, role in enumerate(
            ("fundamental", "industry_competition", "market_catalyst"), start=1
        ):
            _add_logic(store, run_id, logic_id=f"L-RECOVER-00{index}", role=role)
        review = {
            "review_id": "REVIEW-DELIVERY-002",
            "decision": "request_supplement",
            "approved_logic_ids": ["L-RECOVER-001", "L-RECOVER-002"],
        }

        decision = DeliveryDecisionBuilder(store).build(run_id, review)

        assert decision.delivery_mode == "provisional"
        assert len(decision.selected_logic_ids) == 3
        assert decision.recovery_reason == "reviewer_did_not_select_three_logics"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recovery_review_is_audited_without_promoting_logic_statuses():
    root, store, run_id = _store()
    try:
        for index, role in enumerate(
            ("fundamental", "industry_competition", "market_catalyst"), start=1
        ):
            _add_logic(store, run_id, logic_id=f"L-AUDIT-00{index}", role=role)
        delivery = DeliveryDecisionBuilder(store).build(run_id, {})

        register_recovery_review(store, run_id, delivery, {})

        review = store.get_record(run_id, str(delivery.review_id))
        assert review is not None
        assert review["status"] == "provisional"
        assert review["payload"]["approved_logic_ids"] == []
        assert review["payload"]["provisional_logic_ids"] == delivery.selected_logic_ids
        assert all(
            store.get_record(run_id, logic_id)["status"] == "candidate"
            for logic_id in delivery.selected_logic_ids
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
