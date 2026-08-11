"""Single source of truth for investment-research Agent identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from openharness.invest_research.contracts import INPUT_MODELS, OUTPUT_MODELS


PLUGIN_NAME = "investment-research"
ToolImplementationStatus = Literal[
    "available_in_openharness",
    "available_in_plugin",
    "pending_implementation",
]
EvidenceQueryScope = Literal[
    "none",
    "authorized_records",
    "run_records_read_only",
    "approved_records_only",
]

COMMON_DISALLOWED_TOOLS = (
    "agent",
    "task_create",
    "task_update",
    "task_stop",
    "task_get",
    "task_list",
    "task_output",
    "send_message",
    "team_create",
    "team_delete",
    "bash",
    "write_file",
    "edit_file",
    "notebook_edit",
    "config",
    "enter_worktree",
    "exit_worktree",
    "cron_create",
    "cron_toggle",
    "cron_delete",
    "remote_trigger",
)

EXTERNAL_RESEARCH_TOOLS = (
    "tavily_search",
    "web_fetch",
    "read_uploaded_file",
    "calculator",
)

TOOL_IMPLEMENTATION_STATUS: dict[str, ToolImplementationStatus] = {
    "web_fetch": "available_in_openharness",
    "tavily_search": "available_in_plugin",
    "read_uploaded_file": "available_in_plugin",
    "calculator": "available_in_plugin",
    "evidence_query": "available_in_plugin",
}


@dataclass(frozen=True)
class AgentRegistryEntry:
    """Stable business identity plus OpenHarness runtime metadata."""

    agent_id: str
    display_name: str
    role_title_zh: str
    runtime_agent_name: str
    output_contract_name: str
    max_turns: int
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    evidence_query_scope: EvidenceQueryScope
    model: str = "inherit"
    runtime_status: Literal["definition_only", "validation_ready"] = "definition_only"

    @property
    def tool_statuses(self) -> dict[str, ToolImplementationStatus]:
        return {name: TOOL_IMPLEMENTATION_STATUS[name] for name in self.allowed_tools}

    @property
    def pending_tools(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, status in self.tool_statuses.items()
            if status == "pending_implementation"
        )


def _entry(
    agent_id: str,
    display_name: str,
    role_title_zh: str,
    max_turns: int,
    allowed_tools: tuple[str, ...],
    evidence_query_scope: EvidenceQueryScope,
    extra_disallowed_tools: tuple[str, ...] = (),
    runtime_status: Literal["definition_only", "validation_ready"] = "definition_only",
) -> AgentRegistryEntry:
    return AgentRegistryEntry(
        agent_id=agent_id,
        display_name=display_name,
        role_title_zh=role_title_zh,
        runtime_agent_name=f"{PLUGIN_NAME}:{agent_id}",
        output_contract_name=OUTPUT_MODELS[agent_id].__name__,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        disallowed_tools=COMMON_DISALLOWED_TOOLS + extra_disallowed_tools,
        input_model=INPUT_MODELS[agent_id],
        output_model=OUTPUT_MODELS[agent_id],
        evidence_query_scope=evidence_query_scope,
        runtime_status=runtime_status,
    )


_FULL_RESEARCH_TOOLS = (
    "tavily_search",
    "web_fetch",
    "read_uploaded_file",
    "calculator",
    "evidence_query",
)

AGENT_REGISTRY: dict[str, AgentRegistryEntry] = {
    "planner": _entry(
        "planner",
        "Planner",
        "主研究员／项目负责人",
        8,
        ("tavily_search", "web_fetch"),
        "none",
        runtime_status="validation_ready",
    ),
    "fundamental": _entry(
        "fundamental",
        "Fundamental",
        "公司基本面／财务分析师",
        16,
        _FULL_RESEARCH_TOOLS,
        "authorized_records",
        runtime_status="validation_ready",
    ),
    "industry_competition": _entry(
        "industry_competition",
        "IndustryCompetition",
        "行业与竞争研究员",
        16,
        _FULL_RESEARCH_TOOLS,
        "authorized_records",
        runtime_status="validation_ready",
    ),
    "market_catalyst": _entry(
        "market_catalyst",
        "MarketCatalyst",
        "市场信息与事件研究员",
        12,
        _FULL_RESEARCH_TOOLS,
        "authorized_records",
        runtime_status="validation_ready",
    ),
    "risk": _entry(
        "risk",
        "Risk",
        "独立风险与反方研究员",
        12,
        ("evidence_query", "tavily_search", "web_fetch", "read_uploaded_file", "calculator"),
        "authorized_records",
        runtime_status="validation_ready",
    ),
    "reviewer_arbiter": _entry(
        "reviewer_arbiter",
        "ReviewerArbiter",
        "研究审核与仲裁负责人",
        10,
        ("evidence_query",),
        "run_records_read_only",
        EXTERNAL_RESEARCH_TOOLS,
        runtime_status="validation_ready",
    ),
    "report_writer": _entry(
        "report_writer",
        "ReportWriter",
        "研报撰写与编辑人员",
        8,
        ("evidence_query",),
        "approved_records_only",
        EXTERNAL_RESEARCH_TOOLS,
        runtime_status="validation_ready",
    ),
}


def get_agent_entry(agent_id: str) -> AgentRegistryEntry:
    """Return one registry entry or raise a clear error."""

    try:
        return AGENT_REGISTRY[agent_id]
    except KeyError as exc:
        raise ValueError(f"Unknown investment-research agent_id: {agent_id}") from exc


def iter_agent_entries() -> tuple[AgentRegistryEntry, ...]:
    """Return entries in deterministic workflow order."""

    return tuple(AGENT_REGISTRY.values())


def resolve_runtime_agent_name(agent_id: str) -> str:
    """Map a permanent business ID to the namespaced OpenHarness ID."""

    return get_agent_entry(agent_id).runtime_agent_name
