"""Best-effort Markdown report generation for investment research runs.

This module is deliberately deterministic. It never invents facts: it only
renders records already present in the current Run and labels missing material.
It is used when the LLM ReportWriter cannot complete, or when an upstream
Agent failed but the user still needs a reviewable deliverable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from openharness.invest_research.evidence_store import EvidenceStore


MISSING = "当前阶段未获得足够可靠资料，待后续补充。"


def write_fallback_report(
    *,
    store: EvidenceStore,
    run_id: str,
    output_path: Path,
    results: dict[str, Any],
    receipts: Iterable[dict[str, Any]],
    reason: str,
    review_output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a transparent report from whatever the Run managed to collect."""

    records = _load_records(store, run_id)
    parameter = _first_payload(records.get("parameter_card", []))
    company = _company_name(parameter, records)
    ticker = _ticker(parameter)

    logic_records = records.get("logic", [])
    fact_records = records.get("fact", [])
    source_records = records.get("source", [])
    catalyst_records = records.get("catalyst", [])
    risk_records = records.get("risk", [])

    issues = list((review_output or {}).get("issues") or [])
    if not issues:
        issues = [
            item.get("payload", {})
            for item in records.get("issue", [])
            if isinstance(item.get("payload"), dict)
        ]

    receipt_list = [item for item in receipts if isinstance(item, dict)]
    completed_agents = [
        agent_id
        for agent_id, result in results.items()
        if getattr(result, "status", None) == "succeeded"
    ]
    failed_agents = [
        agent_id
        for agent_id, result in results.items()
        if getattr(result, "status", None) not in {None, "succeeded"}
    ]
    if not completed_agents and not failed_agents:
        completed_agents = _unique_agent_ids(
            item.get("agent_id")
            for item in receipt_list
            if item.get("execution_status") == "succeeded"
        )
        failed_agents = _unique_agent_ids(
            item.get("agent_id")
            for item in receipt_list
            if item.get("execution_status") not in {None, "succeeded"}
        )

    agent_outputs = _agent_outputs(results)
    if not agent_outputs:
        agent_outputs = _artifact_outputs(records)
    receipt_lines = _receipt_lines(receipt_list)
    logic_lines = _record_lines(logic_records, "logic_id", _logic_summary)
    fact_lines = _record_lines(fact_records, "fact_id", _fact_summary, limit=12)
    catalyst_lines = _record_lines(catalyst_records, "catalyst_id", _catalyst_summary)
    risk_lines = _record_lines(risk_records, "risk_id", _risk_summary)
    source_lines = _record_lines(source_records, "source_id", _source_summary, limit=12)
    issue_lines = [
        f"- `{item.get('issue_id', 'ISSUE-UNKNOWN')}`："
        f"{item.get('problem_statement') or item.get('description') or MISSING}"
        for item in issues
        if isinstance(item, dict)
    ]
    if not issue_lines:
        issue_lines = _failure_issue_lines(results)

    report_quality = "fallback"
    sections = [
        f"# {company}（{ticker}）上市公司研究报告",
        "",
        "> 本报告由 OpenHarness 多 Agent 投研流程生成。报告包含演示/验证运行数据，"
        "部分内容可能仍需人工核验，不构成投资建议。",
        "",
        f"- Run ID：`{run_id}`",
        f"- 生成方式：本地兜底报告（{reason}）",
        f"- 报告质量：`{report_quality}`",
        "",
        "## 一、公司概况",
        "",
        _company_overview(parameter),
        "",
        "## 二、最近一年经营变化",
        "",
        _bullets_or_missing(fact_lines),
        "",
        "## 三、三个最值得关注的投资逻辑",
        "",
        _bullets_or_missing(
            logic_lines,
            "当前已收集的候选投资逻辑如下，尚未全部完成严格审查：",
        ),
        "",
        "## 四、两家主要竞争对手对比",
        "",
        _competitor_summary(parameter, agent_outputs),
        "",
        "## 五、未来半年可能的催化因素",
        "",
        _bullets_or_missing(catalyst_lines),
        "",
        "## 六、主要风险",
        "",
        _bullets_or_missing(
            risk_lines or _derived_risk_lines(agent_outputs),
            "当前风险信息来自 Risk Agent 或上游 Agent 明确标出的失效条件/待验证事项：",
        ),
        "",
        "## 七、已完成与未完成的 Agent",
        "",
        f"- 已完成：{', '.join(completed_agents) if completed_agents else '无'}",
        f"- 未完成或失败：{', '.join(failed_agents) if failed_agents else '无'}",
        "",
        "## 八、Reviewer 审查意见",
        "",
        _bullets_or_missing(issue_lines),
        "",
        "## 九、待验证事项与研究限制",
        "",
        _limitations(records, reason),
        "",
        "## 十、协作运行记录",
        "",
        _bullets_or_missing(receipt_lines),
        "",
        "## 十一、来源索引",
        "",
        _bullets_or_missing(source_lines),
        "",
        "## 十二、合规声明",
        "",
        "本报告仅用于展示多 Agent 协作流程和研究资料组织方式，不构成任何投资建议、"
        "目标价或交易指令。报告中的事实、推测和市场预期应在正式使用前由人工再次核验。",
        "",
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Use UTF-8 with BOM because the report is mainly opened on Windows.
    # It prevents older PowerShell/Notepad paths from rendering Chinese as mojibake.
    output_path.write_text("\n".join(sections), encoding="utf-8-sig")
    return {
        "path": str(output_path),
        "report_quality": report_quality,
        "completed_agents": completed_agents,
        "failed_agents": failed_agents,
        "source_count": len(source_records),
        "fact_count": len(fact_records),
        "logic_count": len(logic_records),
        "warning_count": len(issues),
    }


def _load_records(store: EvidenceStore, run_id: str) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for record_type in (
        "parameter_card",
        "source",
        "fact",
        "logic",
        "catalyst",
        "risk",
        "artifact",
        "review",
        "issue",
    ):
        try:
            output[record_type] = store.query_records(
                run_id,
                record_types=(record_type,),
                limit=100,
            )
        except (AttributeError, KeyError, ValueError):
            output[record_type] = []
    return output


def _first_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    return records[0].get("payload", {}) if records else {}


def _company_name(parameter: dict[str, Any], records: dict[str, list[dict[str, Any]]]) -> str:
    identity = parameter.get("company_identity") or {}
    if isinstance(identity, dict):
        for key in ("short_name", "legal_name", "company_name"):
            if identity.get(key):
                return str(identity[key])
    for key in ("company_name", "short_name", "legal_name"):
        if parameter.get(key):
            return str(parameter[key])
    for item in records.get("fact", []):
        payload = item.get("payload", {})
        if payload.get("company_name"):
            return str(payload["company_name"])
    return "目标上市公司"


def _ticker(parameter: dict[str, Any]) -> str:
    identity = parameter.get("company_identity") or {}
    if isinstance(identity, dict):
        for key in ("ticker", "stock_code", "security_code"):
            if identity.get(key):
                return str(identity[key])
    for key in ("ticker", "stock_code", "security_code"):
        if parameter.get(key):
            return str(parameter[key])
    return "待确认"


def _company_overview(parameter: dict[str, Any]) -> str:
    identity = parameter.get("company_identity") or {}
    if isinstance(identity, dict):
        pieces = [
            identity.get("legal_name") or identity.get("short_name"),
            identity.get("exchange"),
            identity.get("primary_business"),
        ]
        text = "；".join(str(item) for item in pieces if item)
        if text:
            return text + "。"
    return MISSING


def _competitor_summary(
    parameter: dict[str, Any],
    agent_outputs: dict[str, dict[str, Any]] | None = None,
) -> str:
    industry_output = (agent_outputs or {}).get("industry_competition") or {}
    peer_comparison = industry_output.get("peer_comparison")
    if isinstance(peer_comparison, list) and peer_comparison:
        lines = ["已收集的三家公司对比结果如下，仍需人工核对币种、口径和时间范围："]
        for row in peer_comparison[:3]:
            if isinstance(row, dict):
                name = row.get("company_name") or row.get("company") or "未命名公司"
                details = "; ".join(
                    f"{key}={value}"
                    for key, value in row.items()
                    if key not in {"company_name", "company"} and value not in (None, "")
                )
                lines.append(f"- {name}：{details or _json_text(row)}")
        return "\n".join(lines)

    candidates = parameter.get("recommended_competitors") or parameter.get("competitor_candidates")
    if isinstance(candidates, list) and candidates:
        names = [
            str(item.get("company_name"))
            for item in candidates
            if isinstance(item, dict) and item.get("company_name")
        ]
        if names:
            return (
                "当前研究计划中的竞品为："
                + "、".join(names)
                + "。详细可比指标仍需结合已登记来源核验。"
            )
    return MISSING


def _agent_outputs(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    for agent_id, result in results.items():
        payload = getattr(result, "structured_output", None)
        if isinstance(payload, dict):
            outputs[agent_id] = payload
    return outputs


def _artifact_outputs(records: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    outputs: dict[str, dict[str, Any]] = {}
    for record in records.get("artifact", []):
        payload = record.get("payload") or {}
        agent_id = payload.get("agent_id") or record.get("submitted_by")
        output = payload.get("output")
        if agent_id and isinstance(output, dict) and agent_id not in outputs:
            outputs[str(agent_id)] = output
    return outputs


def _failure_issue_lines(results: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for agent_id, result in results.items():
        status = getattr(result, "status", None)
        if status in {None, "succeeded"}:
            continue
        failure_class = getattr(result, "failure_class", None) or "unknown"
        error = getattr(result, "error", None) or "未提供错误详情"
        lines.append(
            f"- `{agent_id}`：执行状态 `{status}`，失败类型 `{failure_class}`，{error}"
        )
    return lines


def _derived_risk_lines(agent_outputs: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for agent_id, output in agent_outputs.items():
        for logic in output.get("logic_candidates", []) or []:
            if not isinstance(logic, dict):
                continue
            title = logic.get("title") or logic.get("logic_id") or "候选逻辑"
            for condition in logic.get("falsification_conditions", []) or []:
                lines.append(f"- `{agent_id}` 候选逻辑“{title}”的失效条件：{condition}")
        for item in output.get("unverified_items", []) or []:
            if isinstance(item, dict):
                lines.append(
                    f"- `{agent_id}` 待验证事项："
                    f"{item.get('item') or item.get('reason') or _json_text(item)}"
                )
        for event in output.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            for signal in event.get("failure_signals", []) or []:
                lines.append(f"- `{agent_id}` 催化失效信号：{signal}")
    return lines[:24]


def _record_lines(
    records: list[dict[str, Any]],
    id_key: str,
    formatter: Any,
    *,
    limit: int = 8,
) -> list[str]:
    lines: list[str] = []
    for record in records[:limit]:
        payload = record.get("payload", {})
        record_id = payload.get(id_key) or record.get("record_id") or "UNKNOWN"
        lines.append(f"- `{record_id}`：{formatter(payload)}")
    return lines


def _logic_summary(payload: dict[str, Any]) -> str:
    return "；".join(
        str(payload.get(key))
        for key in ("title", "mechanism")
        if payload.get(key)
    ) or _json_text(payload)


def _fact_summary(payload: dict[str, Any]) -> str:
    metric = payload.get("metric_name") or payload.get("statement")
    value = _readable_financial_value(payload.get("value"), payload.get("unit"))
    pieces = [metric, value, payload.get("period")]
    return "；".join(str(item) for item in pieces if item is not None) or _json_text(payload)


def _catalyst_summary(payload: dict[str, Any]) -> str:
    return "；".join(
        str(payload.get(key))
        for key in ("title", "event_type", "expected_date", "direction", "confidence_basis")
        if payload.get(key)
    ) or _json_text(payload)


def _risk_summary(payload: dict[str, Any]) -> str:
    return "；".join(
        str(payload.get(key))
        for key in ("title", "severity", "trigger_conditions", "impact_path")
        if payload.get(key)
    ) or _json_text(payload)


def _source_summary(payload: dict[str, Any]) -> str:
    return "；".join(
        str(payload.get(key))
        for key in ("title", "publisher", "source_grade", "url_or_file")
        if payload.get(key)
    ) or _json_text(payload)


def _receipt_lines(receipts: Iterable[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        lines.append(
            f"- `{receipt.get('agent_id', 'unknown')}`："
            f"{receipt.get('execution_status', 'unknown')}，"
            f"工具调用 {receipt.get('tool_count', 0)} 次"
        )
    return lines


def _limitations(records: dict[str, list[dict[str, Any]]], reason: str) -> str:
    items = [
        f"- 本次交付采用尽力生成模式，原因：{reason}。",
        f"- 已登记来源 {len(records.get('source', []))} 条、"
        f"事实 {len(records.get('fact', []))} 条、"
        f"候选逻辑 {len(records.get('logic', []))} 条。",
        "- 未被来源直接支持的内容不得视为已确认事实。",
        "- 对于缺失的 Agent 结果、来源或指标，后续需要重新运行对应研究任务补充。",
    ]
    return "\n".join(items)


def _bullets_or_missing(lines: list[str], prefix: str | None = None) -> str:
    if lines:
        return ((prefix + "\n") if prefix else "") + "\n".join(lines)
    return MISSING


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _readable_financial_value(value: Any, unit: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        unit_text = str(unit or "")
        if unit_text in {"千元", "CNY thousand", "RMB thousand"}:
            return f"{number:,.0f}千元（约{number / 100_000:,.2f}亿元）"
        return f"{number:,.2f}{unit_text}"
    return f"{value}{unit or ''}"


def _unique_agent_ids(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text and text not in output:
            output.append(text)
    return output
