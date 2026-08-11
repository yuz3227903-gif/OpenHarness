"""Investment-research Agent definitions and structured contracts.

This package contains the seven business definitions and their contracts.
All seven roles share a restricted runtime adapter, run-scoped evidence store,
role-specific tool whitelist, and Pydantic input/output contracts.
"""

from openharness.invest_research.agent_registry import (
    AGENT_REGISTRY,
    AgentRegistryEntry,
    get_agent_entry,
    iter_agent_entries,
    resolve_runtime_agent_name,
)
from openharness.invest_research.contracts import INPUT_MODELS, OUTPUT_MODELS
from openharness.invest_research.prompt_assembler import (
    PromptAssembler,
    PromptAssemblyError,
    PromptLayers,
    assemble_prompt,
    render_output_contract,
)
from openharness.invest_research.runtime_adapter import (
    AgentExecutionRequest,
    AgentExecutionResult,
    InvestmentResearchRuntimeAdapter,
    PlannerRuntimeAdapter,
    RuntimePreflight,
)

__all__ = [
    "AGENT_REGISTRY",
    "INPUT_MODELS",
    "OUTPUT_MODELS",
    "AgentRegistryEntry",
    "AgentExecutionRequest",
    "AgentExecutionResult",
    "InvestmentResearchRuntimeAdapter",
    "PromptAssembler",
    "PromptAssemblyError",
    "PromptLayers",
    "PlannerRuntimeAdapter",
    "RuntimePreflight",
    "assemble_prompt",
    "get_agent_entry",
    "iter_agent_entries",
    "render_output_contract",
    "resolve_runtime_agent_name",
]
