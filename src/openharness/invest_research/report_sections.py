"""Section-by-section report writing with bounded context and resumable artifacts."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openharness.invest_research.contracts import (
    ReportResult,
    ReportSectionRequest,
    ReportSectionResult,
)
from openharness.invest_research.report_delivery import run_output_dir
from openharness.invest_research.runtime_adapter import (
    AgentExecutionResult,
    UsageRecord,
)


MODEL_SECTION_IDS = (
    "company_overview",
    "operating_changes",
    "investment_logics",
    "peer_comparison",
    "catalysts",
    "risks",
)
ALL_SECTION_IDS = (*MODEL_SECTION_IDS, "tracking_indicators", "limitations")
SECTION_BATCHES = (
    ("company_overview", "operating_changes", "peer_comparison"),
    ("investment_logics", "catalysts", "risks"),
)

SECTION_TITLES = {
    "company_overview": "一、公司概况",
    "operating_changes": "二、最近一年经营变化",
    "investment_logics": "三、三个最值得关注的投资逻辑",
    "peer_comparison": "四、两家主要竞争对手对比",
    "catalysts": "五、未来半年可能的催化因素",
    "risks": "六、主要风险与证伪条件",
    "tracking_indicators": "七、后续跟踪指标",
    "limitations": "八、研究限制与合规声明",
}

SECTION_OBJECTIVES = {
    "company_overview": (
        "说明公司身份、主营业务、业务结构、主要产品和行业位置。只陈述材料包可追溯的事实，"
        "不写投资判断。"
    ),
    "operating_changes": (
        "分析最近一年收入、利润、毛利率、现金流和业务结构变化，按‘变化结论—事实—原因解释—"
        "限制’组织，不能把推测写成事实。"
    ),
    "investment_logics": (
        "撰写恰好三条投资逻辑。每条说明逻辑结论、支持事实、当前关注原因、后续验证指标、"
        "失效条件和审查状态，并引用对应 L-ID 与 F-ID。"
    ),
    "peer_comparison": (
        "比较目标公司与两家竞品的业务、规模、盈利、技术、客户和优劣势。口径不一致或材料"
        "不足时必须明确标记不可比，不得强行得出结论。"
    ),
    "catalysts": (
        "按未来半年时间线整理催化，区分已公告事实、公开报道、市场预期和推测，说明时间窗口、"
        "影响路径、方向和失效信号。"
    ),
    "risks": (
        "说明主要风险、影响路径、触发条件、证伪指标和受影响逻辑。保留反方证据，不能把一般"
        "不确定性描述为必然风险。"
    ),
}

SECTION_CONTENT_LIMITS = {
    "company_overview": 1_200,
    "operating_changes": 1_800,
    "investment_logics": 2_200,
    "peer_comparison": 1_800,
    "catalysts": 1_400,
    "risks": 1_800,
}

_ID_PATTERN = re.compile(
    r"^(?:PC|S|F|L|CAT|RISK|ART|REVIEW|ISSUE|REPORT)-[A-Za-z0-9][A-Za-z0-9_-]*$"
)


class SectionContextError(ValueError):
    """Raised when a report section cannot receive a safe bounded context."""


class SectionContextBuilder:
    """Select only the reviewed material needed by one report chapter."""

    def __init__(
        self,
        report_context: dict[str, Any],
        *,
        default_max_chars: int = 16_000,
        logic_max_chars: int = 24_000,
    ) -> None:
        self._context = dict(report_context)
        self._default_max_chars = default_max_chars
        self._logic_max_chars = logic_max_chars

    def build(self, section_id: str) -> dict[str, Any]:
        if section_id not in MODEL_SECTION_IDS:
            raise SectionContextError(f"unknown model report section: {section_id}")

        common = {
            "context_version": "1.0",
            "run_id": self._context.get("run_id"),
            "source_policy": self._context.get("source_policy"),
            "delivery": self._context.get("delivery") or {},
            "company": self._context.get("company") or {},
            "research_period": self._context.get("research_period"),
            "catalyst_window": self._context.get("catalyst_window"),
            "competitors": self._context.get("competitors") or [],
        }
        material_by_section = {
            "company_overview": {
                "company": self._context.get("company") or {},
                "operating_facts": (self._context.get("operating_changes") or [])[:6],
            },
            "operating_changes": {
                "operating_changes": self._context.get("operating_changes") or [],
                "review": self._context.get("review") or {},
            },
            "investment_logics": {
                "investment_logics": self._context.get("investment_logics") or [],
                "risks": (self._context.get("risks") or [])[:6],
                "review": self._context.get("review") or {},
            },
            "peer_comparison": {
                "peer_comparison": self._context.get("peer_comparison") or [],
                "peer_comparison_limitations": self._context.get(
                    "peer_comparison_limitations"
                )
                or [],
                "review": self._context.get("review") or {},
            },
            "catalysts": {
                "catalysts": self._context.get("catalysts") or [],
                "review": self._context.get("review") or {},
            },
            "risks": {
                "risks": self._context.get("risks") or [],
                "investment_logics": self._context.get("investment_logics") or [],
                "review": self._context.get("review") or {},
            },
        }
        payload = {
            **common,
            "section_id": section_id,
            "section_title": SECTION_TITLES[section_id],
            "writing_objective": SECTION_OBJECTIVES[section_id],
            "section_material": material_by_section[section_id],
        }

        referenced_source_ids = _collect_ids(payload, prefixes=("S-",))
        sources = [
            item
            for item in self._context.get("sources") or []
            if isinstance(item, dict)
            and str(item.get("source_id") or "") in referenced_source_ids
        ]
        payload["sources"] = sources
        payload["allowed_evidence_ids"] = sorted(_collect_ids(payload))
        max_chars = (
            self._logic_max_chars
            if section_id == "investment_logics"
            else self._default_max_chars
        )
        compacted = _compact_to_limit(payload, max_chars=max_chars)
        compacted["allowed_evidence_ids"] = sorted(_collect_ids(compacted))
        if not compacted["allowed_evidence_ids"]:
            compacted["context_warning"] = (
                "本章节暂无可引用的正式编号，只能返回 partial 并披露资料缺口。"
            )
        return compacted

    def build_all(self) -> dict[str, dict[str, Any]]:
        return {section_id: self.build(section_id) for section_id in MODEL_SECTION_IDS}


@dataclass
class SectionExecutionRecord:
    section_id: str
    result: ReportSectionResult
    execution_status: str
    artifact_id: str
    attempts: int
    duration_seconds: float
    usage: UsageRecord = field(default_factory=UsageRecord)
    model: str | None = None
    error: str | None = None
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "result": self.result.model_dump(mode="json"),
            "execution_status": self.execution_status,
            "artifact_id": self.artifact_id,
            "attempts": self.attempts,
            "duration_seconds": round(self.duration_seconds, 3),
            "usage": self.usage.model_dump(mode="json"),
            "model": self.model,
            "error": self.error,
            "reused": self.reused,
        }


@dataclass
class SectionReportOutcome:
    report_payload: dict[str, Any]
    section_records: dict[str, SectionExecutionRecord]
    total_usage: UsageRecord
    all_model_sections_failed: bool


class SectionReportOrchestrator:
    """Execute the existing ReportWriter once per chapter and assemble the report."""

    def __init__(
        self,
        *,
        gateway: Any,
        project_root: Path,
        run_id: str,
        report_context: dict[str, Any],
        input_base: dict[str, Any],
        event_callback: Callable[[str, str, str], None] | None = None,
        reuse_partial: bool = True,
    ) -> None:
        self.gateway = gateway
        self.project_root = project_root
        self.run_id = run_id
        self.report_context = report_context
        self.input_base = dict(input_base)
        self.event_callback = event_callback
        self.reuse_partial = reuse_partial
        self.contexts = SectionContextBuilder(report_context).build_all()

    async def run(self) -> SectionReportOutcome:
        records: dict[str, SectionExecutionRecord] = {}
        for batch in SECTION_BATCHES:
            batch_records = await asyncio.gather(
                *(self._run_or_reuse(section_id) for section_id in batch)
            )
            records.update({item.section_id: item for item in batch_records})

        tracking = _tracking_section(self.report_context)
        limitations = _limitations_section(self.report_context, records)
        records[tracking.section_id] = _program_record(self.run_id, tracking)
        records[limitations.section_id] = _program_record(self.run_id, limitations)
        _write_section_record(
            self.project_root,
            self.run_id,
            records[tracking.section_id],
        )
        _write_section_record(
            self.project_root,
            self.run_id,
            records[limitations.section_id],
        )

        payload = ReportAssembler(self.report_context).assemble(records)
        total_usage = UsageRecord(
            input_tokens=sum(
                item.usage.input_tokens for item in records.values() if not item.reused
            ),
            output_tokens=sum(
                item.usage.output_tokens for item in records.values() if not item.reused
            ),
            total_tokens=sum(
                item.usage.total_tokens for item in records.values() if not item.reused
            ),
        )
        self._emit("report_assembled", "report", "八个章节已完成确定性组装。")
        return SectionReportOutcome(
            report_payload=payload,
            section_records=records,
            total_usage=total_usage,
            all_model_sections_failed=all(
                records[section_id].execution_status == "failed"
                for section_id in MODEL_SECTION_IDS
            ),
        )

    async def _run_or_reuse(self, section_id: str) -> SectionExecutionRecord:
        cached = _load_section_record(
            self.project_root,
            self.run_id,
            section_id,
            reuse_partial=self.reuse_partial,
        )
        if cached is not None:
            cached.reused = True
            self._emit(
                "report_section_completed",
                section_id,
                f"复用已保存章节：{SECTION_TITLES[section_id]}。",
            )
            return cached
        return await self._execute_section(section_id)

    async def _execute_section(self, section_id: str) -> SectionExecutionRecord:
        context = self.contexts[section_id]
        allowed_ids = list(context.get("allowed_evidence_ids") or [])
        last_result: AgentExecutionResult | None = None
        total_usage = UsageRecord()
        started = time.monotonic()
        for attempt in (1, 2):
            event_type = "report_section_started" if attempt == 1 else "report_section_retrying"
            self._emit(
                event_type,
                section_id,
                f"{'开始' if attempt == 1 else '重试'}生成章节：{SECTION_TITLES[section_id]}。",
            )
            payload = {
                **self.input_base,
                "task_id": _section_task_id(section_id, attempt),
                "objective": SECTION_OBJECTIVES[section_id],
                "section_id": section_id,
                "allowed_evidence_ids": allowed_ids,
                "attempt": attempt,
            }
            ReportSectionRequest.model_validate(payload)
            last_result = await self.gateway.execute(
                agent_id="report_writer",
                input_payload=payload,
                task_prompt=_section_task_prompt(section_id, allowed_ids, attempt),
                context_package={"report_section_context": context},
                max_turns=2,
                # deepseek-v4-flash may consume several thousand reasoning
                # tokens before emitting the JSON.  A 6k cap caused otherwise
                # valid real section runs to end as empty assistant messages.
                max_output_tokens=12_000,
                tool_call_limits={},
                timeout_seconds=180,
                retry_transient=False,
                output_contract="report_section",
                persist_output=False,
            )
            total_usage = UsageRecord(
                input_tokens=total_usage.input_tokens + last_result.usage.input_tokens,
                output_tokens=total_usage.output_tokens + last_result.usage.output_tokens,
                total_tokens=total_usage.total_tokens + last_result.usage.total_tokens,
            )
            if last_result.status == "succeeded" and last_result.structured_output:
                try:
                    section = _validate_section_output(
                        section_id=section_id,
                        payload=last_result.structured_output,
                        context=context,
                        selected_logic_ids=self.input_base.get("selected_logic_ids") or [],
                    )
                except ValueError as exc:
                    last_result.error = str(exc)
                else:
                    record = SectionExecutionRecord(
                        section_id=section_id,
                        result=section,
                        execution_status=(
                            "completed" if section.status == "completed" else "partial"
                        ),
                        artifact_id=_section_artifact_id(self.run_id, section_id),
                        attempts=attempt,
                        duration_seconds=time.monotonic() - started,
                        usage=total_usage,
                        model=last_result.model,
                        error=None,
                    )
                    _write_section_record(self.project_root, self.run_id, record)
                    self._emit(
                        "report_section_completed"
                        if record.execution_status == "completed"
                        else "report_section_partial",
                        section_id,
                        f"章节已{('完成' if record.execution_status == 'completed' else '带缺口完成')}："
                        f"{SECTION_TITLES[section_id]}。",
                    )
                    return record

        error = (
            last_result.error
            if last_result is not None and last_result.error
            else "章节在两次有限尝试后仍未生成有效输出。"
        )
        placeholder = ReportSectionResult(
            status="partial",
            completed_scope=[],
            evidence_refs=[],
            unverified_items=[
                {
                    "item": SECTION_TITLES[section_id],
                    "reason": error,
                    "required_evidence": "重新运行当前章节或人工补充。",
                }
            ],
            limitations=[error],
            handoff_requests=[],
            blocking_reasons=[],
            section_id=section_id,
            title=SECTION_TITLES[section_id],
            content="当前章节未能生成足够可靠内容，待后续单独重试或人工补充。",
            evidence_ids=[],
            logic_ids=[],
            warnings=[error],
        )
        record = SectionExecutionRecord(
            section_id=section_id,
            result=placeholder,
            execution_status="failed",
            artifact_id=_section_artifact_id(self.run_id, section_id),
            attempts=2,
            duration_seconds=time.monotonic() - started,
            usage=total_usage,
            model=last_result.model if last_result is not None else None,
            error=error,
        )
        _write_section_record(self.project_root, self.run_id, record)
        self._emit(
            "report_section_failed",
            section_id,
            f"章节生成失败，已保留占位和错误回执：{SECTION_TITLES[section_id]}。",
        )
        return record

    def _emit(self, event_type: str, section_id: str, message: str) -> None:
        if self.event_callback is not None:
            self.event_callback(event_type, section_id, message)


class ReportAssembler:
    """Create one backwards-compatible ReportResult from independent chapters."""

    def __init__(self, report_context: dict[str, Any]) -> None:
        self.context = report_context

    def assemble(self, records: dict[str, SectionExecutionRecord]) -> dict[str, Any]:
        missing = [section_id for section_id in ALL_SECTION_IDS if section_id not in records]
        if missing:
            raise ValueError("missing report sections: " + ", ".join(missing))
        company = self.context.get("company") or {}
        research_period = self.context.get("research_period") or {}
        catalyst_window = self.context.get("catalyst_window") or {}
        selected_logic_ids = [
            str(item.get("logic_id"))
            for item in self.context.get("investment_logics") or []
            if item.get("logic_id")
        ][:3]
        failed = [
            section_id
            for section_id in MODEL_SECTION_IDS
            if records[section_id].execution_status == "failed"
        ]
        partial = [
            section_id
            for section_id in MODEL_SECTION_IDS
            if records[section_id].execution_status == "partial"
        ]
        status = "completed" if not failed and not partial else "partial"
        title_company = (
            company.get("short_name")
            or company.get("legal_name")
            or "目标上市公司"
        )
        sections = [
            {
                "section_id": section_id,
                "title": records[section_id].result.title,
                "content": records[section_id].result.content,
                "evidence_ids": records[section_id].result.evidence_ids,
            }
            for section_id in ALL_SECTION_IDS
        ]
        payload = {
            "protocol_version": "1.0",
            "status": status,
            "completed_scope": [
                section_id
                for section_id in ALL_SECTION_IDS
                if records[section_id].execution_status != "failed"
            ],
            "evidence_refs": sorted(
                {
                    evidence_id
                    for record in records.values()
                    for evidence_id in record.result.evidence_ids
                }
            ),
            "unverified_items": [
                item.model_dump(mode="json")
                for record in records.values()
                for item in record.result.unverified_items
            ],
            "limitations": [
                limitation
                for record in records.values()
                for limitation in record.result.limitations
            ],
            "handoff_requests": [],
            "blocking_reasons": [],
            "title": f"{title_company}上市公司研究报告",
            "metadata": {
                "company_name": title_company,
                "ticker": str(company.get("ticker") or "待确认"),
                "as_of_date": research_period.get("end_date"),
                "research_period": research_period,
                "catalyst_window": catalyst_window,
                "protocol_version": "1.0",
            },
            "sections": sections,
            "section_evidence_map": {
                section_id: records[section_id].result.evidence_ids
                for section_id in ALL_SECTION_IDS
            },
            "included_logic_ids": selected_logic_ids,
            "tracking_indicators": _tracking_items(self.context),
            "compliance_statement": (
                "本报告由多 Agent 协作流程基于公开资料生成，部分内容仍需人工核验，"
                "不构成投资建议、目标价或交易指令。"
            ),
            "report_blockers": [
                {
                    "section_id": section_id,
                    "problem": records[section_id].error or "章节未完整生成",
                    "required_record_ids": [],
                }
                for section_id in failed
            ],
        }
        return ReportResult.model_validate(payload).model_dump(mode="json")


def _section_task_prompt(section_id: str, allowed_ids: list[str], attempt: int) -> str:
    retry_note = (
        "这是当前章节的第二次且最后一次尝试。只修正上一轮的格式或内容缺口。"
        if attempt == 2
        else "这是当前章节的第一次写作。"
    )
    return (
        f"你只负责报告章节 {section_id}（{SECTION_TITLES[section_id]}），不得输出其他章节。"
        f"{SECTION_OBJECTIVES[section_id]}"
        "写作顺序固定为：本章结论、事实与编号、原因或影响、反方信息或不确定性、后续验证方法。"
        f"正文控制在 {SECTION_CONTENT_LIMITS[section_id]} 个中文字符以内，突出结论和证据，避免重复材料。"
        "不得调用工具，不得搜索，不得创建事实、来源、逻辑或风险编号。"
        f"只允许引用这些编号：{', '.join(allowed_ids) if allowed_ids else '无'}。"
        "证据不足时返回 status=partial，并在 unverified_items 和 limitations 中说明；"
        "最终只返回符合 ReportSectionResult 的一个 JSON 对象。"
        f"{retry_note}"
    )


def _validate_section_output(
    *,
    section_id: str,
    payload: dict[str, Any],
    context: dict[str, Any],
    selected_logic_ids: list[str],
) -> ReportSectionResult:
    result = ReportSectionResult.model_validate(payload)
    if result.section_id != section_id:
        raise ValueError(
            f"section_id mismatch: expected {section_id}, got {result.section_id}"
        )
    allowed = set(context.get("allowed_evidence_ids") or [])
    referenced = set(result.evidence_ids) | set(result.logic_ids) | set(
        result.evidence_refs
    )
    unauthorized = sorted(referenced - allowed)
    if unauthorized:
        raise ValueError("section referenced unauthorized IDs: " + ", ".join(unauthorized))
    if section_id == "investment_logics" and result.status == "completed":
        if set(result.logic_ids) != set(selected_logic_ids):
            raise ValueError(
                "investment_logics must use exactly the DeliveryDecision logic IDs"
            )
    if section_id == "peer_comparison" and result.status == "completed":
        competitor_names = [
            str(item.get("company_name") or item.get("name") or "").strip()
            for item in context.get("competitors") or []
            if isinstance(item, dict)
        ][:2]
        missing_names = [name for name in competitor_names if name and name not in result.content]
        if missing_names:
            result.status = "partial"
            result.warnings.append(
                "竞品章节未明确覆盖全部两家竞品：" + "、".join(missing_names)
            )
            result.limitations.append("竞品覆盖不完整，需人工复核。")
    return result


def _tracking_items(context: dict[str, Any]) -> list[str]:
    items: list[str] = []
    for logic in context.get("investment_logics") or []:
        if not isinstance(logic, dict):
            continue
        items.extend(str(item) for item in logic.get("tracking_indicators") or [] if item)
        items.extend(
            f"证伪观察：{item}"
            for item in logic.get("falsification_conditions") or []
            if item
        )
    for risk in context.get("risks") or []:
        if not isinstance(risk, dict):
            continue
        items.extend(str(item) for item in risk.get("monitoring_plan") or [] if item)
        items.extend(
            f"风险证伪：{item}"
            for item in risk.get("falsification_indicators") or []
            if item
        )
    for catalyst in context.get("catalysts") or []:
        if not isinstance(catalyst, dict):
            continue
        items.extend(
            f"催化跟踪：{item}" for item in catalyst.get("affected_metrics") or [] if item
        )
    return list(dict.fromkeys(items))[:16]


def _tracking_section(context: dict[str, Any]) -> ReportSectionResult:
    items = _tracking_items(context)
    content = (
        "\n".join(f"- {item}" for item in items)
        if items
        else "当前材料尚未形成可稳定追踪的指标，建议后续结合定期报告补充。"
    )
    evidence_ids = sorted(
        _collect_ids(
            {
                "investment_logics": context.get("investment_logics") or [],
                "risks": context.get("risks") or [],
                "catalysts": context.get("catalysts") or [],
            }
        )
    )
    return ReportSectionResult(
        status="completed" if items else "partial",
        completed_scope=["tracking_indicators"] if items else [],
        evidence_refs=evidence_ids,
        unverified_items=[] if items else [
            {
                "item": "后续跟踪指标",
                "reason": "当前逻辑、风险和催化记录中缺少明确指标。",
                "required_evidence": "后续定期报告和经营数据。",
            }
        ],
        limitations=[] if items else ["跟踪指标有待补充。"],
        handoff_requests=[],
        blocking_reasons=[],
        section_id="tracking_indicators",
        title=SECTION_TITLES["tracking_indicators"],
        content=content,
        evidence_ids=evidence_ids,
        logic_ids=[],
        warnings=[],
    )


def _limitations_section(
    context: dict[str, Any], records: dict[str, SectionExecutionRecord]
) -> ReportSectionResult:
    review = context.get("review") or {}
    delivery = context.get("delivery") or {}
    items: list[str] = []
    items.extend(str(item) for item in delivery.get("warnings") or [] if item)
    items.extend(str(item) for item in review.get("limitations") or [] if item)
    for item in review.get("unverified_items") or []:
        if isinstance(item, dict):
            description = str(item.get("item") or "待验证事项")
            reason = str(item.get("reason") or "原因未说明")
            required = str(item.get("required_evidence") or "后续补充可靠来源")
            items.append(f"{description}：{reason}；需补充：{required}")
        elif item:
            items.append(str(item))
    items.extend(str(item) for item in context.get("peer_comparison_limitations") or [] if item)
    for issue in review.get("issues") or []:
        if isinstance(issue, dict):
            statement = issue.get("problem_statement")
            if statement:
                items.append(str(statement))
    for section_id, record in records.items():
        if record.execution_status == "failed":
            items.append(f"{SECTION_TITLES[section_id]}生成失败：{record.error or '原因未记录'}")
        elif record.execution_status == "partial":
            items.append(f"{SECTION_TITLES[section_id]}为带缺口交付，需人工复核。")
    if delivery.get("delivery_mode") == "provisional":
        items.append("三条投资逻辑由系统按可追溯证据暂定，尚待人工复核。")
    items = list(dict.fromkeys(items))
    content_lines = [f"- {item}" for item in items] or ["- 当前未记录额外研究限制。"]
    content_lines.extend(
        [
            "",
            "本报告由多 Agent 协作流程基于公开资料生成，部分内容仍需人工核验。",
            "本报告不构成投资建议、目标价或交易指令。",
        ]
    )
    evidence_ids = sorted(_collect_ids({"review": review, "delivery": delivery}))
    return ReportSectionResult(
        status="completed" if evidence_ids else "partial",
        completed_scope=["limitations"] if evidence_ids else [],
        evidence_refs=evidence_ids,
        unverified_items=[],
        limitations=items,
        handoff_requests=[],
        blocking_reasons=[],
        section_id="limitations",
        title=SECTION_TITLES["limitations"],
        content="\n".join(content_lines),
        evidence_ids=evidence_ids,
        logic_ids=[],
        warnings=[],
    )


def _program_record(run_id: str, result: ReportSectionResult) -> SectionExecutionRecord:
    section_id = str(result.section_id)
    return SectionExecutionRecord(
        section_id=section_id,
        result=result,
        execution_status="completed" if result.status == "completed" else "partial",
        artifact_id=_section_artifact_id(run_id, section_id),
        attempts=0,
        duration_seconds=0.0,
        model="deterministic_backend",
    )


def _write_section_record(
    project_root: Path, run_id: str, record: SectionExecutionRecord
) -> None:
    directory = run_output_dir(project_root, run_id) / "sections"
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{record.section_id}.json"
    md_path = directory / f"{record.section_id}.md"
    json_path.write_text(
        json.dumps(record.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    md_path.write_text(
        f"# {record.result.title}\n\n{record.result.content}\n\n"
        + (
            "证据索引："
            + "、".join(f"`{item}`" for item in record.result.evidence_ids)
            + "\n"
            if record.result.evidence_ids
            else ""
        ),
        encoding="utf-8-sig",
    )


def _load_section_record(
    project_root: Path,
    run_id: str,
    section_id: str,
    *,
    reuse_partial: bool,
) -> SectionExecutionRecord | None:
    path = run_output_dir(project_root, run_id) / "sections" / f"{section_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        result = ReportSectionResult.model_validate(payload["result"])
        execution_status = str(payload.get("execution_status") or "")
        if execution_status == "completed" or (
            reuse_partial and execution_status == "partial"
        ):
            return SectionExecutionRecord(
                section_id=section_id,
                result=result,
                execution_status=execution_status,
                artifact_id=str(payload.get("artifact_id") or _section_artifact_id(run_id, section_id)),
                attempts=int(payload.get("attempts") or 0),
                duration_seconds=float(payload.get("duration_seconds") or 0.0),
                usage=UsageRecord.model_validate(payload.get("usage") or {}),
                model=payload.get("model"),
                error=payload.get("error"),
                reused=True,
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _section_task_id(section_id: str, attempt: int) -> str:
    return f"TASK-REPORT-SECTION-{section_id.upper().replace('_', '-')}-{attempt:02d}"


def _section_artifact_id(run_id: str, section_id: str) -> str:
    run_suffix = re.sub(r"[^A-Za-z0-9]", "", run_id)[-10:] or "UNKNOWN"
    section = section_id.upper().replace("_", "-")
    return f"ART-SECTION-{run_suffix}-{section}"


def _collect_ids(value: Any, prefixes: tuple[str, ...] | None = None) -> set[str]:
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and _ID_PATTERN.fullmatch(item):
            if prefixes is None or item.startswith(prefixes):
                found.add(item)

    visit(value)
    return found


def _compact_to_limit(payload: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    compacted = _truncate_strings(payload, limit=1_600)
    if len(json.dumps(compacted, ensure_ascii=False)) <= max_chars:
        return compacted
    for list_limit in (8, 6, 4, 3):
        compacted = _truncate_lists(compacted, limit=list_limit)
        if len(json.dumps(compacted, ensure_ascii=False)) <= max_chars:
            return compacted
    compacted = _truncate_strings(compacted, limit=600)
    if len(json.dumps(compacted, ensure_ascii=False)) > max_chars:
        raise SectionContextError(
            f"section context still exceeds {max_chars} characters after compaction"
        )
    return compacted


def _truncate_strings(value: Any, *, limit: int) -> Any:
    if isinstance(value, dict):
        return {key: _truncate_strings(child, limit=limit) for key, child in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(child, limit=limit) for child in value]
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value


def _truncate_lists(value: Any, *, limit: int) -> Any:
    if isinstance(value, dict):
        return {key: _truncate_lists(child, limit=limit) for key, child in value.items()}
    if isinstance(value, list):
        return [_truncate_lists(child, limit=limit) for child in value[:limit]]
    return value


__all__ = [
    "ALL_SECTION_IDS",
    "MODEL_SECTION_IDS",
    "ReportAssembler",
    "SectionContextBuilder",
    "SectionContextError",
    "SectionExecutionRecord",
    "SectionReportOrchestrator",
    "SectionReportOutcome",
]
