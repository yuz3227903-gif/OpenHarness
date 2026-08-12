from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from openharness.invest_research.tavily_tool import (
    TAVILY_SEARCH_ENDPOINT,
    TavilySearchInput,
    TavilySearchTool,
)
from openharness.tools.base import ToolExecutionContext


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse, **kwargs) -> None:
        self.response = response
        self.kwargs = kwargs
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    async def post(self, url: str, *, headers: dict, json: dict):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.response


class TavilyToolTests(unittest.TestCase):
    def test_missing_key_fails_before_creating_an_http_client(self):
        def fail_factory(**kwargs):
            del kwargs
            raise AssertionError("HTTP client must not be created without a key")

        tool = TavilySearchTool(client_factory=fail_factory)
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False):
            result = asyncio.run(
                tool.execute(
                    TavilySearchInput(query="上市公司 官方公告"),
                    ToolExecutionContext(cwd=Path.cwd()),
                )
            )

        self.assertTrue(result.is_error)
        self.assertIn("not configured", result.output)
        self.assertNotIn("Authorization", result.output)

    def test_success_returns_only_filtered_search_fields(self):
        fake_response = _FakeResponse(
            200,
            {
                "request_id": "request-demo",
                "response_time": 0.12,
                "answer": "must not be forwarded",
                "results": [
                    {
                        "title": "示例公告",
                        "url": "https://example.com/disclosure",
                        "content": "公开资料摘要",
                        "raw_content": "must not be forwarded",
                        "score": 0.98,
                        "published_date": "2026-08-01",
                    }
                ],
            },
        )
        created: dict[str, _FakeClient] = {}

        def client_factory(**kwargs):
            fake_client = _FakeClient(fake_response, **kwargs)
            created["client"] = fake_client
            return fake_client

        tool = TavilySearchTool(client_factory=client_factory)
        fake_key = "tvly-unit-test-secret"

        with patch.dict(os.environ, {"TAVILY_API_KEY": fake_key}, clear=False):
            result = asyncio.run(
                tool.execute(
                    TavilySearchInput(
                        query="宁德时代 官方公告",
                        max_results=3,
                        include_domains=["cninfo.com.cn"],
                    ),
                    ToolExecutionContext(cwd=Path.cwd()),
                )
            )

        self.assertFalse(result.is_error)
        fake_client = created["client"]
        self.assertEqual(len(fake_client.requests), 1)
        request = fake_client.requests[0]
        self.assertFalse(created["client"].kwargs["trust_env"])
        self.assertEqual(request["url"], TAVILY_SEARCH_ENDPOINT)
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {fake_key}")
        self.assertFalse(request["json"]["include_answer"])
        self.assertFalse(request["json"]["include_raw_content"])

        output = json.loads(result.output)
        self.assertEqual(output["result_count"], 1)
        self.assertEqual(output["results"][0]["title"], "示例公告")
        self.assertNotIn("answer", output)
        self.assertNotIn("raw_content", output["results"][0])
        self.assertNotIn(fake_key, result.output)

    def test_invalid_date_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            TavilySearchInput(
                query="测试搜索",
                start_date="2026-08-11",
                end_date="2026-08-01",
            )


if __name__ == "__main__":
    unittest.main()
