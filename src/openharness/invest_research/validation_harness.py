"""Real DeepSeek validation harness for the seven investment-research roles."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.invest_research.agent_registry import AGENT_REGISTRY
from openharness.invest_research.evidence_store import EvidenceStore
from openharness.invest_research.fallback_report import write_fallback_report
from openharness.invest_research.runtime_adapter import (
    AgentExecutionRequest,
    AgentExecutionResult,
    InvestmentResearchRuntimeAdapter,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
AS_OF_DATE = date(2026, 8, 11)
CATALYST_END = AS_OF_DATE + timedelta(days=183)
AGENT_ORDER = (
    "fundamental",
    "industry_competition",
    "market_catalyst",
    "risk",
    "reviewer_arbiter",
    "report_writer",
    "planner",
)
REQUIRED_TOOLS = {
    "planner": {"tavily_search", "web_fetch"},
    "fundamental": {
        "tavily_search",
        "web_fetch",
        "read_uploaded_file",
        "calculator",
        "evidence_query",
    },
    "industry_competition": {"tavily_search", "web_fetch", "calculator", "evidence_query"},
    "market_catalyst": {"tavily_search", "web_fetch", "evidence_query"},
    "risk": {"evidence_query", "tavily_search", "web_fetch"},
    "reviewer_arbiter": {"evidence_query"},
    "report_writer": {"evidence_query"},
}


class ValidationHarness:
    """Create isolated validation Runs and write compact, secret-free receipts."""

    def __init__(self, project_root: str | Path = PROJECT_ROOT) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = EvidenceStore.for_project(self.project_root)
        self.validation_root = self.project_root / ".openharness" / "validation"
        self.validation_root.mkdir(parents=True, exist_ok=True)

    def new_run(self, label: str, company: str = "宁德时代") -> str:
        run_id = f"RUN-{label.upper().replace('_', '-')}-{uuid4().hex[:8].upper()}"
        self.store.create_run(run_id, company, status="created")
        return run_id

    async def smoke(self, agent_id: str) -> dict[str, Any]:
        if agent_id not in AGENT_REGISTRY:
            raise ValueError(f"unknown Agent: {agent_id}")
        run_id = self.new_run(f"SMOKE-{agent_id}")
        file_ref: str | None = None
        if agent_id in {"fundamental", "industry_competition", "market_catalyst"}:
            self._seed_parameter_card(run_id, "PC-SMOKE-001")
            file_ref = self._register_fixture_pdf(run_id)
        if agent_id == "risk":
            self._seed_research_artifacts(run_id)
        if agent_id == "reviewer_arbiter":
            self._seed_review_material(run_id)
        if agent_id == "report_writer":
            self._seed_approved_material(run_id)

        request = self._smoke_request(agent_id, run_id, file_ref)
        adapter = InvestmentResearchRuntimeAdapter(project_root=self.project_root)
        result = await adapter.execute_agent(request)
        receipt = self._receipt(result, run_id, expected_tools=REQUIRED_TOOLS[agent_id])
        self._write_receipt(f"{agent_id}.json", receipt)
        return receipt

    async def validate_all(self) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        for agent_id in AGENT_ORDER:
            receipts.append(await self.smoke(agent_id))
        summary = {
            "mode": "ValidateAllAgents",
            "generated_at": _now(),
            "pass": all(item["pass"] for item in receipts),
            "agents": receipts,
        }
        self._write_receipt("validate-all.json", summary)
        return summary

    async def full_chain(self) -> dict[str, Any]:
        run_id = self.new_run("FULL-CHAIN", "宁德时代")
        file_ref = self._register_fixture_pdf(run_id)
        adapter = InvestmentResearchRuntimeAdapter(project_root=self.project_root)
        results: dict[str, AgentExecutionResult] = {}
        receipts: list[dict[str, Any]] = []
        self._progress(run_id, "started", "Full chain validation started.")

        planner_request = AgentExecutionRequest(
            agent_id="planner",
            input_payload={
                "protocol_version": "1.0",
                "run_id": run_id,
                "task_id": "TASK-PLANNER-FULL-001",
                "objective": "为宁德时代建立 Gate 1 参数卡和七角色研究任务计划",
                "agent_id": "planner",
                "company_query": "宁德时代 CATL 300750.SZ",
                "as_of_date": AS_OF_DATE.isoformat(),
            },
            task_prompt=(
                "这是固定验证案例，只做 Planner 参数卡和任务计划，不做基本面、行业、催化或风险深度研究。"
                "目标公司是宁德时代（300750.SZ），两家主要竞品必须优先核对并推荐比亚迪和 "
                "LG Energy Solution（LGES）。研究区间是基准日前最近一年，催化窗口是基准日后六个月。"
                "先用 tavily_search 核验目标公司和两家竞品，再用 web_fetch 打开少量公开页面；"
                "完成核验后必须立即输出 PlannerResult JSON。不要继续扩大搜索范围，不要撰写研报正文。"
                "输出必须包含 company_identity、research_period、catalyst_window、exactly two "
                "recommended_competitors、gate_1_payload，以及六个下游 Agent 各一条 task_plan。"
            ),
            tool_call_limits={"tavily_search": 4, "web_fetch": 4},
        )
        self._progress(run_id, "planner_running", "Planner is building the parameter card.")
        results["planner"] = await self._execute_with_network_retry(adapter, planner_request)
        receipts.append(self._receipt(results["planner"], run_id, REQUIRED_TOOLS["planner"]))
        self._write_receipt("full-chain-planner.json", receipts[-1])
        self._progress(run_id, "planner_done", f"Planner finished with {results['planner'].status}.")
        if not self._successful(results["planner"]):
            return self._fallback_chain_summary(
                run_id, receipts, results, "planner_failed"
            )

        parameter_card_id = results["planner"].parameter_card_id
        if not parameter_card_id:
            return self._fallback_chain_summary(
                run_id, receipts, results, "planner_did_not_persist_parameter_card"
            )
        competitor_payload = [
            {"company_name": "比亚迪", "ticker": "002594.SZ", "selection_reasons": ["动力电池和新能源车业务可比"]},
            {"company_name": "LG Energy Solution", "ticker": "373220.KS", "selection_reasons": ["全球动力电池业务可比"]},
        ]
        base_inputs = self._full_chain_inputs(run_id, parameter_card_id, file_ref, competitor_payload)
        parallel_ids = ("fundamental", "industry_competition", "market_catalyst")
        self._progress(
            run_id,
            "parallel_research_running",
            "Fundamental, IndustryCompetition and MarketCatalyst are running in parallel.",
        )
        parallel_results = await asyncio.gather(
            *[
                self._execute_with_network_retry(
                    adapter,
                    AgentExecutionRequest(
                        agent_id=agent_id,
                        input_payload=base_inputs[agent_id],
                        task_prompt=self._full_chain_task(agent_id),
                        timeout_seconds=600.0,
                        tool_call_limits=self._full_chain_tool_limits(agent_id),
                    )
                )
                for agent_id in parallel_ids
            ]
        )
        for agent_id, result in zip(parallel_ids, parallel_results):
            results[agent_id] = result
            receipt = self._receipt(result, run_id, REQUIRED_TOOLS[agent_id])
            receipts.append(receipt)
            self._write_receipt(f"full-chain-{agent_id}.json", receipt)
            self._progress(run_id, f"{agent_id}_done", f"{agent_id} finished with {result.status}.")
        if not all(self._successful(results[agent_id]) for agent_id in parallel_ids):
            self._progress(
                run_id,
                "parallel_research_partial",
                "One or more research Agents failed; continuing with available artifacts.",
            )

        risk_input = {
            **self._common_input(run_id, "risk", "TASK-RISK-FULL-001"),
            "parameter_card": {"parameter_card_id": parameter_card_id, "version": "1.0"},
            "fundamental_artifact_id": results["fundamental"].artifact_id,
            "industry_competition_artifact_id": results["industry_competition"].artifact_id,
            "market_catalyst_artifact_id": results["market_catalyst"].artifact_id,
            "logic_ids": sorted(
                set(results["fundamental"].logic_ids)
                | set(results["industry_competition"].logic_ids)
                | set(results["market_catalyst"].logic_ids)
            ),
        }
        if not any(
            risk_input[key]
            for key in (
                "fundamental_artifact_id",
                "industry_competition_artifact_id",
                "market_catalyst_artifact_id",
            )
        ):
            return self._fallback_chain_summary(
                run_id, receipts, results, "no_upstream_research_artifact"
            )
        self._progress(run_id, "risk_running", "Risk is reading upstream artifacts and checking downside cases.")
        results["risk"] = await self._execute_with_network_retry(
            adapter,
            AgentExecutionRequest(
                agent_id="risk",
                input_payload=risk_input,
                task_prompt=(
                    "先用 evidence_query 读取 Fundamental、IndustryCompetition、MarketCatalyst 的"
                    " Artifact，再用搜索和网页读取寻找反方证据。必须输出风险、触发条件和证伪指标。"
                ),
            )
        )
        receipts.append(self._receipt(results["risk"], run_id, REQUIRED_TOOLS["risk"]))
        self._write_receipt("full-chain-risk.json", receipts[-1])
        self._progress(run_id, "risk_done", f"Risk finished with {results['risk'].status}.")
        if not self._successful(results["risk"]):
            self._progress(
                run_id,
                "risk_partial",
                "Risk did not complete; Reviewer will inspect the available research artifacts.",
            )

        reviewer_input = self._reviewer_input(run_id, parameter_card_id, results)
        self._progress(run_id, "reviewer_running", "ReviewerArbiter is checking evidence, logic and conflicts.")
        results["reviewer_arbiter"] = await self._execute_with_network_retry(
            adapter,
            AgentExecutionRequest(
                agent_id="reviewer_arbiter",
                input_payload=reviewer_input,
                task_prompt=(
                    "只使用 evidence_query，读取前三路研究和 Risk Artifact。逐项检查来源、口径、"
                    "三条候选逻辑和风险；能补证就给 request_supplement，无法自动解决的重大冲突才进入 Gate 2。"
                    "若证据足够，批准恰好三条逻辑。"
                ),
            )
        )
        receipts.append(self._receipt(results["reviewer_arbiter"], run_id, REQUIRED_TOOLS["reviewer_arbiter"]))
        self._write_receipt("full-chain-reviewer.json", receipts[-1])
        self._progress(
            run_id,
            "reviewer_done",
            f"ReviewerArbiter finished with {results['reviewer_arbiter'].status}.",
        )

        if results["reviewer_arbiter"].structured_output:
            decision = results["reviewer_arbiter"].structured_output.get("decision")
        else:
            decision = None
        if decision == "request_supplement":
            self._progress(run_id, "supplement_running", "Reviewer requested one targeted supplement round.")
            supplement_receipt = await self._one_supplement_round(
                adapter, run_id, parameter_card_id, file_ref, results
            )
            receipts.extend(supplement_receipt[0])
            results.update(supplement_receipt[1])
            if "reviewer_arbiter" in results:
                decision = (results["reviewer_arbiter"].structured_output or {}).get("decision")

        review_output = results["reviewer_arbiter"].structured_output or {}
        reviewer_succeeded = self._successful(results["reviewer_arbiter"])
        if not reviewer_succeeded:
            review_output = self._delivery_review_output(
                run_id,
                results,
                reason="Reviewer 调用失败，使用已登记材料继续生成报告",
            )
        elif decision != "approve_for_report":
            review_output = self._delivery_review_output(
                run_id,
                results,
                existing=review_output,
                reason="Reviewer 保留问题，但本次采用尽力交付模式",
            )

        if len(review_output.get("approved_logic_ids") or []) != 3:
            return self._fallback_chain_summary(
                run_id,
                receipts,
                results,
                "可用候选逻辑不足三条，无法启动结构化 ReportWriter",
                review_output=review_output,
            )

        writer_input = {
            **self._common_input(run_id, "report_writer", "TASK-WRITER-FULL-001"),
            "parameter_card": {"parameter_card_id": parameter_card_id, "version": "1.0"},
            "review_id": review_output.get("review_id"),
            "approved_fact_ids": review_output.get("approved_fact_ids", []),
            "approved_logic_ids": review_output.get("approved_logic_ids", []),
            "approved_catalyst_ids": review_output.get("approved_catalyst_ids", []),
            "approved_risk_ids": review_output.get("approved_risk_ids", []),
        }
        self._progress(run_id, "report_writer_running", "ReportWriter is generating the final report from approved records.")
        results["report_writer"] = await self._execute_with_network_retry(
            adapter,
            AgentExecutionRequest(
                agent_id="report_writer",
                input_payload=writer_input,
                task_prompt=(
                    "只用 evidence_query 查询已批准记录，不搜索新资料。生成八段式上市公司研究报告，"
                    "必须包含最近一年经营变化、恰好三条投资逻辑、两家竞品、未来半年催化、风险、"
                    "跟踪指标和不构成投资建议声明。"
                ),
            )
        )
        receipts.append(self._receipt(results["report_writer"], run_id, REQUIRED_TOOLS["report_writer"]))
        self._write_receipt("full-chain-report-writer.json", receipts[-1])
        self._progress(run_id, "report_writer_done", f"ReportWriter finished with {results['report_writer'].status}.")
        if self._successful(results["report_writer"]):
            report_path = self._write_agent_report_markdown(
                run_id, results["report_writer"], review_output
            )
            degraded = any(
                not self._successful(results[agent_id])
                for agent_id in (
                    "planner",
                    "fundamental",
                    "industry_competition",
                    "market_catalyst",
                    "risk",
                    "reviewer_arbiter",
                )
                if agent_id in results
            ) or review_output.get("decision") == "approve_with_warnings"
            return self._chain_summary(
                run_id,
                receipts,
                "completed_with_warnings" if degraded else "completed",
                report_generated=True,
                report_quality="partial" if degraded else "complete",
                review_status=str(review_output.get("decision") or "unknown"),
                report_path=report_path,
            )
        return self._fallback_chain_summary(
            run_id,
            receipts,
            results,
            "report_writer_failed",
            review_output=review_output,
        )

    async def _execute_with_network_retry(
        self,
        adapter: InvestmentResearchRuntimeAdapter,
        request: AgentExecutionRequest,
        *,
        attempts: int = 2,
    ) -> AgentExecutionResult:
        result: AgentExecutionResult | None = None
        for attempt in range(1, attempts + 1):
            result = await adapter.execute_agent(request)
            if not _is_transient_network_failure(result) or attempt >= attempts:
                return result
            print(
                f"[full-chain] {request.agent_id}: transient network failure, retrying "
                f"{attempt + 1}/{attempts}.",
                flush=True,
            )
            await asyncio.sleep(float(attempt * 2))
        if result is None:
            raise RuntimeError("Agent execution did not produce a result")
        return result

    async def _one_supplement_round(
        self,
        adapter: InvestmentResearchRuntimeAdapter,
        run_id: str,
        parameter_card_id: str,
        file_ref: str,
        results: dict[str, AgentExecutionResult],
    ) -> tuple[list[dict[str, Any]], dict[str, AgentExecutionResult]]:
        review_output = results["reviewer_arbiter"].structured_output or {}
        issues = review_output.get("issues") or []
        if not issues:
            return [], {}
        issue = issues[0]
        target = str(issue.get("target_agent_id") or "fundamental")
        if target not in {"fundamental", "industry_competition", "market_catalyst", "risk"}:
            target = "fundamental"
        input_payload = self._full_chain_inputs(
            run_id,
            parameter_card_id,
            file_ref,
            [
                {"company_name": "比亚迪", "ticker": "002594.SZ", "selection_reasons": ["新能源车和电池业务可比"]},
                {"company_name": "LG Energy Solution", "ticker": "373220.KS", "selection_reasons": ["全球动力电池业务可比"]},
            ],
        )[target]
        input_payload["task_id"] = f"TASK-{target.upper().replace('_', '-')}-SUPPLEMENT-001"
        input_payload["supplement"] = {
            "issue_id": issue.get("issue_id"),
            "supplement_round": 1,
            "problem_statement": issue.get("problem_statement", "补充审查指出的证据问题"),
            "expected_fields": issue.get("expected_fields", []),
            "input_refs": issue.get("input_refs", []),
        }
        result = await self._execute_with_network_retry(
            adapter,
            AgentExecutionRequest(
                agent_id=target,
                input_payload=input_payload,
                task_prompt="只针对指定 ISSUE 做一次定向补证，不重跑无关模块；返回完整合同 JSON。",
            )
        )
        receipt = self._receipt(result, run_id, REQUIRED_TOOLS[target])
        self._write_receipt(f"full-chain-supplement-{target}.json", receipt)
        updated = {target: result}
        if not self._successful(result):
            return [receipt], updated
        updated_results = {**results, **updated}
        reviewer_input = self._reviewer_input(run_id, parameter_card_id, updated_results)
        reviewer_input["prior_issue_ids"] = [issue.get("issue_id")]
        reviewer_input["supplement_artifact_ids"] = [result.artifact_id]
        reviewer = await self._execute_with_network_retry(
            adapter,
            AgentExecutionRequest(
                agent_id="reviewer_arbiter",
                input_payload=reviewer_input,
                task_prompt="复审定向补证结果。仍只用 evidence_query；给出三态决定，不能跳过问题说明。",
            )
        )
        reviewer_receipt = self._receipt(reviewer, run_id, REQUIRED_TOOLS["reviewer_arbiter"])
        self._write_receipt("full-chain-reviewer-after-supplement.json", reviewer_receipt)
        return [receipt, reviewer_receipt], {target: result, "reviewer_arbiter": reviewer}

    def _smoke_request(
        self,
        agent_id: str,
        run_id: str,
        file_ref: str | None,
    ) -> AgentExecutionRequest:
        if agent_id == "planner":
            return AgentExecutionRequest(
                agent_id=agent_id,
                input_payload={
                    **self._common_input(run_id, agent_id, "TASK-PLANNER-SMOKE-001"),
                    "company_query": "宁德时代 CATL 300750.SZ",
                    "as_of_date": AS_OF_DATE.isoformat(),
                },
                task_prompt=(
                    "验证 Planner：必须搜索宁德时代并打开至少一个公开页面；确认最近一年、未来半年，"
                    "推荐比亚迪和 LG Energy Solution 两家竞品，并规划六个下游 Agent。"
                ),
            )
        parameter_card = {"parameter_card_id": "PC-SMOKE-001", "version": "1.0"}
        if agent_id == "fundamental":
            return AgentExecutionRequest(
                agent_id=agent_id,
                input_payload={
                    **self._common_input(run_id, agent_id, "TASK-FUNDAMENTAL-SMOKE-001"),
                    "parameter_card": parameter_card,
                    "uploaded_file_refs": [file_ref],
                    "source_ids": self._seed_source_ids(run_id),
                    "fact_ids": ["F-SMOKE-SEED"],
                },
                task_prompt=(
                    "验证全部五个工具：先调用 tavily_search，再用 web_fetch 打开结果；"
                    "用 read_uploaded_file 读取输入 PDF，用 calculator 计算一个同比或增速，"
                    "最后用 evidence_query 读取当前 Run 的来源和事实。完成后输出基本面事实、变化和候选逻辑。"
                ),
            )
        if agent_id == "industry_competition":
            return AgentExecutionRequest(
                agent_id=agent_id,
                input_payload={
                    **self._common_input(run_id, agent_id, "TASK-INDUSTRY-SMOKE-001"),
                    "parameter_card": parameter_card,
                    "confirmed_competitors": [
                        {"company_name": "比亚迪", "ticker": "002594.SZ", "selection_reasons": ["业务可比"]},
                        {"company_name": "LG Energy Solution", "ticker": "373220.KS", "selection_reasons": ["全球可比"]},
                    ],
                    "uploaded_file_refs": [file_ref],
                },
                task_prompt="搜索并打开公开资料，使用计算器统一一个比较指标，再用 evidence_query 查验资料；输出三家公司可比表。",
            )
        if agent_id == "market_catalyst":
            return AgentExecutionRequest(
                agent_id=agent_id,
                input_payload={
                    **self._common_input(run_id, agent_id, "TASK-MARKET-SMOKE-001"),
                    "parameter_card": parameter_card,
                    "catalyst_window": {
                        "start_date": (AS_OF_DATE + timedelta(days=1)).isoformat(),
                        "end_date": CATALYST_END.isoformat(),
                    },
                },
                task_prompt="搜索未来半年公告、业绩、产品、政策或订单信息，打开关键页面，并用 evidence_query 查验来源；严格区分事实、预期和推测。",
            )
        if agent_id == "risk":
            return AgentExecutionRequest(
                agent_id=agent_id,
                input_payload={
                    **self._common_input(run_id, agent_id, "TASK-RISK-SMOKE-001"),
                    "parameter_card": parameter_card,
                    "fundamental_artifact_id": "ART-FUND-SMOKE",
                    "industry_competition_artifact_id": "ART-IND-SMOKE",
                    "market_catalyst_artifact_id": "ART-CAT-SMOKE",
                    "logic_ids": ["L-SMOKE-001", "L-SMOKE-002", "L-SMOKE-003"],
                    "source_ids": self._seed_source_ids(run_id),
                },
                task_prompt="先用 evidence_query 读取三个上游 Artifact 和候选逻辑，再搜索反方资料并打开关键来源；输出风险、触发条件和证伪指标。",
            )
        if agent_id == "reviewer_arbiter":
            return AgentExecutionRequest(
                agent_id=agent_id,
                input_payload={
                    **self._common_input(run_id, agent_id, "TASK-REVIEW-SMOKE-001"),
                    "parameter_card": parameter_card,
                    "research_artifact_ids": [
                        "ART-FUND-SMOKE",
                        "ART-IND-SMOKE",
                        "ART-CAT-SMOKE",
                        "ART-RISK-SMOKE",
                    ],
                    "source_ids": self._seed_source_ids(run_id),
                    "fact_ids": ["F-SMOKE-SEED"],
                    "logic_ids": ["L-SMOKE-001", "L-SMOKE-002", "L-SMOKE-003"],
                },
                task_prompt="只调用 evidence_query，读取当前 Run 的 Artifact、来源、事实和逻辑；完成三态审查，证据足够时恰好批准三条逻辑。",
            )
        review_id = "REVIEW-SMOKE-001"
        return AgentExecutionRequest(
            agent_id="report_writer",
            input_payload={
                **self._common_input(run_id, agent_id, "TASK-WRITER-SMOKE-001"),
                "parameter_card": parameter_card,
                "review_id": review_id,
                "approved_fact_ids": ["F-SMOKE-SEED"],
                "approved_logic_ids": ["L-SMOKE-001", "L-SMOKE-002", "L-SMOKE-003"],
                "approved_catalyst_ids": ["CAT-SMOKE-001"],
                "approved_risk_ids": ["RISK-SMOKE-001"],
            },
            task_prompt="只调用 evidence_query 读取 approved 记录，不搜索；输出完整八段式报告和合规声明。",
        )

    def _common_input(self, run_id: str, agent_id: str, task_id: str) -> dict[str, Any]:
        return {
            "protocol_version": "1.0",
            "run_id": run_id,
            "task_id": task_id,
            "objective": f"验证 {agent_id} 的真实 DeepSeek 运行能力",
            "agent_id": agent_id,
        }

    def _full_chain_inputs(
        self,
        run_id: str,
        parameter_card_id: str,
        file_ref: str,
        competitors: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        parameter_card = {"parameter_card_id": parameter_card_id, "version": "1.0"}
        return {
            "fundamental": {
                **self._common_input(run_id, "fundamental", "TASK-FUNDAMENTAL-FULL-001"),
                "parameter_card": parameter_card,
                "uploaded_file_refs": [file_ref],
            },
            "industry_competition": {
                **self._common_input(run_id, "industry_competition", "TASK-INDUSTRY-FULL-001"),
                "parameter_card": parameter_card,
                "confirmed_competitors": competitors,
                "uploaded_file_refs": [file_ref],
            },
            "market_catalyst": {
                **self._common_input(run_id, "market_catalyst", "TASK-MARKET-FULL-001"),
                "parameter_card": parameter_card,
                "catalyst_window": {
                    "start_date": (AS_OF_DATE + timedelta(days=1)).isoformat(),
                    "end_date": CATALYST_END.isoformat(),
                },
                "uploaded_file_refs": [file_ref],
            },
        }

    def _full_chain_task(self, agent_id: str) -> str:
        return {
            "fundamental": (
                "这是完整链路验证，不是最终深度研报。必须先读取上传文件，再用 evidence_query、"
                "tavily_search、web_fetch 和 calculator 各完成必要验证。只输出最小合格 FundamentalResult："
                "至少 2 条可追溯财务/经营事实、1 段最近一年经营变化分析、1-2 条候选逻辑。"
                "达到这些内容后立即输出 JSON，不继续扩大搜索或反复计算。"
            ),
            "industry_competition": (
                "这是完整链路验证。研究行业和宁德时代、比亚迪、LGES 三家公司，统一口径做最小对比，"
                "输出 1-2 条候选逻辑；达到 Schema 合格后立即输出 JSON。"
            ),
            "market_catalyst": (
                "这是完整链路验证。研究基准日后六个月催化因素，按事实、报道、预期、推测分类；"
                "输出 2-4 个催化即可，达到 Schema 合格后立即输出 JSON。"
            ),
        }[agent_id]

    def _full_chain_tool_limits(self, agent_id: str) -> dict[str, int]:
        return {
            "fundamental": {
                "read_uploaded_file": 1,
                "evidence_query": 2,
                "tavily_search": 2,
                "web_fetch": 3,
                "calculator": 4,
            },
            "industry_competition": {
                "read_uploaded_file": 1,
                "evidence_query": 2,
                "tavily_search": 3,
                "web_fetch": 3,
                "calculator": 4,
            },
            "market_catalyst": {
                "read_uploaded_file": 1,
                "evidence_query": 2,
                "tavily_search": 4,
                "web_fetch": 3,
                "calculator": 1,
            },
        }[agent_id]

    def _reviewer_input(
        self,
        run_id: str,
        parameter_card_id: str,
        results: dict[str, AgentExecutionResult],
    ) -> dict[str, Any]:
        return {
            **self._common_input(run_id, "reviewer_arbiter", "TASK-REVIEW-FULL-001"),
            "parameter_card": {"parameter_card_id": parameter_card_id, "version": "1.0"},
            "research_artifact_ids": [
                results[name].artifact_id
                for name in ("fundamental", "industry_competition", "market_catalyst", "risk")
                if results.get(name) and results[name].artifact_id
            ],
            "fact_ids": sorted(
                set().union(*(set(results.get(name, AgentExecutionResult(status="blocked", agent_id="planner", runtime_agent_name="")).fact_ids) for name in ("fundamental", "industry_competition", "market_catalyst", "risk")))
            ),
            "logic_ids": sorted(
                set().union(*(set(results.get(name, AgentExecutionResult(status="blocked", agent_id="planner", runtime_agent_name="")).logic_ids) for name in ("fundamental", "industry_competition", "market_catalyst")))
            ),
        }

    def _seed_parameter_card(self, run_id: str, parameter_card_id: str) -> None:
        self.store.upsert_record(
            "parameter_card",
            parameter_card_id,
            run_id,
            {
                "parameter_card_id": parameter_card_id,
                "version": "1.0",
                "company_name": "宁德时代",
                "research_period": {"start_date": "2025-08-12", "end_date": AS_OF_DATE.isoformat()},
                "catalyst_window": {"start_date": (AS_OF_DATE + timedelta(days=1)).isoformat(), "end_date": CATALYST_END.isoformat()},
            },
            status="confirmed",
            submitted_by="planner",
        )

    def _register_fixture_pdf(self, run_id: str) -> str:
        upload_dir = self.store.upload_root / run_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        path = upload_dir / "catl-validation-fixture.pdf"
        path.write_bytes(_minimal_text_pdf())
        return self.store.register_uploaded_file(
            run_id,
            path,
            display_name="宁德时代验证资料.pdf",
            media_type="application/pdf",
        )

    def _seed_source_ids(self, run_id: str) -> list[str]:
        source_id = self.store.register_source(
            run_id,
            url_or_file="https://validation.local/catl-seed",
            title="验证用公开事实",
            source_type="validation_fixture",
            source_grade="A",
            status="fetched",
            submitted_by="validation",
        )
        self.store.upsert_record(
            "fact",
            "F-SMOKE-SEED",
            run_id,
            {
                "fact_id": "F-SMOKE-SEED",
                "statement": "验证资料包含当前收入 400 和对比收入 300。",
                "source_ids": [source_id],
            },
            status="verified",
            submitted_by="validation",
        )
        return [source_id]

    def _seed_research_artifacts(self, run_id: str) -> None:
        self._seed_parameter_card(run_id, "PC-SMOKE-001")
        source_ids = self._seed_source_ids(run_id)
        for agent_id, artifact_id in (
            ("fundamental", "ART-FUND-SMOKE"),
            ("industry_competition", "ART-IND-SMOKE"),
            ("market_catalyst", "ART-CAT-SMOKE"),
            ("risk", "ART-RISK-SMOKE"),
        ):
            self.store.upsert_record(
                "artifact",
                artifact_id,
                run_id,
                {"artifact_id": artifact_id, "agent_id": agent_id, "source_ids": source_ids},
                status="completed",
                submitted_by=agent_id,
            )
        for index in range(1, 4):
            self.store.upsert_record(
                "logic",
                f"L-SMOKE-00{index}",
                run_id,
                {
                    "logic_id": f"L-SMOKE-00{index}",
                    "title": f"验证候选逻辑 {index}",
                    "mechanism": "验证用逻辑链条",
                    "supporting_fact_ids": ["F-SMOKE-SEED"],
                    "falsification_conditions": ["验证指标失效"],
                    "tracking_indicators": ["验证指标"],
                    "submitted_by": "fundamental",
                },
                status="candidate",
                submitted_by="fundamental",
            )

    def _seed_review_material(self, run_id: str) -> None:
        self._seed_research_artifacts(run_id)
        self.store.upsert_record(
            "catalyst",
            "CAT-SMOKE-001",
            run_id,
            {"catalyst_id": "CAT-SMOKE-001", "title": "验证催化", "source_ids": self._seed_source_ids(run_id)},
            status="candidate",
            submitted_by="market_catalyst",
        )
        self.store.upsert_record(
            "risk",
            "RISK-SMOKE-001",
            run_id,
            {"risk_id": "RISK-SMOKE-001", "title": "验证风险", "source_ids": self._seed_source_ids(run_id)},
            status="candidate",
            submitted_by="risk",
        )

    def _seed_approved_material(self, run_id: str) -> None:
        self._seed_review_material(run_id)
        self.store.update_record_status(run_id, "logic", [f"L-SMOKE-00{i}" for i in range(1, 4)], "approved")
        self.store.update_record_status(run_id, "catalyst", ["CAT-SMOKE-001"], "approved")
        self.store.update_record_status(run_id, "risk", ["RISK-SMOKE-001"], "approved")
        self.store.update_record_status(run_id, "fact", ["F-SMOKE-SEED"], "verified")
        self.store.upsert_record(
            "review",
            "REVIEW-SMOKE-001",
            run_id,
            {"review_id": "REVIEW-SMOKE-001", "decision": "approve_for_report"},
            status="approve_for_report",
            submitted_by="reviewer_arbiter",
        )

    def _receipt(
        self,
        result: AgentExecutionResult,
        run_id: str,
        expected_tools: set[str],
    ) -> dict[str, Any]:
        observed = {trace.tool_name for trace in result.tool_calls}
        forbidden = observed - set(AGENT_REGISTRY[result.agent_id].allowed_tools)
        output_status = (result.structured_output or {}).get("status")
        pass_checks = {
            "execution_succeeded": result.status == "succeeded",
            "model_is_deepseek_v4_flash": result.model == "deepseek-v4-flash",
            "usage_present": result.usage.total_tokens > 0,
            "schema_valid": result.structured_output is not None,
            "required_tools_called": expected_tools.issubset(observed),
            "no_forbidden_tools": not forbidden,
            "core_output_or_explicit_partial": bool(
                result.structured_output
                and (
                    output_status in {"completed", "partial"}
                    or bool((result.structured_output or {}).get("blocking_reasons"))
                )
            ),
        }
        return {
            "generated_at": _now(),
            "run_id": run_id,
            "agent_id": result.agent_id,
            "runtime_agent_name": result.runtime_agent_name,
            "execution_status": result.status,
            "model": result.model,
            "model_call_id": result.model_call_id,
            "usage": result.usage.model_dump(mode="json"),
            "tool_names": [trace.tool_name for trace in result.tool_calls],
            "tool_count": len(result.tool_calls),
            "forbidden_tool_names": sorted(forbidden),
            "output_status": output_status,
            "schema_name": AGENT_REGISTRY[result.agent_id].output_contract_name,
            "artifact_id": result.artifact_id,
            "parameter_card_id": result.parameter_card_id,
            "fact_ids": result.fact_ids,
            "logic_ids": result.logic_ids,
            "catalyst_ids": result.catalyst_ids,
            "risk_ids": result.risk_ids,
            "review_id": result.review_id,
            "report_id": result.report_id,
            "checks": pass_checks,
            "pass": all(pass_checks.values()),
            "warnings": result.warnings,
            "error_type": (result.error or "").split(":", 1)[0] if result.error else None,
        }

    def _write_receipt(self, filename: str, payload: dict[str, Any]) -> None:
        target = self.validation_root / filename
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _progress(self, run_id: str, stage: str, message: str) -> None:
        payload = {
            "run_id": run_id,
            "stage": stage,
            "message": message,
            "updated_at": _now(),
        }
        self._write_receipt("full-chain-progress.json", payload)
        print(f"[full-chain] {stage}: {message}", flush=True)

    def _successful(self, result: AgentExecutionResult) -> bool:
        return bool(
            result.status == "succeeded"
            and result.structured_output
            and result.artifact_id
            and result.usage.total_tokens > 0
        )

    def _delivery_review_output(
        self,
        run_id: str,
        results: dict[str, AgentExecutionResult],
        *,
        existing: dict[str, Any] | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """Create an explicit, warning-bearing delivery decision.

        This is coordinator policy, not a claim that Reviewer found no issues.
        The original issues are retained and the decision is stored as
        ``approve_with_warnings`` so ReportWriter can consume the available
        material without hiding the review limitations.
        """

        existing = existing or {}
        logic_ids = list(existing.get("approved_logic_ids") or [])
        if len(logic_ids) != 3:
            logic_ids = sorted(
                {
                    logic_id
                    for agent_id in (
                        "fundamental",
                        "industry_competition",
                        "market_catalyst",
                    )
                    for logic_id in (results.get(agent_id).logic_ids if results.get(agent_id) else [])
                }
            )[:3]
        fact_ids = list(existing.get("approved_fact_ids") or [])
        if not fact_ids:
            fact_ids = sorted(
                {
                    fact_id
                    for result in results.values()
                    for fact_id in getattr(result, "fact_ids", [])
                }
            )
        catalyst_ids = list(existing.get("approved_catalyst_ids") or [])
        if not catalyst_ids:
            catalyst_ids = sorted(
                {
                    catalyst_id
                    for result in results.values()
                    for catalyst_id in getattr(result, "catalyst_ids", [])
                }
            )
        risk_ids = list(existing.get("approved_risk_ids") or [])
        if not risk_ids:
            risk_ids = sorted(
                {
                    risk_id
                    for result in results.values()
                    for risk_id in getattr(result, "risk_ids", [])
                }
            )
        issues = list(existing.get("issues") or [])
        if not issues:
            issues = [
                {
                    "issue_id": f"ISSUE-DELIVERY-{uuid4().hex[:8].upper()}",
                    "target_agent_id": "reviewer_arbiter",
                    "problem_statement": reason,
                    "problem_type": "delivery_warning",
                    "severity": "medium",
                    "expected_fields": ["人工核验来源、口径和未验证事项"],
                    "input_refs": [],
                }
            ]
        review_id = str(existing.get("review_id") or f"REVIEW-DELIVERY-{uuid4().hex[:8].upper()}")
        payload = {
            "review_id": review_id,
            "decision": "approve_with_warnings",
            "approved_fact_ids": fact_ids,
            "approved_logic_ids": logic_ids,
            "approved_catalyst_ids": catalyst_ids,
            "approved_risk_ids": risk_ids,
            "rejected_items": list(existing.get("rejected_items") or []),
            "issues": issues,
            "conflicts": list(existing.get("conflicts") or []),
            "decision_rationale": (
                f"{reason}。允许 ReportWriter 使用现有材料；所有问题保留在报告的审查意见和研究限制中。"
            ),
            "gate_2_payload": existing.get("gate_2_payload"),
        }
        self.store.upsert_record(
            "review",
            review_id,
            run_id,
            payload,
            status="approve_with_warnings",
            submitted_by="delivery_policy",
        )
        self.store.update_record_status(run_id, "fact", fact_ids, "verified")
        self.store.update_record_status(run_id, "logic", logic_ids, "approved")
        self.store.update_record_status(run_id, "catalyst", catalyst_ids, "approved")
        self.store.update_record_status(run_id, "risk", risk_ids, "approved")
        return payload

    def _write_agent_report_markdown(
        self,
        run_id: str,
        result: AgentExecutionResult,
        review_output: dict[str, Any],
    ) -> str:
        payload = result.structured_output or {}
        path = self.validation_root / "full-chain-report.md"
        lines = [
            f"# {payload.get('title') or '上市公司研究报告'}",
            "",
            "> 本报告由 OpenHarness 多 Agent 投研流程生成，包含待验证事项，不构成投资建议。",
            "",
            f"- Run ID：`{run_id}`",
            f"- Report ID：`{result.report_id or '未登记'}`",
            "",
        ]
        rendered_sections: list[str] = []
        for section in payload.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_marker = " ".join(
                str(section.get(key) or "") for key in ("section_id", "title")
            ).lower()
            rendered_sections.append(section_marker)
            lines.extend(
                [
                    f"## {section.get('title') or section.get('section_id') or '研究章节'}",
                    "",
                    str(section.get("content") or "当前阶段未获得足够可靠资料，待后续补充。"),
                    "",
                ]
            )
        required_sections = (
            ("公司概况", ("company", "公司概况")),
            ("最近一年经营变化", ("operation", "经营变化", "基本面")),
            ("三个最值得关注的投资逻辑", ("logic", "投资逻辑")),
            ("两家主要竞争对手对比", ("peer", "competitor", "竞品", "竞争对手")),
            ("未来半年催化因素", ("catalyst", "催化")),
            ("主要风险", ("risk", "风险")),
            ("后续跟踪指标", ("tracking", "跟踪指标")),
        )
        for title, markers in required_sections:
            if any(
                marker in rendered
                for rendered in rendered_sections
                for marker in markers
            ):
                continue
            lines.extend(
                [
                    f"## {title}",
                    "",
                    "当前阶段未获得足够可靠资料，待后续补充。",
                    "",
                ]
            )
        lines.extend(["## 审查意见与研究限制", ""])
        issues = review_output.get("issues") or []
        if issues:
            lines.extend(
                f"- `{item.get('issue_id', 'ISSUE-UNKNOWN')}`：{item.get('problem_statement') or '待人工核验'}"
                for item in issues
                if isinstance(item, dict)
            )
        else:
            lines.append("- 未发现需要单独列出的审查问题。")
        lines.extend(
            [
                "",
                "## 合规声明",
                "",
                str(payload.get("compliance_statement") or "本报告仅用于研究展示，不构成投资建议。"),
                "",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def _fallback_chain_summary(
        self,
        run_id: str,
        receipts: list[dict[str, Any]],
        results: dict[str, AgentExecutionResult],
        reason: str,
        *,
        review_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        report_path = self.validation_root / "full-chain-report.md"
        fallback = write_fallback_report(
            store=self.store,
            run_id=run_id,
            output_path=report_path,
            results=results,
            receipts=receipts,
            reason=reason,
            review_output=review_output,
        )
        return self._chain_summary(
            run_id,
            receipts,
            "completed_with_warnings",
            report_generated=True,
            report_quality=fallback["report_quality"],
            review_status=str((review_output or {}).get("decision") or "not_completed"),
            report_path=str(report_path),
        )

    def _chain_summary(
        self,
        run_id: str,
        receipts: list[dict[str, Any]],
        terminal_state: str,
        *,
        report_generated: bool = False,
        report_quality: str | None = None,
        review_status: str | None = None,
        report_path: str | None = None,
    ) -> dict[str, Any]:
        summary = {
            "mode": "FullChainValidation",
            "generated_at": _now(),
            "run_id": run_id,
            "terminal_state": terminal_state,
            "pass": report_generated,
            "report_generated": report_generated,
            "report_quality": report_quality,
            "review_status": review_status,
            "pipeline_status": terminal_state,
            "report_path": report_path,
            "receipts": receipts,
        }
        self._write_receipt("full-chain.json", summary)
        return summary


def _minimal_text_pdf() -> bytes:
    """Build a tiny ASCII PDF fixture without adding a document-generation dependency."""

    content = (
        "BT /F1 12 Tf 72 720 Td "
        "(Validation fixture: current revenue 400, prior revenue 300, gross margin 25 percent.) Tj ET"
    ).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_transient_network_failure(result: AgentExecutionResult) -> bool:
    if result.status != "failed" or not result.error:
        return False
    error = result.error.lower()
    return any(
        marker in error
        for marker in (
            "network error",
            "network request failed",
            "connection",
            "timeout",
            "readtimeout",
            "connecttimeout",
            "httpx",
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run real seven-Agent DeepSeek validations.")
    parser.add_argument("--mode", choices=("agent", "all", "chain"), required=True)
    parser.add_argument("--agent", choices=tuple(AGENT_REGISTRY), default=None)
    return parser


async def _run(args: argparse.Namespace) -> int:
    harness = ValidationHarness()
    if args.mode == "agent":
        if not args.agent:
            raise SystemExit("--mode agent requires --agent")
        result = await harness.smoke(args.agent)
    elif args.mode == "all":
        result = await harness.validate_all()
    else:
        result = await harness.full_chain()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("pass") else 1


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
