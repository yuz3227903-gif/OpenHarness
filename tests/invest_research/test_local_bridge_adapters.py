"""Detection belongs to the adapters, and it must report honestly."""

from __future__ import annotations

import subprocess

import pytest

from openharness.local_bridge.adapters import registry
from openharness.local_bridge.adapters.base import (
    AgentCapabilities,
    DetectionResult,
    LocalAgentAdapter,
)
from openharness.local_bridge.adapters.codex import CodexAdapter
from openharness.local_bridge.adapters.openclaw import OpenClawAdapter


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def codex():
    return CodexAdapter()


def _found(monkeypatch, path="/usr/local/bin/codex"):
    monkeypatch.setattr("openharness.local_bridge.adapters.base.shutil.which",
                        lambda name: path)


def _run(monkeypatch, result):
    def fake_run(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr("openharness.local_bridge.adapters.base.subprocess.run", fake_run)


class TestDetection:
    def test_a_missing_binary_is_reported_as_not_installed(self, codex, monkeypatch):
        monkeypatch.setattr("openharness.local_bridge.adapters.base.shutil.which",
                            lambda name: None)
        result = codex.detect()
        assert (result.installed, result.status) == (False, "not_installed")
        assert result.version is None
        assert "codex" in result.detail

    def test_a_working_binary_reports_its_version(self, codex, monkeypatch):
        _found(monkeypatch)
        _run(monkeypatch, _Completed(stdout="codex-cli 0.5.1\n"))
        result = codex.detect()
        assert (result.installed, result.status) == (True, "available")
        assert result.version == "0.5.1"
        assert result.executable == "/usr/local/bin/codex"

    def test_a_hanging_probe_is_reported_not_treated_as_available(self, codex, monkeypatch):
        _found(monkeypatch)
        _run(monkeypatch, subprocess.TimeoutExpired(cmd="codex", timeout=6))
        result = codex.detect()
        # Installed but unusable is its own state; it must not read as ready.
        assert (result.installed, result.status) == (True, "error")
        assert "超过" in result.detail

    def test_a_non_zero_exit_is_reported(self, codex, monkeypatch):
        _found(monkeypatch)
        _run(monkeypatch, _Completed(stderr="boom", returncode=2))
        result = codex.detect()
        assert result.status == "error"
        assert "退出码 2" in result.detail

    def test_an_unexecutable_binary_is_reported(self, codex, monkeypatch):
        _found(monkeypatch)
        _run(monkeypatch, OSError("permission denied"))
        result = codex.detect()
        assert result.status == "error"
        assert "无法执行" in result.detail

    def test_a_version_on_stderr_is_still_read(self, codex, monkeypatch):
        _found(monkeypatch)
        _run(monkeypatch, _Completed(stdout="", stderr="codex 1.2.3"))
        assert codex.detect().version == "1.2.3"

    def test_the_probe_never_goes_through_a_shell(self, codex, monkeypatch):
        """A shell would make the executable path an injection surface."""
        seen = {}
        _found(monkeypatch)

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["shell"] = kwargs.get("shell")
            seen["timeout"] = kwargs.get("timeout")
            return _Completed(stdout="0.1.0")

        monkeypatch.setattr("openharness.local_bridge.adapters.base.subprocess.run", fake_run)
        codex.detect()
        assert isinstance(seen["args"], list)
        assert seen["shell"] is False
        assert seen["timeout"] and seen["timeout"] > 0


class TestCapabilities:
    @pytest.mark.parametrize("adapter", [CodexAdapter(), OpenClawAdapter()])
    def test_every_adapter_declares_capabilities(self, adapter):
        capabilities = adapter.capabilities()
        assert isinstance(capabilities, AgentCapabilities)
        assert capabilities.chat is True
        assert set(capabilities.to_dict()) == {
            "chat", "streaming", "shell", "file_read", "file_write",
            "diff", "mcp", "skills", "resume_session", "approval",
        }

    def test_capabilities_travel_with_the_detection(self, monkeypatch):
        _found(monkeypatch)
        _run(monkeypatch, _Completed(stdout="0.1.0"))
        # The UI decides what to show from this, so it must not arrive empty.
        assert CodexAdapter().detect().capabilities["shell"] is True


class TestRegistry:
    def test_the_shipped_adapters_are_registered(self):
        assert set(registry.available_providers()) >= {"codex", "openclaw"}

    def test_an_unknown_provider_returns_none(self):
        assert registry.get_adapter("does-not-exist") is None

    def test_detect_all_covers_every_registered_adapter(self, monkeypatch):
        monkeypatch.setattr("openharness.local_bridge.adapters.base.shutil.which",
                            lambda name: None)
        results = registry.detect_all()
        assert {item["provider"] for item in results} >= {"codex", "openclaw"}
        assert all(item["status"] == "not_installed" for item in results)

    def test_one_broken_adapter_does_not_hide_the_others(self, monkeypatch):
        class Exploding(LocalAgentAdapter):
            provider = "exploding"
            display_name = "Exploding"
            executables = ("nope",)

            def capabilities(self):
                return AgentCapabilities()

            def detect(self):
                raise RuntimeError("adapter is broken")

        registry.register_adapter(Exploding())
        try:
            monkeypatch.setattr("openharness.local_bridge.adapters.base.shutil.which",
                                lambda name: None)
            results = {item["provider"]: item for item in registry.detect_all()}
            assert results["exploding"]["status"] == "error"
            assert "adapter is broken" in results["exploding"]["detail"]
            # The healthy adapters still reported.
            assert results["codex"]["status"] == "not_installed"
        finally:
            registry._registry().pop("exploding", None)

    def test_an_incomplete_adapter_fails_at_import_time(self):
        with pytest.raises(TypeError, match="provider"):
            class Nameless(LocalAgentAdapter):
                display_name = "Nameless"
                executables = ("x",)

                def capabilities(self):
                    return AgentCapabilities()

    def test_results_are_serialisable(self, monkeypatch):
        monkeypatch.setattr("openharness.local_bridge.adapters.base.shutil.which",
                            lambda name: None)
        import json
        json.dumps(registry.detect_all())  # must not raise

    def test_a_detection_result_round_trips_to_a_dict(self):
        result = DetectionResult(
            provider="codex", display_name="Codex", installed=True,
            status="available", version="1.0",
        )
        assert result.to_dict()["provider"] == "codex"
