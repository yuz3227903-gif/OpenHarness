from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_workbench_sse_uses_default_message_events_and_cursor_replay():
    server = (
        PROJECT_ROOT / "src" / "openharness" / "invest_research" / "workbench_server.py"
    ).read_text(encoding="utf-8")
    app = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'f"id: {event[\'event_seq\']}\\ndata: "' in server
    assert "event: {event['event_type']}" not in server
    assert "source.onmessage=handleEvent" in app
    assert "after=${state.eventSeq}" in app
    assert "data.event_seq" in app
    assert 'STORE.latest_event_seq("research-room")' in server
    assert "liveEventSource" in app
    assert "scheduleRealtimeRefresh" in app
    assert "direct_agent_progress" in server
    assert "elapsed_seconds" in server
    assert "last_activity_at" in server
    assert "agent-status-label" in app


def test_workbench_renders_structured_agent_handoff_details():
    app = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert "本轮使用工具" in app
    assert "本轮证据增量" in app
    assert "projection.source_ids" in app
    assert "projection.logic_ids" in app
    assert "/api/threads/" in app
    assert "线程交接" in app
    assert "thread-composer" in app
    assert "thread_id:rootMessageId" in app
    # The in-chat "查看线程" button is gone: replies are inline comments under
    # the message, and commenting on an Agent queues it a task. The detail pane
    # it used to open is still reachable from a search hit.
    assert "data-comment-toggle" in app
    assert "submitComment" in app
    assert "showMessage(ref.thread_id || ref.message_id)" in app


def test_workbench_exposes_safe_thread_context_endpoint():
    server = (
        PROJECT_ROOT / "src" / "openharness" / "invest_research" / "workbench_server.py"
    ).read_text(encoding="utf-8")

    assert "def _thread_payload" in server
    assert "STORE.get_thread" in server
    assert 'path.startswith("/api/threads/")' in server


def test_workbench_routes_non_planner_mentions_to_direct_agent_tasks():
    server = (
        PROJECT_ROOT / "src" / "openharness" / "invest_research" / "workbench_server.py"
    ).read_text(encoding="utf-8")
    app = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")
    html = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "_start_direct_agent_tasks" in server
    assert '"status": "agents_started"' in server
    assert "DIRECT_AGENT_IDS" in server
    assert "for agent_id in dict.fromkeys(agent_ids)" in server
    assert "root_message_id=message[\"message_id\"]" in server
    assert "thread_id=root_message_id" in server
    assert "requested_thread_id or message[\"message_id\"]" in server
    assert "include_replies=False" in server
    assert "data.status==='agents_started'" in app
    assert "可同时 @多个 Agent" in html


def test_custom_agent_detail_can_open_prefilled_editor_and_replace_avatar():
    app = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")
    html = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert 'id="edit-agent"' in app
    assert "openAgentEditor" in app
    assert "agentForm.elements.system_prompt.value" in app
    assert "agentForm.elements.profile.value" in app
    assert "method:'PATCH'" in app
    assert "avatar_data_url:avatar" in app
    assert 'id="agent-dialog-title"' in html
    assert 'id="agent-avatar-preview"' in html


def test_agent_editor_renders_ark_plan_model_catalog_and_effective_model() -> None:
    app = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")
    html = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "state.modelSettings=data.model_settings" in app
    assert "renderModelOptions" in app
    assert "state.modelSettings.models.map" in app
    assert "model-description" in html
    assert "当前模型" in app
    assert "ark-code-latest" in html


def test_builtin_agent_detail_allows_model_only_switch() -> None:
    app = (
        PROJECT_ROOT
        / ".openharness"
        / "plugins"
        / "investment-research"
        / "workbench"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'id="agent-model-select"' in app
    assert 'id="save-agent-model"' in app
    assert "saveAgentModel" in app
    assert "body:JSON.stringify({model})" in app


def test_full_research_flow_uses_persisted_agent_model_resolver() -> None:
    server = (
        PROJECT_ROOT / "src" / "openharness" / "invest_research" / "workbench_server.py"
    ).read_text(encoding="utf-8")

    assert "OpenHarnessRuntimeGateway" in server
    assert "model_resolver=_resolve_agent_model" in server
    assert "gateway=runtime_gateway" in server
