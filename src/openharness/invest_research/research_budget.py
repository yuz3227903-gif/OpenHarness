"""Central runtime budgets for the investment-research workflow."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentBudget:
    """Finite model and tool limits for one workflow role."""

    max_turns: int
    max_output_tokens: int
    tool_call_limits: dict[str, int]


class ResearchBudgetPolicy:
    """Single source of truth for the balanced production-like profile."""

    tavily_max_results = 3
    tavily_snippet_chars = 800
    web_fetch_max_chars = 6_000
    token_warning_threshold = 500_000

    _BUDGETS: dict[str, AgentBudget] = {
        "planner": AgentBudget(
            max_turns=6,
            max_output_tokens=6_000,
            # Planner must identify two competitors before it can hand work to
            # the parallel research stage.  One extra search is cheaper than
            # aborting an otherwise usable full-chain run.
            tool_call_limits={"tavily_search": 4, "web_fetch": 3},
        ),
        "fundamental": AgentBudget(
            max_turns=8,
            max_output_tokens=8_000,
            tool_call_limits={
                "tavily_search": 2,
                "web_fetch": 2,
                "calculator": 1,
                "evidence_query": 1,
            },
        ),
        "industry_competition": AgentBudget(
            max_turns=8,
            max_output_tokens=8_000,
            tool_call_limits={
                "tavily_search": 3,
                "web_fetch": 3,
                "calculator": 2,
                "evidence_query": 1,
            },
        ),
        "market_catalyst": AgentBudget(
            max_turns=7,
            max_output_tokens=7_000,
            tool_call_limits={
                "tavily_search": 3,
                "web_fetch": 3,
                "calculator": 1,
                "evidence_query": 1,
            },
        ),
        "risk": AgentBudget(
            max_turns=3,
            max_output_tokens=6_000,
            tool_call_limits={
                "evidence_query": 0,
                "tavily_search": 0,
                "web_fetch": 0,
                "calculator": 0,
            },
        ),
        "reviewer_initial": AgentBudget(
            max_turns=4,
            max_output_tokens=6_000,
            tool_call_limits={"evidence_query": 2},
        ),
        "reviewer_final": AgentBudget(
            max_turns=3,
            max_output_tokens=6_000,
            tool_call_limits={"evidence_query": 1},
        ),
        "report_writer": AgentBudget(
            max_turns=2,
            max_output_tokens=12_000,
            tool_call_limits={},
        ),
    }

    def for_agent(self, agent_id: str, *, stage: str | None = None) -> AgentBudget:
        key = agent_id
        if agent_id == "reviewer_arbiter":
            key = "reviewer_final" if stage == "final" else "reviewer_initial"
        try:
            budget = self._BUDGETS[key]
        except KeyError as exc:
            raise KeyError(f"No research budget configured for {agent_id!r}") from exc
        return AgentBudget(
            max_turns=budget.max_turns,
            max_output_tokens=budget.max_output_tokens,
            tool_call_limits=dict(budget.tool_call_limits),
        )


DEFAULT_RESEARCH_BUDGET_POLICY = ResearchBudgetPolicy()


__all__ = [
    "AgentBudget",
    "DEFAULT_RESEARCH_BUDGET_POLICY",
    "ResearchBudgetPolicy",
]
