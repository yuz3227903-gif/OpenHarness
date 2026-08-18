"""Minimal CrewAI Flow proving Planner-to-parallel-research handoff."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from openharness.invest_research.orchestration import configure_crewai_environment

configure_crewai_environment()

from crewai.flow.flow import Flow, listen, start

from openharness.invest_research.delivery_decision import (
    DeliveryDecisionBuilder,
    register_recovery_review,
)
from openharness.invest_research.evidence_audit import (
    EvidenceAuditService,
    review_id_for,
)
from openharness.invest_research.fallback_report import write_fallback_report
from openharness.invest_research.orchestration.flow_state import (
    CollaborationEvent,
    FlowAgentResult,
    ResearchFlowState,
)
from openharness.invest_research.orchestration.runtime_gateway import (
    OpenHarnessRuntimeGateway,
)
from openharness.invest_research.planner_recovery import (
    is_competitor_placeholder,
    recover_partial_planner_output,
)
from openharness.invest_research.report_context import ReportContextBuilder
from openharness.invest_research.report_delivery import (
    run_output_dir,
    write_report_context,
    write_report_markdown,
)
from openharness.invest_research.report_sections import SectionReportOrchestrator
from openharness.invest_research.research_budget import (
    DEFAULT_RESEARCH_BUDGET_POLICY,
    ResearchBudgetPolicy,
)
from openharness.invest_research.runtime_adapter import AgentExecutionResult
from openharness.invest_research.source_catalog import build_shared_source_catalog


_REPORT_CONTEXT_MAX_CHARS = 24_000


def _compact_text(value: Any, *, limit: int = 110) -> str:
    """Return a channel-safe one-line preview without exposing raw model traces."""

    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _named_items(items: Any, *keys: str, limit: int = 2) -> list[str]:
    previews: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            value = next((item.get(key) for key in keys if item.get(key)), None)
        else:
            value = item
        preview = _compact_text(value)
        if preview and preview not in previews:
            previews.append(preview)
        if len(previews) >= limit:
            break
    return previews


def build_agent_delivery_message(result: AgentExecutionResult) -> str:
    """Build a useful, auditable handoff message from validated Agent output.

    The message intentionally exposes conclusions, tool names and evidence counts,
    but never raw prompts, hidden reasoning or full tool responses.
    """

    output = result.structured_output or {}
    details: list[str] = []
    if result.agent_id == "planner":
        company = output.get("company_identity") or {}
        company_name = company.get("short_name") or company.get("legal_name")
        competitors = _named_items(
            output.get("recommended_competitors"), "company_name", "short_name", limit=2
        )
        if company_name:
            details.append(f"目标公司已核验为 {_compact_text(company_name)}")
        if competitors:
            details.append(f"竞品候选为 {'、'.join(competitors)}")
    elif result.agent_id == "fundamental":
        details.extend(_named_items(output.get("change_drivers"), limit=2))
        details.extend(_named_items(output.get("logic_candidates"), "title", limit=1))
    elif result.agent_id == "industry_competition":
        details.extend(_named_items(output.get("relative_strengths"), limit=1))
        details.extend(_named_items(output.get("logic_candidates"), "title", limit=1))
    elif result.agent_id == "market_catalyst":
        details.extend(_named_items(output.get("events"), "title", limit=2))
        details.extend(_named_items(output.get("logic_candidates"), "title", limit=1))
    elif result.agent_id == "risk":
        details.extend(_named_items(output.get("risk_items"), "title", limit=3))
    elif result.agent_id == "reviewer_arbiter":
        decision = output.get("decision")
        rationale = _compact_text(output.get("decision_rationale"), limit=150)
        if decision:
            details.append(f"审查结论为 {decision}")
        if rationale:
            details.append(rationale)
    elif result.agent_id == "report_writer":
        details.extend(_named_items(output.get("sections"), "title", "section_id", limit=2))

    evidence_parts = []
    for label, values in (
        ("来源", result.source_ids),
        ("事实", result.fact_ids),
        ("逻辑", result.logic_ids),
        ("催化", result.catalyst_ids),
        ("风险", result.risk_ids),
    ):
        if values:
            evidence_parts.append(f"{label}{len(values)}条")

    tools = list(dict.fromkeys(call.tool_name for call in result.tool_calls))
    unverified_count = len(output.get("unverified_items") or []) + len(
        output.get("unverified_events") or []
    )
    limitation_count = len(output.get("limitations") or []) + len(
        output.get("source_limitations") or []
    )
    issue_count = len(output.get("issues") or [])

    sentences = ["本轮工作已完成。"]
    if tools:
        sentences.append(f"调用工具：{'、'.join(tools)}。")
    else:
        sentences.append("本轮基于上游已交付材料完成，未调用外部工具。")
    if evidence_parts:
        sentences.append(f"已登记{'、'.join(evidence_parts)}。")
    if details:
        sentences.append(f"核心交付：{'；'.join(details[:3])}。")
    pending = unverified_count + limitation_count + issue_count
    if pending:
        sentences.append(f"另有{pending}项待验证、限制或审查问题，已随成果一并移交。")
    return "".join(sentences)


class InvestmentResearchSmokeFlow(Flow[ResearchFlowState]):
    """CrewAI controls order; OpenHarness executes the seven research roles."""

    def __init__(
        self,
        gateway: OpenHarnessRuntimeGateway | None = None,
        *,
        complete_report: bool = False,
        budget_policy: ResearchBudgetPolicy | None = None,
        pause_callback: Callable[[], None] | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__()
        self.gateway = gateway or OpenHarnessRuntimeGateway()
        self.complete_report = complete_report
        self.budget_policy = budget_policy or DEFAULT_RESEARCH_BUDGET_POLICY
        self.pause_callback = pause_callback
        self.event_callback = event_callback
        self._project_root = Path(__file__).resolve().parents[4]
        self._raw_results: dict[str, AgentExecutionResult] = {}

    def _recover_planner_parameter_card(
        self, result: AgentExecutionResult
    ) -> AgentExecutionResult:
        """Recover a traceable provisional card from a partial Planner output."""

        # A provider can return a useful structured fragment together with a
        # transient/network/schema failure.  Try deterministic recovery for
        # every non-blocked result that has a dict payload; only an existing
        # card or an empty payload should skip recovery.
        if result.parameter_card_id or not isinstance(result.structured_output, dict):
            return result
        original_status = result.status
        original_error = result.error
        outcome = recover_partial_planner_output(
            run_id=self.state.run_id,
            company_query=self.state.company_query,
            as_of_date=self.state.as_of_date,
            output=result.structured_output,
            source_ids=result.source_ids,
        )
        if outcome is None:
            return result

        store = self.gateway.evidence_store
        if store is None:
            return result
        gate_payload = outcome.payload.get("gate_1_payload") or {}
        if hasattr(store, "persist_provisional_parameter_card"):
            store.persist_provisional_parameter_card(
                run_id=self.state.run_id,
                parameter_card_id=outcome.parameter_card_id,
                payload=gate_payload,
                reason=outcome.reason,
            )
        else:
            store.upsert_record(
                "parameter_card",
                outcome.parameter_card_id,
                self.state.run_id,
                {
                    "parameter_card_id": outcome.parameter_card_id,
                    "version": "1.0",
                    "run_id": self.state.run_id,
                    "recovery_used": True,
                    "recovery_reason": outcome.reason,
                    **gate_payload,
                },
                status="provisional",
                submitted_by="system",
            )

        result.structured_output = outcome.payload
        result.parameter_card_id = outcome.parameter_card_id
        result.degraded = True
        warning = (
            "Planner returned a structured partial result without a usable parameter card; "
            "the backend created a provisional card so specialist research could continue."
        )
        if original_status != "succeeded":
            warning += (
                f" 原始执行状态为 {original_status}"
                + (f"，原错误：{original_error}。" if original_error else "。")
                + "该状态已转换为可审计的暂定交接，不代表原始调用没有问题。"
            )
        if outcome.placeholder_competitor_count:
            warning += (
                f" IndustryCompetition must identify and replace "
                f"{outcome.placeholder_competitor_count} competitor placeholder(s)."
            )
        result.warnings = list(dict.fromkeys([*result.warnings, warning]))
        # Downstream Flow listeners use `succeeded + parameter_card_id` as the
        # handoff contract.  Promote only after the provisional card has been
        # persisted and keep the original failure class/warning for audit.
        result.status = "succeeded"
        result.error = None
        self.state.recovery_used = True
        if not self.state.recovery_reason:
            self.state.recovery_reason = outcome.reason
        self.state.recovery_actions.append(outcome.reason)
        self._event(
            "planner_parameter_card_recovered",
            "system",
            "Planner 部分结果已恢复为暂定参数卡；缺失竞品交由行业竞品 Agent 核验补齐。",
            target_agent_ids=["industry_competition"],
            task_id="TASK-CREWAI-PLANNER-001",
            artifact_id=result.artifact_id,
        )
        return result

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
        if self.pause_callback is not None:
            self.pause_callback()
        event = CollaborationEvent(
            event_type=event_type,
            actor_id=actor_id,
            target_agent_ids=target_agent_ids or [],
            message=message,
            task_id=task_id,
            artifact_id=artifact_id,
        )
        self.state.events.append(event)
        if self.event_callback is not None:
            try:
                payload = event.model_dump(mode="json")
                payload["run_id"] = self.state.run_id
                result = self._raw_results.get(actor_id)
                if result is not None and (
                    artifact_id is None or result.artifact_id == artifact_id
                ):
                    payload["result_projection"] = {
                        "execution_status": result.status,
                        "output_status": (
                            (result.structured_output or {}).get("status")
                        ),
                        "tool_names": list(
                            dict.fromkeys(call.tool_name for call in result.tool_calls)
                        ),
                        "total_tokens": result.usage.total_tokens,
                        "source_ids": result.source_ids,
                        "fact_ids": result.fact_ids,
                        "logic_ids": result.logic_ids,
                        "catalyst_ids": result.catalyst_ids,
                        "risk_ids": result.risk_ids,
                        "warnings": result.warnings[:3],
                    }
                self.event_callback(payload)
            except Exception:
                # UI projection must never break the research flow.
                pass

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
        cache_hits = sum(
            1 for item in result.tool_calls if bool((item.metadata or {}).get("cache_hit"))
        )
        external_calls = sum(
            1
            for item in result.tool_calls
            if bool((item.metadata or {}).get("external_call"))
        )
        budget_exhausted = sum(
            1
            for item in result.tool_calls
            if (item.metadata or {}).get("reason") == "tool_budget_exhausted"
        )
        self.state.agent_results[result.agent_id] = FlowAgentResult(
            agent_id=result.agent_id,
            execution_status=result.status,
            output_status=output_status,
            artifact_id=result.artifact_id,
            parameter_card_id=result.parameter_card_id,
            model=result.model,
            model_call_id=result.model_call_id,
            tool_names=[item.tool_name for item in result.tool_calls],
            tool_invocation_count=len(result.tool_calls),
            external_tool_call_count=external_calls,
            cache_hit_count=cache_hits,
            budget_exhausted_count=budget_exhausted,
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
        budget = self.budget_policy.for_agent("planner")
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
                "执行顺序是硬约束：先核验目标公司，再用尽可能少的搜索同时识别两家主要竞品；"
                "只有已经得到两家竞品候选后，才使用 web_fetch 核验关键页面。"
                "不得把全部搜索预算消耗在单一公司或单一竞品上。"
                "只执行规划职责。使用 tavily_search 核验目标公司和竞品，必要时使用 "
                "web_fetch 读取公开页面。输出完整 PlannerResult JSON，不撰写研报正文。"
                "task_plan 必须且只能包含六项，assigned_agent_id 分别为 fundamental、"
                "industry_competition、market_catalyst、risk、reviewer_arbiter、report_writer。"
            ),
            max_turns=budget.max_turns,
            max_output_tokens=budget.max_output_tokens,
            tool_call_limits=budget.tool_call_limits,
            timeout_seconds=150,
        )
        result = self._recover_planner_parameter_card(result)
        self._store_result(result)
        self.state.parameter_card_id = result.parameter_card_id
        self.state.planner_output = result.structured_output
        if result.status == "succeeded" and result.parameter_card_id:
            self.state.current_stage = "planner_completed"
            self._event(
                "artifact_delivered",
                "planner",
                build_agent_delivery_message(result)
                + " 现将参数卡和任务计划交给三个并行研究 Agent。",
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
        budget = self.budget_policy.for_agent(agent_id)
        effective_limits = {
            tool_name: min(
                int(requested_limit),
                int(budget.tool_call_limits.get(tool_name, 0)),
            )
            for tool_name, requested_limit in tool_call_limits.items()
        }
        started_at = datetime.now(UTC)
        self._event(
            "task_started",
            actor_id,
            dispatch_message
            or (
                f"请执行：{input_payload.get('objective') or '完成专业研究任务'}。"
                "输入包括 Planner 参数卡、共享来源目录及已授权证据；"
                "完成后请提交核心结论、证据编号和待验证事项。"
            ),
            target_agent_ids=[agent_id],
            task_id=task_id,
            artifact_id=context_package.get("planner_artifact_id"),
        )
        planned_tools = [
            tool_name
            for tool_name, allowed_count in effective_limits.items()
            if allowed_count > 0
        ]
        sender = actor_id if actor_id in {
            "planner", "reviewer_arbiter", "risk", "report_writer"
        } else "planner"
        tool_plan = (
            f"计划按需使用 {'、'.join(planned_tools)}"
            if planned_tools
            else "本轮只读取已授权的上游材料，不调用外部工具"
        )
        self._event(
            "agent_acknowledged",
            agent_id,
            f"@{sender} 已收到任务。{tool_plan}；我会把结论、证据编号、"
            "不可比项和待验证问题分别写清楚，再提交结构化成果。",
            target_agent_ids=[sender],
            task_id=task_id,
            artifact_id=context_package.get("planner_artifact_id"),
        )
        result = await self.gateway.execute(
            agent_id=agent_id,
            input_payload=input_payload,
            task_prompt=task_prompt,
            context_package=context_package,
            max_turns=budget.max_turns,
            max_output_tokens=budget.max_output_tokens,
            tool_call_limits=effective_limits,
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
            build_agent_delivery_message(result)
            if result.status == "succeeded"
            else result.error or "研究任务失败。",
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
                    "Planner did not produce a usable parameter card after bounded recovery: "
                    f"{planner_result.error or 'structured output was not traceable enough to recover'}",
                    trigger_stage="planner_parameter_card_missing",
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
        company_identity = planner_output.get("company_identity") or {}
        entity_names = [
            company_identity.get("legal_name"),
            company_identity.get("short_name"),
            *[
                item.get("company_name")
                for item in competitors
                if isinstance(item, dict)
            ],
        ]
        source_catalog = (
            build_shared_source_catalog(
                self.gateway.evidence_store,
                self.state.run_id,
                entities=entity_names,
            )
            if self.gateway.evidence_store is not None
            and hasattr(self.gateway.evidence_store, "source_catalog")
            else []
        )
        shared_context = {
            "planner_artifact_id": planner_result.artifact_id,
            "planner_context": planner_context,
            "shared_source_catalog": source_catalog,
            "source_reuse_policy": (
                "Reuse existing S-IDs first. Search only for a clearly identified gap; "
                "prefer A/B-grade sources and fetch a page only when its existing catalog "
                "status is not fetched or verified. Search snippets are not financial facts."
            ),
            "planner_recovery": {
                "recovery_used": bool(
                    self.state.recovery_reason
                    and self.state.recovery_reason.startswith("planner_")
                ),
                "recovery_reason": self.state.recovery_reason,
                "competitor_placeholders": [
                    item.get("company_name")
                    for item in competitors
                    if is_competitor_placeholder(item)
                ],
            },
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

        if shared_context["planner_recovery"]["competitor_placeholders"]:
            specs["industry_competition"]["prompt"] += (
                " confirmed_competitors 中名称以‘待 IndustryCompetition 核验’开头的项目"
                "只是系统占位，不是真实竞品。必须优先搜索并替换为真实、可追溯的主要竞品；"
                "不能把占位名称写入事实或最终比较表。无法核验时返回 partial 并披露缺口。"
            )

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
        budget = self.budget_policy.for_agent("risk")
        task_id = "TASK-CREWAI-RISK-001"
        started_at = datetime.now(UTC)
        self._event(
            "task_handoff",
            "system",
            "将可用的上游研究产物交给 @risk 进行反方检验；缺失模块需要在结果中保留限制说明。",
            target_agent_ids=["risk"],
            task_id=task_id,
        )
        self._event(
            "agent_acknowledged",
            "risk",
            f"@planner 已收到{len(upstream)}份上游成果。我将逐条挑战候选逻辑，"
            "重点整理风险标题、触发条件、影响路径、证伪指标和持续监测方法。",
            target_agent_ids=["planner"],
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
            max_turns=budget.max_turns,
            max_output_tokens=budget.max_output_tokens,
            tool_call_limits=budget.tool_call_limits,
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
            build_agent_delivery_message(result)
            if succeeded
            else result.error or "风险诊断失败。",
            target_agent_ids=["system"],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return self.summary()

    @listen(run_risk)
    async def run_reviewer(self, _: dict[str, Any]) -> dict[str, Any]:
        if self.state.current_stage != "risk_completed":
            return self.summary()

        budget = self.budget_policy.for_agent("reviewer_arbiter", stage="initial")
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
        logic_ids = self._collect_limited_ids("logic", upstream_ids, limit=8)
        audit = EvidenceAuditService(self.gateway.evidence_store).audit(
            self.state.run_id,
            "initial",
            logic_ids=logic_ids,
        )
        self.state.initial_evidence_audit = audit.model_dump(mode="json")
        expected_review_id = review_id_for(self.state.run_id, "initial")
        started_at = datetime.now(UTC)
        self._event(
            "task_handoff",
            "system",
                "风险诊断已完成，现将可用上游产物交给 @reviewer_arbiter 审查，并记录缺失模块。",
            target_agent_ids=["reviewer_arbiter"],
            task_id=task_id,
        )
        self._event(
            "agent_acknowledged",
            "reviewer_arbiter",
            f"@risk 已收到{len(artifacts)}份研究交付。我会先执行轻量证据审计，"
            "再抽查三条候选逻辑的 S/F/L 链；普通质量问题只记录警告，不无限阻断交付。",
            target_agent_ids=["risk"],
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
                "review_stage": "initial",
                "expected_review_id": expected_review_id,
                "audit_artifact_id": audit.audit_id,
                "audit_status": audit.status,
                "parameter_card": {
                    "parameter_card_id": self.state.parameter_card_id,
                    "version": "1.0",
                },
                "research_artifact_ids": artifacts,
                "source_ids": self._collect_limited_ids("source", upstream_ids, limit=12),
                "fact_ids": self._collect_limited_ids("fact", upstream_ids, limit=16),
                "logic_ids": logic_ids,
            },
            task_prompt=(
                "先读取 context_package.evidence_audit，再使用 evidence_query 定向抽查候选 L-ID、"
                "关联 F-ID 和 S-ID；最多两次查询，禁止外部搜索。只要三条逻辑均满足最小可追溯"
                "底线，普通来源等级、角色集中、数据口径和覆盖问题应选择 approve_with_warnings，"
                "不要阻断报告。只有缺少可修复的关键事实才 request_supplement，高等级冲突才"
                "require_human_resolution。输出 ReviewDecision JSON，最多记录3个最重要问题。"
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
                "evidence_audit": self.state.initial_evidence_audit,
                "moderate_review_policy": {
                    "hard_floor": "three logics; each has at least one current-Run Fact linked to a current-Run Source",
                    "soft_warnings": "source grade, single-fact support, role concentration, ordinary scope or comparability gaps",
                },
            },
            max_turns=budget.max_turns,
            max_output_tokens=budget.max_output_tokens,
            tool_call_limits=budget.tool_call_limits,
            timeout_seconds=120,
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
            build_agent_delivery_message(result)
            if succeeded
            else result.error or "审查失败。",
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
            risk_budget = self.budget_policy.for_agent("risk")
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
                max_turns=risk_budget.max_turns,
                max_output_tokens=risk_budget.max_output_tokens,
                tool_call_limits=risk_budget.tool_call_limits,
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
                build_agent_delivery_message(result)
                if result.status == "succeeded"
                else result.error or "风险补充失败。",
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
        # call. DeliveryDecisionBuilder will recover three traceable provisional
        # logics before ReportWriter is considered.
        prior_reviewer = self.state.agent_results.get("reviewer_arbiter")
        if prior_reviewer and prior_reviewer.execution_status != "succeeded":
            self.state.review_status = "failed_with_warnings"
            self.state.final_review_output = self.state.review_output or {}
            self.state.current_stage = "completed"
            self.state.pipeline_status = "running_with_warnings"
            self.state.warnings.append(
                "ReviewerArbiter failed; skipped duplicate final review and will "
                "attempt evidence-backed provisional delivery."
            )
            return self.summary()

        self.state.current_stage = "final_reviewer_running"
        budget = self.budget_policy.for_agent("reviewer_arbiter", stage="final")
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
        logic_ids = self._collect_limited_ids("logic", upstream_ids, limit=8)
        audit = EvidenceAuditService(self.gateway.evidence_store).audit(
            self.state.run_id,
            "final",
            logic_ids=logic_ids,
            prior_review=self.state.review_output,
        )
        self.state.final_evidence_audit = audit.model_dump(mode="json")
        expected_review_id = review_id_for(self.state.run_id, "final")
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
                "review_stage": "final",
                "expected_review_id": expected_review_id,
                "audit_artifact_id": audit.audit_id,
                "audit_status": audit.status,
                "parameter_card": {
                    "parameter_card_id": self.state.parameter_card_id,
                    "version": "1.0",
                },
                "research_artifact_ids": artifact_ids,
                "source_ids": self._collect_limited_ids("source", upstream_ids, limit=12),
                "fact_ids": self._collect_limited_ids("fact", upstream_ids, limit=16),
                "logic_ids": logic_ids,
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
                "使用 context_package.evidence_audit，并可调用 evidence_query 一次抽查补充后的关键"
                "S/F/L 链。禁止外部搜索。若三条逻辑满足最小可追溯底线，优先"
                "approve_with_warnings 并保留普通问题；不要因 C 级来源、单条事实支持或角色集中"
                "阻断报告。只有明确高等级冲突才 require_human_resolution。输出 ReviewDecision JSON。"
            ),
            context_package={
                "research_artifact_ids": artifact_ids,
                "initial_review": self.state.review_output or {},
                "supplement_round": self.state.supplement_round,
                "compact_research_brief": self._compact_research_brief(upstream_ids),
                "evidence_audit": self.state.final_evidence_audit,
            },
                max_turns=budget.max_turns,
                max_output_tokens=budget.max_output_tokens,
                tool_call_limits=budget.tool_call_limits,
                timeout_seconds=120,
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
            build_agent_delivery_message(result)
            + " 审查问题将随报告一并披露。"
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
        store = self.gateway.evidence_store
        if store is None:
            return self._write_fallback_report("evidence store is unavailable")
        reviewer_result = self.state.agent_results.get("reviewer_arbiter")
        recovery_reason = None
        if reviewer_result and reviewer_result.execution_status != "succeeded":
            recovery_reason = (
                f"reviewer_{reviewer_result.failure_class or reviewer_result.execution_status}"
            )
        try:
            delivery = DeliveryDecisionBuilder(store).build(
                self.state.run_id,
                review,
                recovery_reason=recovery_reason,
                audit_result=(
                    self.state.final_evidence_audit
                    or self.state.initial_evidence_audit
                ),
            )
        except (OSError, ValueError) as exc:
            return self._write_fallback_report(
                f"delivery decision could not be built: {type(exc).__name__}: {exc}"
            )
        self.state.delivery_decision = delivery.model_dump(mode="json")
        self.state.delivery_mode = delivery.delivery_mode
        self.state.recovery_used = delivery.recovery_used
        self.state.recovery_reason = delivery.recovery_reason
        if delivery.warnings:
            self.state.warnings.extend(
                warning for warning in delivery.warnings if warning not in self.state.warnings
            )
        if delivery.delivery_mode == "fallback":
            return self._write_fallback_report(
                delivery.recovery_reason
                or "fewer than three evidence-backed candidate logics are available"
            )
        logic_ids = list(delivery.selected_logic_ids)
        if delivery.delivery_mode == "provisional":
            register_recovery_review(store, self.state.run_id, delivery, review)
            action = (
                "Selected three traceable provisional logics with evidence-quality "
                "and research-role diversity ranking."
            )
            if action not in self.state.recovery_actions:
                self.state.recovery_actions.append(action)
            self._event(
                "delivery_recovered",
                "system",
                "Reviewer 未完成正式确认，系统从可追溯证据中暂定三条逻辑并继续生成报告。",
                target_agent_ids=["report_writer"],
            )
        try:
            report_context = ReportContextBuilder(
                store, max_chars=_REPORT_CONTEXT_MAX_CHARS
            ).build(
                self.state.run_id,
                review,
                delivery_decision=self.state.delivery_decision,
            )
            write_report_context(
                self._project_root, self.state.run_id, report_context
            )
        except (OSError, ValueError) as exc:
            return self._write_fallback_report(
                f"report context could not be built: {type(exc).__name__}: {exc}"
            )

        task_id = "TASK-CREWAI-REPORT-ASSEMBLE-001"
        report_input = {
            "protocol_version": "1.0",
            "run_id": self.state.run_id,
            "task_id": task_id,
            "objective": "根据审查后的证据材料分章节生成上市公司研究报告。",
            "agent_id": "report_writer",
            "parameter_card": {
                "parameter_card_id": self.state.parameter_card_id,
                "version": "1.0",
            },
            "review_id": delivery.review_id,
            "delivery_mode": delivery.delivery_mode,
            "selected_logic_ids": logic_ids,
            "approved_fact_ids": review.get("approved_fact_ids", []),
            "approved_logic_ids": logic_ids if delivery.delivery_mode == "formal" else [],
            "approved_catalyst_ids": review.get("approved_catalyst_ids", []),
            "approved_risk_ids": review.get("approved_risk_ids", []),
        }
        self.state.current_stage = "report_sections_running"
        self.state.pipeline_status = "running"
        orchestrator = SectionReportOrchestrator(
            gateway=self.gateway,
            project_root=self._project_root,
            run_id=self.state.run_id,
            report_context=report_context,
            input_base=report_input,
            event_callback=lambda event_type, section_id, message: self._event(
                event_type,
                "report_writer",
                message,
                target_agent_ids=["system"],
                task_id=f"TASK-REPORT-SECTION-{section_id.upper().replace('_', '-')}",
            ),
        )
        try:
            outcome = await orchestrator.run()
        except (OSError, ValueError) as exc:
            return self._write_fallback_report(
                f"section report generation could not start: {type(exc).__name__}: {exc}",
                trigger_stage="report_sections_running",
            )

        for section_id, record in outcome.section_records.items():
            self.state.section_statuses[section_id] = record.execution_status
            self.state.section_retry_counts[section_id] = max(0, record.attempts - 1)
            self.state.section_artifact_ids[section_id] = record.artifact_id
            self.state.section_token_usage[section_id] = record.usage.total_tokens
            self.state.section_durations[section_id] = record.duration_seconds
        self.state.completed_section_ids = [
            key for key, value in self.state.section_statuses.items() if value == "completed"
        ]
        self.state.partial_section_ids = [
            key for key, value in self.state.section_statuses.items() if value == "partial"
        ]
        self.state.failed_section_ids = [
            key for key, value in self.state.section_statuses.items() if value == "failed"
        ]
        self.state.total_retry_count += sum(self.state.section_retry_counts.values())
        if outcome.all_model_sections_failed:
            return self._write_fallback_report(
                "all six model-written report sections failed",
                trigger_stage="report_sections_running",
            )

        self.state.current_stage = "report_assembling"
        try:
            persisted = store.persist_agent_output(
                run_id=self.state.run_id,
                task_id=task_id,
                agent_id="report_writer",
                payload=outcome.report_payload,
            )
        except (OSError, ValueError) as exc:
            return self._write_fallback_report(
                f"assembled report could not be persisted: {type(exc).__name__}: {exc}",
                trigger_stage="report_assembling",
            )
        result = AgentExecutionResult(
            status="succeeded",
            agent_id="report_writer",
            runtime_agent_name="investment-research:report_writer",
            model="deepseek-v4-flash",
            structured_output=outcome.report_payload,
            raw_output=json.dumps(outcome.report_payload, ensure_ascii=False),
            usage=outcome.total_usage,
            artifact_id=persisted.get("artifact_id"),
            report_id=persisted.get("report_id"),
            degraded=bool(self.state.partial_section_ids or self.state.failed_section_ids),
            warnings=[
                f"Report section {section_id} was delivered as {status}."
                for section_id, status in self.state.section_statuses.items()
                if status != "completed"
            ],
        )
        self._store_result(result, started_at=datetime.now(UTC), completed_at=datetime.now(UTC))
        self.state.report_result = outcome.report_payload
        self.state.report_path = self._write_report_markdown(result)
        self.state.report_generated = True
        self.state.formal_report_succeeded = True
        self.state.fallback_used = False
        degraded = any(
            item.execution_status != "succeeded"
            for agent_id, item in self.state.agent_results.items()
            if agent_id != "report_writer"
        ) or delivery.delivery_mode == "provisional" or self.state.review_status in {
            "approve_with_warnings",
            "request_supplement",
            "require_human_resolution",
        }
        degraded = degraded or bool(
            self.state.partial_section_ids or self.state.failed_section_ids
        )
        self.state.report_quality = "partial" if degraded else "complete"
        self.state.pipeline_status = "completed_with_warnings" if degraded else "completed"
        self._event(
            "report_generated",
            "report_writer",
            build_agent_delivery_message(result)
            + f" 已完成{len(self.state.completed_section_ids)}个正式章节、"
            f"{len(self.state.partial_section_ids)}个带警告章节，Markdown 报告已组装。",
            target_agent_ids=["system"],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return self.summary()

    def _write_fallback_report(
        self,
        reason: str,
        *,
        trigger_stage: str | None = None,
    ) -> dict[str, Any]:
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
        self.state.formal_report_succeeded = False
        self.state.delivery_mode = "fallback"
        self.state.fallback_trigger_stage = trigger_stage or self.state.current_stage
        self.state.current_stage = "completed"
        self.state.pipeline_status = "completed_with_warnings"
        self.state.warnings.append(reason)
        self._event("fallback_report_generated", "system", "已使用本地兜底程序生成报告。", target_agent_ids=["system"])
        return self.summary()

    def _apply_review_statuses(self, review: dict[str, Any]) -> None:
        """Promote only explicitly review-approved records for ReportWriter."""

        store = self.gateway.evidence_store
        if (
            store is None
            or review.get("audit_status") == "fail"
            or review.get("decision") not in {
            "approve_for_report",
            "approve_with_warnings",
            }
        ):
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
                delivery_decision=self.state.delivery_decision,
                failed_agents=[
                    agent_id
                    for agent_id, item in self.state.agent_results.items()
                    if item.execution_status != "succeeded"
                ],
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

    def _run_metrics(self) -> dict[str, Any]:
        agent_usage = {
            agent_id: item.total_tokens
            for agent_id, item in self.state.agent_results.items()
        }
        total_tokens = sum(agent_usage.values())
        tool_invocations = sum(
            item.tool_invocation_count for item in self.state.agent_results.values()
        )
        external_calls = sum(
            item.external_tool_call_count for item in self.state.agent_results.values()
        )
        cache_hits = sum(
            item.cache_hit_count for item in self.state.agent_results.values()
        )
        cacheable_calls = external_calls + cache_hits
        source_references = [
            source_id
            for item in self.state.agent_results.values()
            for source_id in item.source_ids
        ]
        reused_ids = {
            source_id
            for source_id in source_references
            if source_references.count(source_id) > 1
        }
        reused_references = sum(
            1 for source_id in source_references if source_id in reused_ids
        )
        store = self.gateway.evidence_store
        source_grade_counts = (
            store.source_grade_counts(self.state.run_id)
            if store is not None and hasattr(store, "source_grade_counts")
            else {grade: 0 for grade in ("A", "B", "C", "D")}
        )
        return {
            "total_tokens": total_tokens,
            "agent_token_usage": agent_usage,
            "tool_invocation_count": tool_invocations,
            "external_tool_call_count": external_calls,
            "cache_hit_count": cache_hits,
            "cache_hit_rate": round(cache_hits / cacheable_calls, 4)
            if cacheable_calls
            else 0.0,
            "estimated_saved_external_calls": cache_hits,
            "source_grade_counts": source_grade_counts,
            "source_reuse_ratio": round(reused_references / len(source_references), 4)
            if source_references
            else 0.0,
            "budget_exhausted_count": sum(
                item.budget_exhausted_count
                for item in self.state.agent_results.values()
            ),
        }

    def _metric_warnings(self, metrics: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        if int(metrics["total_tokens"]) > self.budget_policy.token_warning_threshold:
            warnings.append(
                "Full-chain token usage exceeded the balanced budget warning threshold "
                f"({metrics['total_tokens']}/{self.budget_policy.token_warning_threshold})."
            )
        grades = metrics["source_grade_counts"]
        if int(grades.get("A", 0)) + int(grades.get("B", 0)) == 0 and sum(
            int(value) for value in grades.values()
        ):
            warnings.append(
                "No A/B-grade source was registered; delivery may continue with traceable "
                "C-grade evidence and an explicit quality warning."
            )
        if int(metrics["budget_exhausted_count"]):
            warnings.append(
                f"Tool budgets were reached {metrics['budget_exhausted_count']} time(s); "
                "affected Agents should return partial results instead of blocking delivery."
            )
        cacheable_calls = int(metrics["external_tool_call_count"]) + int(
            metrics["cache_hit_count"]
        )
        if cacheable_calls >= 4 and float(metrics["cache_hit_rate"]) < 0.1:
            warnings.append(
                "Run-scoped search/fetch cache hit rate was below 10%; review whether "
                "Agents are expressing equivalent research requests with inconsistent queries."
            )
        return warnings

    def summary(self) -> dict[str, Any]:
        """Return a JSON-safe receipt proving orchestration and runtime boundaries."""

        metrics = self._run_metrics()
        runtime_warnings = list(
            dict.fromkeys([*self.state.warnings, *self._metric_warnings(metrics)])
        )
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
            "initial_evidence_audit": self.state.initial_evidence_audit,
            "final_evidence_audit": self.state.final_evidence_audit,
            "fallback_used": self.state.fallback_used,
            "formal_report_succeeded": self.state.formal_report_succeeded,
            "delivery_mode": self.state.delivery_mode,
            "delivery_decision": self.state.delivery_decision,
            "recovery_used": self.state.recovery_used,
            "recovery_reason": self.state.recovery_reason,
            "fallback_trigger_stage": self.state.fallback_trigger_stage,
            "recovery_actions": self.state.recovery_actions,
            "section_statuses": self.state.section_statuses,
            "completed_section_ids": self.state.completed_section_ids,
            "partial_section_ids": self.state.partial_section_ids,
            "failed_section_ids": self.state.failed_section_ids,
            "section_retry_counts": self.state.section_retry_counts,
            "section_artifact_ids": self.state.section_artifact_ids,
            "section_token_usage": self.state.section_token_usage,
            "section_durations": self.state.section_durations,
            "total_retry_count": self.state.total_retry_count,
            "warnings": runtime_warnings,
            **metrics,
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
                        "initial_evidence_audit": self.state.initial_evidence_audit,
                        "final_evidence_audit": self.state.final_evidence_audit,
                        "report_quality": self.state.report_quality,
                        "review_status": self.state.review_status,
                        "fallback_used": self.state.fallback_used,
                        "formal_report_succeeded": self.state.formal_report_succeeded,
                        "delivery_mode": self.state.delivery_mode,
                        "delivery_decision": self.state.delivery_decision,
                        "recovery_used": self.state.recovery_used,
                        "recovery_reason": self.state.recovery_reason,
                        "fallback_trigger_stage": self.state.fallback_trigger_stage,
                        "recovery_actions": self.state.recovery_actions,
                        "section_statuses": self.state.section_statuses,
                        "completed_section_ids": self.state.completed_section_ids,
                        "partial_section_ids": self.state.partial_section_ids,
                        "failed_section_ids": self.state.failed_section_ids,
                        "section_retry_counts": self.state.section_retry_counts,
                        "section_artifact_ids": self.state.section_artifact_ids,
                        "section_token_usage": self.state.section_token_usage,
                        "section_durations": self.state.section_durations,
                        "artifact_history": self.state.artifact_history,
                        "agent_results": payload.get("agent_results", {}),
                        "events": payload.get("events", []),
                        "total_retry_count": self.state.total_retry_count,
                        "warnings": payload.get("warnings", []),
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
