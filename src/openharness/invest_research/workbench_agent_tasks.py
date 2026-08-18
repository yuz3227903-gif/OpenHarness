"""Build safe, typed requests for direct ``@Agent`` workbench tasks.

The full CrewAI flow remains owned by Planner.  Other Agents can be called
directly inside a project channel, but they must reuse the latest Run's
parameter card and evidence instead of receiving an untyped chat prompt.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from openharness.invest_research.evidence_store import EvidenceStore


DIRECT_AGENT_IDS = frozenset(
    {
        "fundamental",
        "industry_competition",
        "market_catalyst",
        "risk",
        "reviewer_arbiter",
        "report_writer",
    }
)


class DirectAgentTaskError(ValueError):
    """A direct Agent task cannot be built from the available channel context."""


def direct_task_budget(agent_id: str) -> dict[str, Any]:
    """Return a bounded budget for one user-triggered direct Agent task.

    The budget stays below a full research run, but it has to be large enough
    for the role to finish. The first version was far tighter than the role
    definitions allow — Fundamental was capped at 6 turns against a definition
    ceiling of 16, while a real run of the same role used 13 tool calls — so a
    direct task failed on the budget rather than on the work. These numbers sit
    just under each definition's maxTurns.
    """

    budgets: dict[str, dict[str, Any]] = {
        "fundamental": {
            "max_turns": 12,
            "max_output_tokens": 6_000,
            "tool_call_limits": {
                "tavily_search": 4,
                "web_fetch": 4,
                "read_uploaded_file": 2,
                "calculator": 4,
                "evidence_query": 4,
            },
        },
        "industry_competition": {
            "max_turns": 12,
            "max_output_tokens": 6_000,
            "tool_call_limits": {
                "tavily_search": 4,
                "web_fetch": 4,
                "read_uploaded_file": 2,
                "calculator": 3,
                "evidence_query": 4,
            },
        },
        "market_catalyst": {
            "max_turns": 10,
            "max_output_tokens": 6_000,
            "tool_call_limits": {
                "tavily_search": 4,
                "web_fetch": 3,
                "read_uploaded_file": 2,
                "calculator": 2,
                "evidence_query": 3,
            },
        },
        "risk": {
            "max_turns": 10,
            "max_output_tokens": 5_000,
            "tool_call_limits": {
                # Risk argues against upstream work; it never searches.
                "evidence_query": 6,
                "tavily_search": 0,
                "web_fetch": 0,
                "read_uploaded_file": 0,
                "calculator": 2,
            },
        },
        "reviewer_arbiter": {
            "max_turns": 8,
            "max_output_tokens": 5_000,
            "tool_call_limits": {"evidence_query": 4},
        },
        "report_writer": {
            # Its definition caps at 2 turns; it writes from delivered material.
            "max_turns": 2,
            "max_output_tokens": 8_000,
            "tool_call_limits": {},
        },
    }
    try:
        return budgets[agent_id]
    except KeyError as exc:
        raise DirectAgentTaskError(f"Agent does not support direct tasks: {agent_id}") from exc


def build_direct_agent_input(
    *,
    agent_id: str,
    objective: str,
    task_id: str,
    summary: dict[str, Any] | None,
    evidence_store: EvidenceStore,
    as_of_date: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one role-specific input payload from the latest completed Run.

    Returns ``(input_payload, context_package)``.  No records are copied across
    Runs, and every referenced S/F/L/ART/REVIEW ID remains in its original Run.
    """

    if agent_id not in DIRECT_AGENT_IDS:
        raise DirectAgentTaskError(f"Agent does not support direct tasks: {agent_id}")
    clean_objective = " ".join(str(objective or "").split())
    if not clean_objective:
        raise DirectAgentTaskError("Direct Agent tasks require a non-empty objective")
    if not isinstance(summary, dict):
        raise DirectAgentTaskError("频道还没有可复用的研究上下文，请先 @Planner 创建一次研究。")

    run_id = str(summary.get("run_id") or "")
    parameter_card_id = str(summary.get("parameter_card_id") or "")
    if not run_id or not parameter_card_id or not evidence_store.run_exists(run_id):
        raise DirectAgentTaskError("最近一次研究缺少有效 Run 或参数卡，请先 @Planner 重新研究。")
    parameter_record = evidence_store.get_record(run_id, parameter_card_id)
    if not parameter_record:
        raise DirectAgentTaskError("参数卡未登记在证据库中，请先 @Planner 重新研究。")
    parameter_payload = dict(parameter_record.get("payload") or {})
    parameter_ref = {
        "parameter_card_id": parameter_card_id,
        "version": str(parameter_payload.get("version") or "1.0"),
    }
    agent_results = summary.get("agent_results") or {}
    source_ids = _collect_result_ids(agent_results, "source_ids")
    fact_ids = _collect_result_ids(agent_results, "fact_ids")
    logic_ids = _collect_result_ids(agent_results, "logic_ids")
    common: dict[str, Any] = {
        "protocol_version": "1.0",
        "agent_id": agent_id,
        "run_id": run_id,
        "task_id": task_id,
        "objective": clean_objective,
        "parameter_card_version": parameter_ref["version"],
        "input_refs": [],
        "supplement": None,
        "parameter_card": parameter_ref,
    }

    if agent_id == "fundamental":
        common.update(source_ids=source_ids, fact_ids=fact_ids, uploaded_file_refs=[])
    elif agent_id == "industry_competition":
        competitors = list(parameter_payload.get("recommended_competitors") or [])
        if len(competitors) != 2:
            raise DirectAgentTaskError("参数卡中没有两家已确认竞品，无法直接执行行业竞品任务。")
        common.update(
            confirmed_competitors=competitors,
            source_ids=source_ids,
            fact_ids=fact_ids,
            uploaded_file_refs=[],
        )
    elif agent_id == "market_catalyst":
        window = parameter_payload.get("catalyst_window") or {
            "start_date": as_of_date.isoformat(),
            "end_date": (as_of_date + timedelta(days=183)).isoformat(),
        }
        common.update(
            catalyst_window=window,
            source_ids=source_ids,
            fact_ids=fact_ids,
            uploaded_file_refs=[],
        )
    elif agent_id == "risk":
        artifacts = {
            role: _result_value(agent_results, role, "artifact_id")
            for role in ("fundamental", "industry_competition", "market_catalyst")
        }
        if not any(artifacts.values()):
            raise DirectAgentTaskError("Risk 缺少上游研究成果，请先完成基本面、行业或催化任务。")
        common.update(
            fundamental_artifact_id=artifacts["fundamental"],
            industry_competition_artifact_id=artifacts["industry_competition"],
            market_catalyst_artifact_id=artifacts["market_catalyst"],
            logic_ids=logic_ids,
            source_ids=source_ids,
        )
    elif agent_id == "reviewer_arbiter":
        artifacts = [
            value
            for role in ("fundamental", "industry_competition", "market_catalyst", "risk")
            if (value := _result_value(agent_results, role, "artifact_id"))
        ]
        if not artifacts:
            raise DirectAgentTaskError("Reviewer 缺少可审查的研究成果。")
        common.update(
            review_stage="initial",
            expected_review_id=f"REVIEW-WB-{uuid4().hex[:12].upper()}",
            audit_artifact_id=None,
            audit_status=None,
            research_artifact_ids=artifacts,
            source_ids=source_ids,
            fact_ids=fact_ids,
            logic_ids=logic_ids,
            prior_issue_ids=[],
            supplement_artifact_ids=[],
        )
    elif agent_id == "report_writer":
        decision = summary.get("delivery_decision") or {}
        selected_logic_ids = list(
            decision.get("selected_logic_ids")
            or summary.get("approved_logic_ids")
            or []
        )[:3]
        review_id = str(decision.get("review_id") or "")
        if len(selected_logic_ids) != 3 or not review_id:
            raise DirectAgentTaskError("ReportWriter 缺少三条已选逻辑或审查记录。")
        delivery_mode = str(decision.get("delivery_mode") or "provisional")
        approved = selected_logic_ids if delivery_mode == "formal" else []
        common.update(
            review_id=review_id,
            delivery_mode=delivery_mode,
            selected_logic_ids=selected_logic_ids,
            approved_fact_ids=[],
            approved_logic_ids=approved,
            approved_catalyst_ids=[],
            approved_risk_ids=[],
            gate_2_decision_id=None,
            section_id=None,
            allowed_evidence_ids=[*source_ids, *fact_ids, *selected_logic_ids],
            attempt=1,
        )

    context_package = {
        "channel_task": True,
        "company_identity": parameter_payload.get("company_identity") or {},
        "research_period": parameter_payload.get("research_period") or {},
        "catalyst_window": parameter_payload.get("catalyst_window") or {},
        "confirmed_competitors": parameter_payload.get("recommended_competitors") or [],
        "instruction": (
            "这是频道中的直接 @Agent 任务。只回答用户明确提出的目标；"
            "若完整角色合同包含更大范围，可返回 partial 并清楚说明未完成部分。"
        ),
    }
    return common, context_package


def direct_task_prompt(agent_id: str, objective: str) -> str:
    """Create the dynamic task layer without weakening role/governance prompts."""

    peers = "、".join(f"@{item}" for item in sorted(DIRECT_AGENT_IDS) if item != agent_id)
    return (
        f"你在项目频道中被直接 @{agent_id}。用户任务是：{objective}\n"
        "请先复用当前 Run 已授权的资料，再按需使用本角色工具。"
        "输出必须遵守本角色 JSON 合同；证据不足时返回 partial，禁止编造来源。\n"
        f"若某部分必须由其他角色完成，可在结论文本中写 {peers} 之一并说明需要什么，"
        "系统会为它创建任务；不要替它作答，也不要 @ 自己。"
    )


def _collect_result_ids(agent_results: Any, field: str) -> list[str]:
    values: list[str] = []
    if not isinstance(agent_results, dict):
        return values
    for result in agent_results.values():
        if not isinstance(result, dict):
            continue
        for value in result.get(field) or []:
            text = str(value)
            if text and text not in values:
                values.append(text)
    return values


def _result_value(agent_results: Any, agent_id: str, field: str) -> str | None:
    if not isinstance(agent_results, dict):
        return None
    result = agent_results.get(agent_id)
    if not isinstance(result, dict):
        return None
    value = result.get(field)
    return str(value) if value else None


__all__ = [
    "DIRECT_AGENT_IDS",
    "DirectAgentTaskError",
    "build_direct_agent_input",
    "direct_task_budget",
    "direct_task_prompt",
]
