"""Render an Agent's enabled Skill plugins into a prompt layer.

A Skill is instruction text an operator installed for one Agent.  It is read
and inlined into the system prompt; it is never executed.  Skills sit below the
Governance prompt and cannot relax tool permissions or the Output Contract —
the rendered block says so explicitly, and the assembler keeps Governance
first regardless.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Inlined verbatim.
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml"}
MAX_SKILL_CHARS = 20_000
MAX_SKILLS_PER_AGENT = 10
#: A packaged Skill is a folder of instructions. These are the names that
#: normally hold the instructions themselves, in the order they are preferred.
SKILL_ENTRY_NAMES = ("skill.md", "skill.markdown", "readme.md", "index.md", "skill.txt")
#: Guards against an archive that would fill memory: an instruction pack is a
#: handful of small text files, never thousands or hundreds of megabytes.
MAX_ZIP_MEMBERS = 200
MAX_ZIP_UNPACKED_BYTES = 4 * 1024 * 1024
#: 技能包套着技能包是常事：从别处导出的一堆技能压在一个 zip 里，或者打包时多
#: 压了一层。往里再找两层就够覆盖这些情况；再深的嵌套多半不是技能包，而是
#: 拿来撑爆解压的东西，所以这里是硬上限而不是提示。
MAX_ZIP_DEPTH = 2

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


def read_packaged_skill(archive: Path) -> str:
    """Pull the instructions out of a zipped Skill.

    A packaged Skill is a folder: the instructions are normally in SKILL.md,
    with supporting files alongside. Announcing the archive by name told the
    Agent a plugin existed without telling it what the plugin says, which is
    the same as not installing it — so the text is inlined here instead.
    """

    try:
        with zipfile.ZipFile(archive) as bundle:
            text = _read_bundle(bundle, label=archive.name, depth=0)
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("could not read packaged skill %s: %s", archive, exc)
        return f"（插件包 {archive.name} 无法读取：{exc}）"
    return text if text is not None else f"（插件包 {archive.name} 里没有找到说明文件。）"


def _read_bundle(bundle: zipfile.ZipFile, *, label: str, depth: int) -> str | None:
    """把一个压缩包里的说明文字取出来；取不到返回 ``None``。

    找的顺序是：这一层有没有明确的入口文件（SKILL.md 之类）→ 里面套的 zip 有
    没有 → 这一层退而求其次的那个文本文件。套的包排在启发式挑选之前，是因为
    外层常常只剩许可证或说明性的 README，真正的技能在里面那个包里；先挑外层
    就会把一份不相干的文本当成技能装上。
    """

    members = [
        item for item in bundle.infolist()
        if not item.is_dir() and not item.filename.startswith("__MACOSX/")
    ]
    too_big = (
        len(members) > MAX_ZIP_MEMBERS
        or sum(item.file_size for item in members) > MAX_ZIP_UNPACKED_BYTES
    )
    if too_big:
        # 内层跳过就是了，外面还有别的包可以试；最外层要说清楚为什么没内联。
        if depth:
            return None
        if len(members) > MAX_ZIP_MEMBERS:
            return f"（插件包 {label} 文件过多，未内联。）"
        return f"（插件包 {label} 解压后过大，未内联。）"

    chosen = _pick_named_entry(members)
    if chosen is None and depth < MAX_ZIP_DEPTH:
        nested = _read_nested_bundles(bundle, members, depth=depth)
        if nested is not None:
            return nested
    if chosen is None:
        chosen = _pick_entry(members)
    if chosen is None:
        if depth:
            return None            # 内层没有就换下一个内层包，不用解释
        names = "、".join(Path(item.filename).name for item in members[:8])
        return f"（插件包 {label} 里没有找到说明文件。包含：{names}）"

    with bundle.open(chosen) as handle:
        text = handle.read(MAX_SKILL_CHARS * 4).decode("utf-8", errors="replace")
    other = [
        Path(item.filename).name for item in members
        if item.filename != chosen.filename
    ][:12]
    extras = f"\n\n（同包内其他文件：{'、'.join(other)}）" if other else ""
    return text.strip() + extras


def _read_nested_bundles(
    bundle: zipfile.ZipFile, members: list[zipfile.ZipInfo], *, depth: int,
) -> str | None:
    """依次进入包里套着的 zip，返回第一个能读出说明的。

    内层包整个读进内存，所以先按外层已经算过的解压上限拦一道——一个技能包是
    几个小文本文件，不该有几十兆的内层包。读坏一个不影响接着试下一个：一个损
    坏的附件不该让整个技能装不上。
    """

    inner =[item for item in members if Path(item.filename).suffix.lower() == ".zip"]
    for item in sorted(inner, key=lambda entry: (entry.filename.count("/"), entry.filename)):
        if item.file_size > MAX_ZIP_UNPACKED_BYTES:
            continue
        try:
            with bundle.open(item) as handle:
                payload = handle.read(MAX_ZIP_UNPACKED_BYTES + 1)
            if len(payload) > MAX_ZIP_UNPACKED_BYTES:
                continue
            with zipfile.ZipFile(io.BytesIO(payload)) as child:
                text = _read_bundle(
                    child, label=Path(item.filename).name, depth=depth + 1,
                )
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            log.warning("could not read nested skill archive %s: %s", item.filename, exc)
            continue
        if text:
            return text
    return None


def _pick_named_entry(members: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    """这一层有没有叫得出名字的入口文件（SKILL.md 等）。"""

    by_name = {Path(item.filename).name.lower(): item for item in members}
    for preferred in SKILL_ENTRY_NAMES:
        if preferred in by_name:
            return by_name[preferred]
    return None


def _pick_entry(members: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    """Which file in the archive holds the instructions."""

    named = _pick_named_entry(members)
    if named is not None:
        return named
    # Otherwise the shallowest markdown file: a top-level doc beats one buried
    # in an examples folder.
    markdown = [
        item for item in members
        if Path(item.filename).suffix.lower() in {".md", ".markdown"}
    ]
    if markdown:
        return min(markdown, key=lambda item: (item.filename.count("/"), item.filename))
    text_files = [
        item for item in members if Path(item.filename).suffix.lower() in TEXT_SUFFIXES
    ]
    if text_files:
        return min(text_files, key=lambda item: (item.filename.count("/"), item.filename))
    return None


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
    if candidate.suffix.lower() == ".zip":
        text = read_packaged_skill(candidate)
    elif candidate.suffix.lower() not in TEXT_SUFFIXES:
        return f"（打包插件 {record.get('filename')}，内容未内联。）"
    else:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            log.warning("could not read skill file %s: %s", candidate, exc)
            return ""
    if len(text) > MAX_SKILL_CHARS:
        text = text[:MAX_SKILL_CHARS] + "\n…（技能内容超长，已截断）"
    return text


__all__ = [
    "MAX_SKILLS_PER_AGENT",
    "MAX_SKILL_CHARS",
    "read_packaged_skill",
    "render_skills_prompt",
]
