"""Render an Agent's enabled Skill plugins into a prompt layer.

A Skill is instruction text an operator installed for one Agent.  It is read
and inlined into the system prompt; it is never executed.  Skills sit below the
Governance prompt and cannot relax tool permissions or the Output Contract —
the rendered block says so explicitly, and the assembler keeps Governance
first regardless.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Inlined verbatim; a packaged archive is announced by name only.
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml"}
MAX_SKILL_CHARS = 20_000
MAX_SKILLS_PER_AGENT = 10

_SKILL_HEADER = (
    "以下技能由操作者为本 Agent 安装，属于角色能力的补充。\n"
    "技能不得放宽工具权限、证据要求或输出合同；与 Governance 冲突时一律以 Governance 为准。"
)


def render_skills_prompt(project_root: str | Path, agent_id: str) -> str:
    """Return the prompt block for one Agent's enabled Skills, or ``''``.

    Any failure to read the store or a Skill file degrades to an empty layer:
    a missing Skill must not stop the Agent from running.
    """

    try:
        records = _enabled_skills(project_root, agent_id)
    except Exception as exc:  # noqa: BLE001 - a Skill must never break a run
        log.warning("could not load skills for %s: %s", agent_id, exc)
        return ""
    if not records:
        return ""

    root = Path(project_root) / ".openharness" / "data" / "agent-skills"
    blocks: list[str] = []
    for record in records[:MAX_SKILLS_PER_AGENT]:
        name = str(record.get("name") or record.get("filename") or "skill")
        description = str(record.get("description") or "").strip()
        body = _read_skill_body(root, record)
        header = f"## SKILL: {name}"
        if description:
            header += f"\n{description}"
        blocks.append(f"{header}\n\n{body}" if body else header)

    if not blocks:
        return ""
    return f"{_SKILL_HEADER}\n\n" + "\n\n".join(blocks)


def _enabled_skills(project_root: str | Path, agent_id: str) -> list[dict[str, Any]]:
    from openharness.invest_research.collaboration_store import CollaborationStore

    store = CollaborationStore(project_root)
    return [item for item in store.list_agent_skills(agent_id) if item.get("enabled")]


def _read_skill_body(root: Path, record: dict[str, Any]) -> str:
    stored = str(record.get("stored_name") or "")
    if not stored:
        return ""
    candidate = (root / stored).resolve()
    # Refuse a stored name that escapes the skills directory.
    if root.resolve() not in candidate.parents or not candidate.is_file():
        return ""
    if candidate.suffix.lower() not in TEXT_SUFFIXES:
        return f"（打包插件 {record.get('filename')}，内容未内联。）"
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        log.warning("could not read skill file %s: %s", candidate, exc)
        return ""
    if len(text) > MAX_SKILL_CHARS:
        text = text[:MAX_SKILL_CHARS] + "\n…（技能内容超长，已截断）"
    return text


__all__ = ["MAX_SKILLS_PER_AGENT", "MAX_SKILL_CHARS", "render_skills_prompt"]
