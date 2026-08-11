"""Minimal CrewAI Flow proving OpenHarness Agent-to-Agent handoff."""

from __future__ import annotations

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
from openharness.invest_research.runtime_adapter import AgentExecutionResult


class InvestmentResearchSmokeFlow(Flow[ResearchFlowState]):
    """CrewAI controls order; OpenHarness executes Planner and Fundamental."""

    def __init__(self, gateway: OpenHarnessRuntimeGateway | None = None) -> None:
        super().__init__()
        self.gateway = gateway or OpenHarnessRuntimeGateway()

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

    def _store_result(self, result: AgentExecutionResult) -> None:
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
            error=result.error,
        )

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
                "参数卡已生成，现将公司身份、研究周期和规划结果交给 Fundamental。",
                target_agent_ids=["fundamental"],
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

    @listen(run_planner)
    async def run_fundamental(self, planner_result: AgentExecutionResult) -> dict[str, Any]:
        if planner_result.status != "succeeded" or not planner_result.parameter_card_id:
            return self.summary()

        self.state.current_stage = "fundamental_running"
        task_id = "TASK-CREWAI-FUNDAMENTAL-001"
        self._event(
            "task_handoff",
            "planner",
            "@Fundamental 请根据已确认参数卡研究最近一年的经营变化和候选投资逻辑。",
            target_agent_ids=["fundamental"],
            task_id=task_id,
            artifact_id=planner_result.artifact_id,
        )
        planner_output = planner_result.structured_output or {}
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
        result = await self.gateway.execute(
            agent_id="fundamental",
            input_payload={
                "protocol_version": "1.0",
                "run_id": self.state.run_id,
                "task_id": task_id,
                "objective": "分析目标公司最近一年的经营和财务变化，形成可追溯事实与候选逻辑。",
                "agent_id": "fundamental",
                "parameter_card": {
                    "parameter_card_id": planner_result.parameter_card_id,
                    "version": "1.0",
                },
                "source_ids": planner_result.source_ids,
            },
            task_prompt=(
                "严格执行 Fundamental 职责。读取 Planner 参数卡，搜索并核验最近一年公开财务和经营资料，"
                "最多进行两次搜索、两次网页读取和一次计算；完成基本事实后立即输出 FundamentalResult JSON。"
                "事实必须有来源，不承担行业全景或最终投资建议。资料不足时返回 partial，不要继续无休止搜索。"
            ),
            context_package={
                "planner_artifact_id": planner_result.artifact_id,
                "planner_context": planner_context,
            },
            max_turns=6,
            tool_call_limits={
                "tavily_search": 2,
                "web_fetch": 2,
                "read_uploaded_file": 0,
                "calculator": 1,
                "evidence_query": 1,
            },
        )
        self._store_result(result)
        self.state.current_stage = "completed" if result.status == "succeeded" else "failed"
        self.state.pipeline_status = (
            "completed" if result.status == "succeeded" else "fundamental_failed"
        )
        self._event(
            "artifact_delivered" if result.status == "succeeded" else "task_failed",
            "fundamental",
            (
                "基本面研究成果已提交，Planner 参数卡到 Fundamental 成果的传递已完成。"
                if result.status == "succeeded"
                else result.error or "Fundamental 执行失败。"
            ),
            target_agent_ids=["planner"],
            task_id=task_id,
            artifact_id=result.artifact_id,
        )
        return self.summary()

    def summary(self) -> dict[str, Any]:
        """Return a JSON-safe receipt proving orchestration and runtime boundaries."""

        return {
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
            "agent_results": {
                key: value.model_dump(mode="json")
                for key, value in self.state.agent_results.items()
            },
            "events": [item.model_dump(mode="json") for item in self.state.events],
        }


__all__ = ["InvestmentResearchSmokeFlow"]
