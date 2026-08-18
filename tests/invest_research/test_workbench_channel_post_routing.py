"""发一条频道消息，服务端把它分到哪条路上。

这些走真实的 HTTP 处理器而不是直接调函数：分流逻辑就写在那个处理器里，用户
报的"@ 一下会话就卡住"也正是从这里进来的。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openharness.invest_research import workbench_server as server  # noqa: E402
from openharness.invest_research.ark_key_pool import ArkKeyPool  # noqa: E402
from openharness.invest_research.collaboration_store import CollaborationStore  # noqa: E402
from openharness.invest_research.workbench_chat_model import ChatTurn  # noqa: E402


@pytest.fixture
def live(tmp_path, monkeypatch):
    """一台真跑起来的服务，配着一个空的库和一个立刻作答的模型。"""

    monkeypatch.setattr(server, "DISCUSSION_THREADS", {})
    monkeypatch.setattr(server, "DISCUSSION_RUNNING", {})
    monkeypatch.setattr(server, "TASK_CANCELLATIONS", set())
    store = CollaborationStore(tmp_path)
    monkeypatch.setattr(server, "STORE", store)
    monkeypatch.setattr(server, "_ark_key_pool", lambda: ArkKeyPool(["a"]))
    monkeypatch.setattr(
        server, "chat_complete",
        lambda **kwargs: ChatTurn(text="回答", model=kwargs.get("model", "m")),
    )

    httpd = server.build_server(0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(port, store)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class Client:
    def __init__(self, port: int, store: CollaborationStore) -> None:
        self.port = port
        self.store = store

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def say(self, body: str) -> tuple[int, dict]:
        return self.post("/api/channels/research-room/messages", {"body": body})


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestMentioningAnAgent:
    def test_redo_is_answered_instead_of_queued(self, live):
        live.store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="旧的", status="done", metadata={"objective": "复核估值区间"},
        )
        before = len(live.store.list_tasks("research-room"))

        status, payload = live.say("@risk 重新做一下当前的子任务")

        assert status == 202
        assert payload["status"] == "agents_replying"
        assert payload["intent"] == "redo"
        # 关键：没有再排一份研究任务，也就没有那四十秒的等待。
        assert len(live.store.list_tasks("research-room")) == before

    def test_a_question_gets_a_conversation_not_a_contract(self, live):
        status, payload = live.say("@fundamental 你怎么看这家公司的现金流")

        assert (status, payload["status"]) == (202, "agents_replying")
        assert payload["intent"] == "reply"

    def test_asking_for_a_deliverable_still_starts_real_work(self, live):
        status, payload = live.say("@fundamental 出一份毛利率拆解的报告")

        assert (status, payload["status"]) == (202, "agents_started")
        assert live.store.list_tasks("research-room")

    def test_the_agent_actually_says_something_back(self, live):
        live.say("@risk 重新做一下")

        assert wait_for(lambda: any(
            item["author_type"] == "agent"
            for item in live.store.list_messages("research-room")
        ))


class TestStoppingFromTheChannel:
    def test_at_stop_ends_the_current_topic(self, live):
        task = live.store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="在跑的活儿", status="running",
        )

        status, payload = live.say("@终止")

        assert (status, payload["status"]) == (202, "stopped")
        assert task["task_id"] in payload["cancelled_task_ids"]
        assert live.store.get_task(task["task_id"])["status"] == "cancelling"

    def test_a_targeted_stop_only_stops_that_agent(self, live):
        mine = live.store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="我的", status="running",
        )
        theirs = live.store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="fundamental",
            title="别人的", status="running",
        )

        status, payload = live.say("@risk 停下")

        assert (status, payload["status"]) == (202, "stopped")
        assert payload["cancelled_task_ids"] == [mine["task_id"]]
        assert live.store.get_task(theirs["task_id"])["status"] == "running"


class TestSwitchingTopics:
    def test_a_new_topic_withdraws_the_work_from_the_old_one(self, live):
        stale = live.store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="上一个课题的活儿", status="queued",
        )

        status, _ = live.say("我们换个课题，接下来研究储能行业的竞争格局")

        assert status == 202
        assert live.store.get_task(stale["task_id"])["status"] == "cancelling"

    def test_the_new_topic_is_the_one_being_discussed(self, live):
        live.say("我们换个课题，接下来研究储能行业的竞争格局")

        assert wait_for(lambda: any(
            item["author_type"] == "agent"
            for item in live.store.list_messages("research-room")
        ))

    def test_going_deeper_on_the_same_topic_does_not_stop_anything(self, live):
        running = live.store.create_task(
            channel_id="research-room", created_by="owner", assignee_id="risk",
            title="在跑的活儿", status="running",
        )

        live.say("这个课题再深一点，把上下游也算进来")

        assert live.store.get_task(running["task_id"])["status"] == "running"
