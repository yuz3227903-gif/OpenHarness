"""Fireworks Ark Plan model catalog used by the research workbench."""

from __future__ import annotations

from typing import Any


ARK_PLAN_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
DEFAULT_MODEL = "ark-code-latest"

MODEL_CATALOG: list[dict[str, Any]] = [
    {"id": "ark-code-latest", "name": "Ark Code Latest", "status": "default", "description": "默认入口；目标模型由方舟控制台管理，支持 Auto 调度，切换后约 3–5 分钟生效。"},
    {"id": "auto", "name": "Auto", "status": "enabled", "description": "基于效果与速度自动匹配模型和算力组合。"},
    {"id": "doubao-seed-evolving", "name": "Doubao-Seed-Evolving", "status": "enabled", "description": "面向 Coding 与 Agent 场景持续升级，支持超长上下文与复杂任务编排。"},
    {"id": "doubao-seed-2.1-turbo", "name": "Doubao-Seed-2.1-turbo", "status": "enabled", "description": "效果与成本均衡，适合复杂 Coding、Agent 与多模态任务。"},
    {"id": "doubao-seed-2.0-lite", "name": "Doubao-Seed-2.0-lite", "status": "enabled", "description": "兼顾质量与速度的通用生产级模型，默认开启深度思考。"},
    {"id": "doubao-seed-2.0-mini", "name": "Doubao-Seed-2.0-mini", "status": "enabled", "description": "低时延、高并发和成本优先的轻量模型，默认开启深度思考。"},
    {"id": "glm-5.3", "name": "GLM-5.3", "status": "preview", "description": "尝鲜旗舰模型，擅长编程、网络安全和长任务。"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash", "status": "enabled", "description": "强调 Agent 能力与响应速度的正式版本。"},
    {"id": "kimi-k3", "name": "Kimi-K3", "status": "medium-plan", "description": "旗舰多模态模型，适合软件工程、知识工作和深度推理。"},
    {"id": "glm-5.2", "name": "GLM-5.2", "status": "enabled", "description": "支持超长上下文，长程任务表现突出。"},
    {"id": "kimi-k2.7-code", "name": "Kimi-K2.7-Code", "status": "preview", "description": "长上下文 Coding 模型，支持文本、图片与视频输入。"},
    {"id": "minimax-m3", "name": "MiniMax-M3", "status": "enabled", "description": "适用于 Agent 推理、工具调用、代码和长上下文任务。"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro", "status": "high-factor", "description": "适合复杂问题的高能力模型，默认开启深度思考。"},
    {"id": "minimax-m2.7", "name": "MiniMax-M2.7", "status": "retiring", "description": "即将下线；支持 Agent Teams、Skills 与复杂工具编排。"},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6", "status": "retiring", "description": "即将下线；默认开启深度思考。"},
]


def model_ids() -> list[str]:
    return [str(item["id"]) for item in MODEL_CATALOG]


__all__ = ["ARK_PLAN_BASE_URL", "DEFAULT_MODEL", "MODEL_CATALOG", "model_ids"]
