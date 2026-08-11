"""Pydantic input and output contracts for the seven research Agents."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AgentId = Literal[
    "planner",
    "fundamental",
    "industry_competition",
    "market_catalyst",
    "risk",
    "reviewer_arbiter",
    "report_writer",
]
ResearchStatus = Literal["completed", "partial", "blocked", "failed"]
StatementClass = Literal[
    "disclosed_fact",
    "public_report",
    "market_expectation",
    "analysis_inference",
    "speculation",
]
SourceGrade = Literal["A", "B", "C", "D"]
Severity = Literal["low", "medium", "high"]
ReviewRoute = Literal[
    "approve_for_report",
    "approve_with_warnings",
    "request_supplement",
    "require_human_resolution",
]

RunId = Annotated[str, Field(pattern=r"^RUN-[A-Za-z0-9][A-Za-z0-9_-]*$")]
TaskId = Annotated[str, Field(pattern=r"^TASK-[A-Za-z0-9][A-Za-z0-9_-]*$")]
SourceId = Annotated[str, Field(pattern=r"^S-[A-Za-z0-9][A-Za-z0-9_-]*$")]
FactId = Annotated[str, Field(pattern=r"^F-[A-Za-z0-9][A-Za-z0-9_-]*$")]
LogicId = Annotated[str, Field(pattern=r"^L-[A-Za-z0-9][A-Za-z0-9_-]*$")]
IssueId = Annotated[str, Field(pattern=r"^ISSUE-[A-Za-z0-9][A-Za-z0-9_-]*$")]
ArtifactId = Annotated[str, Field(pattern=r"^ART-[A-Za-z0-9][A-Za-z0-9_-]*$")]
CatalystId = Annotated[str, Field(pattern=r"^CAT-[A-Za-z0-9][A-Za-z0-9_-]*$")]
RiskId = Annotated[str, Field(pattern=r"^RISK-[A-Za-z0-9][A-Za-z0-9_-]*$")]
ReviewId = Annotated[str, Field(pattern=r"^REVIEW-[A-Za-z0-9][A-Za-z0-9_-]*$")]


class ContractModel(BaseModel):
    """Base model shared by all strict contracts."""

    model_config = ConfigDict(extra="forbid")


class DateRange(ContractModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_order(self) -> "DateRange":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class ParameterCardRef(ContractModel):
    parameter_card_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class SupplementRequest(ContractModel):
    issue_id: IssueId
    supplement_round: int = Field(ge=1, le=2)
    problem_statement: str = Field(min_length=1)
    expected_fields: list[str] = Field(default_factory=list)
    input_refs: list[str] = Field(default_factory=list)


class UnverifiedItem(ContractModel):
    item: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    required_evidence: str | None = None


class HandoffRequest(ContractModel):
    target_agent_id: AgentId
    request: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)


class AgentInputBase(ContractModel):
    protocol_version: Literal["1.0"] = "1.0"
    run_id: RunId
    task_id: TaskId
    objective: str = Field(min_length=1)
    parameter_card_version: str | None = None
    input_refs: list[str] = Field(default_factory=list)
    supplement: SupplementRequest | None = None


class AgentResultBase(ContractModel):
    protocol_version: Literal["1.0"] = "1.0"
    status: ResearchStatus
    completed_scope: list[str]
    evidence_refs: list[str]
    unverified_items: list[UnverifiedItem]
    limitations: list[str]
    handoff_requests: list[HandoffRequest]
    blocking_reasons: list[str]

    @model_validator(mode="after")
    def validate_blocked_state(self) -> "AgentResultBase":
        if self.status == "blocked" and not self.blocking_reasons:
            raise ValueError("blocked results must include blocking_reasons")
        return self


class CompanyIdentity(ContractModel):
    legal_name: str = Field(min_length=1)
    short_name: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    primary_business: str = Field(min_length=1)
    source_ids: list[SourceId] = Field(default_factory=list)


class CompetitorCandidate(ContractModel):
    company_name: str = Field(min_length=1)
    ticker: str | None = None
    exchange: str | None = None
    selection_reasons: list[str] = Field(default_factory=list)
    comparability_limits: list[str] = Field(default_factory=list)
    source_ids: list[SourceId] = Field(default_factory=list)


class TaskPlanItem(ContractModel):
    task_id: TaskId
    assigned_agent_id: AgentId
    objective: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    expected_output: str = Field(min_length=1)
    depends_on: list[TaskId] = Field(default_factory=list)


class Gate1Payload(ContractModel):
    company_identity: CompanyIdentity
    research_period: DateRange
    catalyst_window: DateRange
    recommended_competitors: list[CompetitorCandidate] = Field(min_length=2, max_length=2)


class PlannerInput(AgentInputBase):
    agent_id: Literal["planner"]
    company_query: str = Field(min_length=1)
    as_of_date: date
    uploaded_file_refs: list[str] = Field(default_factory=list)


class PlannerResult(AgentResultBase):
    company_identity: CompanyIdentity | None = None
    research_period: DateRange | None = None
    catalyst_window: DateRange | None = None
    competitor_candidates: list[CompetitorCandidate] = Field(default_factory=list)
    recommended_competitors: list[CompetitorCandidate] = Field(default_factory=list, max_length=2)
    task_plan: list[TaskPlanItem] = Field(default_factory=list)
    dependency_graph: dict[str, list[str]] = Field(default_factory=dict)
    available_materials: list[str] = Field(default_factory=list)
    material_gaps: list[str] = Field(default_factory=list)
    gate_1_payload: Gate1Payload | None = None

    @model_validator(mode="after")
    def validate_completed_plan(self) -> "PlannerResult":
        if self.status == "completed":
            if (
                self.company_identity is None
                or self.research_period is None
                or self.catalyst_window is None
            ):
                raise ValueError(
                    "completed PlannerResult requires company identity and both date ranges"
                )
            if len(self.recommended_competitors) != 2:
                raise ValueError("completed PlannerResult must recommend exactly two competitors")
            if self.gate_1_payload is None:
                raise ValueError("completed PlannerResult requires gate_1_payload")
            expected_agents = {
                "fundamental",
                "industry_competition",
                "market_catalyst",
                "risk",
                "reviewer_arbiter",
                "report_writer",
            }
            assigned_agents = {item.assigned_agent_id for item in self.task_plan}
            if len(self.task_plan) != 6 or assigned_agents != expected_agents:
                raise ValueError(
                    "completed PlannerResult requires exactly one task for each "
                    "of the six downstream Agents"
                )
        return self


class FinancialFact(ContractModel):
    fact_id: FactId
    metric_name: str = Field(min_length=1)
    value: float | str
    period: str = Field(min_length=1)
    unit: str | None = None
    currency: str | None = None
    scope: str | None = None
    source_ids: list[SourceId] = Field(min_length=1)
    statement_class: StatementClass = "disclosed_fact"


class OperatingFact(ContractModel):
    fact_id: FactId
    statement: str = Field(min_length=1)
    period: str | None = None
    source_ids: list[SourceId] = Field(min_length=1)
    statement_class: StatementClass = "disclosed_fact"


class PeriodComparison(ContractModel):
    metric_name: str = Field(min_length=1)
    current_fact_id: FactId
    comparison_fact_id: FactId
    absolute_change: float | None = None
    percentage_change: float | None = None
    unit: str | None = None


class ManagementStatement(ContractModel):
    statement: str = Field(min_length=1)
    source_ids: list[SourceId] = Field(min_length=1)
    corroborating_fact_ids: list[FactId] = Field(default_factory=list)


class CalculatedMetric(ContractModel):
    name: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    inputs: dict[str, float | str]
    result: float
    unit: str | None = None
    supporting_fact_ids: list[FactId] = Field(default_factory=list)


class LogicCandidate(ContractModel):
    logic_id: LogicId
    title: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    supporting_fact_ids: list[FactId] = Field(min_length=1)
    counter_evidence_ids: list[FactId] = Field(default_factory=list)
    falsification_conditions: list[str] = Field(min_length=1)
    tracking_indicators: list[str] = Field(min_length=1)
    submitted_by: AgentId


class FundamentalInput(AgentInputBase):
    agent_id: Literal["fundamental"]
    parameter_card: ParameterCardRef
    source_ids: list[SourceId] = Field(default_factory=list)
    fact_ids: list[FactId] = Field(default_factory=list)
    uploaded_file_refs: list[str] = Field(default_factory=list)


class FundamentalResult(AgentResultBase):
    scope: str | None = None
    financial_facts: list[FinancialFact] = Field(default_factory=list)
    operating_facts: list[OperatingFact] = Field(default_factory=list)
    period_comparisons: list[PeriodComparison] = Field(default_factory=list)
    management_statements: list[ManagementStatement] = Field(default_factory=list)
    calculated_metrics: list[CalculatedMetric] = Field(default_factory=list)
    change_drivers: list[str] = Field(default_factory=list)
    earnings_quality_assessment: str | None = None
    logic_candidates: list[LogicCandidate] = Field(default_factory=list)
    cross_review_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completed_fundamental(self) -> "FundamentalResult":
        if self.status == "completed":
            if not (self.financial_facts or self.operating_facts):
                raise ValueError("completed FundamentalResult requires traceable facts")
            if not self.change_drivers:
                raise ValueError("completed FundamentalResult requires change_drivers")
            if not self.logic_candidates:
                raise ValueError("completed FundamentalResult requires a logic candidate")
        return self


class ComparisonMetricDefinition(ContractModel):
    metric_name: str = Field(min_length=1)
    formula_or_definition: str = Field(min_length=1)
    period: str = Field(min_length=1)
    unit: str | None = None
    currency: str | None = None
    scope: str | None = None


class PeerComparisonRow(ContractModel):
    company_name: str = Field(min_length=1)
    ticker: str | None = None
    values: dict[str, float | str | None]
    source_ids: list[SourceId] = Field(default_factory=list)


class NotComparableField(ContractModel):
    metric_name: str = Field(min_length=1)
    companies: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class CompetitorChangeRequest(ContractModel):
    current_competitor: str = Field(min_length=1)
    proposed_competitor: CompetitorCandidate
    reason: str = Field(min_length=1)


class IndustryCompetitionInput(AgentInputBase):
    agent_id: Literal["industry_competition"]
    parameter_card: ParameterCardRef
    confirmed_competitors: list[CompetitorCandidate] = Field(min_length=2, max_length=2)
    source_ids: list[SourceId] = Field(default_factory=list)
    fact_ids: list[FactId] = Field(default_factory=list)
    uploaded_file_refs: list[str] = Field(default_factory=list)


class IndustryCompetitionResult(AgentResultBase):
    industry_definition: str | None = None
    market_scope: str | None = None
    industry_drivers: list[str] = Field(default_factory=list)
    policy_and_value_chain: list[str] = Field(default_factory=list)
    competition_structure: str | None = None
    comparison_dictionary: list[ComparisonMetricDefinition] = Field(default_factory=list)
    peer_comparison: list[PeerComparisonRow] = Field(default_factory=list)
    not_comparable_fields: list[NotComparableField] = Field(default_factory=list)
    relative_strengths: list[str] = Field(default_factory=list)
    relative_weaknesses: list[str] = Field(default_factory=list)
    logic_candidates: list[LogicCandidate] = Field(default_factory=list)
    source_limitations: list[str] = Field(default_factory=list)
    competitor_change_request: CompetitorChangeRequest | None = None

    @model_validator(mode="after")
    def validate_completed_comparison(self) -> "IndustryCompetitionResult":
        if self.status == "completed":
            if not self.industry_definition or not self.comparison_dictionary:
                raise ValueError(
                    "completed IndustryCompetitionResult requires industry definition "
                    "and a comparison dictionary"
                )
            if len(self.peer_comparison) != 3:
                raise ValueError(
                    "completed IndustryCompetitionResult requires target company plus "
                    "exactly two competitors"
                )
            if not self.logic_candidates:
                raise ValueError(
                    "completed IndustryCompetitionResult requires a logic candidate"
                )
        return self


class CatalystEvent(ContractModel):
    catalyst_id: CatalystId
    title: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    evidence_class: StatementClass
    announced_at: date | None = None
    expected_date: date | None = None
    expected_window: DateRange | None = None
    confidence_basis: str = Field(min_length=1)
    transmission_path: str = Field(min_length=1)
    affected_metrics: list[str] = Field(default_factory=list)
    direction: Literal["positive", "negative", "mixed", "uncertain"]
    failure_signals: list[str] = Field(default_factory=list)
    source_ids: list[SourceId] = Field(default_factory=list)
    related_fact_ids: list[FactId] = Field(default_factory=list)


class MarketCatalystInput(AgentInputBase):
    agent_id: Literal["market_catalyst"]
    parameter_card: ParameterCardRef
    catalyst_window: DateRange
    source_ids: list[SourceId] = Field(default_factory=list)
    fact_ids: list[FactId] = Field(default_factory=list)
    uploaded_file_refs: list[str] = Field(default_factory=list)


class MarketCatalystResult(AgentResultBase):
    catalyst_window: DateRange | None = None
    events: list[CatalystEvent] = Field(default_factory=list)
    logic_candidates: list[LogicCandidate] = Field(default_factory=list)
    unverified_events: list[UnverifiedItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completed_catalysts(self) -> "MarketCatalystResult":
        if self.status == "completed":
            if self.catalyst_window is None:
                raise ValueError("completed MarketCatalystResult requires catalyst_window")
            if not self.events:
                raise ValueError(
                    "completed MarketCatalystResult requires at least one supported event; "
                    "use partial when reliable events are unavailable"
                )
        return self


class AssumptionChallenge(ContractModel):
    logic_id: LogicId
    critical_assumption: str = Field(min_length=1)
    alternative_explanation: str | None = None
    counter_evidence_ids: list[FactId] = Field(default_factory=list)


class RiskItem(ContractModel):
    risk_id: RiskId
    title: str = Field(min_length=1)
    affected_logic_ids: list[LogicId] = Field(default_factory=list)
    trigger_conditions: list[str] = Field(min_length=1)
    impact_path: str = Field(min_length=1)
    severity: Severity
    probability_basis: str = Field(min_length=1)
    time_window: str | None = None
    falsification_indicators: list[str] = Field(default_factory=list)
    monitoring_plan: list[str] = Field(default_factory=list)
    source_ids: list[SourceId] = Field(default_factory=list)


class ConflictCandidate(ContractModel):
    topic: str = Field(min_length=1)
    fact_ids: list[FactId] = Field(min_length=2)
    description: str = Field(min_length=1)
    severity: Severity


class RiskInput(AgentInputBase):
    agent_id: Literal["risk"]
    parameter_card: ParameterCardRef
    fundamental_artifact_id: ArtifactId | None = None
    industry_competition_artifact_id: ArtifactId | None = None
    market_catalyst_artifact_id: ArtifactId | None = None
    logic_ids: list[LogicId] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_at_least_one_upstream_artifact(self) -> "RiskInput":
        if not any(
            (
                self.fundamental_artifact_id,
                self.industry_competition_artifact_id,
                self.market_catalyst_artifact_id,
            )
        ):
            raise ValueError("RiskInput requires at least one available upstream artifact")
        return self
    source_ids: list[SourceId] = Field(default_factory=list)


class RiskResult(AgentResultBase):
    challenged_logic_ids: list[LogicId] = Field(default_factory=list)
    assumption_matrix: list[AssumptionChallenge] = Field(default_factory=list)
    risk_items: list[RiskItem] = Field(default_factory=list)
    counter_evidence: list[FinancialFact | OperatingFact] = Field(default_factory=list)
    conflict_candidates: list[ConflictCandidate] = Field(default_factory=list)
    trigger_conditions: list[str] = Field(default_factory=list)
    impact_paths: list[str] = Field(default_factory=list)
    severity: list[Severity] = Field(default_factory=list)
    probability_basis: list[str] = Field(default_factory=list)
    falsification_indicators: list[str] = Field(default_factory=list)
    monitoring_plan: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completed_risk(self) -> "RiskResult":
        if self.status == "completed":
            if not self.risk_items:
                raise ValueError("completed RiskResult requires at least one risk item")
            if not (self.challenged_logic_ids or self.assumption_matrix):
                raise ValueError(
                    "completed RiskResult must challenge at least one logic or assumption"
                )
            if not self.falsification_indicators:
                raise ValueError("completed RiskResult requires falsification indicators")
        return self


class ReviewIssue(ContractModel):
    issue_id: IssueId
    issue_type: str = Field(min_length=1)
    severity: Severity
    target_agent_id: AgentId
    parent_task_id: TaskId
    problem_statement: str = Field(min_length=1)
    required_evidence_quality: SourceGrade
    input_refs: list[str] = Field(default_factory=list)
    expected_fields: list[str] = Field(default_factory=list)
    supplement_round: int = Field(ge=1, le=2)


class ReviewConflict(ContractModel):
    conflict_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    severity: Severity
    involved_agent_ids: list[AgentId] = Field(min_length=2)
    evidence_refs: list[str] = Field(default_factory=list)
    description: str = Field(min_length=1)


class Gate2Payload(ContractModel):
    conflicts: list[ReviewConflict] = Field(min_length=1)
    reviewer_recommendation: str = Field(min_length=1)
    affected_report_sections: list[str] = Field(default_factory=list)


class ReviewerArbiterInput(AgentInputBase):
    agent_id: Literal["reviewer_arbiter"]
    parameter_card: ParameterCardRef
    research_artifact_ids: list[ArtifactId] = Field(min_length=1)
    source_ids: list[SourceId] = Field(default_factory=list)
    fact_ids: list[FactId] = Field(default_factory=list)
    logic_ids: list[LogicId] = Field(default_factory=list)
    prior_issue_ids: list[IssueId] = Field(default_factory=list)
    supplement_artifact_ids: list[ArtifactId] = Field(default_factory=list)


class ReviewDecision(AgentResultBase):
    review_id: ReviewId
    decision: ReviewRoute
    approved_fact_ids: list[FactId] = Field(default_factory=list)
    approved_logic_ids: list[LogicId] = Field(default_factory=list, max_length=3)
    approved_catalyst_ids: list[CatalystId] = Field(default_factory=list)
    approved_risk_ids: list[RiskId] = Field(default_factory=list)
    rejected_items: list[str] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)
    conflicts: list[ReviewConflict] = Field(default_factory=list)
    decision_rationale: str = Field(min_length=1)
    gate_2_payload: Gate2Payload | None = None

    @model_validator(mode="after")
    def validate_route_requirements(self) -> "ReviewDecision":
        if self.decision in {"approve_for_report", "approve_with_warnings"} and len(self.approved_logic_ids) != 3:
            raise ValueError(
                f"{self.decision} requires exactly three approved_logic_ids"
            )
        if self.decision == "request_supplement" and not self.issues:
            raise ValueError("request_supplement requires at least one issue")
        if self.decision == "require_human_resolution" and self.gate_2_payload is None:
            raise ValueError("require_human_resolution requires gate_2_payload")
        return self


class ReportWriterInput(AgentInputBase):
    agent_id: Literal["report_writer"]
    parameter_card: ParameterCardRef
    review_id: ReviewId
    approved_fact_ids: list[FactId] = Field(default_factory=list)
    approved_logic_ids: list[LogicId] = Field(min_length=3, max_length=3)
    approved_catalyst_ids: list[CatalystId] = Field(default_factory=list)
    approved_risk_ids: list[RiskId] = Field(default_factory=list)
    gate_2_decision_id: str | None = None


class ReportMetadata(ContractModel):
    company_name: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    as_of_date: date
    research_period: DateRange
    catalyst_window: DateRange
    protocol_version: Literal["1.0"] = "1.0"


class ReportSection(ContractModel):
    section_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ReportBlocker(ContractModel):
    section_id: str | None = None
    problem: str = Field(min_length=1)
    required_record_ids: list[str] = Field(default_factory=list)


class ReportResult(AgentResultBase):
    title: str | None = None
    metadata: ReportMetadata | None = None
    sections: list[ReportSection] = Field(default_factory=list)
    section_evidence_map: dict[str, list[str]] = Field(default_factory=dict)
    three_approved_logics: list[LogicCandidate] = Field(default_factory=list, max_length=3)
    catalysts: list[CatalystEvent] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    peer_comparison: list[PeerComparisonRow] = Field(default_factory=list)
    tracking_indicators: list[str] = Field(default_factory=list)
    compliance_statement: str | None = None
    report_blockers: list[ReportBlocker] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completed_report(self) -> "ReportResult":
        if self.status == "completed":
            if len(self.three_approved_logics) != 3:
                raise ValueError("completed ReportResult requires exactly three approved logics")
            if self.metadata is None or not self.title or not self.sections:
                raise ValueError("completed ReportResult requires title, metadata, and sections")
            if not self.compliance_statement:
                raise ValueError("completed ReportResult requires compliance_statement")
        return self


INPUT_MODELS: dict[str, type[AgentInputBase]] = {
    "planner": PlannerInput,
    "fundamental": FundamentalInput,
    "industry_competition": IndustryCompetitionInput,
    "market_catalyst": MarketCatalystInput,
    "risk": RiskInput,
    "reviewer_arbiter": ReviewerArbiterInput,
    "report_writer": ReportWriterInput,
}

OUTPUT_MODELS: dict[str, type[AgentResultBase]] = {
    "planner": PlannerResult,
    "fundamental": FundamentalResult,
    "industry_competition": IndustryCompetitionResult,
    "market_catalyst": MarketCatalystResult,
    "risk": RiskResult,
    "reviewer_arbiter": ReviewDecision,
    "report_writer": ReportResult,
}


def model_schema_pair(agent_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return JSON schemas for one Agent's input and output contracts."""

    try:
        input_model = INPUT_MODELS[agent_id]
        output_model = OUTPUT_MODELS[agent_id]
    except KeyError as exc:
        raise ValueError(f"Unknown investment-research agent_id: {agent_id}") from exc
    return input_model.model_json_schema(), output_model.model_json_schema()
