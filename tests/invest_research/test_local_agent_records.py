"""Registering a local Agent, and the guard rails on doing so."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

from openharness.invest_research import workbench_server as server
from openharness.invest_research.collaboration_store import CollaborationStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = CollaborationStore(tmp_path)
    monkeypatch.setattr(server, "STORE", store)
    return store


def body(**overrides):
    payload = {
        "agent_type": "local",
        "name": "我的 Codex",
        "provider": "codex",
        "workspace": None,      # filled by the caller
        "bridge_id": "local",
        "permission_mode": "ask",
    }
    payload.update(overrides)
    return payload


class TestCreatingALocalAgent:
    def test_it_is_stored_with_its_provider_and_workspace(self, store, tmp_path):
        workspace = tmp_path / "projects" / "demo"
        workspace.mkdir(parents=True)
        agent = server._create_local_agent(body(workspace=str(workspace)), None)

        assert agent["agent_type"] == "local"
        assert agent["provider"] == "codex"
        assert agent["runtime"] == "local"
        assert agent["workspace"] == str(workspace.resolve())
        assert agent["agent_id"].startswith("local-")

    def test_it_appears_beside_hosted_agents(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        agent = server._create_local_agent(body(workspace=str(workspace)), None)
        roster = {item["agent_id"]: item for item in store.list_agents()}
        assert agent["agent_id"] in roster
        # The built-in roles keep working and now answer the same questions.
        assert roster["planner"]["agent_type"] == "hosted"
        assert roster["planner"]["runtime"] == "hosted"
        assert roster["planner"]["provider"] == "openharness"

    def test_capabilities_come_from_the_adapter_not_the_client(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        # A page claiming an ability must not be able to grant it.
        agent = server._create_local_agent(
            body(workspace=str(workspace), capabilities={"shell": False, "mcp": False}), None
        )
        assert agent["capabilities"]["shell"] is True

    def test_a_local_agent_starts_with_an_unknown_connection(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        agent = server._create_local_agent(body(workspace=str(workspace)), None)
        # Nothing has asked the bridge yet, so claiming "online" would be a lie.
        assert agent["status"] == "unknown"

    def test_the_status_follows_what_the_bridge_reported(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        agent = server._create_local_agent(body(workspace=str(workspace)), None)
        store.set_agent_connection_status(agent["agent_id"], "online")
        updated = next(a for a in store.list_agents() if a["agent_id"] == agent["agent_id"])
        assert updated["status"] == "online"

    def test_an_unknown_provider_is_refused(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        with pytest.raises(ValueError, match="未知的本地 Agent 类型"):
            server._create_local_agent(body(workspace=str(workspace), provider="not-real"), None)

    def test_a_bad_permission_mode_is_refused(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        with pytest.raises(ValueError, match="permission_mode"):
            server._create_local_agent(
                body(workspace=str(workspace), permission_mode="yolo"), None
            )

    def test_a_name_is_required(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        with pytest.raises(ValueError, match="name"):
            server._create_local_agent(body(workspace=str(workspace), name="  "), None)


class TestWorkspaceGuards:
    def test_a_workspace_is_required(self):
        with pytest.raises(ValueError, match="workspace"):
            server._validate_workspace("")

    def test_a_relative_path_is_refused(self):
        with pytest.raises(ValueError, match="绝对路径"):
            server._validate_workspace("projects/demo")

    def test_a_filesystem_root_is_refused(self, tmp_path):
        root = str(tmp_path.anchor or tmp_path.root)
        with pytest.raises(ValueError, match="根目录"):
            server._validate_workspace(root)

    def test_the_home_directory_is_refused(self):
        from pathlib import Path
        with pytest.raises(ValueError, match="主目录"):
            server._validate_workspace(str(Path.home()))

    @pytest.mark.parametrize("name", [".ssh", ".aws", ".gnupg", ".kube", ".docker"])
    def test_credential_directories_are_refused(self, tmp_path, name):
        target = tmp_path / name / "sub"
        target.mkdir(parents=True)
        with pytest.raises(ValueError, match="敏感数据"):
            server._validate_workspace(str(target))

    def test_a_sensitive_directory_anywhere_in_the_path_is_refused(self, tmp_path):
        # Nesting a project under .ssh must not slip past a leaf-only check.
        target = tmp_path / ".ssh" / "projects" / "demo"
        target.mkdir(parents=True)
        with pytest.raises(ValueError, match="敏感数据"):
            server._validate_workspace(str(target))

    def test_an_ordinary_project_directory_is_accepted(self, tmp_path):
        target = tmp_path / "projects" / "my-app"
        target.mkdir(parents=True)
        assert server._validate_workspace(str(target)) == str(target.resolve())

    def test_a_broad_container_is_refused_only_at_its_own_level(self, tmp_path):
        """AppData holds both secrets and the system temp directory.

        Refusing it wherever it appears would reject every path beneath it —
        including this test's own tmp_path on Windows — without making anything
        safer. So the container and its direct children are refused, and
        ordinary project paths further down are allowed.
        """
        container = tmp_path / "AppData"
        (container / "Roaming").mkdir(parents=True)
        deep = container / "Local" / "Temp" / "projects" / "demo"
        deep.mkdir(parents=True)

        with pytest.raises(ValueError, match="系统区域"):
            server._validate_workspace(str(container))
        with pytest.raises(ValueError, match="系统区域"):
            server._validate_workspace(str(container / "Roaming"))
        assert server._validate_workspace(str(deep)) == str(deep.resolve())

    def test_a_credential_directory_is_refused_at_any_depth(self, tmp_path):
        """Unlike a broad container, nesting under one of these is the attack."""
        deep = tmp_path / ".aws" / "a" / "b" / "c"
        deep.mkdir(parents=True)
        with pytest.raises(ValueError, match="敏感数据"):
            server._validate_workspace(str(deep))

    def test_a_project_named_like_a_system_dir_is_still_allowed(self, tmp_path):
        # Component-exact matching, so "windows-client" is not "windows".
        target = tmp_path / "projects" / "windows-client"
        target.mkdir(parents=True)
        assert server._validate_workspace(str(target)) == str(target.resolve())

    def test_a_traversal_path_is_resolved_before_checking(self, tmp_path):
        (tmp_path / ".ssh").mkdir()
        sneaky = tmp_path / "projects" / ".." / ".ssh"
        (tmp_path / "projects").mkdir()
        with pytest.raises(ValueError, match="敏感数据"):
            server._validate_workspace(str(sneaky))


class TestAvatarIsOptional:
    """Creating an Agent without picking an avatar must not fail on the avatar.

    An untouched file input still yields an empty File, so the page used to
    send a data URL of an empty octet-stream and the server rejected it as a
    bad image. Both dialogs share the reader, so the hosted flow had it too.
    """

    APP = (
        PROJECT_ROOT / ".openharness" / "plugins" / "investment-research"
        / "workbench" / "app.js"
    )

    def test_the_reader_treats_an_empty_file_as_no_file(self):
        source = self.APP.read_text(encoding="utf-8")
        assert "if(!file || !file.size) return Promise.resolve('');" in source

    def test_the_server_accepts_an_absent_avatar(self):
        assert server._save_avatar("") is None
        assert server._save_avatar(None or "") is None

    def test_the_server_still_rejects_a_non_image(self):
        with pytest.raises(ValueError, match="avatar"):
            server._save_avatar("data:application/octet-stream;base64,AAAA")

    def test_a_local_agent_can_be_created_without_an_avatar(self, store, tmp_path):
        workspace = tmp_path / "demo"
        workspace.mkdir()
        agent = server._create_local_agent(body(workspace=str(workspace)), None)
        assert agent["avatar_path"] is None


class TestHostedAgentsAreUnaffected:
    def test_an_existing_custom_agent_still_creates(self, store):
        agent = store.create_agent(
            name="研究助手", profile="p", role="r",
            system_prompt="s", model="deepseek-v4-flash",
        )
        assert agent["agent_type"] == "hosted"
        assert agent["type"] == "custom"
        assert agent["agent_id"].startswith("custom-")

    def test_a_hosted_agent_has_no_workspace_or_bridge(self, store):
        agent = store.create_agent(
            name="研究助手", profile="p", role="r",
            system_prompt="s", model="deepseek-v4-flash",
        )
        assert agent["workspace"] is None
        assert agent["bridge_id"] is None
