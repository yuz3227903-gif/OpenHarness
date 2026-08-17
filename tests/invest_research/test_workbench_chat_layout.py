from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH = PROJECT_ROOT / ".openharness" / "plugins" / "investment-research" / "workbench"


def _read(name: str) -> str:
    return (WORKBENCH / name).read_text(encoding="utf-8")


def test_workbench_uses_one_shared_shell_and_design_system():
    html = _read("index.html")
    shell = _read("shell.css")
    tokens = _read("tokens.css")

    assert '<link rel="stylesheet" href="/tokens.css">' in html
    assert '<link rel="stylesheet" href="/shell.css">' in html
    assert '<link rel="stylesheet" href="/views.css">' in html
    assert '<link rel="stylesheet" href="/chat.css">' not in html
    assert '<link rel="stylesheet" href="/search.css">' not in html
    assert "--rail-width:64px" in tokens.replace(" ", "")
    assert "--sidebar-width:240px" in tokens.replace(" ", "")
    assert "--drawer-width:320px" in tokens.replace(" ", "")
    assert "--rail-bg:#F6C5CF" in tokens.replace(" ", "")
    assert "grid-template-columns:var(--rail-width) var(--sidebar-width) minmax(0,1fr)" in shell


def test_header_drawer_and_channel_actions_are_wired():
    html = _read("index.html")
    javascript = _read("app.js")

    for element_id in (
        "channel-search",
        "channel-mute",
        "pause-run",
        "resume-run",
        "channel-settings",
        "channel-members",
        "close-context",
        "drawer-scrim",
    ):
        assert f'id="{element_id}"' in html

    assert "function showUtilityDrawer(kind)" in javascript
    assert "function openContextDrawer(title='上下文')" in javascript
    assert "state.drawerMode" in javascript


def test_global_navigation_and_channel_tabs_have_separate_state():
    html = _read("index.html")
    javascript = _read("app.js")

    for view in ("search", "channel", "activity", "tasks", "members", "graph"):
        assert f'data-view="{view}"' in html
    for drawer in ("notifications", "help", "settings"):
        assert f'data-drawer="{drawer}"' in html
    for tab in ("chat", "tasks", "files"):
        assert f'data-channel-tab="{tab}"' in html

    assert "workspaceView: 'channel'" in javascript
    assert "channelTab: 'chat'" in javascript
    assert "drawerMode: null" in javascript
    assert "function setWorkspaceView(view,options={})" in javascript
    assert "function setChannelTab(tab)" in javascript
    assert "document.querySelectorAll('.pane-tab[data-channel-tab]')" in javascript
    assert "activePane" not in javascript
    assert "paneMode" not in javascript
    assert "setPane(" not in javascript


def test_composer_keeps_multi_agent_and_task_actions():
    html = _read("index.html")
    shell = _read("shell.css")

    assert "可同时 @多个 Agent" in html
    assert 'id="composer-as-task"' in html
    assert 'id="attachment-input"' in html
    # One attach control, not two. The "media" and "file" buttons opened the
    # same picker, so the second only made the composer look busier.
    assert 'id="attachment-file-button"' not in html
    assert html.count('type="file"') == html.count('id="attachment-input"') + \
        html.count('name="avatar"')
    assert ".composer" in shell
    assert "position:absolute" in shell


def test_file_rows_and_reports_open_inside_the_shared_drawer():
    javascript = _read("app.js")
    views = _read("views.css")

    assert "function showFile(fileId)" in javascript
    assert 'data-file-id="${esc(file.file_id)}"' in javascript
    assert "row.addEventListener('keydown'" in javascript
    assert "openContextDrawer('文件详情')" in javascript
    assert "openContextDrawer('研究报告')" in javascript
    assert "window.open()" not in javascript
    assert ".report-preview" in views
    assert ".file-row:not(.file-head):hover" in views


def test_channel_mute_exposes_observable_pressed_state():
    javascript = _read("app.js")

    assert "setAttribute('aria-pressed',String(state.channelMuted))" in javascript


def test_sidebar_has_workspace_pins_favorites_and_no_joint_channels():
    javascript = _read("app.js")

    assert "investment-research.sidebar-prefs.v1" in javascript
    assert "data-pinned-drop" in javascript
    assert "data-toggle-favorite" in javascript
    assert "data-toggle-pin" in javascript
    assert "sectionHtml('favorites','已收藏'" in javascript
    assert "sectionHtml('pinned','已置顶'" in javascript
    assert "联合频道" not in javascript
    assert "create-joint-channel" not in javascript


def test_pinned_sidebar_items_are_removed_from_original_groups():
    javascript = _read("app.js")

    assert "const pinnedIds=new Set(cleanPrefs.pinned)" in javascript
    assert "const visibleProjects=projects.filter(item=>!pinnedIds.has(sidebarItemId(item))" in javascript
    assert "const visibleDirects=directs.filter(item=>!pinnedIds.has(sidebarItemId(item))" in javascript
    assert "sectionHtml('channels','频道',visibleProjects.length" in javascript
    # 私信 counts conversations, not the roster. Padding it with every Agent
    # that had no conversation yet made it read as "the members of this
    # channel", which a direct message has nothing to do with.
    assert "sectionHtml('dms','私信',visibleDirects.length," in javascript
    assert "visibleAgents" not in javascript


def test_task_channel_filter_uses_real_channel_labels():
    javascript = _read("app.js")

    assert "function channelOptions(selected, placeholder='全部频道')" in javascript
    assert "const label=channel.kind==='direct' ? `与 ${channel.name} 的私信` : `#${channel.name}`" in javascript
    assert 'id="task-filter-channel">${channelOptions(channel)}' in javascript


def test_all_agent_dm_rows_separate_profile_and_chat_actions():
    javascript = _read("app.js")

    assert "function agentRowHtml(agent)" in javascript
    assert "data-profile-agent" in javascript
    assert "data-dm-agent" in javascript
    assert "await startDirectMessage(button.dataset.dmAgent)" in javascript
    # Opening a DM must not immediately replace it with the profile drawer.
    dm_start = javascript[javascript.index("async function startDirectMessage") :]
    assert "showAgent(agentId);" not in dm_start.split("function setupCreationDialogs", 1)[0]


def test_agent_avatar_and_skill_upload_controls_have_overflow_safe_styles():
    shell = _read("shell.css")
    views = _read("views.css")
    javascript = _read("app.js")

    assert ".dm-avatar,.agent-avatar{width:32px;height:32px" in shell
    assert ".skill-upload-control" in views
    assert "overflow-wrap:anywhere" in views
    assert "id=\"skill-input\"" in javascript
