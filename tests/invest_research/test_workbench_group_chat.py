"""Several Agents discussing one topic in one channel."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openharness.invest_research.ark_key_pool import ArkKeyPool  # noqa: E402
from openharness.invest_research.collaboration_store import CollaborationStore  # noqa: E402
from openharness.invest_research.workbench_chat_model import ChatModelError, ChatTurn  # noqa: E402
from openharness.invest_research.workbench_group_chat import (  # noqa: E402
    GroupDiscussion,
    Speaker,
    parse_mentions,
)


def agent(agent_id, name=None, **extra):
    return {
        "agent_id": agent_id, "name": name or agent_id.title(), "role": "研究员",
        "system_prompt": f"你是{agent_id}", "model": "deepseek-v4-flash", **extra,
    }


def scripted(replies, *, delay=0.0, recorder=None):
    """A model stand-in that returns a queued reply per agent."""

    lock = threading.Lock()

    def complete(*, messages, model, api_key, key_label="", **_kwargs):
        if delay:
            time.sleep(delay)
        with lock:
            if recorder is not None:
                recorder.append({"api_key": api_key, "key_label": key_label,
                                 "system": messages[0]["content"], "user": messages[1]["content"]})
            text = replies.pop(0) if replies else "（无话可说）"
        return ChatTurn(text=text, model=model, key_label=key_label)

    return complete


@pytest.fixture
def store(tmp_path):
    return CollaborationStore(tmp_path)


@pytest.fixture
def pool():
    return ArkKeyPool(["key-a", "key-b", "key-c"])


class TestMentionParsing:
    def test_a_known_agent_is_recognised(self):
        assert parse_mentions("这点请 @risk 复核", ["risk", "fundamental"]) == ["risk"]

    def test_an_unknown_handle_is_ignored(self):
        # "@团队" is talking, not assigning; inventing a participant from a typo
        # would be worse than missing one.
        assert parse_mentions("@团队 我们再看看 @nobody", ["risk"]) == []

    def test_case_does_not_matter_but_the_real_id_is_returned(self):
        assert parse_mentions("@Risk 看一下", ["risk"]) == ["risk"]

    def test_the_same_mention_twice_is_one_target(self):
        assert parse_mentions("@risk 和 @risk", ["risk"]) == ["risk"]

    def test_several_targets_keep_their_order(self):
        assert parse_mentions("@a 再 @b", ["a", "b"]) == ["a", "b"]


class TestSpeakerResolution:
    def test_a_speaker_carries_its_persona_and_model(self):
        speaker = Speaker.from_agent(agent("risk", "顾谨", model="glm-5.2"))
        assert (speaker.name, speaker.model) == ("顾谨", "glm-5.2")
        assert speaker.persona == "你是risk"

    def test_a_missing_model_falls_back_to_the_default(self):
        assert Speaker.from_agent({"agent_id": "x"}).model


class TestRunningADiscussion:
    def test_everyone_in_the_room_replies(self, store, pool):
        discussion = GroupDiscussion(
            store=store, pool=pool, complete=scripted(["一", "二", "三"]), rounds=1,
        )
        topic = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="message", body="这个标的怎么看？",
        )
        outcome = discussion.run(
            channel_id="research-room", topic="这个标的怎么看？",
            topic_message_id=topic["message_id"],
            participants=[agent("a"), agent("b"), agent("c")],
        )
        assert len(outcome.replies) == 3
        bodies = [item["body"] for item in store.list_messages("research-room")]
        assert {"一", "二", "三"} <= set(bodies)

    def test_replies_are_posted_into_the_channel_as_the_agent(self, store, pool):
        discussion = GroupDiscussion(
            store=store, pool=pool, complete=scripted(["我的看法"]), rounds=1,
        )
        discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("risk", "顾谨")],
        )
        posted = [m for m in store.list_messages("research-room") if m["author_id"] == "risk"]
        assert len(posted) == 1
        assert posted[0]["author_type"] == "agent"
        assert posted[0]["metadata"]["discussion"] is True

    def test_at_least_two_agents_are_mid_call_at_once(self, store, pool):
        """The room talks, it does not take polite turns."""

        overlap = []
        active = []
        lock = threading.Lock()

        def slow(*, messages, model, api_key, key_label="", **_kwargs):
            with lock:
                active.append(api_key)
                overlap.append(len(active))
            time.sleep(0.25)
            with lock:
                active.remove(api_key)
            return ChatTurn(text="观点", model=model, key_label=key_label)

        discussion = GroupDiscussion(store=store, pool=pool, complete=slow, rounds=1)
        outcome = discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("a"), agent("b"), agent("c")],
        )
        assert max(overlap) >= 2, f"没有并行发言：{overlap}"
        assert outcome.max_concurrent >= 2

    def test_concurrent_speakers_use_different_keys(self, store, pool):
        seen = []
        lock = threading.Lock()

        def record(*, messages, model, api_key, key_label="", **_kwargs):
            with lock:
                seen.append(api_key)
            time.sleep(0.2)
            return ChatTurn(text="观点", model=model, key_label=key_label)

        GroupDiscussion(store=store, pool=pool, complete=record, rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("a"), agent("b"), agent("c")],
        )
        assert sorted(seen) == ["key-a", "key-b", "key-c"]

    def test_a_fourth_agent_waits_for_a_key(self, store, pool):
        """Three keys is the cap, whoever else is in the room."""

        live = []
        peak = []
        lock = threading.Lock()

        def slow(*, messages, model, api_key, key_label="", **_kwargs):
            with lock:
                live.append(api_key)
                peak.append(len(live))
            time.sleep(0.15)
            with lock:
                live.remove(api_key)
            return ChatTurn(text="观点", model=model, key_label=key_label)

        GroupDiscussion(store=store, pool=pool, complete=slow, rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent(name) for name in ("a", "b", "c", "d", "e")],
        )
        assert max(peak) <= 3, f"超过了 Key 数量的并发：{peak}"

    def test_which_key_spoke_is_recorded_but_never_the_key(self, store, pool):
        GroupDiscussion(store=store, pool=pool, complete=scripted(["观点"]), rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("a")],
        )
        message = [m for m in store.list_messages("research-room") if m["author_id"] == "a"][0]
        assert message["metadata"]["ark_key"].startswith("key#")
        assert "key-a" not in str(message["metadata"])


class TestHandoffs:
    def test_an_at_mention_creates_a_task_for_the_named_agent(self, store, pool):
        discussion = GroupDiscussion(
            store=store, pool=pool, complete=scripted(["@risk 这条口径请你复核", "收到"]),
            rounds=2,
        )
        outcome = discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("fundamental"), agent("risk")],
        )
        assert outcome.handoffs, "没有产生任务派发"
        handoff = outcome.handoffs[0]
        assert (handoff["from_agent"], handoff["to_agent"]) == ("fundamental", "risk")
        tasks = store.list_tasks("research-room")
        assert any(task["assignee_id"] == "risk" for task in tasks)

    def test_the_named_agent_answers_in_the_next_wave(self, store, pool):
        recorder = []
        discussion = GroupDiscussion(
            store=store, pool=pool,
            complete=scripted(["@risk 请你复核", "别的", "我来复核"], recorder=recorder),
            rounds=2,
        )
        discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("fundamental"), agent("risk")],
        )
        # The last prompt tells the named Agent who asked, so it answers the
        # request rather than restating its opening position.
        assert any("点名要你回应" in item["user"] for item in recorder)

    def test_answering_a_handoff_closes_its_task(self, store, pool):
        """Otherwise the board fills with tasks that never move."""

        discussion = GroupDiscussion(
            store=store, pool=pool, complete=scripted(["@risk 这条口径请你复核", "我复核过了"]),
            rounds=2,
        )
        outcome = discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("fundamental"), agent("risk")],
        )
        task_id = outcome.handoffs[0]["task_id"]
        task = store.get_task(task_id)
        assert task["status"] == "completed"
        assert task["metadata"]["answered_in_discussion"] is True
        assert task["metadata"]["reply_message_id"]

    def test_an_unanswered_handoff_says_so_instead_of_queueing_forever(self, store, pool):
        # One round only: nobody gets a turn to answer. No worker picks these
        # up, so leaving it queued would be a task that never runs.
        discussion = GroupDiscussion(
            store=store, pool=pool, complete=scripted(["@risk 请你复核"]), rounds=1,
        )
        outcome = discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("fundamental"), agent("risk")],
        )
        assert outcome.unanswered == [outcome.handoffs[0]["task_id"]]
        task = store.get_task(outcome.handoffs[0]["task_id"])
        assert task["status"] == "blocked"
        assert "没轮到" in task["metadata"]["reason"] or "结束" in task["metadata"]["reason"]

    def test_no_handoff_is_left_queued_after_a_discussion(self, store, pool):
        discussion = GroupDiscussion(
            store=store, pool=pool,
            complete=scripted(["@risk 看一下", "@fundamental 你也看一下", "好"]), rounds=2,
        )
        discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("fundamental"), agent("risk")],
        )
        statuses = {task["status"] for task in store.list_tasks("research-room")}
        assert "queued" not in statuses, statuses

    def test_mentioning_yourself_is_not_a_handoff(self, store, pool):
        discussion = GroupDiscussion(
            store=store, pool=pool, complete=scripted(["@risk 我自己来"]), rounds=1,
        )
        outcome = discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("risk")],
        )
        assert outcome.handoffs == []

    def test_mentioning_someone_outside_the_room_is_not_a_handoff(self, store, pool):
        discussion = GroupDiscussion(
            store=store, pool=pool, complete=scripted(["@outsider 帮个忙"]), rounds=1,
        )
        outcome = discussion.run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("risk")],
        )
        assert outcome.handoffs == []
        assert store.list_tasks("research-room") == []


class TestFailuresAreVisible:
    def test_one_broken_speaker_does_not_end_the_discussion(self, store, pool):
        def flaky(*, messages, model, api_key, key_label="", **_kwargs):
            # Keyed off the persona line, not the roster: every speaker is told
            # about every other member, so a looser match would fail all three.
            if "你是b" in messages[0]["content"]:
                raise ChatModelError("模型返回 HTTP 429：too many requests", kind="rate_limit")
            return ChatTurn(text="观点", model=model, key_label=key_label)

        outcome = GroupDiscussion(store=store, pool=pool, complete=flaky, rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("a"), agent("b"), agent("c")],
        )
        assert len(outcome.replies) == 2
        assert [item["kind"] for item in outcome.failures] == ["rate_limit"]

    def test_a_failed_turn_says_so_in_the_channel(self, store, pool):
        def broken(**_kwargs):
            raise ChatModelError("模型返回了空回复。", kind="empty_response")

        GroupDiscussion(store=store, pool=pool, complete=broken, rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("a", "阿尔法")],
        )
        bodies = [m["body"] for m in store.list_messages("research-room")]
        # Silence would read as "the Agent had nothing to say".
        assert any("没能发言" in body and "空回复" in body for body in bodies)

    def test_an_unexpected_error_is_contained_too(self, store, pool):
        def exploding(**_kwargs):
            raise ZeroDivisionError("boom")

        outcome = GroupDiscussion(store=store, pool=pool, complete=exploding, rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("a"), agent("b")],
        )
        assert len(outcome.failures) == 2
        assert outcome.replies == []


class TestSessionIsolation:
    """A channel discussion and a 1:1 conversation are different sessions."""

    def test_a_channel_speaker_never_sees_the_direct_conversation(self, store, pool):
        store.ensure_direct_channel("risk", "Risk")
        store.add_message(
            channel_id="dm-risk", author_id="owner", author_type="human",
            message_kind="message", body="私信里的机密数字是 42",
        )
        recorder = []
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["观点"], recorder=recorder), rounds=1,
        ).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("risk")],
        )
        assert "42" not in recorder[0]["user"]

    def test_a_direct_conversation_never_sees_the_channel_discussion(self, store, pool):
        store.ensure_direct_channel("risk", "Risk")
        store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="message", body="频道里讨论的是 A 股标的",
        )
        recorder = []
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["好的"], recorder=recorder), rounds=1,
        ).run(
            channel_id="dm-risk", topic="单独问你一句", topic_message_id="MSG-2",
            participants=[agent("risk")],
        )
        assert "A 股标的" not in recorder[0]["user"]

    def test_two_channels_do_not_bleed_into_each_other(self, store, pool):
        store.create_channel(
            name="other-room", topic="别的公司", description="", member_ids=["risk"],
        )
        store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="message", body="研究室的话题",
        )
        recorder = []
        other = [c for c in store.list_channels() if c["name"] == "other-room"][0]
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["观点"], recorder=recorder), rounds=1,
        ).run(
            channel_id=other["channel_id"], topic="另一个话题", topic_message_id="MSG-3",
            participants=[agent("risk")],
        )
        assert "研究室的话题" not in recorder[0]["user"]


class TestAttachedDocuments:
    """A file handed over is only a message if the Agent sees inside it."""

    def test_the_document_text_reaches_the_model(self, store, pool):
        recorder = []
        store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="看看这份纪要",
            metadata={"attachments": [{"file_id": "F1", "filename": "纪要.txt"}]},
        )
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["读过了"], recorder=recorder), rounds=1,
            render_attachments=lambda items: "【附件：纪要.txt】\n毛利率下滑 3 个百分点",
        ).run(
            channel_id="research-room", topic="看看这份纪要", topic_message_id="MSG-1",
            participants=[agent("risk")],
        )
        assert "毛利率下滑 3 个百分点" in recorder[0]["user"]

    def test_a_file_with_no_words_is_still_a_topic(self, store, pool):
        recorder = []
        store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="",
            metadata={"attachments": [{"file_id": "F1", "filename": "年报.txt"}]},
        )
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["读过了"], recorder=recorder), rounds=1,
            render_attachments=lambda items: "【附件：年报.txt】\n全年营收 200 亿",
        ).run(
            channel_id="research-room", topic="", topic_message_id="MSG-1",
            participants=[agent("risk")],
        )
        assert "全年营收 200 亿" in recorder[0]["user"]
        # An empty headline would read as a topic nobody set.
        assert "用户发来的文件" in recorder[0]["user"]

    def test_a_message_without_attachments_is_unchanged(self, store, pool):
        recorder = []
        store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="纯文字",
        )
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["好"], recorder=recorder), rounds=1,
            render_attachments=lambda items: "不应该被调用",
        ).run(
            channel_id="research-room", topic="纯文字", topic_message_id="MSG-1",
            participants=[agent("risk")],
        )
        assert "不应该被调用" not in recorder[0]["user"]


class TestPromptShape:
    def test_a_lone_speaker_is_not_given_a_roster(self, store, pool):
        recorder = []
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["观点"], recorder=recorder), rounds=1,
        ).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("risk", "顾谨")],
        )
        # There is nobody to @, so listing an empty roster would only invite a
        # made-up handle.
        assert "其他成员" not in recorder[0]["system"]
        assert "@" not in recorder[0]["system"]

    def test_the_roster_lists_the_other_members(self, store, pool):
        recorder = []
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["一", "二"], recorder=recorder), rounds=1,
        ).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1",
            participants=[agent("risk", "顾谨"), agent("fundamental", "陈实")],
        )
        systems = " ".join(item["system"] for item in recorder)
        assert "@fundamental" in systems and "@risk" in systems

    def test_the_topic_reaches_the_model(self, store, pool):
        recorder = []
        GroupDiscussion(
            store=store, pool=pool, complete=scripted(["观点"], recorder=recorder), rounds=1,
        ).run(
            channel_id="research-room", topic="科大讯飞值得买吗", topic_message_id="MSG-1",
            participants=[agent("risk")],
        )
        assert "科大讯飞值得买吗" in recorder[0]["user"]

    def test_an_empty_room_is_not_an_error(self, store, pool):
        outcome = GroupDiscussion(store=store, pool=pool, complete=scripted([]), rounds=1).run(
            channel_id="research-room", topic="话题", topic_message_id="MSG-1", participants=[],
        )
        assert (outcome.replies, outcome.rounds) == ([], 0)
