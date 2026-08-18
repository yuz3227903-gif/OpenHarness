from __future__ import annotations

from pathlib import Path

from openharness.invest_research import workbench_server as server


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH = PROJECT_ROOT / ".openharness" / "plugins" / "investment-research" / "workbench"


def _read(name: str) -> str:
    return (WORKBENCH / name).read_text(encoding="utf-8")


def test_search_workspace_uses_the_shared_full_bleed_shell() -> None:
    html = _read("index.html")
    app = _read("app.js")
    shell = _read("shell.css")

    assert 'class="search-topbar"' in html
    assert 'class="search-filterbar"' in html
    assert 'class="search-results-viewport"' in html
    assert "state.workspaceView==='search'" in app
    assert "setWorkspaceView('search')" in app
    assert ".app-shell.no-sidebar" in shell
    assert "shell.classList.toggle('no-sidebar',!channelOpen)" in app


def test_search_visual_tokens_match_the_shared_red_hard_edge_design() -> None:
    tokens = _read("tokens.css").replace(" ", "")
    views = _read("views.css")

    assert "--brand:#C8102E" in tokens
    assert "--brand-dark:#76091F" in tokens
    assert "--brand-soft:#FDE8ED" in tokens
    assert "--hard-shadow:4px4px0#111111" in tokens
    assert "--rail-width:64px" in tokens
    assert ".search-result-card" in views
    assert ".search-result-card:first-child" in views


def test_search_components_and_clear_action_remain_wired() -> None:
    html = _read("index.html")
    app = _read("app.js")

    for component in (
        "search-icon-button",
        "search-input-component",
        "search-filter-component",
        "search-clear-button",
    ):
        assert component in html

    assert "$('search-clear')?.addEventListener('click'" in app
    assert "function searchTimeLabel" in app
    assert "const kindOrder=['message','task','file','agent','channel']" in app
    assert "if(!hasFilters)" in app


def test_search_filters_separate_categories_from_values() -> None:
    html = _read("index.html")
    app = _read("app.js")

    assert '<option value="">全部发送者</option>' in html
    assert '<option value="all">全部内容</option>' in html
    assert '<option value="">全部频道</option>' in html
    assert '<option value="">发送者</option>' not in html
    assert '<option value="">频道</option>' not in html
    assert 'const publicChannels=(data.channels||[]).filter(item=>item.kind!==\'direct\')' in app
    assert 'value="${esc(item.id)}">#${esc(item.name)}' in app
    assert "item.channel_kind==='direct'" in app


def test_search_backend_exposes_public_channel_options_but_keeps_dm_hits() -> None:
    class FakeStore:
        def list_channels(self):
            return [
                {"channel_id": "research-room", "name": "投研项目", "kind": "channel"},
                {"channel_id": "dm-planner", "name": "Planner", "kind": "direct"},
            ]

        def list_agents(self):
            return [{"agent_id": "planner", "name": "Planner", "role": "任务规划"}]

        def list_messages(self, channel_id, limit=500):
            if channel_id == "dm-planner":
                return [{
                    "message_id": "m-1", "author_id": "planner", "body": "hello",
                    "created_at": "2026-08-17T00:00:00+00:00", "message_kind": "agent_message",
                    "thread_id": None,
                }]
            return []

        def list_tasks(self, channel_id):
            return []

        def list_files(self, channel_id):
            return []

    original_store = server.STORE
    server.STORE = FakeStore()
    try:
        result = server._search_workspace(
            query="hello", scope="all", sender="", channel_id="", since_days=0, sort="relevance"
        )
    finally:
        server.STORE = original_store

    assert result["channels"] == [{"id": "research-room", "name": "投研项目", "kind": "channel"}]
    assert result["results"][0]["channel_kind"] == "direct"


def test_utility_drawers_do_not_change_the_selected_global_view() -> None:
    app = _read("app.js")

    assert "document.querySelectorAll('[data-drawer]')" in app
    assert "showUtilityDrawer(button.dataset.drawer)" in app
    assert "document.querySelectorAll('.rail-btn[data-view]')" in app
    assert "button.dataset.view===state.workspaceView" in app
