import asyncio
import os
import unittest

from openharness.api.client import ApiMessageRequest
from openharness.config.settings import Settings
from openharness.invest_research.ark_key_pool import ArkKeyPool, ArkKeyUnavailable
from openharness.invest_research.runtime_adapter import InvestmentResearchRuntimeAdapter


class _DelayedClient:
    def __init__(
        self,
        key: str,
        state: dict[str, int | list[str]],
        *,
        wait_event: asyncio.Event | None = None,
        started_event: asyncio.Event | None = None,
    ) -> None:
        self.key = key
        self.state = state
        self.wait_event = wait_event
        self.started_event = started_event

    async def stream_message(self, request):
        del request
        self.state["active"] = int(self.state["active"]) + 1
        self.state["max_active"] = max(
            int(self.state["max_active"]), int(self.state["active"])
        )
        if self.started_event is not None:
            self.started_event.set()
        try:
            if self.wait_event is not None:
                await self.wait_event.wait()
            else:
                await asyncio.sleep(0.05)
            yield self.key
        finally:
            self.state["active"] = int(self.state["active"]) - 1

    async def close(self) -> None:
        return None


class RuntimeAdapterKeyPoolTests(unittest.TestCase):
    def _client_settings(self) -> Settings:
        return Settings(
            provider="volcengine",
            api_format="openai",
            model="deepseek-v4-flash",
            base_url="https://example.invalid/api/v3",
        )

    def test_concurrent_model_turns_use_distinct_pool_keys_and_reuse_after_release(self):
        keys = ["key-one", "key-two", "key-three"]
        state: dict[str, int | list[str]] = {
            "active": 0,
            "max_active": 0,
            "seen": [],
        }

        def factory(settings):
            key = settings.api_key
            assert key in keys
            state["seen"].append(key)
            return _DelayedClient(key, state)

        adapter = InvestmentResearchRuntimeAdapter(
            api_client_factory=factory,
            ark_key_pool=ArkKeyPool(keys),
            key_lease_timeout_seconds=0.5,
            require_search_configuration=False,
        )
        client = adapter._create_api_client(self._client_settings())
        request = ApiMessageRequest(model="deepseek-v4-flash", messages=[])

        async def consume():
            async def one_turn():
                return [event async for event in client.stream_message(request)]

            return await asyncio.gather(*(one_turn() for _ in range(4)))

        results = asyncio.run(consume())
        seen = state["seen"]
        self.assertEqual(len(results), 4)
        self.assertEqual(len(seen), 4)
        self.assertEqual(set(seen[:3]), set(keys))
        self.assertLessEqual(int(state["max_active"]), 3)
        self.assertEqual(int(state["active"]), 0)

    def test_pool_wait_has_a_bound_and_does_not_block_the_event_loop(self):
        pool = ArkKeyPool(["only-key"])
        release = asyncio.Event()
        started = asyncio.Event()
        state: dict[str, int | list[str]] = {"active": 0, "max_active": 0, "seen": []}

        def factory(settings):
            return _DelayedClient(
                settings.api_key,
                state,
                wait_event=release,
                started_event=started,
            )

        adapter = InvestmentResearchRuntimeAdapter(
            api_client_factory=factory,
            ark_key_pool=pool,
            key_lease_timeout_seconds=1.0,
            require_search_configuration=False,
        )
        client = adapter._create_api_client(self._client_settings())
        request = ApiMessageRequest(model="deepseek-v4-flash", messages=[])

        async def consume():
            first = asyncio.create_task(
                self._consume_client(client, request)
            )
            await asyncio.wait_for(started.wait(), timeout=1.0)
            with self.assertRaises(ArkKeyUnavailable):
                await self._consume_client(client, request)
            release.set()
            await first

        asyncio.run(consume())
        self.assertEqual(int(state["active"]), 0)

    async def _consume_client(self, client, request):
        return [event async for event in client.stream_message(request)]

    def test_pool_lease_does_not_mutate_process_global_api_key(self):
        previous = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "process-value"
        try:
            adapter = InvestmentResearchRuntimeAdapter(
                api_client_factory=lambda settings: _DelayedClient(
                    settings.api_key,
                    {"active": 0, "max_active": 0, "seen": []},
                ),
                ark_key_pool=ArkKeyPool(["per-call-key"]),
                key_lease_timeout_seconds=0.5,
                require_search_configuration=False,
            )
            client = adapter._create_api_client(self._client_settings())
            request = ApiMessageRequest(model="deepseek-v4-flash", messages=[])
            asyncio.run(self._consume_client(client, request))
            self.assertEqual(os.environ["OPENAI_API_KEY"], "process-value")
        finally:
            if previous is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
