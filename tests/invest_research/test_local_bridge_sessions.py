"""Sessions, streaming, approval and the workspace boundary."""

from __future__ import annotations

import sys
import time

import pytest

from openharness.local_bridge.adapters.base import AgentCapabilities, LocalAgentAdapter
from openharness.local_bridge.events import (
    AgentEvent,
    UnknownEventType,
    message_delta,
)
from openharness.local_bridge.sessions import (
    SessionError,
    SessionManager,
    looks_dangerous,
    within_workspace,
)


class EchoAdapter(LocalAgentAdapter):
    """A real process, so the streaming path is genuinely exercised.

    Python is present wherever these tests run, which lets a turn spawn, emit
    output line by line and exit for real rather than through a stub.
    """

    provider = "echo-test"
    display_name = "Echo Test"
    executables = ("python",)

    def __init__(self, script: str | None = None) -> None:
        self.script = script or (
            "import sys,time\n"
            "for i in range(3):\n"
            "    print(f'line {i}', flush=True)\n"
            "    time.sleep(0.02)\n"
        )

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(chat=True, streaming=True, shell=True)

    def build_command(self, *, prompt, workspace, session_ref):
        return [sys.executable, "-c", self.script]


class DangerousAdapter(EchoAdapter):
    provider = "danger-test"
    display_name = "Danger Test"

    def build_command(self, *, prompt, workspace, session_ref):
        # The literal text is what the approval check reads.
        return [sys.executable, "-c", "print('done')", "git push origin main"]


@pytest.fixture
def manager():
    return SessionManager()


def wait_until(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestEventProtocol:
    def test_an_unknown_type_is_refused(self):
        # A typo in an adapter must fail loudly, not become an event the UI
        # quietly drops.
        with pytest.raises(UnknownEventType):
            AgentEvent("message.oops", "S-1")

    def test_a_known_type_is_accepted(self):
        assert AgentEvent("message.delta", "S-1").type == "message.delta"

    def test_an_event_serialises_flat(self):
        payload = message_delta("S-1", "hi").to_dict()
        assert payload["type"] == "message.delta"
        assert payload["data"]["text"] == "hi"
        assert set(payload) == {"type", "session_id", "seq", "created_at", "data"}


class TestSessionLifecycle:
    def test_creating_a_session_requires_a_real_workspace(self, manager, tmp_path):
        with pytest.raises(SessionError, match="不存在"):
            manager.create(agent_id="a", provider="echo-test",
                           workspace=str(tmp_path / "nope"))

    def test_a_new_session_announces_itself(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        assert session.status == "idle"
        assert [event.type for event in session.events_since(0)] == ["session.created"]

    def test_sessions_are_listed_per_agent(self, manager, tmp_path):
        manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.create(agent_id="b", provider="echo-test", workspace=str(tmp_path))
        assert len(manager.list()) == 2
        assert len(manager.list(agent_id="a")) == 1

    def test_an_unknown_session_is_refused(self, manager):
        with pytest.raises(SessionError, match="不存在"):
            manager.get("S-NOPE")


class TestStreaming:
    def test_a_turn_streams_output_as_it_arrives(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.send(session.session_id, "hello", EchoAdapter())

        assert wait_until(lambda: session.status == "idle" and
                          any(e.type == "message.end" for e in session.events_since(0)))
        types = [event.type for event in session.events_since(0)]
        assert "command.start" in types
        assert "message.end" in types
        deltas = [e.data["text"] for e in session.events_since(0) if e.type == "message.delta"]
        assert deltas == ["line 0", "line 1", "line 2"]

    def test_events_carry_increasing_sequence_numbers(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.send(session.session_id, "hello", EchoAdapter())
        assert wait_until(lambda: session.status == "idle")
        seqs = [event.seq for event in session.events_since(0)]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

    def test_a_cursor_replays_only_what_is_new(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.send(session.session_id, "hello", EchoAdapter())
        assert wait_until(lambda: session.status == "idle")
        everything = session.events_since(0)
        midpoint = everything[len(everything) // 2].seq
        # This is what a reconnecting page does; it must not re-see the start.
        assert all(event.seq > midpoint for event in session.events_since(midpoint))

    def test_one_session_runs_one_turn_at_a_time(self, manager, tmp_path):
        slow = EchoAdapter("import time\ntime.sleep(1.5)\nprint('done')\n")
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.send(session.session_id, "first", slow)
        with pytest.raises(SessionError, match="正在执行"):
            manager.send(session.session_id, "second", slow)
        manager.cancel(session.session_id)

    def test_an_empty_prompt_is_refused(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        with pytest.raises(SessionError, match="不能为空"):
            manager.send(session.session_id, "   ", EchoAdapter())

    def test_a_missing_binary_is_reported_not_raised(self, manager, tmp_path):
        class Missing(EchoAdapter):
            provider = "missing-test"
            def build_command(self, *, prompt, workspace, session_ref):
                raise FileNotFoundError("Codex 未安装或不在 PATH 中")

        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.send(session.session_id, "hi", Missing())
        assert wait_until(lambda: session.status == "idle")
        kinds = [e.data.get("kind") for e in session.events_since(0) if e.type == "error"]
        assert "agent_not_found" in kinds

    def test_a_turn_runs_inside_the_workspace(self, manager, tmp_path):
        workspace = tmp_path / "project"
        workspace.mkdir()
        printer = EchoAdapter("import os\nprint(os.getcwd(), flush=True)\n")
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(workspace))
        manager.send(session.session_id, "where", printer)
        assert wait_until(lambda: session.status == "idle")
        printed = [e.data["text"] for e in session.events_since(0) if e.type == "message.delta"]
        assert str(workspace.resolve()) in printed[0]

    def test_cancelling_stops_a_running_process(self, manager, tmp_path):
        slow = EchoAdapter("import time\ntime.sleep(30)\n")
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.send(session.session_id, "long", slow)
        assert wait_until(lambda: session._process is not None)
        assert manager.cancel(session.session_id) is True
        assert wait_until(lambda: session.status == "idle")

    def test_cancelling_immediately_after_sending_still_works(self, manager, tmp_path):
        """The turn spawns in a worker thread, so this cancel lands first.

        Reporting "nothing to cancel" here and then running the turn anyway is
        exactly what a user clicking stop straight after send would hit.
        """
        slow = EchoAdapter("import time\ntime.sleep(30)\nprint('should not finish')\n")
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        manager.send(session.session_id, "long", slow)
        assert manager.cancel(session.session_id) is True
        assert wait_until(lambda: session.status == "idle", timeout=20)
        printed = [e.data.get("text") for e in session.events_since(0)
                   if e.type == "message.delta"]
        assert "should not finish" not in printed

    def test_cancelling_an_idle_session_reports_nothing_to_do(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        assert manager.cancel(session.session_id) is False

    def test_cancelling_releases_a_pending_approval(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="danger-test",
                                 workspace=str(tmp_path), permission_mode="ask")
        manager.send(session.session_id, "push it", DangerousAdapter())
        assert wait_until(lambda: session.status == "awaiting_approval")
        # Otherwise the turn would sit on the question until the timeout.
        assert manager.cancel(session.session_id) is True
        assert wait_until(lambda: session.status == "idle", timeout=20)
        assert not any(e.type == "command.start" for e in session.events_since(0))


class TestApproval:
    def test_a_dangerous_command_asks_first(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="danger-test",
                                 workspace=str(tmp_path), permission_mode="ask")
        manager.send(session.session_id, "push it", DangerousAdapter())
        assert wait_until(lambda: any(e.type == "approval.required"
                                      for e in session.events_since(0)))
        assert session.status == "awaiting_approval"
        # Nothing ran while the question was open.
        assert not any(e.type == "command.start" for e in session.events_since(0))
        manager.resolve_approval(
            session.session_id,
            next(e.data["approval_id"] for e in session.events_since(0)
                 if e.type == "approval.required"),
            "deny",
        )
        assert wait_until(lambda: session.status == "idle")

    def test_denying_stops_the_command(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="danger-test",
                                 workspace=str(tmp_path), permission_mode="ask")
        manager.send(session.session_id, "push it", DangerousAdapter())
        assert wait_until(lambda: any(e.type == "approval.required"
                                      for e in session.events_since(0)))
        approval_id = next(e.data["approval_id"] for e in session.events_since(0)
                           if e.type == "approval.required")
        manager.resolve_approval(session.session_id, approval_id, "deny")
        assert wait_until(lambda: session.status == "idle")
        assert not any(e.type == "command.start" for e in session.events_since(0))

    def test_allowing_lets_the_command_run(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="danger-test",
                                 workspace=str(tmp_path), permission_mode="ask")
        manager.send(session.session_id, "push it", DangerousAdapter())
        assert wait_until(lambda: any(e.type == "approval.required"
                                      for e in session.events_since(0)))
        approval_id = next(e.data["approval_id"] for e in session.events_since(0)
                           if e.type == "approval.required")
        manager.resolve_approval(session.session_id, approval_id, "allow_once")
        assert wait_until(lambda: session.status == "idle")
        assert any(e.type == "command.start" for e in session.events_since(0))

    def test_allowing_for_the_session_stops_asking_again(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="danger-test",
                                 workspace=str(tmp_path), permission_mode="ask")
        manager.send(session.session_id, "push it", DangerousAdapter())
        assert wait_until(lambda: any(e.type == "approval.required"
                                      for e in session.events_since(0)))
        approval_id = next(e.data["approval_id"] for e in session.events_since(0)
                           if e.type == "approval.required")
        manager.resolve_approval(session.session_id, approval_id, "allow_session")
        assert wait_until(lambda: session.status == "idle")

        before = sum(1 for e in session.events_since(0) if e.type == "approval.required")
        manager.send(session.session_id, "push again", DangerousAdapter())
        assert wait_until(lambda: session.status == "idle")
        after = sum(1 for e in session.events_since(0) if e.type == "approval.required")
        assert after == before

    def test_an_unknown_decision_is_refused(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        with pytest.raises(SessionError, match="decision"):
            manager.resolve_approval(session.session_id, "A-NOPE", "maybe")

    def test_resolving_an_unknown_approval_is_refused(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        with pytest.raises(SessionError, match="不存在"):
            manager.resolve_approval(session.session_id, "A-NOPE", "deny")


class TestDangerDetection:
    @pytest.mark.parametrize("command", [
        "rm -rf /", "git push origin main", "npm publish", "sudo shutdown now",
        "dd if=/dev/zero of=/dev/sda", "chmod 777 /etc",
    ])
    def test_dangerous_commands_are_flagged(self, command):
        assert looks_dangerous(command) is not None

    @pytest.mark.parametrize("command", [
        "npm install", "git status", "ls -la", "python -m pytest",
    ])
    def test_ordinary_commands_are_not(self, command):
        assert looks_dangerous(command) is None

    def test_matching_ignores_spacing_and_case(self):
        assert looks_dangerous("GIT   PUSH origin main") is not None


class TestWorkspaceBoundary:
    def test_a_path_inside_the_workspace_passes(self, tmp_path):
        inside = tmp_path / "src" / "main.py"
        inside.parent.mkdir(parents=True)
        inside.touch()
        assert within_workspace(inside, tmp_path) is True

    def test_the_workspace_itself_passes(self, tmp_path):
        assert within_workspace(tmp_path, tmp_path) is True

    def test_a_path_outside_is_refused(self, tmp_path):
        outside = tmp_path.parent / "elsewhere"
        assert within_workspace(outside, tmp_path) is False

    def test_traversal_is_resolved_before_comparing(self, tmp_path):
        workspace = tmp_path / "project"
        workspace.mkdir()
        # A prefix comparison on the raw string would let this through.
        assert within_workspace(workspace / ".." / "secret", workspace) is False

    def test_a_file_touched_outside_the_workspace_is_flagged(self, manager, tmp_path):
        workspace = tmp_path / "project"
        workspace.mkdir()
        escaped = str(tmp_path / "secrets.txt")
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(workspace))
        event = AgentEvent("file.write", session.session_id, data={"path": escaped})
        # The bridge cannot stop the CLI reaching out, so the page must be told.
        assert manager._mark_outside_workspace(session, event).data["outside_workspace"] is True

    def test_a_file_inside_the_workspace_is_not_flagged(self, manager, tmp_path):
        workspace = tmp_path / "project"
        workspace.mkdir()
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(workspace))
        event = AgentEvent("file.write", session.session_id,
                           data={"path": str(workspace / "main.py")})
        assert "outside_workspace" not in manager._mark_outside_workspace(session, event).data

    def test_a_non_file_event_is_left_alone(self, manager, tmp_path):
        session = manager.create(agent_id="a", provider="echo-test", workspace=str(tmp_path))
        event = message_delta(session.session_id, "hello")
        assert "outside_workspace" not in manager._mark_outside_workspace(session, event).data
