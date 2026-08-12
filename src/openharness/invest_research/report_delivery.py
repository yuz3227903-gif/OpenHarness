"""Deterministic Markdown and validation artifact delivery helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def run_output_dir(project_root: Path, run_id: str) -> Path:
    safe_run_id = "".join(
        character for character in run_id if character.isalnum() or character in "-_"
    )
    if safe_run_id != run_id or not safe_run_id.startswith("RUN-"):
        raise ValueError(f"unsafe run_id for output path: {run_id}")
    return project_root / ".openharness" / "validation" / safe_run_id


def write_report_context(
    project_root: Path, run_id: str, context: dict[str, Any]
) -> Path:
    path = run_output_dir(project_root, run_id) / "report-context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(context, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    return path


def write_report_markdown(
    project_root: Path,
    run_id: str,
    payload: dict[str, Any],
    *,
    review_output: dict[str, Any] | None = None,
) -> Path:
    lines = [
        f"# {payload.get('title') or '上市公司研究报告'}",
        "",
        f"> Run ID：`{run_id}`。本报告由 CrewAI 协作流程和 OpenHarness Agent 执行底座生成。",
        "> 报告中的待验证事项、审查意见和限制条件必须结合原始来源人工复核；本报告不构成投资建议。",
        "",
    ]
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        lines.extend(
            [
                f"## {section.get('title') or section.get('section_id') or '研究章节'}",
                "",
                str(
                    section.get("content")
                    or "当前阶段未获得足够可靠资料，待后续补充。"
                ),
                "",
            ]
        )
        evidence_ids = [str(item) for item in section.get("evidence_ids", []) if item]
        if evidence_ids:
            lines.extend(["证据索引：" + "、".join(f"`{item}`" for item in evidence_ids), ""])

    lines.extend(["## Reviewer 审查意见", ""])
    issues = list((review_output or {}).get("issues") or [])
    if issues:
        lines.extend(
            f"- `{item.get('issue_id', 'ISSUE-UNKNOWN')}`："
            f"{item.get('problem_statement', '待人工核验')}"
            for item in issues
            if isinstance(item, dict)
        )
    else:
        lines.append("- 当前没有单独列出的审查问题。")

    limitations = [str(item) for item in payload.get("limitations", []) if item]
    if limitations:
        lines.extend(["", "## 补充研究限制", ""])
        lines.extend(f"- {item}" for item in limitations)

    lines.extend(
        [
            "",
            str(payload.get("compliance_statement") or "本报告不构成投资建议。"),
            "",
        ]
    )
    content = "\n".join(lines)
    run_path = run_output_dir(project_root, run_id) / "report.md"
    compatibility_path = (
        project_root / ".openharness" / "validation" / "full-chain-report.md"
    )
    run_path.parent.mkdir(parents=True, exist_ok=True)
    compatibility_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(content, encoding="utf-8-sig")
    compatibility_path.write_text(content, encoding="utf-8-sig")
    return run_path


def write_run_summary(
    project_root: Path,
    run_id: str,
    payload: dict[str, Any],
    *,
    compatibility_name: str | None = None,
) -> Path:
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    path = run_output_dir(project_root, run_id) / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if compatibility_name:
        compatibility = (
            project_root / ".openharness" / "validation" / compatibility_name
        )
        compatibility.parent.mkdir(parents=True, exist_ok=True)
        compatibility.write_text(content, encoding="utf-8")
    return path


__all__ = [
    "run_output_dir",
    "write_report_context",
    "write_report_markdown",
    "write_run_summary",
]
