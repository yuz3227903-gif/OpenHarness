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


class TestDeletingAFile:
    def test_a_deleted_file_is_detached_from_its_message(self, store):
        record = store.add_file(
            channel_id="research-room", message_id=None,
            owner_id="owner", owner_type="human", source="upload",
            filename="memo.txt", stored_name="research-room/memo.txt",
            media_type="text/plain", size_bytes=12,
        )
        # The message carries a copy of its attachments so the timeline can
        # render without a second query; a stale copy would show a download
        # that 404s.
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="看这个",
            metadata={"attachments": [
                {"file_id": record["file_id"], "filename": "memo.txt"},
                {"file_id": "F-OTHER", "filename": "keep.txt"},
            ]},
        )
        store.add_file(
            file_id=record["file_id"], channel_id="research-room",
            message_id=message["message_id"], owner_id="owner", owner_type="human",
            source="upload", filename="memo.txt", stored_name="research-room/memo.txt",
            media_type="text/plain", size_bytes=12,
        )

        removed = store.delete_file(record["file_id"])
        assert removed["filename"] == "memo.txt"
        assert store.get_file(record["file_id"]) is None
        refreshed = next(
            item for item in store.list_messages("research-room")
            if item["message_id"] == message["message_id"]
        )
        # Only the deleted one goes; the other attachment stays put.
        assert [item["file_id"] for item in refreshed["metadata"]["attachments"]] == ["F-OTHER"]

    def test_deleting_an_unknown_file_is_not_an_error(self, store):
        assert store.delete_file("F-NOPE") is None

    def test_attaching_an_uploaded_file_records_which_message_it_belongs_to(self, store):
        """The file is uploaded before the message exists, so the link arrives late."""

        uploaded = store.add_file(
            channel_id="research-room", message_id=None, owner_id="owner",
            owner_type="human", source="upload", filename="memo.txt",
            stored_name="research-room/memo.txt", media_type="text/plain", size_bytes=12,
        )
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="看这个",
        )
        store.add_file(
            file_id=uploaded["file_id"], channel_id="research-room",
            message_id=message["message_id"], owner_id="owner", owner_type="human",
            source="upload", filename="memo.txt", stored_name="research-room/memo.txt",
            media_type="text/plain", size_bytes=12,
        )
        assert store.get_file(uploaded["file_id"])["message_id"] == message["message_id"]

    def test_a_later_resync_does_not_clear_the_link(self, store):
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="看这个",
        )
        record = store.add_file(
            channel_id="research-room", message_id=message["message_id"], owner_id="owner",
            owner_type="human", source="upload", filename="memo.txt",
            stored_name="research-room/memo.txt", media_type="text/plain", size_bytes=12,
        )
        store.add_file(
            file_id=record["file_id"], channel_id="research-room", owner_id="owner",
            owner_type="human", source="upload", filename="memo.txt",
            stored_name="research-room/memo.txt", media_type="text/plain", size_bytes=30,
        )
        assert store.get_file(record["file_id"])["message_id"] == message["message_id"]

    def test_a_file_detaches_even_without_a_recorded_link(self, store):
        """Rows written before the link existed must still clean up."""

        record = store.add_file(
            channel_id="research-room", message_id=None, owner_id="owner",
            owner_type="human", source="upload", filename="memo.txt",
            stored_name="research-room/memo.txt", media_type="text/plain", size_bytes=12,
        )
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="看这个",
            metadata={"attachments": [{"file_id": record["file_id"], "filename": "memo.txt"}]},
        )
        store.delete_file(record["file_id"])
        refreshed = next(
            item for item in store.list_messages("research-room")
            if item["message_id"] == message["message_id"]
        )
        assert refreshed["metadata"]["attachments"] == []

    def test_a_file_with_no_message_can_still_be_deleted(self, store):
        record = store.add_file(
            channel_id="research-room", message_id=None, owner_id="system",
            owner_type="system", source="agent_report", filename="report.md",
            stored_name="research-room/report.md", media_type="text/markdown",
            size_bytes=40,
        )
        assert store.delete_file(record["file_id"]) is not None
        assert store.get_file(record["file_id"]) is None


class TestDirectMessagesCarryAttachments:
    def test_an_attachment_is_recorded_on_the_direct_message(self, store, monkeypatch):
        """Uploading in a DM used to leave the file orphaned in the channel."""

        monkeypatch.setattr(server, "STORE", store)
        store.ensure_direct_channel("risk", "Risk")
        uploaded = store.add_file(
            channel_id="dm-risk", message_id=None, owner_id="owner", owner_type="human",
            source="upload", filename="纪要.txt", stored_name="dm-risk/纪要.txt",
            media_type="text/plain", size_bytes=20,
        )
        resolved = server._channel_attachments("dm-risk", [uploaded["file_id"]])
        assert [item["filename"] for item in resolved] == ["纪要.txt"]

    def test_a_file_from_another_channel_is_not_accepted(self, store, monkeypatch):
        monkeypatch.setattr(server, "STORE", store)
        elsewhere = store.add_file(
            channel_id="research-room", message_id=None, owner_id="owner",
            owner_type="human", source="upload", filename="别的.txt",
            stored_name="research-room/别的.txt", media_type="text/plain", size_bytes=10,
        )
        assert server._channel_attachments("dm-risk", [elsewhere["file_id"]]) == []


class TestSkillsReachTheChatTurn:
    def test_installed_skills_are_attached_to_the_persona(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "STORE", store)
        monkeypatch.setattr(
            server, "render_skills_prompt", lambda *a, **k: "", raising=False,
        )
        agent = next(item for item in store.list_agents() if item["agent_id"] == "risk")
        assert "skills_prompt" in server._agent_with_skills(agent)

    def test_an_invoked_skill_leads_the_persona(self, store, monkeypatch):
        monkeypatch.setattr(server, "STORE", store)
        agent = next(item for item in store.list_agents() if item["agent_id"] == "risk")
        prepared = server._agent_with_skills(agent, invoked="调用了技能 X")
        # The called Skill comes first: this turn is about that Skill.
        assert prepared["skills_prompt"].startswith("调用了技能 X")

    def test_the_speaker_carries_the_skill_text_into_the_prompt(self, store):
        from openharness.invest_research.workbench_chat_model import ChatTurn
        from openharness.invest_research.workbench_group_chat import GroupDiscussion

        seen = []
        pool = ArkKeyPool(["a"])

        def record(**kwargs):
            seen.append(kwargs["messages"][0]["content"])
            return ChatTurn(text="好", model="m")

        GroupDiscussion(store=store, pool=pool, complete=record, rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="M",
            participants=[{"agent_id": "risk", "name": "顾谨", "role": "风险",
                           "system_prompt": "你是风险", "model": "m",
                           "skills_prompt": "## SKILL: 现金流审查\n四步法"}],
        )
        assert "现金流审查" in seen[0]


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
