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
    "fundamental_running",
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
    total_tokens: int = 0
    error: str | None = None


class ResearchFlowState(BaseModel):
    """Shared state for the minimal Planner-to-Fundamental CrewAI Flow."""

    id: str = ""
    run_id: str = Field(default_factory=lambda: f"RUN-CREWAI-{uuid4().hex[:12].upper()}")
    company_query: str = "宁德时代 CATL 300750.SZ"
    as_of_date: date = Field(default_factory=date.today)
    current_stage: FlowStage = "created"
    pipeline_status: str = "created"
    parameter_card_id: str | None = None
    planner_output: dict[str, Any] | None = None
    agent_results: dict[str, FlowAgentResult] = Field(default_factory=dict)
    events: list[CollaborationEvent] = Field(default_factory=list)
    orchestrator_llm_calls: int = 0


__all__ = [
    "CollaborationEvent",
    "FlowAgentResult",
    "FlowStage",
    "ResearchFlowState",
]
