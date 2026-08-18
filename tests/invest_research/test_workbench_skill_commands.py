"""Calling an installed Skill by name, and reading a packaged one."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openharness.invest_research import workbench_skill_commands as commands  # noqa: E402
from openharness.invest_research.agent_skills import read_packaged_skill  # noqa: E402


def skill(name, *, enabled=True, skill_id=None, body="按这个方法做"):
    return {
        "skill_id": skill_id or f"SKILL-{name}", "name": name, "enabled": enabled,
        "filename": f"{name}.md", "body": body,
    }


def read_body(record):
    return record.get("body", "")


class TestParsingTheCommand:
    def test_a_leading_slash_is_a_command(self):
        assert commands.parse_command("/audit 看一下这季度") == ("audit", "看一下这季度")

    def test_a_command_with_no_argument_is_still_a_command(self):
        assert commands.parse_command("/audit") == ("audit", "")

    def test_ordinary_chat_is_not_a_command(self):
        assert commands.parse_command("今天的估值怎么看？") is None

    def test_a_slash_in_the_middle_is_not_a_command(self):
        assert commands.parse_command("看看 A/B 测试") is None

    def test_a_multi_line_argument_survives(self):
        parsed = commands.parse_command("/audit 第一行\n第二行")
        assert parsed == ("audit", "第一行\n第二行")


class TestSlugs:
    def test_a_name_becomes_a_typeable_handle(self):
        assert commands.slugify("Cash Flow Audit") == "cash-flow-audit"

    def test_punctuation_and_non_ascii_collapse(self):
        # A handle has to be typeable in the composer, so anything that is not
        # a letter, digit, dash or underscore becomes a dash.
        assert commands.slugify("Audit :: Cash!!") == "audit-cash"

    def test_case_does_not_matter(self):
        assert commands.slugify("AUDIT") == commands.slugify("audit")


class TestResolving:
    def test_a_named_skill_is_found(self):
        found = commands.resolve("/audit 这季度", [skill("audit")], read_body=read_body)
        assert found is not None
        assert (found.name, found.argument) == ("audit", "这季度")

    def test_the_skill_instructions_lead_the_turn(self):
        found = commands.resolve(
            "/audit x", [skill("audit", body="第一步：核对口径")], read_body=read_body,
        )
        layer = found.prompt_layer()
        assert "显式调用了技能" in layer
        assert "第一步：核对口径" in layer

    def test_a_disabled_skill_cannot_be_called(self):
        with pytest.raises(commands.UnknownSkill):
            commands.resolve("/audit x", [skill("audit", enabled=False)], read_body=read_body)

    def test_an_unknown_skill_is_refused_not_guessed(self):
        # Running the closest Skill would answer a question nobody asked.
        with pytest.raises(commands.UnknownSkill) as caught:
            commands.resolve("/auditt x", [skill("audit")], read_body=read_body)
        assert "/audit" in str(caught.value)

    def test_the_error_says_so_when_there_are_no_skills_at_all(self):
        with pytest.raises(commands.UnknownSkill, match="还没有启用任何技能"):
            commands.resolve("/audit", [], read_body=read_body)

    def test_a_name_with_spaces_is_callable_by_its_slug(self):
        found = commands.resolve(
            "/cash-flow-audit 看一下", [skill("Cash Flow Audit")], read_body=read_body,
        )
        assert found.name == "Cash Flow Audit"

    def test_ordinary_chat_resolves_to_nothing(self):
        assert commands.resolve("你好", [skill("audit")], read_body=read_body) is None

    def test_available_slugs_lists_only_enabled_skills(self):
        listed = commands.available_slugs([skill("a"), skill("b", enabled=False)])
        assert listed == ["a"]


class TestPackagedSkills:
    def _zip(self, path, files):
        with zipfile.ZipFile(path, "w") as bundle:
            for name, content in files.items():
                bundle.writestr(name, content)
        return path

    def test_skill_md_is_read_out_of_the_archive(self, tmp_path):
        archive = self._zip(tmp_path / "s.zip", {
            "SKILL.md": "# 现金流审查\n第一步：核对口径",
            "examples/one.md": "示例",
        })
        body = read_packaged_skill(archive)
        assert "核对口径" in body
        # A packaged Skill is a folder; saying what else is in it is useful.
        assert "one.md" in body

    def test_a_nested_skill_md_is_found(self, tmp_path):
        archive = self._zip(tmp_path / "s.zip", {"pack/SKILL.md": "内容在子目录里"})
        assert "内容在子目录里" in read_packaged_skill(archive)

    def test_a_readme_is_the_fallback(self, tmp_path):
        archive = self._zip(tmp_path / "s.zip", {"README.md": "说明文字"})
        assert "说明文字" in read_packaged_skill(archive)

    def test_the_shallowest_markdown_wins(self, tmp_path):
        archive = self._zip(tmp_path / "s.zip", {
            "top.md": "顶层文档", "deep/deeper/notes.md": "埋得很深",
        })
        assert "顶层文档" in read_packaged_skill(archive)

    def test_an_archive_with_no_instructions_says_what_it_holds(self, tmp_path):
        archive = self._zip(tmp_path / "s.zip", {"logo.png": "x", "run.exe": "y"})
        body = read_packaged_skill(archive)
        assert "没有找到说明文件" in body
        assert "logo.png" in body

    def test_a_corrupt_archive_reports_itself(self, tmp_path):
        (tmp_path / "bad.zip").write_bytes(b"not a zip at all")
        assert "无法读取" in read_packaged_skill(tmp_path / "bad.zip")

    def test_an_archive_with_too_many_members_is_refused(self, tmp_path):
        files = {f"f{i}.md": "x" for i in range(250)}
        archive = self._zip(tmp_path / "big.zip", files)
        assert "文件过多" in read_packaged_skill(archive)

    def test_an_oversized_archive_is_refused(self, tmp_path):
        archive = self._zip(tmp_path / "huge.zip", {"SKILL.md": "x" * (5 * 1024 * 1024)})
        assert "解压后过大" in read_packaged_skill(archive)

    def test_mac_metadata_is_ignored(self, tmp_path):
        archive = self._zip(tmp_path / "s.zip", {
            "__MACOSX/._SKILL.md": "垃圾", "SKILL.md": "真正的内容",
        })
        assert "真正的内容" in read_packaged_skill(archive)
