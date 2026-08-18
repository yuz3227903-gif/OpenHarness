"""Translating a real Codex `exec --json` stream into bridge events."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openharness.local_bridge.adapters.codex import CodexAdapter  # noqa: E402


def translate(payload, session="S-1"):
    line = payload if isinstance(payload, str) else json.dumps(payload)
    return CodexAdapter().translate(line, session)


def kinds(events):
    return [event.type for event in events]


class TestHowATurnIsStarted:
    def test_a_turn_runs_headless_as_json(self):
        args = CodexAdapter().run_args(session_ref=None)
        assert args[:2] == ["exec", "--json"]

    def test_a_workspace_that_is_not_a_repository_is_still_allowed(self):
        # Refusing to run outside git would be our restriction, not the user's.
        assert "--skip-git-repo-check" in CodexAdapter().run_args(session_ref=None)

    def test_a_known_session_is_resumed(self):
        args = CodexAdapter().run_args(session_ref="abc123")
        assert "--resume" in args and "abc123" in args

    def test_the_prompt_is_a_separate_argument(self, tmp_path, monkeypatch):
        adapter = CodexAdapter()
        monkeypatch.setattr(adapter, "find_executable", lambda: "codex")
        command = adapter.build_command(
            prompt="rm -rf /; echo hi", workspace=str(tmp_path), session_ref=None,
        )
        # Never a shell string: the prompt cannot become a command.
        assert command[0] == "codex"
        assert command[-1] == "rm -rf /; echo hi"


class TestTranslatingEvents:
    def test_an_agent_message_becomes_assistant_text(self):
        events = translate({"msg": {"type": "agent_message", "message": "已经改好了"}})
        assert kinds(events) == ["message.delta"]
        assert events[0].data["text"] == "已经改好了"

    def test_reasoning_becomes_thinking(self):
        events = translate({"msg": {"type": "agent_reasoning", "text": "先看目录"}})
        assert kinds(events) == ["thinking"]

    def test_a_command_start_carries_the_command_and_directory(self):
        events = translate({"msg": {
            "type": "exec_command_begin", "command": ["bash", "-lc", "ls"], "cwd": "/tmp",
        }})
        assert kinds(events) == ["command.start"]
        assert events[0].data["command"] == "bash -lc ls"
        assert events[0].data["cwd"] == "/tmp"

    def test_command_output_is_forwarded(self):
        events = translate({"msg": {"type": "exec_command_output_delta", "chunk": "file.txt"}})
        assert kinds(events) == ["command.output"]

    def test_long_command_output_is_trimmed(self):
        events = translate({"msg": {"type": "exec_command_output_delta", "chunk": "x" * 9000}})
        assert len(events[0].data["line"]) <= 2000

    def test_a_command_end_carries_the_exit_code(self):
        events = translate({"msg": {"type": "exec_command_end", "exit_code": 2}})
        assert events[0].data["exit_code"] == 2

    def test_a_patch_becomes_file_writes(self):
        events = translate({"msg": {
            "type": "patch_apply_end", "changes": {"src/a.py": {}, "src/b.py": {}},
        }})
        assert kinds(events) == ["file.write", "file.write"]
        assert {event.data["path"] for event in events} == {"src/a.py", "src/b.py"}

    def test_a_tool_call_is_reported(self):
        assert kinds(translate({"msg": {"type": "mcp_tool_call_begin", "tool": "search"}})) \
            == ["tool.start"]

    def test_an_error_is_reported_as_an_error(self):
        events = translate({"msg": {"type": "error", "message": "no such file"}})
        assert kinds(events) == ["error"]
        assert "no such file" in events[0].data["message"]

    def test_content_carried_as_parts_is_joined(self):
        events = translate({"msg": {
            "type": "agent_message", "content": [{"type": "text", "text": "第一段"},
                                                 {"type": "text", "text": "第二段"}],
        }})
        assert events[0].data["text"] == "第一段\n第二段"

    def test_the_final_answer_on_a_completion_event_is_not_lost(self):
        events = translate({"msg": {
            "type": "task_complete", "last_agent_message": "全部完成",
        }})
        assert kinds(events) == ["message.delta"]


class TestToleratingVersionDrift:
    def test_a_plain_text_line_is_still_shown(self):
        # An older Codex, or a banner line. Dropping it would lose the answer.
        assert kinds(translate("Reading files…")) == ["message.delta"]

    def test_malformed_json_is_shown_rather_than_dropped(self):
        assert kinds(translate('{"msg": broken')) == ["message.delta"]

    def test_a_blank_line_produces_nothing(self):
        assert translate("   ") == []

    def test_an_unknown_event_type_is_ignored_quietly(self):
        # Not an error: a new Codex release may add events we have no place for.
        assert translate({"msg": {"type": "token_count", "tokens": 5}}) == []

    def test_a_top_level_type_works_as_well_as_a_nested_one(self):
        assert kinds(translate({"type": "agent_message", "message": "hi"})) == ["message.delta"]


class TestResuming:
    def test_the_session_id_is_captured_for_the_next_turn(self):
        events = translate({"msg": {"type": "session_configured", "session_id": "sess-42"}})
        assert CodexAdapter().session_reference(events) == "sess-42"

    def test_a_turn_without_a_session_id_has_nothing_to_resume(self):
        events = translate({"msg": {"type": "agent_message", "message": "hi"}})
        assert CodexAdapter().session_reference(events) is None
