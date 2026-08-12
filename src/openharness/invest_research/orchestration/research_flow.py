"""Minimal CrewAI Flow proving Planner-to-parallel-research handoff."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openharness.invest_research.orchestration import configure_crewai_environment

configure_crewai_environment()

from crewai.flow.flow import Flow, listen, start

from openharness.invest_research.orchestration.flow_state import (
    CollaborationEvent,
    FlowAgentResult,
    ResearchFlowState,
)
from openharness.invest_research.orchestration.runtime_gateway import (
    OpenHarnessRuntimeGateway,
)
from openharness.invest_research.fallback_report import write_fallback_report
from openharness.invest_research.report_context import ReportContextBuilder
from openharness.invest_research.report_delivery import (
    run_output_dir,
    write_report_context,
    write_report_markdown,
)
from openharness.invest_research.runtime_adapter import AgentExecutionResult


_REPORT_CONTEXT_MAX_CHARS = 24_000


class InvestmentResearchSmokeFlow(Flow[ResearchFlowState]):
    """CrewAI controls order; OpenHarness executes the seven research roles."""

    def __init__(
        self,
        gateway: OpenHarnessRuntimeGateway | None = None,
        *,
        complete_report: bool = False,
    ) -> None:
        super().__init__()
        self.gateway = gateway or OpenHarnessRuntimeGateway()
        self.complete_report = complete_report
        self._project_root = Path(__file__).resolve().parents[4]
        self._raw_results: dict[str, AgentExecutionResult] = {}

    def _event(
        self,
        event_type: str,
        actor_id: str,
        message: str,
        *,
        target_agent_ids: list[str] | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
    ) -> None:
        self.state.events.append(
            CollaborationEvent(
                event_type=event_type,
                actor_id=actor_id,
                target_agent_ids=target_agent_ids or [],
                message=message,
                task_id=task_id,
                artifact_id=artifact_id,
            )
        )

    def _store_result(
        self,
        result: AgentExecutionResult,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        output_status = None
        if result.structured_output:
            output_status = str(result.structured_output.get("status") or "") or None
        self.state.agent_results[result.agent_id] = FlowAgentResult(
            agent_id=result.agent_id,
            execution_status=result.status,
            output_status=output_status,
            artifact_id=result.artifact_id,
            parameter_card_id=result.parameter_card_id,
            model=result.model,
            model_call_id=result.model_call_id,
            tool_names=[item.tool_name for item in result.tool_calls],
            total_tokens=result.usage.total_tokens,
            source_ids=result.source_ids,
            fact_ids=result.fact_ids,
            logic_ids=result.logic_ids,
            catalyst_ids=result.catalyst_ids,
            risk_ids=result.risk_ids,
            review_id=result.review_id,
            failure_class=result.failure_class,
            retry_count=result.retry_count,
            degraded=result.degraded,
            fallback_used=result.fallback_used,
            started_at=started_at,
            completed_at=completed_at,
            error=result.error,
        )
        self._raw_results[result.agent_id] = result
        self.state.total_retry_count += result.retry_count
        if result.warnings:
            self.state.warnings.extend(result.warnings)
        if result.artifact_id:
            self.state.artifact_history.setdefault(result.agent_id, []).append(result.artifact_id)

    @start()
    async def run_planner(self) -> AgentExecutionResult:
        self.state.current_stage = "planner_running"
        self.state.pipeline_status = "running"
        task_id = "TASK-CREWAI-PLANNER-001"
        self._event(
            "task_started",
            "system",
            f"启动 Planner，为 {self.state.company_query} 建立参数卡和后续任务计划。",
            target_agent_ids=["planner"],
            task_id=task_id,
        )
        result = await self.gateway.execute(
            agent_id="planner",
            input_payload={
                "protocol_version": "1.0",
                "run_id": self.state.run_id,
                "task_id": task_id,
                "objective": "建立研究参数卡，核验目标公司并推荐两家主要竞争对手。",
                "agent_id": "planner",
                "company_query": self.state.company_query,
                "as_of_date": self.state.as_of_date.isoformat(),
            },
            task_prompt=(
                "只执行规划职责。使用 tavily_search 核验目标公司和竞品，必要时使用 "
                "web_fetch 读取公开页面。输出完整 PlannerResult JSON，不撰写研报正文。"
                "task_plan 必须且只能包含六项，assigned_agent_id 分别为 fundamental、"
                "industry_competition、market_catalyst、risk、reviewer_arbiter、report_writer。"
            ),
            max_turns=8,
            tool_call_limits={"tavily_search": 4, "web_fetch": 4},
        )
        self._store_result(result)
        self.state.parameter_card_id = result.parameter_card_id
        self.state.planner_output = result.structured_output
        if result.status == "succeeded" and result.parameter_card_id:
            self.state.current_stage = "planner_completed"
            self._event(
                "artifact_delivered",
                "planner",
                "参数卡已生成，现将规划结果交给三个并行研究 Agent。",
                target_agent_ids=[
                    "fundamental",
                    "industry_competition",
                    "market_catalyst",
                ],
                task_id=task_id,
                artifact_id=result.artifact_id,
            )
        else:
            self.state.current_stage = "failed"
            self.state.pipeline_status = "planner_failed"
            self._event(
                "task_failed",
                "planner",
                result.error or "Planner 未生成可供下游使用的参数卡。",
                task_id=task_id,
            )
        return result

    async def _run_research_agent(
        self,
        agent_id: str,
        input_payload: dict[str, Any],
        task_prompt: str,
        context_package: dict[str, Any],
        tool_call_limits: dict[str, int],
        *,
        actor_id: str = "planner",
        dispatch_message: str | None = None,
        return_target_agent_id: str = "planner",
        timeout_seconds: float | None = None,
    ) -> AgentExecutionResult:
        task_id = input_payload["task_id"]
        started_at = datetime.now(UTC)
        self._event(
            "task_started",
            actor_id,
            dispatch_message or f"并行派发研究任务给 @{agent_id}。",
            target_agent_ids=[agent_id],
            task_id=task_id,
            artifact_id=context_package.get("planner_artifact_id"),
        )
        result = await self.gateway.execute(
            agent_id=agent_id,
            input_payload=input_payload,
            task_prompt=task_prompt,
            context_package=context_package,
            tool_call_limits=tool_call_limits,
            timeout_seconds=timeout_seconds or 300,
        )
        completed_at = datetime.now(UTC)
        self._store_result(
            result,
            started_at=started_at,
            completed_at=completed_at,
        )
        self._event(
            "artifact_delivered" if result.status == "succeeded" else "task_failed",
            agent_id,
            "研究成果已提交。" if result.status == "succeeded" else result.error or "研究任务失败。",
            target_agent_ids=[return_target_agent_id],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return result

    @listen(run_planner)
    async def run_parallel_research(
        self, planner_result: AgentExecutionResult
    ) -> dict[str, Any]:
        if planner_result.status != "succeeded" or not planner_result.parameter_card_id:
            if self.complete_report and not self.state.report_generated:
                return self._write_fallback_report(
                    f"Planner did not produce a usable parameter card: {planner_result.error or planner_result.status}"
                )
            return self.summary()

        self.state.current_stage = "research_running"
        planner_output = planner_result.structured_output or {}
        competitors = planner_output.get("recommended_competitors") or []
        catalyst_window = planner_output.get("catalyst_window")
        if len(competitors) != 2 or not catalyst_window:
            if self.complete_report:
                return self._write_fallback_report(
                    "Planner output was returned but did not contain exactly two competitors and a catalyst window"
                )
            self.state.current_stage = "failed"
            self.state.pipeline_status = "planner_output_incomplete"
            return self.summary()

        planner_context = {
            key: planner_output.get(key)
            for key in (
                "company_identity",
                "research_period",
                "catalyst_window",
                "recommended_competitors",
            )
            if planner_output.get(key) is not None
        }
        parameter_card = {
            "parameter_card_id": planner_result.parameter_card_id,
            "version": "1.0",
        }
        shared_context = {
            "planner_artifact_id": planner_result.artifact_id,
            "planner_context": planner_context,
        }
        specs = {
            "fundamental": {
                "task_id": "TASK-CREWAI-FUNDAMENTAL-001",
                "objective": "分析最近一年的经营和财务变化，形成可追溯事实与候选投资逻辑。",
                "extra_input": {},
                "prompt": "核验最近一年公开财务和经营资料，输出 FundamentalResult JSON；资料不足时返回 partial。",
                "limits": {"tavily_search": 2, "web_fetch": 2, "calculator": 1, "evidence_query": 1},
            },
            "industry_competition": {
                "task_id": "TASK-CREWAI-INDUSTRY-001",
                "objective": "分析行业趋势、竞争格局并与两家确认竞品进行统一口径比较。",
                "extra_input": {"confirmed_competitors": competitors},
                "prompt": (
                    "研究行业和三家公司，完成统一口径竞品比较并输出 IndustryCompetitionResult JSON。"
                    "若 status=completed，必须至少给出一条基于竞争差异、具有 F-ID 支持的 logic_candidates；"
                    "若证据不足以形成该逻辑，则返回 status=partial，并明确待验证缺口，不能返回 completed 后遗漏逻辑。"
                ),
                "limits": {"tavily_search": 3, "web_fetch": 3, "calculator": 2, "evidence_query": 1},
            },
            "market_catalyst": {
                "task_id": "TASK-CREWAI-MARKET-001",
                "objective": "研究基准日后六个月的市场信息与催化因素。",
                "extra_input": {"catalyst_window": catalyst_window},
                "prompt": "研究未来半年催化，区分事实、报道、预期和推测，输出 MarketCatalystResult JSON。",
                "limits": {"tavily_search": 4, "web_fetch": 3, "calculator": 1, "evidence_query": 1},
            },
        }

        results = await asyncio.gather(
            *(
                self._run_research_agent(
                    agent_id,
                    {
                        "protocol_version": "1.0",
                        "run_id": self.state.run_id,
                        "task_id": spec["task_id"],
                        "objective": spec["objective"],
                        "agent_id": agent_id,
                        "parameter_card": parameter_card,
                        "source_ids": planner_result.source_ids,
                        **spec["extra_input"],
                    },
                    spec["prompt"],
                    shared_context,
                    spec["limits"],
                )
                for agent_id, spec in specs.items()
            )
        )
        successful_results = [result for result in results if result.status == "succeeded"]
        failed_results = [result for result in results if result.status != "succeeded"]
        self.state.current_stage = "research_completed"
        self.state.pipeline_status = (
            "running_with_warnings" if failed_results else "running"
        )
        if failed_results:
            failed_agent_ids = ", ".join(result.agent_id for result in failed_results)
            self._event(
                "task_handoff",
                "system",
                f"部分并行研究未形成有效产物（{failed_agent_ids}）；将使用其余成果继续 @risk，缺口交由后续审查记录。",
                target_agent_ids=["risk"],
                task_id="TASK-CREWAI-RISK-001",
            )
        return self.summary()

    @listen(run_parallel_research)
    async def run_risk(self, _: dict[str, Any]) -> dict[str, Any]:
        if self.state.current_stage != "research_completed":
            return self.summary()

        upstream = {
            agent_id: self.state.agent_results[agent_id].artifact_id
            for agent_id in (
                "fundamental",
                "industry_competition",
                "market_catalyst",
            )
            if self.state.agent_results.get(agent_id)
            and self.state.agent_results[agent_id].artifact_id
        }
        if not upstream:
            if self.complete_report:
                return self._write_fallback_report(
                    "all parallel research Agents failed; no upstream artifact was available"
                )
            self.state.current_stage = "failed"
            self.state.pipeline_status = "missing_research_artifact"
            return self.summary()

        self.state.current_stage = "risk_running"
        task_id = "TASK-CREWAI-RISK-001"
        started_at = datetime.now(UTC)
        self._event(
            "task_handoff",
            "system",
            "将可用的上游研究产物交给 @risk 进行反方检验；缺失模块需要在结果中保留限制说明。",
            target_agent_ids=["risk"],
            task_id=task_id,
        )
        result = await self.gateway.execute(
            agent_id="risk",
            input_payload={
                "protocol_version": "1.0",
                "run_id": self.state.run_id,
                "task_id": task_id,
                "objective": "综合三路研究成果，识别风险、反方证据、触发条件和证伪指标。",
                "agent_id": "risk",
                "parameter_card": {
                    "parameter_card_id": self.state.parameter_card_id,
                    "version": "1.0",
                },
                "fundamental_artifact_id": upstream.get("fundamental"),
                "industry_competition_artifact_id": upstream.get(
                    "industry_competition"
                ),
                "market_catalyst_artifact_id": upstream.get("market_catalyst"),
                "logic_ids": self._collect_limited_ids("logic", upstream.keys(), limit=8),
            },
            task_prompt=(
                "Do not call tools in this stage unless the compact brief is completely missing. "
                "Use only context_package.compact_research_brief and the provided logic_ids to output RiskResult JSON. "
                "Produce status=partial if evidence is incomplete. Generate 3-5 risk_items from candidate logic, "
                "unverified_items, limitations and catalyst failure_signals. Each risk item must include title, "
                "trigger_conditions, impact_path, severity, probability_basis, falsification_indicators and monitoring_plan. "
                "Do not block, do not search the web, and do not inspect every source."
            ),
            context_package={
                "upstream_artifact_ids": upstream,
                "missing_upstream_agents": sorted(
                    {"fundamental", "industry_competition", "market_catalyst"}
                    - set(upstream)
                ),
                "compact_research_brief": self._compact_research_brief(
                    ("fundamental", "industry_competition", "market_catalyst")
                ),
            },
            max_turns=3,
            tool_call_limits={
                "evidence_query": 0,
                "tavily_search": 0,
                "web_fetch": 0,
                "calculator": 0,
            },
            timeout_seconds=120,
            retry_transient=False,
        )
        completed_at = datetime.now(UTC)
        succeeded = result.status == "succeeded"
        if not succeeded and self.complete_report:
            result = self._fallback_risk_result(result, upstream)
            succeeded = True
        self._store_result(result, started_at=started_at, completed_at=completed_at)
        self.state.current_stage = "risk_completed"
        self.state.pipeline_status = "running" if succeeded else "running_with_warnings"
        if not succeeded:
            self.state.warnings.append(
                f"Risk Agent did not complete: {result.error or result.status}."
            )
        self._event(
            "artifact_delivered" if succeeded else "task_failed",
            "risk",
            "风险诊断成果已提交。" if succeeded else result.error or "风险诊断失败。",
            target_agent_ids=["system"],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return self.summary()

    @listen(run_risk)
    async def run_reviewer(self, _: dict[str, Any]) -> dict[str, Any]:
        if self.state.current_stage != "risk_completed":
            return self.summary()

        upstream_ids = (
            "fundamental",
            "industry_competition",
            "market_catalyst",
            "risk",
        )
        artifacts = [
            self.state.agent_results[agent_id].artifact_id
            for agent_id in upstream_ids
            if self.state.agent_results.get(agent_id)
            and self.state.agent_results[agent_id].artifact_id
        ]
        self.state.current_stage = "reviewer_running"
        task_id = "TASK-CREWAI-REVIEWER-001"
        started_at = datetime.now(UTC)
        self._event(
            "task_handoff",
            "system",
                "风险诊断已完成，现将可用上游产物交给 @reviewer_arbiter 审查，并记录缺失模块。",
            target_agent_ids=["reviewer_arbiter"],
            task_id=task_id,
        )
        result = await self.gateway.execute(
            agent_id="reviewer_arbiter",
            input_payload={
                "protocol_version": "1.0",
                "run_id": self.state.run_id,
                "task_id": task_id,
                "objective": "核查来源、统计口径、逻辑冲突与证据缺口，并给出结构化审查路由。",
                "agent_id": "reviewer_arbiter",
                "parameter_card": {
                    "parameter_card_id": self.state.parameter_card_id,
                    "version": "1.0",
                },
                "research_artifact_ids": artifacts,
                "source_ids": self._collect_limited_ids("source", upstream_ids, limit=12),
                "fact_ids": self._collect_limited_ids("fact", upstream_ids, limit=16),
                "logic_ids": self._collect_limited_ids("logic", upstream_ids, limit=8),
            },
            task_prompt=(
                "Do not call tools in this stage unless the compact brief is completely missing. "
                "Use context_package.compact_research_brief as the primary review package. "
                "Output ReviewDecision JSON quickly. If there are at least three candidate logic IDs, "
                "prefer decision=approve_with_warnings and include exactly three approved_logic_ids. "
                "Record missing modules, source gaps or口径问题 as issues/limitations. "
                "Limit issues to the 3 most important problems. Do not block and do not perform exhaustive evidence checks."
            ),
            context_package={
                "research_artifact_ids": artifacts,
                "missing_research_agents": sorted(
                    agent_id
                    for agent_id in upstream_ids
                    if not self.state.agent_results.get(agent_id)
                    or not self.state.agent_results[agent_id].artifact_id
                ),
                "compact_research_brief": self._compact_research_brief(upstream_ids),
            },
            max_turns=3,
            tool_call_limits={"evidence_query": 0},
            timeout_seconds=90,
            retry_transient=False,
        )
        completed_at = datetime.now(UTC)
        self._store_result(
            result,
            started_at=started_at,
            completed_at=completed_at,
        )
        succeeded = result.status == "succeeded"
        self.state.review_output = result.structured_output or {}
        if (
            succeeded
            and
            self.state.review_output.get("decision") == "request_supplement"
            and self.state.review_output.get("issues")
        ):
            self.state.current_stage = "supplement_pending"
            self.state.pipeline_status = "running"
        else:
            if self.complete_report:
                self.state.current_stage = "supplement_completed"
                self.state.pipeline_status = "running_with_warnings" if not succeeded else "running"
            else:
                self.state.current_stage = "completed" if succeeded else "failed"
                self.state.pipeline_status = "completed" if succeeded else "reviewer_failed"
            if not succeeded:
                self.state.warnings.append(
                    f"Reviewer did not complete: {result.error or result.status}."
                )
        self._event(
            "artifact_delivered" if succeeded else "task_failed",
            "reviewer_arbiter",
            "结构化审查结果已提交。" if succeeded else result.error or "审查失败。",
            target_agent_ids=["system"],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return self.summary()

    @listen(run_reviewer)
    async def run_supplements(self, _: dict[str, Any]) -> dict[str, Any]:
        if self.state.current_stage != "supplement_pending":
            return self.summary()

        issues = self.state.review_output.get("issues", []) if self.state.review_output else []
        issues_by_agent = {
            agent_id: [issue for issue in issues if issue.get("target_agent_id") == agent_id]
            for agent_id in ("fundamental", "industry_competition", "market_catalyst", "risk")
        }
        self.state.supplement_round = 1
        self.state.current_stage = "supplement_research_running"
        parameter_card = {
            "parameter_card_id": self.state.parameter_card_id,
            "version": "1.0",
        }
        planner_output = self.state.planner_output or {}
        specs: dict[str, tuple[dict[str, Any], dict[str, int]]] = {}
        if self.state.agent_results.get("fundamental"):
            specs["fundamental"] = (
                {"source_ids": self.state.agent_results["fundamental"].source_ids},
                {"tavily_search": 1, "web_fetch": 1, "calculator": 0, "evidence_query": 6},
            )
        if self.state.agent_results.get("industry_competition"):
            specs["industry_competition"] = (
                {
                    "source_ids": self.state.agent_results["industry_competition"].source_ids,
                    "confirmed_competitors": planner_output.get("recommended_competitors", []),
                },
                {"tavily_search": 3, "web_fetch": 4, "calculator": 2, "evidence_query": 2},
            )
        if self.state.agent_results.get("market_catalyst"):
            specs["market_catalyst"] = (
                {
                    "source_ids": self.state.agent_results["market_catalyst"].source_ids,
                    "catalyst_window": planner_output.get("catalyst_window"),
                },
                {"tavily_search": 4, "web_fetch": 4, "calculator": 1, "evidence_query": 2},
            )
        for agent_id in set(issues_by_agent) - set(specs):
            if issues_by_agent[agent_id]:
                self.state.warnings.append(
                    f"Cannot supplement {agent_id}: original artifact is missing."
                )
        research_results = await asyncio.gather(
            *(
                self._run_research_agent(
                    agent_id,
                    {
                        "protocol_version": "1.0",
                        "run_id": self.state.run_id,
                        "task_id": f"TASK-CREWAI-SUPPLEMENT-{agent_id.upper()}-001",
                        "objective": "按审查意见补充核验并更新原研究产物。",
                        "agent_id": agent_id,
                        "parameter_card": parameter_card,
                        "supplement": self._supplement_request(issues_by_agent[agent_id]),
                        **extra_input,
                    },
                    "这是第 1 轮定向补充。仅处理以下审查问题，补充可核验来源后输出更新 JSON："
                    + str(issues_by_agent[agent_id]),
                    {
                        "prior_artifact_id": self.state.agent_results[agent_id].artifact_id,
                        "review_issues": issues_by_agent[agent_id],
                    },
                    limits,
                    actor_id="reviewer_arbiter",
                    dispatch_message=f"审查定向回流给 @{agent_id} 进行第 1 轮补充。",
                    return_target_agent_id="reviewer_arbiter",
                    timeout_seconds=420 if agent_id == "fundamental" else None,
                )
                for agent_id, (extra_input, limits) in specs.items()
                if issues_by_agent[agent_id]
            )
        )
        if not all(result.status in {"succeeded", "invalid_output", "failed", "blocked"} for result in research_results):
            self.state.warnings.append("Supplement stage returned an unexpected result.")

        if issues_by_agent["risk"]:
            self.state.current_stage = "supplement_risk_running"
            upstream = {
                agent_id: item.artifact_id
                for agent_id in ("fundamental", "industry_competition", "market_catalyst")
                if (item := self.state.agent_results.get(agent_id)) and item.artifact_id
            }
            task_id = "TASK-CREWAI-SUPPLEMENT-RISK-001"
            started_at = datetime.now(UTC)
            self._event(
                "task_handoff",
                "reviewer_arbiter",
                "审查定向回流给 @risk，基于更新后的三份研究产物复评。",
                target_agent_ids=["risk"],
                task_id=task_id,
            )
            result = await self.gateway.execute(
                agent_id="risk",
                input_payload={
                    "protocol_version": "1.0",
                    "run_id": self.state.run_id,
                    "task_id": task_id,
                    "objective": "按审查意见复核风险等级、触发条件和逻辑映射。",
                    "agent_id": "risk",
                    "parameter_card": parameter_card,
                    "supplement": self._supplement_request(issues_by_agent["risk"]),
                    "fundamental_artifact_id": upstream.get("fundamental"),
                    "industry_competition_artifact_id": upstream.get("industry_competition"),
                    "market_catalyst_artifact_id": upstream.get("market_catalyst"),
                    "logic_ids": sorted(
                        {
                            logic_id
                            for agent_id in upstream
                            for logic_id in self.state.agent_results[agent_id].logic_ids
                        }
                    ),
                },
                task_prompt=(
                    "Do not call tools. Use context_package.compact_research_brief and review_issues only. "
                    "Return a concise RiskResult JSON update. If evidence is insufficient, keep status=partial."
                ),
                context_package={
                    "upstream_artifact_ids": upstream,
                    "review_issues": issues_by_agent["risk"],
                    "compact_research_brief": self._compact_research_brief(
                        ("fundamental", "industry_competition", "market_catalyst")
                    ),
                },
                max_turns=3,
                tool_call_limits={"evidence_query": 0, "tavily_search": 0, "web_fetch": 0, "calculator": 0},
                timeout_seconds=120,
                retry_transient=False,
            )
            completed_at = datetime.now(UTC)
            if result.status != "succeeded" and self.complete_report:
                result = self._fallback_risk_result(result, upstream)
            self._store_result(result, started_at=started_at, completed_at=completed_at)
            self._event(
                "artifact_delivered" if result.status == "succeeded" else "task_failed",
                "risk",
                "风险补充成果已提交。" if result.status == "succeeded" else result.error or "风险补充失败。",
                target_agent_ids=["reviewer_arbiter"],
                task_id=task_id,
                artifact_id=result.artifact_id,
            )
            if result.status != "succeeded":
                self.state.warnings.append(
                    f"Risk supplement did not complete: {result.error or result.status}."
                )

        self.state.current_stage = "supplement_completed"
        self.state.pipeline_status = "awaiting_reviewer_recheck"
        return self.summary()

    @listen(run_supplements)
    async def run_final_review(self, _: dict[str, Any]) -> dict[str, Any]:
        """Run the final review after supplements; never block report delivery."""

        if (
            not self.complete_report
            or self.state.report_generated
            or self.state.current_stage != "supplement_completed"
        ):
            return self.summary()

        # A failed first reviewer should not trigger a second expensive reviewer
        # call.  The report is still delivered from the collected artifacts and
        # is explicitly marked as partial/fallback by _write_fallback_report.
        prior_reviewer = self.state.agent_results.get("reviewer_arbiter")
        if prior_reviewer and prior_reviewer.execution_status != "succeeded":
            self.state.review_status = "failed_with_warnings"
            return self._write_fallback_report(
                "ReviewerArbiter failed; skipped duplicate final review and delivered collected artifacts"
            )

        self.state.current_stage = "final_reviewer_running"
        upstream_ids = (
            "fundamental",
            "industry_competition",
            "market_catalyst",
            "risk",
        )
        artifact_ids = [
            self.state.agent_results[agent_id].artifact_id
            for agent_id in upstream_ids
            if self.state.agent_results.get(agent_id)
            and self.state.agent_results[agent_id].artifact_id
        ]
        task_id = "TASK-CREWAI-FINAL-REVIEW-001"
        self._event(
            "task_handoff",
            "system",
            "将补充后的研究成果交给 @reviewer_arbiter 进行最终复审。",
            target_agent_ids=["reviewer_arbiter"],
            task_id=task_id,
        )
        result = await self.gateway.execute(
            agent_id="reviewer_arbiter",
            input_payload={
                "protocol_version": "1.0",
                "run_id": self.state.run_id,
                "task_id": task_id,
                "objective": "复核补充后的研究成果，并决定哪些内容可以进入报告。",
                "agent_id": "reviewer_arbiter",
                "parameter_card": {
                    "parameter_card_id": self.state.parameter_card_id,
                    "version": "1.0",
                },
                "research_artifact_ids": artifact_ids,
                "source_ids": self._collect_limited_ids("source", upstream_ids, limit=12),
                "fact_ids": self._collect_limited_ids("fact", upstream_ids, limit=16),
                "logic_ids": self._collect_limited_ids("logic", upstream_ids, limit=8),
                "prior_issue_ids": [
                    issue.get("issue_id")
                    for issue in (self.state.review_output or {}).get("issues", [])
                    if issue.get("issue_id")
                ],
                "supplement_artifact_ids": [
                    artifact_id
                    for agent_id, history in self.state.artifact_history.items()
                    if agent_id != "reviewer_arbiter"
                    for artifact_id in history[1:]
                ],
            },
            task_prompt=(
                "Do not call tools. Use context_package.compact_research_brief and initial_review only. "
                "Output ReviewDecision JSON. Prefer approve_with_warnings when three candidate logic IDs exist; "
                "include exactly three approved_logic_ids. Keep unresolved issues as warnings instead of blocking report delivery."
            ),
            context_package={
                "research_artifact_ids": artifact_ids,
                "initial_review": self.state.review_output or {},
                "supplement_round": self.state.supplement_round,
                "compact_research_brief": self._compact_research_brief(upstream_ids),
            },
                max_turns=3,
                tool_call_limits={"evidence_query": 0},
                timeout_seconds=90,
                retry_transient=False,
        )
        self._store_result(result, started_at=datetime.now(UTC), completed_at=datetime.now(UTC))
        self.state.final_review_output = result.structured_output or {}
        self.state.review_output = self.state.final_review_output
        self.state.review_status = str(
            self.state.final_review_output.get("decision") or result.status
        )
        if result.status != "succeeded":
            self.state.warnings.append(
                f"Final reviewer failed: {result.error or result.status}."
            )
        self._apply_review_statuses(self.state.final_review_output)
        self.state.current_stage = "completed"
        self.state.pipeline_status = "running_with_warnings"
        self._event(
            "artifact_delivered" if result.status == "succeeded" else "task_failed",
            "reviewer_arbiter",
            "最终复审已完成，问题将随报告一并披露。"
            if result.status == "succeeded"
            else "最终复审失败，使用现有材料继续交付。",
            target_agent_ids=["report_writer"],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return self.summary()

    @listen(run_final_review)
    async def run_report(self, _: dict[str, Any]) -> dict[str, Any]:
        """Generate a ReportWriter artifact or a transparent local fallback."""

        if not self.complete_report or self.state.report_generated:
            return self.summary()

        review = self.state.final_review_output or self.state.review_output or {}
        logic_ids = list(review.get("approved_logic_ids") or [])
        if len(logic_ids) != 3:
            return self._write_fallback_report(
                "final review did not approve exactly three logic IDs"
            )

        store = self.gateway.evidence_store
        if store is None:
            return self._write_fallback_report("evidence store is unavailable")
        try:
            report_context = ReportContextBuilder(
                store, max_chars=_REPORT_CONTEXT_MAX_CHARS
            ).build(
                self.state.run_id, review
            )
            write_report_context(
                self._project_root, self.state.run_id, report_context
            )
        except (OSError, ValueError) as exc:
            return self._write_fallback_report(
                f"report context could not be built: {type(exc).__name__}: {exc}"
            )

        task_id = "TASK-CREWAI-REPORT-WRITER-001"
        result = await self.gateway.execute(
            agent_id="report_writer",
            input_payload={
                "protocol_version": "1.0",
                "run_id": self.state.run_id,
                "task_id": task_id,
                "objective": "基于审查结果生成完整上市公司研究报告。",
                "agent_id": "report_writer",
                "parameter_card": {
                    "parameter_card_id": self.state.parameter_card_id,
                    "version": "1.0",
                },
                "review_id": review.get("review_id"),
                "approved_fact_ids": review.get("approved_fact_ids", []),
                "approved_logic_ids": logic_ids,
                "approved_catalyst_ids": review.get("approved_catalyst_ids", []),
                "approved_risk_ids": review.get("approved_risk_ids", []),
            },
            task_prompt=(
                "不得调用工具。只使用 context_package.report_context。输出轻量 ReportResult JSON。"
                "sections 必须恰好包含八个 section_id：company_overview、operating_changes、"
                "investment_logics、peer_comparison、catalysts、risks、tracking_indicators、"
                "limitations。included_logic_ids 必须恰好使用材料包中的三条 L-ID。核心章节"
                "必须填写 evidence_ids。不得新增材料包中不存在的事实或编号。"
            ),
            context_package={"report_context": report_context},
            max_turns=2,
            tool_call_limits={},
            timeout_seconds=180,
            retry_transient=True,
        )
        self._store_result(result, started_at=datetime.now(UTC), completed_at=datetime.now(UTC))
        if result.status != "succeeded":
            return self._write_fallback_report(
                f"ReportWriter failed: {result.error or result.status}"
            )
        self.state.report_result = result.structured_output or {}
        self.state.report_path = self._write_report_markdown(result)
        self.state.report_generated = True
        degraded = any(
            item.execution_status != "succeeded"
            for agent_id, item in self.state.agent_results.items()
            if agent_id != "report_writer"
        ) or self.state.review_status in {
            "approve_with_warnings",
            "request_supplement",
            "require_human_resolution",
        }
        self.state.report_quality = "partial" if degraded else "complete"
        self.state.pipeline_status = "completed_with_warnings" if degraded else "completed"
        self._event(
            "report_generated",
            "report_writer",
            "ReportWriter 已生成 Markdown 研究报告。",
            target_agent_ids=["system"],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return self.summary()

    def _write_fallback_report(self, reason: str) -> dict[str, Any]:
        if self.state.report_generated:
            self.state.warnings.append(f"fallback report already exists; skipped duplicate: {reason}")
            return self.summary()
        output_path = run_output_dir(self._project_root, self.state.run_id) / "report.md"
        store = self.gateway.evidence_store
        if store is not None:
            fallback = write_fallback_report(
                store=store,
                run_id=self.state.run_id,
                output_path=output_path,
                results=self._raw_results,
                receipts=[self._raw_receipt(item) for item in self._raw_results.values()],
                reason=reason,
                review_output=self.state.review_output,
            )
            self.state.report_quality = "fallback" if fallback["report_quality"] == "fallback" else "partial"
            compatibility_path = (
                self._project_root
                / ".openharness"
                / "validation"
                / "full-chain-report.md"
            )
            compatibility_path.parent.mkdir(parents=True, exist_ok=True)
            compatibility_path.write_bytes(output_path.read_bytes())
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                "# 投研 Agent 协作报告\n\n"
                f"本次流程未能生成结构化 ReportWriter 结果：{reason}\n\n"
                "当前阶段未获得足够可靠资料，待后续补充。\n\n"
                "本报告不构成投资建议。\n",
                encoding="utf-8",
            )
            self.state.report_quality = "fallback"
        self.state.report_path = str(output_path)
        self.state.report_generated = True
        self.state.fallback_used = True
        self.state.current_stage = "completed"
        self.state.pipeline_status = "completed_with_warnings"
        self.state.warnings.append(reason)
        self._event("fallback_report_generated", "system", "已使用本地兜底程序生成报告。", target_agent_ids=["system"])
        return self.summary()

    def _apply_review_statuses(self, review: dict[str, Any]) -> None:
        """Promote only explicitly review-approved records for ReportWriter."""

        store = self.gateway.evidence_store
        if store is None or review.get("decision") not in {
            "approve_for_report",
            "approve_with_warnings",
        }:
            return
        for record_type, key, status in (
            ("fact", "approved_fact_ids", "verified"),
            ("logic", "approved_logic_ids", "approved"),
            ("catalyst", "approved_catalyst_ids", "approved"),
            ("risk", "approved_risk_ids", "approved"),
        ):
            ids = [str(item) for item in review.get(key, []) if item]
            if ids:
                store.update_record_status(self.state.run_id, record_type, ids, status)

    def _raw_receipt(self, result: AgentExecutionResult) -> dict[str, Any]:
        return {
            "agent_id": result.agent_id,
            "execution_status": result.status,
            "tool_count": len(result.tool_calls),
            "failure_class": result.failure_class,
            "retry_count": result.retry_count,
            "error": result.error,
        }

    def _write_report_markdown(self, result: AgentExecutionResult) -> str:
        return str(
            write_report_markdown(
                self._project_root,
                self.state.run_id,
                result.structured_output or {},
                review_output=self.state.final_review_output or self.state.review_output,
            )
        )

    def _collect_limited_ids(
        self,
        id_kind: str,
        agent_ids: tuple[str, ...] | list[str] | Any,
        *,
        limit: int,
    ) -> list[str]:
        """Keep downstream review inputs small and deterministic."""

        field_name = {
            "source": "source_ids",
            "fact": "fact_ids",
            "logic": "logic_ids",
            "catalyst": "catalyst_ids",
            "risk": "risk_ids",
        }[id_kind]
        collected: list[str] = []
        for agent_id in agent_ids:
            result = self.state.agent_results.get(str(agent_id))
            if not result:
                continue
            for item in getattr(result, field_name, []):
                if item not in collected:
                    collected.append(item)
                if len(collected) >= limit:
                    return collected
        return collected

    def _compact_research_brief(self, agent_ids: tuple[str, ...] | list[str]) -> dict[str, Any]:
        """Summarize upstream artifacts so Risk/Reviewer avoid reading everything."""

        brief: dict[str, Any] = {
            "run_id": self.state.run_id,
            "parameter_card_id": self.state.parameter_card_id,
            "agents": {},
            "candidate_logic_ids": self._collect_limited_ids("logic", agent_ids, limit=8),
            "key_fact_ids": self._collect_limited_ids("fact", agent_ids, limit=12),
            "key_source_ids": self._collect_limited_ids("source", agent_ids, limit=8),
            "catalyst_ids": self._collect_limited_ids("catalyst", agent_ids, limit=8),
        }
        for agent_id in agent_ids:
            result = self._raw_results.get(agent_id)
            receipt = self.state.agent_results.get(agent_id)
            output = result.structured_output if result and isinstance(result.structured_output, dict) else {}
            brief["agents"][agent_id] = {
                "status": receipt.execution_status if receipt else "missing",
                "output_status": receipt.output_status if receipt else None,
                "artifact_id": receipt.artifact_id if receipt else None,
                "completed_scope": output.get("completed_scope", [])[:4],
                "limitations": output.get("limitations", [])[:4],
                "unverified_items": output.get("unverified_items", [])[:4],
                "logic_candidates": self._summarize_items(
                    output.get("logic_candidates", []),
                    ("logic_id", "title", "mechanism", "falsification_conditions"),
                    limit=4,
                ),
                "risk_items": self._summarize_items(
                    output.get("risk_items", []),
                    ("risk_id", "title", "affected_logic_ids", "trigger_conditions", "severity"),
                    limit=4,
                ),
                "events": self._summarize_items(
                    output.get("events", []),
                    ("catalyst_id", "title", "expected_date", "expected_window", "direction", "failure_signals"),
                    limit=5,
                ),
                "peer_comparison": self._summarize_items(
                    output.get("peer_comparison", []),
                    ("company_name", "ticker", "values"),
                    limit=3,
                ),
            }
        return brief

    def _summarize_items(
        self,
        items: Any,
        fields: tuple[str, ...],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        summarized: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return summarized
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            summarized.append({field: item.get(field) for field in fields if field in item})
        return summarized

    def _fallback_risk_result(
        self,
        failed_result: AgentExecutionResult,
        upstream_artifact_ids: dict[str, str],
    ) -> AgentExecutionResult:
        """Create a transparent Risk artifact when the model times out."""

        brief = self._compact_research_brief(
            ("fundamental", "industry_competition", "market_catalyst")
        )
        logic_ids = brief.get("candidate_logic_ids") or []
        risk_items: list[dict[str, Any]] = []
        index = 1
        for agent_id, agent_brief in (brief.get("agents") or {}).items():
            for item in agent_brief.get("unverified_items", []) or []:
                if len(risk_items) >= 5:
                    break
                label = item.get("item") or item.get("reason") or "上游待验证事项"
                risk_items.append(
                    {
                        "risk_id": f"R-FALLBACK-{index:03d}",
                        "title": f"{agent_id} 待验证事项可能影响结论",
                        "affected_logic_ids": logic_ids[:3],
                        "trigger_conditions": [str(label)],
                        "impact_path": "上游资料尚未完全核验，可能影响投资逻辑、催化判断或竞品比较。",
                        "severity": "medium",
                        "probability_basis": "由上游 Agent 的 unverified_items/limitations 自动降级生成，未经过 Risk Agent 完整模型审查。",
                        "time_window": "未来半年及后续跟踪期",
                        "falsification_indicators": ["补充公开来源后该待验证事项被证伪或影响显著下降"],
                        "monitoring_plan": ["后续重新运行 Risk Agent 或人工核验对应来源"],
                        "source_ids": brief.get("key_source_ids", [])[:3],
                    }
                )
                index += 1
            for event in agent_brief.get("events", []) or []:
                if len(risk_items) >= 5:
                    break
                signals = [str(item) for item in event.get("failure_signals", []) or [] if item]
                if not signals:
                    continue
                risk_items.append(
                    {
                        "risk_id": f"R-FALLBACK-{index:03d}",
                        "title": f"{event.get('title') or '催化因素'} 兑现不及预期",
                        "affected_logic_ids": logic_ids[:3],
                        "trigger_conditions": signals[:3],
                        "impact_path": "催化事件延后或失效，可能削弱短期业绩预期、估值情绪或产业链验证强度。",
                        "severity": "medium",
                        "probability_basis": "由 MarketCatalyst 的 failure_signals 自动降级生成，未经过 Risk Agent 完整模型审查。",
                        "time_window": "未来半年",
                        "falsification_indicators": signals[:3],
                        "monitoring_plan": ["跟踪公司公告、订单交付、政策落地和后续财报披露"],
                        "source_ids": brief.get("key_source_ids", [])[:3],
                    }
                )
                index += 1
        if not risk_items:
            risk_items.append(
                {
                    "risk_id": "R-FALLBACK-001",
                    "title": "Risk Agent 未完成导致风险识别不足",
                    "affected_logic_ids": logic_ids[:3],
                    "trigger_conditions": ["Risk Agent 执行超时或模型未及时返回结构化结果"],
                    "impact_path": "报告中的风险章节主要依赖上游材料，尚未完成独立反方检验。",
                    "severity": "high",
                    "probability_basis": "系统运行记录显示 Risk Agent 未正常交付。",
                    "time_window": "本次研究周期",
                    "falsification_indicators": ["重新运行 Risk Agent 并获得完整 RiskResult"],
                    "monitoring_plan": ["缩短 Risk 输入后重新执行完整链路"],
                    "source_ids": brief.get("key_source_ids", [])[:3],
                }
            )
        payload = {
            "protocol_version": "1.0",
            "status": "partial",
            "completed_scope": ["本地降级风险识别"],
            "evidence_refs": [
                ref
                for refs in upstream_artifact_ids.values()
                for ref in [refs]
                if ref
            ],
            "unverified_items": [
                {
                    "item": "Risk Agent 未完成完整模型分析",
                    "reason": failed_result.error or "Risk runtime failed",
                    "required_evidence": "重新运行 Risk Agent 或由人工补充反方证据",
                }
            ],
            "limitations": ["该 RiskResult 为本地兜底产物，不等同于完整 Risk Agent 审查。"],
            "handoff_requests": [],
            "blocking_reasons": [],
            "challenged_logic_ids": logic_ids[:3],
            "assumption_matrix": [],
            "risk_items": risk_items,
            "counter_evidence": [],
            "conflict_candidates": [],
            "trigger_conditions": [
                condition
                for item in risk_items
                for condition in item.get("trigger_conditions", [])
            ][:8],
            "impact_paths": [item["impact_path"] for item in risk_items[:5]],
            "severity": [item["severity"] for item in risk_items[:5]],
            "probability_basis": [item["probability_basis"] for item in risk_items[:5]],
            "falsification_indicators": [
                indicator
                for item in risk_items
                for indicator in item.get("falsification_indicators", [])
            ][:8],
            "monitoring_plan": [
                plan
                for item in risk_items
                for plan in item.get("monitoring_plan", [])
            ][:8],
        }
        store = self.gateway.evidence_store
        persisted = (
            store.persist_agent_output(
                run_id=self.state.run_id,
                task_id="TASK-CREWAI-RISK-001",
                agent_id="risk",
                payload=payload,
            )
            if store is not None
            else {}
        )
        return AgentExecutionResult(
            status="succeeded",
            agent_id="risk",
            runtime_agent_name="investment-research:risk",
            model=failed_result.model,
            structured_output=payload,
            raw_output=failed_result.raw_output,
            model_call_id=failed_result.model_call_id,
            tool_calls=failed_result.tool_calls,
            usage=failed_result.usage,
            artifact_id=persisted.get("artifact_id"),
            risk_ids=persisted.get("risk_ids", []),
            warnings=[
                *failed_result.warnings,
                f"Risk Agent failed with {failed_result.failure_class}; generated local fallback RiskResult.",
            ],
            failure_class=failed_result.failure_class,
            retry_count=failed_result.retry_count,
            degraded=True,
            fallback_used=True,
            error=failed_result.error,
        )

    def _supplement_request(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        """Collapse one role's review issues into the existing AgentInputBase contract."""

        first = issues[0]
        return {
            "issue_id": first["issue_id"],
            "supplement_round": self.state.supplement_round,
            "problem_statement": "\n".join(issue["problem_statement"] for issue in issues),
            "expected_fields": sorted(
                {field for issue in issues for field in issue.get("expected_fields", [])}
            ),
            "input_refs": sorted(
                {ref for issue in issues for ref in issue.get("input_refs", [])}
            ),
        }

    def summary(self) -> dict[str, Any]:
        """Return a JSON-safe receipt proving orchestration and runtime boundaries."""

        payload = {
            "flow_id": self.state.id,
            "run_id": self.state.run_id,
            "company_query": self.state.company_query,
            "as_of_date": self.state.as_of_date.isoformat(),
            "current_stage": self.state.current_stage,
            "pipeline_status": self.state.pipeline_status,
            "parameter_card_id": self.state.parameter_card_id,
            "orchestrator": "CrewAI Flow",
            "executor": "OpenHarness InvestmentResearchRuntimeAdapter",
            "orchestrator_llm_calls": self.state.orchestrator_llm_calls,
            "runtime_executions": self.gateway.execution_count,
            "report_generated": self.state.report_generated,
            "report_quality": self.state.report_quality,
            "report_path": self.state.report_path,
            "review_status": self.state.review_status,
            "fallback_used": self.state.fallback_used,
            "total_retry_count": self.state.total_retry_count,
            "warnings": self.state.warnings,
            "agent_results": {
                key: value.model_dump(mode="json")
                for key, value in self.state.agent_results.items()
            },
            "events": [item.model_dump(mode="json") for item in self.state.events],
        }
        self._write_checkpoint(payload)
        return payload

    def _write_checkpoint(self, payload: dict[str, Any]) -> None:
        """Persist only resumable, credential-free flow state after each stage."""

        try:
            output_dir = self._project_root / ".openharness" / "validation"
            output_dir.mkdir(parents=True, exist_ok=True)
            checkpoint = output_dir / f"{self.state.run_id}.checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "run_id": self.state.run_id,
                        "current_stage": self.state.current_stage,
                        "pipeline_status": self.state.pipeline_status,
                        "parameter_card_id": self.state.parameter_card_id,
                        "supplement_round": self.state.supplement_round,
                        "report_generated": self.state.report_generated,
                        "report_path": self.state.report_path,
                        "planner_output": self.state.planner_output,
                        "review_output": self.state.review_output,
                        "final_review_output": self.state.final_review_output,
                        "report_quality": self.state.report_quality,
                        "review_status": self.state.review_status,
                        "fallback_used": self.state.fallback_used,
                        "artifact_history": self.state.artifact_history,
                        "agent_results": payload.get("agent_results", {}),
                        "events": payload.get("events", []),
                        "total_retry_count": self.state.total_retry_count,
                        "warnings": self.state.warnings,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            # A checkpoint must never bring down the research flow.
            if "checkpoint" not in self.state.warnings:
                self.state.warnings.append(f"checkpoint write failed: {type(exc).__name__}")


__all__ = ["InvestmentResearchSmokeFlow"]
