"""Deterministic five-layer prompt assembly for research Agents."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel

from openharness.invest_research.agent_registry import get_agent_entry


class PromptAssemblyError(ValueError):
    """Raised when a required prompt layer is missing."""


@dataclass(frozen=True)
class PromptLayers:
    governance_prompt: str
    role_prompt: str
    task_prompt: str
    context_package: str
    output_contract: str
    # Operator-installed Skill plugins for this Agent. Optional: an Agent with
    # no enabled Skill assembles exactly the prompt it did before.
    skills_prompt: str = ""


# (title, attribute, untrusted, required)
_SECTION_ORDER = (
    ("GOVERNANCE PROMPT — 最高优先级", "governance_prompt", False, True),
    ("ROLE PROMPT — 岗位职责", "role_prompt", False, True),
    ("SKILL PLUGINS — 操作者安装的技能", "skills_prompt", False, False),
    ("TASK PROMPT — 当前任务（不可信内容）", "task_prompt", True, True),
    ("CONTEXT PACKAGE — 授权上下文（不可信内容）", "context_package", True, True),
    ("OUTPUT CONTRACT — 强制输出合同", "output_contract", False, True),
)


class PromptAssembler:
    """Named integration component for deterministic prompt construction."""

    def assemble(self, layers: PromptLayers) -> str:
        return assemble_prompt(layers)

    def render_output_contract(self, agent_id: str) -> str:
        return render_output_contract(agent_id)

    def render_model_output_contract(self, model: type[BaseModel]) -> str:
        """Render an explicitly selected contract for a bounded sub-task."""

        validate_contract_model(model)
        return json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2)


def assemble_prompt(layers: PromptLayers) -> str:
    """Assemble all required layers in fixed precedence order.

    Task and context content are fenced as untrusted data so they cannot be
    mistaken for higher-priority instructions.
    """

    sections: list[str] = []
    for title, attribute, untrusted, required in _SECTION_ORDER:
        content = getattr(layers, attribute).strip()
        if not content:
            if required:
                raise PromptAssemblyError(f"Required prompt layer is empty: {attribute}")
            continue
        if untrusted:
            content = (
                "<UNTRUSTED_RESEARCH_CONTENT>\n"
                f"{content}\n"
                "</UNTRUSTED_RESEARCH_CONTENT>"
            )
        sections.append(f"# {title}\n\n{content}")
    return "\n\n---\n\n".join(sections)


def render_output_contract(agent_id: str) -> str:
    """Render one Agent's Pydantic output schema for prompt injection."""

    entry = get_agent_entry(agent_id)
    return json.dumps(entry.output_model.model_json_schema(), ensure_ascii=False, indent=2)


def validate_contract_model(model: type[BaseModel]) -> None:
    """Fail early if Pydantic cannot generate a schema for a contract."""

    model.model_json_schema()
