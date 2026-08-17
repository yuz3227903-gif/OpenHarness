"""Calling one of an Agent's Skills by name from the chat box.

An installed Skill already shapes how an Agent works. A ``/`` command is the
other half: it says *use this one, now, on this*. The Skill's own instructions
move to the front of the turn and the rest of the message becomes its input.

Nothing here executes an uploaded file. A Skill is instruction text, so calling
one means putting those instructions in front of the model — the same thing an
operator does by installing it, scoped to a single turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: ``/name rest of the message``. The name stops at whitespace, so an argument
#: can be anything including Chinese text and punctuation.
_COMMAND = re.compile(r"^\s*/([A-Za-z0-9][A-Za-z0-9_\-.]{0,63})\s*(.*)$", re.DOTALL)


class UnknownSkill(ValueError):
    """The message called a Skill this Agent does not have."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        if available:
            listed = "、".join(f"/{item}" for item in available[:8])
            super().__init__(f"没有找到技能 /{name}；当前可用：{listed}")
        else:
            super().__init__(f"没有找到技能 /{name}；这个 Agent 还没有启用任何技能。")


@dataclass(frozen=True)
class SkillInvocation:
    """One ``/skill`` call, resolved against what the Agent actually has."""

    skill_id: str
    name: str
    slug: str
    argument: str
    instructions: str

    def prompt_layer(self) -> str:
        """The block that goes at the top of this turn's system prompt."""

        head = (
            f"用户用 /{self.slug} 显式调用了技能「{self.name}」。"
            "本轮请按这个技能的方法执行，不要泛泛作答。"
        )
        body = self.instructions.strip()
        return f"{head}\n\n{body}" if body else head


def slugify(name: str) -> str:
    """The handle a Skill answers to after a slash.

    Built from the Skill's own name so what the user types matches what the
    profile card shows, rather than an id nobody has seen.
    """

    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "-", str(name or "").strip()).strip("-")
    return cleaned.lower()[:64]


def parse_command(text: str) -> tuple[str, str] | None:
    """Split ``/name argument``, or ``None`` when this is ordinary chat."""

    match = _COMMAND.match(text or "")
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def available_slugs(skills: list[dict[str, Any]]) -> list[str]:
    return [
        slugify(item.get("name") or item.get("filename") or "")
        for item in skills if item.get("enabled")
    ]


def resolve(
    text: str, skills: list[dict[str, Any]], *, read_body: Any,
) -> SkillInvocation | None:
    """Match a ``/`` command against this Agent's enabled Skills.

    ``read_body`` is injected so this stays independent of where Skill files
    live. Raises :class:`UnknownSkill` when a command names something the Agent
    does not have — guessing the closest Skill would run the wrong one.
    """

    parsed = parse_command(text)
    if parsed is None:
        return None
    name, argument = parsed
    enabled = [item for item in skills if item.get("enabled")]
    wanted = slugify(name)
    for item in enabled:
        label = str(item.get("name") or item.get("filename") or "")
        if slugify(label) == wanted:
            return SkillInvocation(
                skill_id=str(item.get("skill_id") or ""),
                name=label,
                slug=wanted,
                argument=argument,
                instructions=str(read_body(item) or ""),
            )
    raise UnknownSkill(name, available_slugs(enabled))


__all__ = [
    "SkillInvocation",
    "UnknownSkill",
    "available_slugs",
    "parse_command",
    "resolve",
    "slugify",
]
