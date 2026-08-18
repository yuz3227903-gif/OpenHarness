"""Reading an attached document so an Agent can actually talk about it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from openharness.invest_research.workbench_documents import (  # noqa: E402
    describe_attachment,
    extract_text,
    render_attachments,
)


def attachment(tmp_path, name, payload, media_type="text/plain", *, encoding="utf-8"):
    (tmp_path / name).write_bytes(payload.encode(encoding) if isinstance(payload, str) else payload)
    return {
        "file_id": f"F-{name}", "filename": name, "stored_name": name,
        "media_type": media_type, "size_bytes": (tmp_path / name).stat().st_size,
    }


class TestReadingText:
    def test_a_text_file_is_read(self, tmp_path):
        (tmp_path / "memo.txt").write_text("第一季度收入 12 亿", encoding="utf-8")
        assert "12 亿" in extract_text(tmp_path / "memo.txt", filename="memo.txt")

    def test_a_gbk_file_is_not_mojibake(self, tmp_path):
        # Chinese documents on Windows are routinely GB18030, and a wrong guess
        # would hand the model a page of garbage it would then summarise.
        (tmp_path / "gbk.txt").write_bytes("营业收入同比增长".encode("gb18030"))
        assert extract_text(tmp_path / "gbk.txt", filename="gbk.txt") == "营业收入同比增长"

    def test_json_is_pretty_printed(self, tmp_path):
        (tmp_path / "d.json").write_text(json.dumps({"revenue": 12}), encoding="utf-8")
        out = extract_text(tmp_path / "d.json", filename="d.json")
        assert "revenue" in out and "\n" in out

    def test_a_csv_shows_its_head(self, tmp_path):
        rows = "\n".join(f"{i},row{i}" for i in range(100))
        (tmp_path / "t.csv").write_text("id,name\n" + rows, encoding="utf-8")
        out = extract_text(tmp_path / "t.csv", filename="t.csv")
        assert "id | name" in out
        assert "共 101 行" in out

    def test_an_unknown_binary_says_so(self, tmp_path):
        (tmp_path / "a.bin").write_bytes(b"\x00\x01\x02")
        with pytest.raises(ValueError, match="暂不支持"):
            extract_text(tmp_path / "a.bin", filename="a.bin")

    def test_an_image_says_it_cannot_be_read_as_text(self, tmp_path):
        (tmp_path / "a.png").write_bytes(b"\x89PNG")
        with pytest.raises(ValueError, match="图片"):
            extract_text(tmp_path / "a.png", filename="a.png", media_type="image/png")

    def test_a_missing_file_is_an_error_not_an_empty_string(self, tmp_path):
        with pytest.raises(ValueError, match="不在本地存储"):
            extract_text(tmp_path / "gone.txt", filename="gone.txt")


class TestDescribingForAPrompt:
    def test_the_excerpt_carries_the_filename_and_content(self, tmp_path):
        item = attachment(tmp_path, "memo.txt", "毛利率下滑 3 个百分点")
        out = describe_attachment(item, file_root=tmp_path)
        assert "memo.txt" in out
        assert "毛利率下滑 3 个百分点" in out

    def test_a_long_document_is_trimmed_with_a_notice(self, tmp_path):
        item = attachment(tmp_path, "long.txt", "字" * 9000)
        out = describe_attachment(item, file_root=tmp_path, limit=200)
        assert "仅显示前 200 字" in out
        assert len(out) < 600

    def test_an_unreadable_file_says_why_instead_of_going_quiet(self, tmp_path):
        item = attachment(tmp_path, "a.png", "x", media_type="image/png")
        out = describe_attachment(item, file_root=tmp_path)
        # Silence would let the Agent invent contents for a file it never read.
        assert "无法读取" in out and "图片" in out

    def test_a_path_outside_the_upload_root_is_refused(self, tmp_path):
        secret = tmp_path.parent / "secret.txt"
        secret.write_text("private", encoding="utf-8")
        item = {"filename": "secret.txt", "stored_name": "../secret.txt",
                "media_type": "text/plain"}
        out = describe_attachment(item, file_root=tmp_path)
        assert "不在上传目录内" in out
        assert "private" not in out

    def test_an_attachment_with_no_stored_name_is_reported(self, tmp_path):
        out = describe_attachment({"filename": "x.txt"}, file_root=tmp_path)
        assert "无法读取" in out

    def test_several_attachments_are_rendered_together(self, tmp_path):
        one = attachment(tmp_path, "a.txt", "内容甲")
        two = attachment(tmp_path, "b.txt", "内容乙")
        out = render_attachments([one, two], file_root=tmp_path)
        assert "内容甲" in out and "内容乙" in out

    def test_no_attachments_render_to_nothing(self, tmp_path):
        assert render_attachments([], file_root=tmp_path) == ""
