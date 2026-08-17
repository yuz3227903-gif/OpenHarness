"""Which kind of message starts which kind of work."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openharness.invest_research import workbench_server as server  # noqa: E402
from openharness.invest_research.ark_key_pool import ArkKeyPool  # noqa: E402
from openharness.invest_research.collaboration_store import CollaborationStore  # noqa: E402
from openharness.invest_research.workbench_chat_model import ChatTurn  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = CollaborationStore(tmp_path)
    monkeypatch.setattr(server, "STORE", store)
    return store


@pytest.fixture
def quiet_pool(monkeypatch):
    """Three keys and a model that answers instantly."""

    monkeypatch.setattr(server, "_ark_key_pool", lambda: ArkKeyPool(["a", "b", "c"]))
    monkeypatch.setattr(
        server, "chat_complete",
        lambda **kwargs: ChatTurn(text="观点", model=kwargs.get("model", "m"),
                                  key_label=kwargs.get("key_label", "")),
    )


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestWhoIsInTheRoom:
    def test_a_channel_with_members_uses_them(self, store):
        channel = store.create_channel(
            name="小组", topic="t", description="", member_ids=["risk", "fundamental"],
        )
        chosen = {item["agent_id"] for item in server._discussion_participants(channel["channel_id"])}
        assert chosen == {"risk", "fundamental"}

    def test_a_channel_without_members_uses_the_whole_roster(self, store):
        chosen = server._discussion_participants("research-room")
        assert len(chosen) == len(store.list_agents())

    def test_a_removed_agent_is_not_in_the_room(self, store):
        store.set_builtin_agent_removed("risk", True)
        chosen = {item["agent_id"] for item in server._discussion_participants("research-room")}
        assert "risk" not in chosen

    def test_a_local_agent_sits_the_discussion_out(self, store):
        """This server has no standing to answer in a local CLI's name."""

        local = store.create_agent(
            name="My Codex", profile="p", role="r", system_prompt="", model="",
            agent_type="local", provider="codex", workspace=str(Path.cwd()),
        )
        chosen = {item["agent_id"] for item in server._discussion_participants("research-room")}
        assert local["agent_id"] not in chosen

    def test_the_research_default_is_swapped_for_a_chat_model(self, store):
        """ark-code-latest reasons until the budget is gone and answers nothing."""

        models = {item["agent_id"]: item["model"]
                  for item in server._discussion_participants("research-room")}
        assert set(models.values()) == {server.DEFAULT_CHAT_MODEL}

    def test_an_explicit_model_choice_is_respected(self, store):
        store.update_agent_model("risk", "glm-5.2")
        models = {item["agent_id"]: item["model"]
                  for item in server._discussion_participants("research-room")}
        assert models["risk"] == "glm-5.2"


class TestStartingADiscussion:
    def test_a_topic_starts_one_and_returns_immediately(self, store, quiet_pool):
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="这个标的怎么看？",
        )
        status, payload = server._start_group_discussion(
            channel_id="research-room", message=message,
            participants=[a for a in store.list_agents()][:2], rounds=1,
        )
        assert status == 202
        assert payload["status"] == "discussion_started"
        # The cap is the pool size, reported so the UI never has to guess.
        assert payload["concurrency"] == 3
        assert wait_for(lambda: any(
            item["author_type"] == "agent" for item in store.list_messages("research-room")
        ))

    def test_a_second_topic_is_refused_while_the_room_is_talking(self, store, monkeypatch):
        monkeypatch.setattr(server, "_ark_key_pool", lambda: ArkKeyPool(["a"]))
        release = threading.Event()

        def slow(**kwargs):
            release.wait(5)
            return ChatTurn(text="观点", model="m")

        monkeypatch.setattr(server, "chat_complete", slow)
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="话题一",
        )
        agents = store.list_agents()[:1]
        first, _ = server._start_group_discussion(
            channel_id="research-room", message=message, participants=agents, rounds=1,
        )
        assert first == 202
        status, payload = server._start_group_discussion(
            channel_id="research-room", message=message, participants=agents, rounds=1,
        )
        # Queuing it would answer a transcript that is already out of date.
        assert (status, payload["status"]) == (409, "already_discussing")
        release.set()

    def test_an_empty_room_is_refused_with_a_reason(self, store, quiet_pool):
        status, payload = server._start_group_discussion(
            channel_id="research-room", message={"message_id": "M", "body": "话题"},
            participants=[],
        )
        assert (status, payload["status"]) == (409, "empty_room")

    def test_no_credential_is_reported_not_hidden(self, store, monkeypatch):
        def unconfigured():
            raise server.NoArkKeysConfigured("没有配置任何 Ark API Key")

        monkeypatch.setattr(server, "_ark_key_pool", unconfigured)
        status, payload = server._start_group_discussion(
            channel_id="research-room", message={"message_id": "M", "body": "话题"},
            participants=store.list_agents()[:1],
        )
        assert status == 503
        assert payload["status"] == "not_configured"
        assert "Ark API Key" in payload["error"]

    def test_a_finished_discussion_announces_itself(self, store, quiet_pool):
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="话题",
        )
        server._start_group_discussion(
            channel_id="research-room", message=message,
            participants=store.list_agents()[:2], rounds=1,
        )
        assert wait_for(lambda: any(
            event["event_type"] == "discussion_finished"
            for event in store.events_since("research-room", after_seq=0)
        ))


class TestDirectMessagesStayASeparateSession:
    def test_a_direct_reply_reads_only_its_own_channel(self, store, monkeypatch):
        seen = []
        monkeypatch.setattr(server, "_ark_key_pool", lambda: ArkKeyPool(["a"]))

        def record(**kwargs):
            seen.append(kwargs["messages"][1]["content"])
            return ChatTurn(text="收到", model="m")

        monkeypatch.setattr(server, "chat_complete", record)
        store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="频道里在聊 A 股",
        )
        store.ensure_direct_channel("risk", "Risk")
        message = store.add_message(
            channel_id="dm-risk", author_id="owner", author_type="human",
            message_kind="user_message", body="只问你一句",
        )
        agent = next(item for item in store.list_agents() if item["agent_id"] == "risk")

        assert server._start_direct_agent_reply(
            channel_id="dm-risk", agent=agent, root_message_id=message["message_id"],
        ) is True
        assert wait_for(lambda: bool(seen))
        assert "只问你一句" in seen[0]
        assert "A 股" not in seen[0]

    def test_without_a_credential_it_says_so_instead_of_pretending(self, store, monkeypatch):
        def unconfigured():
            raise server.NoArkKeysConfigured("没有配置")

        monkeypatch.setattr(server, "_ark_key_pool", unconfigured)
        agent = next(item for item in store.list_agents() if item["agent_id"] == "risk")
        # False lets the caller fall back to the acknowledgement rather than
        # leaving the user with silence.
        assert server._start_direct_agent_reply(
            channel_id="dm-risk", agent=agent, root_message_id="M",
        ) is False
