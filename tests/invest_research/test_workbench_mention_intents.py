"""@ 一个 Agent 时它被要求做什么，以及在频道里怎么喊停。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openharness.invest_research import workbench_server as server  # noqa: E402
from openharness.invest_research.ark_key_pool import ArkKeyPool  # noqa: E402
from openharness.invest_research.collaboration_store import CollaborationStore  # noqa: E402
from openharness.invest_research.workbench_chat_model import ChatTurn  # noqa: E402
from openharness.invest_research.workbench_intents import (  # noqa: E402
    classify_mention,
    is_stop_command,
    looks_like_new_topic,
)


@pytest.fixture(autouse=True)
def _clean_registries(monkeypatch):
    monkeypatch.setattr(server, "DISCUSSION_THREADS", {})
    monkeypatch.setattr(server, "DISCUSSION_RUNNING", {})
    monkeypatch.setattr(server, "TASK_CANCELLATIONS", set())


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = CollaborationStore(tmp_path)
    monkeypatch.setattr(server, "STORE", store)
    return store


@pytest.fixture
def quiet_pool(monkeypatch):
    monkeypatch.setattr(server, "_ark_key_pool", lambda: ArkKeyPool(["a"]))
    monkeypatch.setattr(
        server, "chat_complete",
        lambda **kwargs: ChatTurn(text="回答", model=kwargs.get("model", "m")),
    )


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestReadingWhatWasAsked:
    def test_redo_is_recognised_however_it_is_phrased(self):
        for text in ("@risk 重新做一遍", "@risk 这个再跑一遍", "@risk redo", "@risk 重做"):
            assert classify_mention(text).intent == "redo", text

    def test_stopping_outranks_the_words_in_the_sentence(self):
        # "别再重新分析了" 同时带着重做和终止的词，说的是终止。
        assert classify_mention("@risk 停下，别再重新分析了").intent == "stop"

    def test_a_real_research_request_still_becomes_research(self):
        assert classify_mention("@fundamental 出一份毛利率的拆解").intent == "research"

    def test_a_question_about_a_report_is_not_a_new_report(self):
        # 这一条是这次修改的要点：问一句话不该换来一个四十秒的研究任务。
        assert classify_mention("@fundamental 这份报告你怎么看").intent == "reply"

    def test_anything_unrecognised_falls_back_to_conversation(self):
        intent = classify_mention("@risk 嗯")
        assert intent.intent == "reply"
        assert intent.is_conversational

    def test_the_at_sign_is_stripped_from_what_was_said(self):
        assert classify_mention("@risk 重新做一下").text == "重新做一下"


class TestStopCommand:
    def test_at_stop_is_recognised_in_chinese_and_english(self):
        for text in ("@终止", "@停止 一下", "@stop", "@全部终止"):
            assert is_stop_command(text), text

    def test_talking_about_stopping_is_not_a_stop_command(self):
        # @ 是这里的信号；聊到"终止条件"不该把频道停掉。
        assert not is_stop_command("我们讨论一下终止条件")

    def test_announcing_a_new_topic_is_recognised(self):
        assert looks_like_new_topic("我们换个课题，接下来研究储能")
        assert not looks_like_new_topic("这个课题再深一点")


class TestStoppingWorkInAChannel:
    def test_the_running_discussion_is_told_to_stop(self, store, quiet_pool):
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="话题",
        )
        server._start_group_discussion(
            channel_id="research-room", message=message,
            participants=store.list_agents()[:1], rounds=3,
        )
        discussion = server.DISCUSSION_RUNNING["research-room"]

        result = server._stop_channel_work("research-room", reason="测试")

        assert discussion.stopped
        assert result["status"] == "stopped"

    def test_queued_tasks_are_withdrawn(self, store, quiet_pool):
        task = store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="进行中的活儿", status="queued",
        )

        result = server._stop_channel_work("research-room", reason="测试")

        assert task["task_id"] in result["cancelled_task_ids"]
        assert store.get_task(task["task_id"])["status"] == "cancelling"

    def test_stopping_one_agent_leaves_the_others_working(self, store, quiet_pool):
        mine = store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="我的活儿", status="running",
        )
        theirs = store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="fundamental",
            title="别人的活儿", status="running",
        )

        result = server._stop_channel_work(
            "research-room", reason="测试", agent_ids=["risk"],
        )

        assert result["cancelled_task_ids"] == [mine["task_id"]]
        assert store.get_task(theirs["task_id"])["status"] == "running"

    def test_a_targeted_stop_does_not_end_the_rooms_discussion(self, store, quiet_pool):
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="话题",
        )
        server._start_group_discussion(
            channel_id="research-room", message=message,
            participants=store.list_agents()[:1], rounds=3,
        )
        discussion = server.DISCUSSION_RUNNING["research-room"]

        server._stop_channel_work("research-room", reason="测试", agent_ids=["risk"])

        # 讨论是整个频道的事，不属于被 @ 的那一个人。
        assert not discussion.stopped

    def test_the_room_is_told_what_happened(self, store, quiet_pool):
        store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="在跑的活儿", status="running",
        )

        server._stop_channel_work("research-room", reason="你要求停下")

        bodies = [item["body"] for item in store.list_messages("research-room")]
        assert any("已终止当前课题" in text for text in bodies)

    def test_clearing_a_quiet_channel_says_nothing(self, store, quiet_pool):
        # 换课题时顺手清场，本来就没在跑就别宣布"已终止"——那会让人以为自己
        # 刚打断了什么。
        result = server._stop_channel_work("research-room", reason="你提出了新的课题")

        assert result["notice"] is None
        bodies = [item["body"] for item in store.list_messages("research-room")]
        assert not any("已终止" in text for text in bodies)

    def test_an_explicit_stop_answers_even_when_nothing_was_running(self, store, quiet_pool):
        result = server._stop_channel_work(
            "research-room", reason="你要求停下", announce_idle=True,
        )

        assert result["notice"] is not None
        assert "没有正在进行" in result["notice"]["body"]


class TestRedoingASubtask:
    def test_the_agents_last_assignment_is_what_gets_redone(self, store):
        store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="旧的", status="done", metadata={"objective": "复核估值区间"},
        )

        assert server._latest_agent_objective("research-room", "risk") == "复核估值区间"

    def test_another_agents_work_is_not_picked_up(self, store):
        store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="fundamental",
            title="别人的", status="done", metadata={"objective": "拆解毛利"},
        )

        assert server._latest_agent_objective("research-room", "risk") == ""

    def test_redo_answers_in_the_channel_instead_of_queueing_a_task(
        self, store, quiet_pool,
    ):
        store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="旧的", status="done", metadata={"objective": "复核估值区间"},
        )
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="@risk 重新做一下", mentions=["risk"],
        )

        payload = server._handle_channel_mention_intent(
            channel_id="research-room", message=message, agent_ids=["risk"],
            intent=classify_mention("@risk 重新做一下"),
        )

        assert payload["status"] == "agents_replying"
        assert wait_for(lambda: any(
            item["author_type"] == "agent"
            for item in store.list_messages("research-room")
        ))
        # 重做走对话，不排队：排队正是原来那四十秒的来源。
        assert store.list_tasks("research-room")[0]["metadata"].get("objective") == "复核估值区间"

    def test_without_a_credential_it_says_so_instead_of_pretending(
        self, store, monkeypatch,
    ):
        def unconfigured():
            raise server.NoArkKeysConfigured("没有配置任何 Ark API Key")

        monkeypatch.setattr(server, "_ark_key_pool", unconfigured)
        message = store.add_message(
            channel_id="research-room", author_id="owner", author_type="human",
            message_kind="user_message", body="@risk 重新做一下", mentions=["risk"],
        )

        payload = server._handle_channel_mention_intent(
            channel_id="research-room", message=message, agent_ids=["risk"],
            intent=classify_mention("@risk 重新做一下"),
        )

        assert payload["status"] == "recorded"
        assert "Ark Key" in payload["notice"]
