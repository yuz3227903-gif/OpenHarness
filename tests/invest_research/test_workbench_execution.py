"""Execution-layer behaviour: Skill prompts, per-Agent queues, Agent handoffs."""

from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from openharness.invest_research import workbench_server as server
from openharness.invest_research.agent_skills import render_skills_prompt
from openharness.invest_research.collaboration_store import CollaborationStore
from openharness.invest_research.prompt_assembler import (
    PromptAssemblyError,
    PromptLayers,
    assemble_prompt,
)

BASE_LAYERS = {
    "governance_prompt": "GOVERNANCE",
    "role_prompt": "ROLE",
    "task_prompt": "TASK",
    "context_package": "CONTEXT",
    "output_contract": "CONTRACT",
}


class TestSkillPromptLayer:
    def test_agent_without_skills_assembles_the_original_prompt(self):
        assembled = assemble_prompt(PromptLayers(**BASE_LAYERS))
        assert "SKILL PLUGINS" not in assembled

    def test_skills_render_between_role_and_task(self):
        assembled = assemble_prompt(PromptLayers(**BASE_LAYERS, skills_prompt="SKILL-BODY"))
        assert "SKILL-BODY" in assembled
        assert (
            assembled.index("ROLE PROMPT")
            < assembled.index("SKILL PLUGINS")
            < assembled.index("TASK PROMPT")
        )

    def test_governance_still_outranks_an_installed_skill(self):
        assembled = assemble_prompt(PromptLayers(**BASE_LAYERS, skills_prompt="SKILL-BODY"))
        assert assembled.index("GOVERNANCE PROMPT") < assembled.index("SKILL PLUGINS")

    @pytest.mark.parametrize("missing", sorted(BASE_LAYERS))
    def test_required_layers_are_still_required(self, missing):
        layers = {**BASE_LAYERS, missing: "   "}
        with pytest.raises(PromptAssemblyError):
            assemble_prompt(PromptLayers(**layers))


class TestRenderSkillsPrompt:
    def _install(self, root, agent_id, body="打分 1-5 并给出理由。", name="评分技能"):
        store = CollaborationStore(root)
        skill_dir = root / ".openharness" / "data" / "agent-skills" / agent_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        return store.add_agent_skill(
            agent_id=agent_id, name=name, description="", filename="SKILL.md",
            stored_name=f"{agent_id}/SKILL.md", media_type="text/markdown",
            size_bytes=len(body.encode("utf-8")),
        )

    def test_no_skills_renders_nothing(self, tmp_path):
        CollaborationStore(tmp_path)
        assert render_skills_prompt(tmp_path, "risk") == ""

    def test_enabled_skill_is_inlined_with_its_name(self, tmp_path):
        self._install(tmp_path, "risk")
        rendered = render_skills_prompt(tmp_path, "risk")
        assert "评分技能" in rendered
        assert "打分 1-5 并给出理由。" in rendered

    def test_rendered_block_states_governance_wins(self, tmp_path):
        self._install(tmp_path, "risk")
        assert "以 Governance 为准" in render_skills_prompt(tmp_path, "risk")

    def test_disabled_skill_is_excluded(self, tmp_path):
        skill = self._install(tmp_path, "risk")
        CollaborationStore(tmp_path).set_agent_skill_enabled(skill["skill_id"], False)
        assert render_skills_prompt(tmp_path, "risk") == ""

    def test_skills_do_not_leak_to_another_agent(self, tmp_path):
        self._install(tmp_path, "risk")
        assert render_skills_prompt(tmp_path, "fundamental") == ""

    def test_a_missing_skill_file_does_not_break_the_run(self, tmp_path):
        store = CollaborationStore(tmp_path)
        store.add_agent_skill(
            agent_id="risk", name="缺失技能", description="", filename="gone.md",
            stored_name="risk/gone.md", media_type="text/markdown", size_bytes=10,
        )
        # The Skill is announced by name; the unreadable body is simply absent.
        assert "缺失技能" in render_skills_prompt(tmp_path, "risk")

    def test_a_stored_name_cannot_escape_the_skills_directory(self, tmp_path):
        secret = tmp_path / "secret.md"
        secret.write_text("PRIVATE", encoding="utf-8")
        store = CollaborationStore(tmp_path)
        store.add_agent_skill(
            agent_id="risk", name="逃逸", description="", filename="secret.md",
            stored_name="../../../secret.md", media_type="text/markdown", size_bytes=7,
        )
        assert "PRIVATE" not in render_skills_prompt(tmp_path, "risk")

    def test_an_oversized_skill_is_truncated(self, tmp_path):
        self._install(tmp_path, "risk", body="x" * 40_000)
        rendered = render_skills_prompt(tmp_path, "risk")
        assert "已截断" in rendered
        assert len(rendered) < 40_000


class TestAgentHandoffTargets:
    def test_a_mentioned_research_agent_is_a_target(self):
        assert server._agent_handoff_targets("@risk 请复核", "fundamental") == ["risk"]

    def test_an_agent_never_hands_work_to_itself(self):
        assert server._agent_handoff_targets("@risk 我自己来", "risk") == []

    def test_planner_is_not_a_handoff_target(self):
        # Planner owns the full flow; an Agent cannot restart it by mentioning it.
        assert server._agent_handoff_targets("@planner 重跑", "risk") == []

    def test_an_unknown_mention_is_ignored(self):
        assert server._agent_handoff_targets("@nobody 看看", "risk") == []

    def test_targets_are_deduplicated(self):
        assert server._agent_handoff_targets("@risk @risk 两次", "fundamental") == ["risk"]

    def test_a_mention_in_the_structured_output_is_found(self):
        # The delivery message is a summary built from a couple of chosen
        # fields, so a mention written anywhere else would be invisible to a
        # scan of that message alone.
        output = {"open_questions": [{"item": "口径冲突", "owner": "@reviewer_arbiter"}]}
        assert server._agent_handoff_targets("本轮工作已完成。", "risk", output) == [
            "reviewer_arbiter"
        ]

    def test_the_same_target_in_body_and_output_is_not_duplicated(self):
        output = {"note": "@risk 复核"}
        assert server._agent_handoff_targets("@risk 复核", "fundamental", output) == ["risk"]

    def test_self_mention_in_output_is_still_ignored(self):
        assert server._agent_handoff_targets("", "risk", {"note": "@risk"}) == []

    def test_a_non_dict_output_is_tolerated(self):
        for output in (None, "plain text", ["@risk"], 42):
            assert server._agent_handoff_targets("交付完成", "fundamental", output) == []


class TestPerAgentQueue:
    def test_one_agent_runs_its_jobs_one_at_a_time(self, monkeypatch):
        overlap = []
        active = []
        lock = threading.Lock()
        done = threading.Event()

        def slow_task(**job):
            with lock:
                active.append(job["task_id"])
                overlap.append(len(active))
            time.sleep(0.15)
            with lock:
                active.remove(job["task_id"])
                if job["task_id"] == "T3":
                    done.set()

        monkeypatch.setattr(server, "_run_direct_agent_task", slow_task)
        monkeypatch.setitem(server.AGENT_QUEUES, "queue-test", server.queue.Queue())
        monkeypatch.setitem(server.AGENT_WORKERS, "queue-test", None)

        for task_id in ("T1", "T2", "T3"):
            server._enqueue_agent_task(
                agent_id="queue-test", job={"task_id": task_id, "agent_id": "queue-test"}
            )
        assert done.wait(5), "queued jobs did not finish"
        # Drain before teardown, or the worker picks up a job after monkeypatch
        # has restored the real task runner.
        server.AGENT_QUEUES["queue-test"].join()
        assert max(overlap) == 1, f"jobs overlapped: {overlap}"

    def test_queue_position_counts_up_while_the_worker_is_busy(self, monkeypatch):
        release = threading.Event()

        def blocking_task(**_job):
            release.wait(3)

        monkeypatch.setattr(server, "_run_direct_agent_task", blocking_task)
        monkeypatch.setitem(server.AGENT_QUEUES, "queue-pos", server.queue.Queue())
        monkeypatch.setitem(server.AGENT_WORKERS, "queue-pos", None)

        first = server._enqueue_agent_task(agent_id="queue-pos", job={"task_id": "A"})
        time.sleep(0.2)  # let the worker take the first job off the queue
        second = server._enqueue_agent_task(agent_id="queue-pos", job={"task_id": "B"})
        third = server._enqueue_agent_task(agent_id="queue-pos", job={"task_id": "C"})
        release.set()
        server.AGENT_QUEUES["queue-pos"].join()

        assert first == 1
        # The worker already took A, so B is next in line and C waits behind it.
        assert (second, third) == (1, 2)


class TestDirectTaskCleanup:
    def test_the_finally_block_does_not_reference_a_removed_registry(self, monkeypatch, tmp_path):
        """A failing direct task must fail with its own error, not a NameError.

        The per-task thread registry was replaced by per-Agent queues; a stale
        reference in the ``finally`` block raised NameError on every direct
        task, masked because the worker catches everything.
        """

        store = CollaborationStore(tmp_path)
        monkeypatch.setattr(server, "STORE", store)
        monkeypatch.setattr(
            server, "build_direct_agent_input" if hasattr(server, "build_direct_agent_input") else "_snapshot",
            lambda *a, **k: (_ for _ in ()).throw(server.DirectAgentTaskError("no context")),
            raising=False,
        )
        # Runs to completion: the task is marked failed and nothing raises out.
        server._run_direct_agent_task(
            channel_id="research-room",
            root_message_id="MSG-1",
            task_id="TASK-CLEANUP-1",
            agent_id="risk",
            objective="复核",
            as_of_date=date(2026, 8, 16),
            summary=None,
        )
        messages = store.list_messages("research-room")
        assert not any("NameError" in str(item["body"]) for item in messages), messages


class TestChannelEditing:
    def test_renaming_a_channel_updates_its_topic(self, tmp_path):
        store = CollaborationStore(tmp_path)
        channel = store.create_channel(
            name="temp", topic="比亚迪", description="", member_ids=["fundamental"],
        )
        updated = store.update_channel(
            channel["channel_id"], name="改名后", topic="国轩高科", description="新说明",
        )
        assert (updated["name"], updated["topic"], updated["description"]) == (
            "改名后", "国轩高科", "新说明",
        )

    def test_updating_an_unknown_channel_returns_none(self, tmp_path):
        assert CollaborationStore(tmp_path).update_channel("nope", name="x") is None

    def test_only_naming_fields_are_writable(self, tmp_path):
        store = CollaborationStore(tmp_path)
        channel = store.create_channel(
            name="temp", topic="t", description="", member_ids=["risk"],
        )
        updated = store.update_channel(channel["channel_id"], kind="direct", name="仍是项目频道")
        assert updated["kind"] == "project"
        assert updated["name"] == "仍是项目频道"

    def test_deleting_a_channel_removes_what_it_contained(self, tmp_path):
        store = CollaborationStore(tmp_path)
        channel = store.create_channel(
            name="temp", topic="t", description="", member_ids=["risk"],
        )
        channel_id = channel["channel_id"]
        store.add_message(
            channel_id=channel_id, author_id="owner", author_type="human",
            message_kind="user_message", body="临时消息",
        )
        store.create_task(
            channel_id=channel_id, created_by="owner", assignee_id="risk", title="临时任务",
        )
        store.add_file(
            channel_id=channel_id, owner_id="owner", owner_type="human", source="upload",
            filename="a.txt", stored_name=f"{channel_id}/a.txt", media_type="text/plain",
            size_bytes=3,
        )
        removed = store.delete_channel(channel_id)

        assert removed["workbench_channels"] == 1
        assert removed["workbench_messages"] >= 1
        assert removed["workbench_tasks"] >= 1
        assert removed["workbench_files"] == 1
        assert all(item["channel_id"] != channel_id for item in store.list_channels())
        # Nothing may survive in the workspace-wide panes with no channel to open.
        assert store.list_messages(channel_id) == []
        assert store.list_tasks(channel_id) == []
        assert store.list_files(channel_id) == []

    def test_deleting_one_channel_leaves_the_others_intact(self, tmp_path):
        store = CollaborationStore(tmp_path)
        keep = store.create_channel(
            name="keep", topic="t", description="", member_ids=["risk"],
        )
        drop = store.create_channel(
            name="drop", topic="t", description="", member_ids=["risk"],
        )
        store.add_message(
            channel_id=keep["channel_id"], author_id="owner", author_type="human",
            message_kind="user_message", body="保留",
        )
        # create_channel seeds its own system message, so compare the count.
        before = len(store.list_messages(keep["channel_id"]))
        store.delete_channel(drop["channel_id"])
        assert len(store.list_messages(keep["channel_id"])) == before
        assert any(item["channel_id"] == keep["channel_id"] for item in store.list_channels())


class TestHandoffDepthLimit:
    def _channel(self, monkeypatch, tmp_path):
        store = CollaborationStore(tmp_path)
        monkeypatch.setattr(server, "STORE", store)
        return store

    def test_a_chain_at_the_limit_is_refused_and_says_so(self, monkeypatch, tmp_path):
        store = self._channel(monkeypatch, tmp_path)
        started = []
        monkeypatch.setattr(
            server, "_start_direct_agent_tasks",
            lambda **kwargs: started.append(kwargs) or [],
        )
        root = store.add_message(
            channel_id="research-room", author_id="risk", author_type="agent",
            message_kind="artifact_delivery", body="@reviewer_arbiter 请仲裁",
        )
        server._dispatch_agent_handoffs(
            channel_id="research-room", root_message=root, from_agent_id="risk",
            targets=["reviewer_arbiter"], objective="请仲裁",
            as_of_date=date(2026, 8, 16),
            handoff_depth=server.MAX_AGENT_HANDOFF_DEPTH,
        )
        assert started == [], "a chain past the limit must not create tasks"
        bodies = [item["body"] for item in store.list_messages("research-room")]
        assert any("已达上限" in body for body in bodies), bodies

    def test_a_chain_below_the_limit_dispatches_with_a_deeper_depth(self, monkeypatch, tmp_path):
        store = self._channel(monkeypatch, tmp_path)
        started = []

        def capture(**kwargs):
            started.append(kwargs)
            return [{"task_id": "TASK-1"}]

        monkeypatch.setattr(server, "_start_direct_agent_tasks", capture)
        root = store.add_message(
            channel_id="research-room", author_id="fundamental", author_type="agent",
            message_kind="artifact_delivery", body="@risk 请复核",
        )
        server._dispatch_agent_handoffs(
            channel_id="research-room", root_message=root, from_agent_id="fundamental",
            targets=["risk"], objective="请复核", as_of_date=date(2026, 8, 16),
            handoff_depth=0,
        )
        assert len(started) == 1
        assert started[0]["handoff_depth"] == 1
        assert started[0]["created_by"] == "fundamental"

    def test_no_targets_dispatches_nothing(self, monkeypatch, tmp_path):
        store = self._channel(monkeypatch, tmp_path)
        monkeypatch.setattr(
            server, "_start_direct_agent_tasks",
            lambda **kwargs: pytest.fail("must not dispatch without targets"),
        )
        # The store seeds demo messages, so compare against the starting count.
        before = len(store.list_messages("research-room"))
        server._dispatch_agent_handoffs(
            channel_id="research-room", root_message={"message_id": "M1"},
            from_agent_id="risk", targets=[], objective="",
            as_of_date=date(2026, 8, 16), handoff_depth=0,
        )
        assert len(store.list_messages("research-room")) == before
