"""Structured state and visible events for CrewAI investment-research flows."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

FlowStage = Literal[
    "created",
    "planner_running",
    "planner_completed",
    "research_running",
    "research_completed",
    "risk_running",
    "risk_completed",
    "reviewer_running",
    "supplement_pending",
    "supplement_research_running",
    "supplement_risk_running",
    "supplement_completed",
    "final_reviewer_running",
    "report_sections_running",
    "report_assembling",
    "completed",
    "failed",
]


class CollaborationEvent(BaseModel):
    """Auditable business event suitable for the future collaboration UI."""

    event_id: str = Field(default_factory=lambda: f"FLOW-EVENT-{uuid4().hex[:12].upper()}")
    event_type: str
    actor_id: str
    target_agent_ids: list[str] = Field(default_factory=list)
    message: str
    task_id: str | None = None
    artifact_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FlowAgentResult(BaseModel):
    """Small state-safe projection of an OpenHarness AgentExecutionResult."""

    agent_id: str
    execution_status: str
    output_status: str | None = None
    artifact_id: str | None = None
    parameter_card_id: str | None = None
    model: str | None = None
    model_call_id: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    tool_invocation_count: int = 0
    external_tool_call_count: int = 0
    cache_hit_count: int = 0
    budget_exhausted_count: int = 0
    total_tokens: int = 0
    source_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    logic_ids: list[str] = Field(default_factory=list)
    catalyst_ids: list[str] = Field(default_factory=list)
    risk_ids: list[str] = Field(default_factory=list)
    review_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_class: str | None = None
    retry_count: int = 0
    degraded: bool = False
    fallback_used: bool = False
    error: str | None = None


class ResearchFlowState(BaseModel):
    """Shared state for the minimal Planner-to-parallel-research CrewAI Flow."""

    id: str = ""
    run_id: str = Field(default_factory=lambda: f"RUN-CREWAI-{uuid4().hex[:12].upper()}")
    company_query: str = "宁德时代 CATL 300750.SZ"
    as_of_date: date = Field(default_factory=date.today)
    current_stage: FlowStage = "created"
    pipeline_status: str = "created"
    parameter_card_id: str | None = None
    planner_output: dict[str, Any] | None = None
    review_output: dict[str, Any] | None = None
    initial_review_output: dict[str, Any] | None = None
    final_review_output: dict[str, Any] | None = None
    initial_evidence_audit: dict[str, Any] | None = None
    final_evidence_audit: dict[str, Any] | None = None
    delivery_decision: dict[str, Any] | None = None
    report_result: dict[str, Any] | None = None
    report_path: str | None = None
    report_quality: Literal["complete", "partial", "fallback"] | None = None
    report_generated: bool = False
    review_status: str | None = None
    warnings: list[str] = Field(default_factory=list)
    total_retry_count: int = 0
    fallback_used: bool = False
    formal_report_succeeded: bool = False
    delivery_mode: Literal["formal", "provisional", "fallback"] | None = None
    recovery_used: bool = False
    recovery_reason: str | None = None
    fallback_trigger_stage: str | None = None
    recovery_actions: list[str] = Field(default_factory=list)
    section_statuses: dict[str, str] = Field(default_factory=dict)
    completed_section_ids: list[str] = Field(default_factory=list)
    partial_section_ids: list[str] = Field(default_factory=list)
    failed_section_ids: list[str] = Field(default_factory=list)
    section_retry_counts: dict[str, int] = Field(default_factory=dict)
    section_artifact_ids: dict[str, str] = Field(default_factory=dict)
    section_token_usage: dict[str, int] = Field(default_factory=dict)
    section_durations: dict[str, float] = Field(default_factory=dict)
    supplement_round: int = 0
    agent_results: dict[str, FlowAgentResult] = Field(default_factory=dict)
    artifact_history: dict[str, list[str]] = Field(default_factory=dict)
    events: list[CollaborationEvent] = Field(default_factory=list)
    orchestrator_llm_calls: int = 0


__all__ = [
    "CollaborationEvent",
    "FlowAgentResult",
    "FlowStage",
    "ResearchFlowState",
]
