from __future__ import annotations

import asyncio
import json
import unittest

from pydantic import BaseModel

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import Settings
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.invest_research.runtime_adapter import (
    AgentExecutionRequest,
    PlannerRuntimeAdapter,
)
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class _NoopInput(BaseModel):
    query: str = "unused"


class _NoopTavilyTool(BaseTool):
    name = "tavily_search"
    description = "Offline test replacement"
    input_model = _NoopInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        del arguments, context
        return ToolResult(output="unused")

    def is_read_only(self, arguments: BaseModel) -> bool:
        del arguments
        return True


class _StaticApiClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.closed = False

    async def stream_message(self, request):
        self.last_request = request
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant",
                content=[TextBlock(text=self.text)],
            ),
            usage=UsageSnapshot(input_tokens=20, output_tokens=10),
            stop_reason=None,
        )

    async def close(self) -> None:
        self.closed = True


def _partial_planner_json() -> str:
    return json.dumps(
        {
            "protocol_version": "1.0",
            "status": "partial",
            "completed_scope": ["运行适配验证"],
            "evidence_refs": [],
            "unverified_items": [],
            "limitations": ["离线单元测试未执行外部搜索"],
            "handoff_requests": [],
            "blocking_reasons": [],
            "competitor_candidates": [],
            "recommended_competitors": [],
            "task_plan": [],
            "dependency_graph": {},
            "available_materials": [],
            "material_gaps": [],
        },
        ensure_ascii=False,
    )


def _planner_input() -> dict:
    return {
        "protocol_version": "1.0",
        "run_id": "RUN-ADAPTER-TEST-001",
        "task_id": "TASK-ADAPTER-TEST-001",
        "objective": "验证 Planner 运行适配器",
        "agent_id": "planner",
        "company_query": "示例上市公司",
        "as_of_date": "2026-08-11",
    }


class RuntimeAdapterTests(unittest.TestCase):
    def make_adapter(self, fake_client: _StaticApiClient) -> PlannerRuntimeAdapter:
        return PlannerRuntimeAdapter(
            settings_loader=lambda: Settings(),
            api_client_factory=lambda settings: fake_client,
            tool_overrides={"tavily_search": _NoopTavilyTool()},
            require_search_configuration=False,
        )

    def test_restricted_registry_contains_exactly_planner_tools(self):
        adapter = self.make_adapter(_StaticApiClient(_partial_planner_json()))
        registry = adapter.build_restricted_tool_registry()

        self.assertEqual(
            [tool.name for tool in registry.list_tools()],
            ["tavily_search", "web_fetch"],
        )
        self.assertIsNone(registry.get("agent"))
        self.assertIsNone(registry.get("bash"))
        self.assertIsNone(registry.get("write_file"))

    def test_non_planner_agent_is_rejected_before_execution(self):
        fake_client = _StaticApiClient(_partial_planner_json())
        adapter = self.make_adapter(fake_client)
        request = AgentExecutionRequest(
            agent_id="fundamental",
            input_payload=_planner_input(),
        )

        with self.assertRaisesRegex(ValueError, "only permits agent_id='planner'"):
            asyncio.run(adapter.execute_agent(request))
        self.assertFalse(fake_client.closed)

    def test_offline_query_engine_output_is_validated_as_planner_result(self):
        fake_client = _StaticApiClient(_partial_planner_json())
        adapter = self.make_adapter(fake_client)
        request = AgentExecutionRequest(
            agent_id="planner",
            input_payload=_planner_input(),
        )

        result = asyncio.run(adapter.execute_agent(request))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.structured_output["status"], "partial")
        self.assertEqual(result.usage.total_tokens, 30)
        self.assertTrue(fake_client.closed)
        self.assertEqual(result.tool_calls, [])

    def test_agent_request_can_override_provider_model(self):
        fake_client = _StaticApiClient(_partial_planner_json())
        adapter = self.make_adapter(fake_client)
        request = AgentExecutionRequest(
            agent_id="planner",
            input_payload=_planner_input(),
            model_override="doubao-seed-2.1-turbo",
        )

        result = asyncio.run(adapter.execute_agent(request))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.model, "doubao-seed-2.1-turbo")
        self.assertEqual(fake_client.last_request.model, "doubao-seed-2.1-turbo")


if __name__ == "__main__":
    unittest.main()
