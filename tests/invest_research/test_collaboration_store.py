from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from openharness.invest_research.collaboration_store import CollaborationStore


@pytest.fixture
def workspace_tmp() -> Path:
    root = Path(__file__).resolve().parents[2] / ".test-workbench" / uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_store_seeds_workspace_and_persists_message(workspace_tmp: Path) -> None:
    store = CollaborationStore(workspace_tmp)
    channels = store.list_channels()
    assert channels[0]["channel_id"] == "research-room"
    assert len(store.list_agents()) == 7

    message = store.add_message(
        channel_id="research-room",
        author_id="owner",
        author_type="human",
        message_kind="user_message",
        body="@Planner 请研究科大讯飞",
        mentions=["planner"],
    )
    assert message["mentions"] == ["planner"]
    assert store.list_messages("research-room")[-1]["message_id"] == message["message_id"]


def test_task_and_sse_event_records_are_queryable(workspace_tmp: Path) -> None:
    store = CollaborationStore(workspace_tmp)
    task = store.create_task(
        channel_id="research-room",
        created_by="owner",
        assignee_id="planner",
        title="研究科大讯飞",
    )
    assert store.update_task(task["task_id"], "running")["status"] == "running"
    event = store.add_event(
        channel_id="research-room",
        event_type="run_started",
        payload={"task_id": task["task_id"]},
    )
    events = store.events_since("research-room", event["event_seq"] - 1)
    assert events[-1]["event_type"] == "run_started"
    assert events[-1]["payload"]["task_id"] == task["task_id"]
    assert store.latest_event_seq("research-room") == event["event_seq"]

    store.add_event(
        channel_id="research-room",
        event_type="flow_event",
        payload={"flow_event_id": "FLOW-EVENT-001", "event_type": "task_started"},
    )
    assert store.has_flow_event("research-room", "FLOW-EVENT-001") is True
    assert store.has_flow_event("research-room", "FLOW-EVENT-MISSING") is False


def test_thread_returns_root_and_replies_when_opened_from_a_reply(workspace_tmp: Path) -> None:
    store = CollaborationStore(workspace_tmp)
    root = store.add_message(
        channel_id="research-room", author_id="owner", author_type="human",
        message_kind="user_message", body="@fundamental 请补充经营变化", mentions=["fundamental"],
    )
    reply = store.add_message(
        channel_id="research-room", thread_id=root["message_id"], author_id="fundamental",
        author_type="agent", message_kind="artifact_delivery", body="已提交交付物",
        metadata={"task_id": "TASK-THREAD-001"},
    )

    thread = store.get_thread(reply["message_id"], "research-room")
    assert thread is not None
    assert thread["root"]["message_id"] == root["message_id"]
    assert [item["message_id"] for item in thread["replies"]] == [reply["message_id"]]
    root_ids = [item["message_id"] for item in store.list_messages("research-room", include_replies=False)]
    assert root["message_id"] in root_ids
    assert reply["message_id"] not in root_ids


def test_artifact_can_be_upserted_and_filtered_by_channel(workspace_tmp: Path) -> None:
    store = CollaborationStore(workspace_tmp)
    artifact = store.upsert_artifact(
        artifact_id="ART-TEST-001",
        channel_id="research-room",
        run_id="RUN-TEST-001",
        task_id="TASK-TEST-001",
        agent_id="fundamental",
        title="基本面研究交付",
        summary="已提交经营变化和财务事实。",
        refs=["S-001", "F-001"],
    )
    assert artifact["artifact_id"] == "ART-TEST-001"
    assert artifact["refs"] == ["S-001", "F-001"]

    updated = store.upsert_artifact(
        artifact_id="ART-TEST-001",
        channel_id="research-room",
        run_id="RUN-TEST-001",
        agent_id="fundamental",
        title="基本面研究交付",
        summary="已补充交叉验证。",
        status="reviewed",
        refs=["S-001", "F-001", "L-001"],
    )
    assert updated["status"] == "reviewed"
    assert store.list_artifacts("research-room")[0]["summary"] == "已补充交叉验证。"
