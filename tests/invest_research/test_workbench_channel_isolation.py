from __future__ import annotations

from openharness.invest_research import workbench_server as server


class _FakeStore:
    def __init__(self) -> None:
        self._tasks = {
            "research-room": [{"task_id": "TASK-OLD", "channel_id": "research-room", "run_id": "RUN-OLD"}],
            "channel-new": [{"task_id": "TASK-NEW", "channel_id": "channel-new", "run_id": None}],
            "dm-planner": [],
        }
        self._files = {
            "research-room": [{"file_id": "FILE-OLD", "channel_id": "research-room"}],
            "channel-new": [],
            "dm-planner": [],
        }

    def list_channels(self):
        return [
            {"channel_id": "research-room", "name": "投研项目", "kind": "channel"},
            {"channel_id": "channel-new", "name": "AAA", "kind": "channel"},
            {"channel_id": "dm-planner", "name": "Planner", "kind": "direct"},
        ]

    def list_agents(self):
        return []

    def list_tasks(self, channel_id=None):
        if channel_id is not None:
            return list(self._tasks.get(channel_id, []))
        return [task for tasks in self._tasks.values() for task in tasks]

    def list_artifacts(self, channel_id=None):
        return []

    def list_files(self, channel_id=None):
        if channel_id is not None:
            return list(self._files.get(channel_id, []))
        return [item for files in self._files.values() for item in files]

    def removed_builtin_agents(self):
        return []

    def latest_event_seq(self, channel_id):
        return 0

    def get_task(self, task_id):
        for tasks in self._tasks.values():
            for task in tasks:
                if task["task_id"] == task_id:
                    return task
        return None


def test_workspace_payload_keeps_new_channel_data_separate(monkeypatch) -> None:
    fake = _FakeStore()
    monkeypatch.setattr(server, "STORE", fake)
    monkeypatch.setattr(server, "_ensure_flow_events_projected", lambda channel_id: server._empty_channel_snapshot())
    monkeypatch.setattr(server, "_workspace_agents", lambda channel_id: [])
    monkeypatch.setattr(server, "_model_settings_payload", lambda: {})

    payload = server._workspace_payload("channel-new")

    assert [task["task_id"] for task in payload["channel_tasks"]] == ["TASK-NEW"]
    assert payload["channel_files"] == []
    assert {task["task_id"] for task in payload["tasks"]} == {"TASK-OLD", "TASK-NEW"}
    assert {item["file_id"] for item in payload["files"]} == {"FILE-OLD"}
    assert payload["run"]["report_available"] is False


def test_workspace_payload_keeps_direct_message_empty(monkeypatch) -> None:
    fake = _FakeStore()
    monkeypatch.setattr(server, "STORE", fake)
    monkeypatch.setattr(server, "_ensure_flow_events_projected", lambda channel_id: server._empty_channel_snapshot())
    monkeypatch.setattr(server, "_workspace_agents", lambda channel_id: [])
    monkeypatch.setattr(server, "_model_settings_payload", lambda: {})

    payload = server._workspace_payload("dm-planner")

    assert payload["channel_tasks"] == []
    assert payload["channel_files"] == []
    assert payload["run"]["report_available"] is False


def test_channel_snapshot_rejects_previous_run_for_an_unrelated_channel(monkeypatch) -> None:
    fake = _FakeStore()
    monkeypatch.setattr(server, "STORE", fake)
    monkeypatch.setattr(server, "_snapshot", lambda: {
        "task_id": "TASK-OLD",
        "run_id": "RUN-OLD",
        "summary": {"run_id": "RUN-OLD"},
        "report_available": True,
    })

    assert server._snapshot_for_channel("channel-new")["report_available"] is False
    assert server._snapshot_for_channel("dm-planner")["run_id"] is None
