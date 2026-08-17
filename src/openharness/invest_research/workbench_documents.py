"""Turning an uploaded file into something an Agent can actually read.

Attaching a document to a message only means something if the Agent sees its
contents. A chat turn is text, so each attachment becomes a bounded excerpt of
text — and when it cannot, the Agent is told that plainly rather than being
handed a filename it will then pretend to have read.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: How much of one document is put in front of an Agent. Enough for a memo or a
#: statement extract; past this the transcript crowds out the conversation.
EXCERPT_CHARS = 4000
#: Rows read from a spreadsheet-like file. The head of a table is what says what
#: the table is.
CSV_ROWS = 40

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".sql",
    ".ini", ".cfg", ".toml",
}


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _read_csv(text: str) -> str:
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return text
    kept = rows[:CSV_ROWS]
    rendered = "\n".join(" | ".join(cell.strip() for cell in row) for row in kept)
    if len(rows) > CSV_ROWS:
        rendered += f"\n…（共 {len(rows)} 行，仅显示前 {CSV_ROWS} 行）"
    return rendered


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a declared dependency
        raise ValueError("这台机器没有安装 PDF 解析库，无法读取内容") from None
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 - a corrupt PDF is a content problem
        raise ValueError(f"PDF 无法解析：{exc}") from exc
    parts = []
    for page in reader.pages[:20]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page must not lose the rest
            continue
    text = "\n".join(part for part in parts if part.strip())
    if not text.strip():
        raise ValueError("这份 PDF 里没有可提取的文字（可能是扫描件）")
    return text


def extract_text(path: Path, *, filename: str = "", media_type: str = "") -> str:
    """Read one file as text, or say why it cannot be read.

    Raises ``ValueError`` with a reason the Agent can repeat to the user. That
    is deliberately louder than returning an empty string: a silent blank would
    let the Agent talk about a document it never saw.
    """

    suffix = Path(filename or path.name).suffix.lower()
    if not path.is_file():
        raise ValueError("文件不在本地存储中")
    if suffix == ".pdf" or media_type == "application/pdf":
        return _read_pdf(path)
    if suffix in TEXT_SUFFIXES or media_type.startswith("text/") or media_type == "application/json":
        text = _decode(path.read_bytes())
        if suffix in {".csv", ".tsv"}:
            return _read_csv(text)
        if suffix == ".json":
            try:
                return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                return text
        return text
    if media_type.startswith("image/"):
        raise ValueError("这是一张图片，当前对话通道只能读取文字内容")
    raise ValueError(f"暂不支持读取 {suffix or media_type or '这种'} 格式的内容")


def describe_attachment(
    attachment: dict[str, Any], *, file_root: Path, limit: int = EXCERPT_CHARS,
) -> str:
    """One attachment, rendered for a prompt."""

    filename = str(attachment.get("filename") or "未命名文件")
    stored = str(attachment.get("stored_name") or "")
    media_type = str(attachment.get("media_type") or "")
    size = attachment.get("size_bytes")
    header = f"【附件：{filename}"
    if isinstance(size, int) and size > 0:
        header += f"，{size} 字节"
    header += "】"

    if not stored:
        return f"{header}\n（无法读取：没有记录存储位置）"
    candidate = (file_root / stored).resolve()
    # A stored name comes from this server, but it still must not escape the
    # upload root — a path is never trusted just because it was written down.
    if file_root.resolve() not in candidate.parents:
        return f"{header}\n（无法读取：文件路径不在上传目录内）"
    try:
        text = extract_text(candidate, filename=filename, media_type=media_type)
    except ValueError as exc:
        return f"{header}\n（无法读取：{exc}）"
    except OSError as exc:
        log.warning("attachment read failed: %s", exc)
        return f"{header}\n（无法读取：{exc}）"

    body = text.strip()
    if len(body) > limit:
        body = f"{body[:limit]}\n…（内容较长，仅显示前 {limit} 字）"
    return f"{header}\n{body}"


def render_attachments(
    attachments: list[dict[str, Any]], *, file_root: Path, limit: int = EXCERPT_CHARS,
) -> str:
    """Every attachment on one message, as prompt text."""

    return "\n\n".join(
        describe_attachment(item, file_root=file_root, limit=limit)
        for item in attachments if isinstance(item, dict)
    )


__all__ = [
    "EXCERPT_CHARS",
    "describe_attachment",
    "extract_text",
    "render_attachments",
]
