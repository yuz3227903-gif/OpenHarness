"""Command-line entry point for the first real Planner validation."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import date

from openharness.invest_research.runtime_adapter import (
    AgentExecutionRequest,
    PlannerRuntimeAdapter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Planner -> configured DeepSeek model -> Tavily/web_fetch -> "
            "PlannerResult. No other Agent or CrewAI flow is started."
        )
    )
    parser.add_argument("--company", default="宁德时代", help="上市公司名称或股票代码")
    parser.add_argument(
        "--as-of-date",
        default=date.today().isoformat(),
        help="研究基准日，格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="只检查模型名称、工具白名单和 Tavily 配置，不调用模型或网络",
    )
    parser.add_argument(
        "--prompt-for-tavily-key",
        action="store_true",
        help="在终端中隐藏输入 Tavily Key；仅对本次进程生效，不写入文件",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    adapter = PlannerRuntimeAdapter()
    preflight = adapter.preflight()
    print(
        json.dumps(
            {"phase": "preflight", **preflight.model_dump(mode="json")},
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.preflight_only:
        return 0 if preflight.ready_for_real_run else 2
    if not preflight.ready_for_real_run:
        print(
            "Planner real run was not started. Configure the missing prerequisite "
            "locally; do not paste credentials into chat or source files.",
            file=sys.stderr,
        )
        return 2

    execution_request = AgentExecutionRequest(
        agent_id="planner",
        input_payload={
            "protocol_version": "1.0",
            "run_id": "RUN-PLANNER-SMOKE-001",
            "task_id": "TASK-PLANNER-SMOKE-001",
            "objective": f"为{args.company}建立上市公司研究参数卡和任务计划",
            "agent_id": "planner",
            "company_query": args.company,
            "as_of_date": args.as_of_date,
            "uploaded_file_refs": [],
        },
        context_package={
            "validation_scope": (
                "首次真实运行验证，仅验证公司识别、日期窗口、竞品候选、"
                "任务拆解和结构化输出；暂不写入正式证据库。"
            )
        },
    )
    result = await adapter.execute_agent(execution_request)
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "succeeded" else 1


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if args.prompt_for_tavily_key and not os.environ.get("TAVILY_API_KEY", "").strip():
        tavily_key = getpass.getpass("Tavily API Key（输入不可见，仅本次运行使用）: ").strip()
        if not tavily_key:
            raise SystemExit("未输入 Tavily API Key，已取消运行。")
        os.environ["TAVILY_API_KEY"] = tavily_key
    try:
        date.fromisoformat(args.as_of_date)
    except ValueError as exc:
        raise SystemExit("--as-of-date 必须使用 YYYY-MM-DD 格式") from exc
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
