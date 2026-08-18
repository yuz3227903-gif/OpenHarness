from __future__ import annotations

from openharness.invest_research.workbench_models import (
    ARK_PLAN_BASE_URL,
    DEFAULT_MODEL,
    MODEL_CATALOG,
    model_ids,
)


def test_ark_plan_catalog_has_default_and_user_selectable_models() -> None:
    assert ARK_PLAN_BASE_URL == "https://ark.cn-beijing.volces.com/api/plan/v3"
    assert DEFAULT_MODEL == "ark-code-latest"
    ids = model_ids()
    assert ids[0] == "ark-code-latest"
    assert "auto" in ids
    assert "doubao-seed-evolving" in ids
    assert "doubao-seed-2.1-turbo" in ids
    assert "deepseek-v4-flash" in ids
    assert "minimax-m3" in ids
    assert "kimi-k2.6" in ids
    assert all({"id", "name", "description", "status"} <= item.keys() for item in MODEL_CATALOG)


def test_workbench_exposes_model_catalog_without_credentials() -> None:
    from openharness.invest_research import workbench_server

    payload = workbench_server._model_settings_payload()
    assert payload["provider"] == "ark-plan"
    assert payload["base_url"] == ARK_PLAN_BASE_URL
    assert payload["default_model"] == DEFAULT_MODEL
    assert payload["api_key_configured"] in {True, False}
    assert "api_key" not in payload
    assert payload["models"] == MODEL_CATALOG
