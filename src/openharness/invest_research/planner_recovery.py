"""Deterministic recovery for a partially completed Planner result.

The recovery never invents a confirmed competitor or research fact.  It only
turns already validated Planner fields into a provisional parameter card so
that the specialist Agents can continue.  Missing competitors are represented
by explicit placeholders that IndustryCompetition must replace or disclose as
unverified.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

from openharness.invest_research.contracts import PlannerResult

_DOWNSTREAM_AGENTS = (
    "fundamental",
    "industry_competition",
    "market_catalyst",
    "risk",
    "reviewer_arbiter",
    "report_writer",
)
_PLACEHOLDER_PREFIX = "待 IndustryCompetition 核验的竞品"


@dataclass(frozen=True)
class PlannerRecoveryOutcome:
    """A validated provisional Planner payload and its audit metadata."""

    payload: dict[str, Any]
    parameter_card_id: str
    placeholder_competitor_count: int
    reason: str


def is_competitor_placeholder(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return str(value.get("company_name") or "").startswith(_PLACEHOLDER_PREFIX)


def recover_partial_planner_output(
    *,
    run_id: str,
    company_query: str,
    as_of_date: date,
    output: dict[str, Any] | None,
    source_ids: list[str],
) -> PlannerRecoveryOutcome | None:
    """Build a provisional parameter card from a traceable partial result.

    Recovery is refused when Planner produced no structured output or no
    recognizable planning fields.  A structured planning result without a
    persisted source is still recoverable, but only as an explicitly
    unverified provisional handoff.  This distinction matters: a temporary
    Planner delivery problem should not discard all downstream work, while a
    completely empty result must not be turned into a fake parameter card.
    """

    if not isinstance(output, dict):
        return None

    if not _has_planner_shape(output):
        return None

    traceable_sources = _unique_strings(
        [*source_ids, *_collect_company_sources(output.get("company_identity"))]
    )
    has_traceable_sources = bool(traceable_sources)

    recovered = dict(output)
    company_identity = _company_identity(
        recovered.get("company_identity"),
        company_query=company_query,
        source_ids=traceable_sources,
    )
    research_period = _date_range(
        recovered.get("research_period"),
        fallback_start=_shift_year(as_of_date, -1),
        fallback_end=as_of_date,
    )
    catalyst_window = _date_range(
        recovered.get("catalyst_window"),
        fallback_start=as_of_date,
        fallback_end=_shift_month(as_of_date, 6),
    )

    competitors = _competitors(recovered)
    placeholder_count = 0
    while len(competitors) < 2:
        placeholder_count += 1
        competitors.append(_placeholder_competitor(placeholder_count))
    competitors = competitors[:2]

    recovered.update(
        {
            "status": "partial",
            "company_identity": company_identity,
            "research_period": research_period,
            "catalyst_window": catalyst_window,
            "competitor_candidates": _merge_competitor_lists(
                recovered.get("competitor_candidates"), competitors
            ),
            "recommended_competitors": competitors,
            "task_plan": _task_plan(recovered.get("task_plan")),
            "gate_1_payload": {
                "company_identity": company_identity,
                "research_period": research_period,
                "catalyst_window": catalyst_window,
                "recommended_competitors": competitors,
            },
        }
    )
    _ensure_common_fields(recovered)
    recovered["completed_scope"] = _unique_strings(
        [*recovered["completed_scope"], "已恢复可供下游执行的暂定参数卡"]
    )
    recovered["evidence_refs"] = _unique_strings(
        [*recovered["evidence_refs"], *traceable_sources]
    )
    limitations = [
        *recovered["limitations"],
        (
            "Planner 未完整满足参数卡合同；系统仅基于已有可追溯结果恢复暂定参数卡。"
            if has_traceable_sources
            else "Planner 未完整满足参数卡合同，且当前结果没有可持久化的 S-ID；参数仅作为暂定输入，必须由下游 Agent 重新核验。"
        ),
    ]
    recovered["limitations"] = _unique_strings(limitations)
    recovered["unverified_items"] = [
        *recovered["unverified_items"],
        {
            "item": "Planner 暂定参数卡",
            "reason": (
                "原始 Planner 结果为 partial，未生成可持久化的 Gate 1 参数卡。"
                if has_traceable_sources
                else "原始 Planner 结果包含可用的规划字段，但没有可持久化的 S-ID；不能将其中内容视为已确认事实。"
            ),
            "required_evidence": "由后续专业 Agent 核验公司资料与两家主要竞品。",
        },
    ]
    if not has_traceable_sources:
        recovered["unverified_items"].append(
            {
                "item": "Planner 原始参数的来源链",
                "reason": "本次 Planner 结果没有可追溯 S-ID。",
                "required_evidence": "由 Fundamental、IndustryCompetition 和 MarketCatalyst 使用真实来源核验。",
            }
        )
    if placeholder_count:
        recovered["handoff_requests"] = [
            *recovered["handoff_requests"],
            {
                "target_agent_id": "industry_competition",
                "request": (
                    f"识别并核验 {placeholder_count} 家缺失竞品，用真实公司替换暂定占位项；"
                    "无法完成时必须返回 partial 并披露缺口。"
                ),
                "input_refs": traceable_sources,
            },
        ]

    # Re-validate the recovered object against the same contract.  status is
    # deliberately partial, so the model's completed-only constraints are not
    # weakened globally.
    validated = PlannerResult.model_validate(recovered).model_dump(mode="json")
    parameter_card_id = _parameter_card_id(run_id)
    if has_traceable_sources:
        reason = (
            "planner_partial_parameter_card_recovered"
            if not placeholder_count
            else "planner_partial_parameter_card_recovered_with_competitor_placeholders"
        )
    else:
        reason = (
            "planner_partial_parameter_card_recovered_without_traceable_sources"
            if not placeholder_count
            else "planner_partial_parameter_card_recovered_without_traceable_sources_and_competitor_placeholders"
        )
    return PlannerRecoveryOutcome(
        payload=validated,
        parameter_card_id=parameter_card_id,
        placeholder_competitor_count=placeholder_count,
        reason=reason,
    )


def _ensure_common_fields(payload: dict[str, Any]) -> None:
    payload.setdefault("protocol_version", "1.0")
    for field in (
        "completed_scope",
        "evidence_refs",
        "unverified_items",
        "limitations",
        "handoff_requests",
        "blocking_reasons",
        "competitor_candidates",
        "recommended_competitors",
        "task_plan",
        "available_materials",
        "material_gaps",
    ):
        payload.setdefault(field, [])
    payload.setdefault("dependency_graph", {})


def _company_identity(
    value: object,
    *,
    company_query: str,
    source_ids: list[str],
) -> dict[str, Any]:
    identity = dict(value) if isinstance(value, dict) else {}
    identity.setdefault("legal_name", company_query)
    identity.setdefault("short_name", company_query)
    identity.setdefault("ticker", "待专业 Agent 核验")
    identity.setdefault("exchange", "待专业 Agent 核验")
    identity.setdefault("primary_business", "待专业 Agent 核验")
    identity["source_ids"] = _unique_strings(
        [*(identity.get("source_ids") or []), *source_ids]
    )
    return identity


def _date_range(
    value: object,
    *,
    fallback_start: date,
    fallback_end: date,
) -> dict[str, str]:
    if isinstance(value, dict) and value.get("start_date") and value.get("end_date"):
        return {
            "start_date": str(value["start_date"]),
            "end_date": str(value["end_date"]),
        }
    return {
        "start_date": fallback_start.isoformat(),
        "end_date": fallback_end.isoformat(),
    }


def _competitors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _merge_competitor_lists(
        payload.get("recommended_competitors"),
        payload.get("competitor_candidates"),
    )[:2]


def _merge_competitor_lists(*values: object) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("company_name") or "").strip()
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            merged.append(dict(item))
    return merged


def _placeholder_competitor(index: int) -> dict[str, Any]:
    return {
        "company_name": f"{_PLACEHOLDER_PREFIX} {index}",
        "ticker": None,
        "exchange": None,
        "selection_reasons": [
            "Planner 在有限工具预算内未确认该竞品，交由行业竞品 Agent 定向识别。"
        ],
        "comparability_limits": [
            "这不是已确认的真实竞品名称，不得直接进入最终报告。"
        ],
        "source_ids": [],
    }


def _task_plan(value: object) -> list[dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("assigned_agent_id") in _DOWNSTREAM_AGENTS:
                existing[str(item["assigned_agent_id"])] = dict(item)
    tasks: list[dict[str, Any]] = []
    previous_research_tasks = [
        "TASK-RECOVERED-FUNDAMENTAL",
        "TASK-RECOVERED-INDUSTRY",
        "TASK-RECOVERED-MARKET",
    ]
    defaults = {
        "fundamental": ("TASK-RECOVERED-FUNDAMENTAL", "研究最近一年经营变化与基本面", []),
        "industry_competition": ("TASK-RECOVERED-INDUSTRY", "识别并比较两家主要竞品", []),
        "market_catalyst": ("TASK-RECOVERED-MARKET", "研究未来半年催化因素", []),
        "risk": ("TASK-RECOVERED-RISK", "基于三路研究识别主要风险", previous_research_tasks),
        "reviewer_arbiter": ("TASK-RECOVERED-REVIEW", "审查事实、逻辑和冲突", ["TASK-RECOVERED-RISK"]),
        "report_writer": ("TASK-RECOVERED-REPORT", "整合已审查材料生成报告", ["TASK-RECOVERED-REVIEW"]),
    }
    for agent_id in _DOWNSTREAM_AGENTS:
        if agent_id in existing:
            tasks.append(existing[agent_id])
            continue
        task_id, objective, depends_on = defaults[agent_id]
        tasks.append(
            {
                "task_id": task_id,
                "assigned_agent_id": agent_id,
                "objective": objective,
                "input_refs": [],
                "expected_output": f"{agent_id} 结构化结果",
                "depends_on": depends_on,
            }
        )
    return tasks


def _collect_company_sources(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [str(item) for item in value.get("source_ids", []) if str(item).startswith("S-")]


def _has_planner_shape(payload: dict[str, Any]) -> bool:
    """Return whether a partial response contains meaningful planning fields.

    This is deliberately a shape check, not an evidence check.  It lets the
    flow continue with a provisional card when a provider returned useful
    company/window/competitor fields but failed to persist sources.  The
    caller marks that card unverified; no IDs are invented here.
    """

    identity = payload.get("company_identity")
    has_identity = isinstance(identity, dict) and any(
        str(identity.get(key) or "").strip()
        for key in ("legal_name", "short_name", "ticker", "primary_business")
    )
    has_window = isinstance(payload.get("research_period"), dict) or isinstance(
        payload.get("catalyst_window"), dict
    )
    competitor_values = (
        payload.get("recommended_competitors"),
        payload.get("competitor_candidates"),
    )
    has_competitor = any(
        isinstance(item, dict) and str(item.get("company_name") or "").strip()
        for values in competitor_values
        if isinstance(values, list)
        for item in values
    )
    return has_identity or has_window or has_competitor


def _unique_strings(values: list[object]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _parameter_card_id(run_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}\x1fgate-1-recovered".encode()).hexdigest()[:12]
    return f"PC-{digest.upper()}"


def _shift_year(value: date, years: int) -> date:
    target_year = value.year + years
    day = min(value.day, calendar.monthrange(target_year, value.month)[1])
    return date(target_year, value.month, day)


def _shift_month(value: date, months: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


__all__ = [
    "PlannerRecoveryOutcome",
    "is_competitor_placeholder",
    "recover_partial_planner_output",
]
