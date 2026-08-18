"""Minimal local web page proving the real Planner runtime integration."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
import webbrowser
from datetime import UTC, date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from openharness.invest_research.runtime_adapter import (
    AgentExecutionRequest,
    AgentExecutionResult,
    PlannerRuntimeAdapter,
)
from openharness.invest_research.validation_harness import ValidationHarness


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_ROOT = (
    PROJECT_ROOT
    / ".openharness"
    / "plugins"
    / "investment-research"
    / "web"
)
MAX_REQUEST_BYTES = 16_384
RUN_LOCK = threading.Lock()
RESULT_LOCK = threading.Lock()
LAST_RESULT: dict[str, Any] | None = None
CHAIN_LOCK = threading.Lock()
CHAIN_STATE_LOCK = threading.Lock()
CHAIN_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
}


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _safe_preview(value: str | None, max_chars: int = 260) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars].rstrip() + "…"
    return cleaned


def _compact_tool_call(index: int, tool_call) -> dict[str, Any]:
    metadata = dict(tool_call.metadata or {})
    return {
        "index": index,
        "tool_name": tool_call.tool_name,
        "tool_input": tool_call.tool_input,
        "is_error": bool(tool_call.is_error),
        "provider": metadata.get("provider"),
        "result_count": metadata.get("result_count"),
        "request_id": metadata.get("request_id"),
        "content_type": metadata.get("content_type"),
        "output_preview": _safe_preview(tool_call.output),
    }


def _compact_result(run_id: str, result: AgentExecutionResult) -> dict[str, Any]:
    structured = dict(result.structured_output or {})
    return {
        "run_id": run_id,
        "execution_status": result.status,
        "business_status": structured.get("status"),
        "agent_id": result.agent_id,
        "runtime_agent_name": result.runtime_agent_name,
        "model": result.model,
        "company_identity": structured.get("company_identity"),
        "research_period": structured.get("research_period"),
        "catalyst_window": structured.get("catalyst_window"),
        "recommended_competitors": structured.get("recommended_competitors", []),
        "task_plan": structured.get("task_plan", []),
        "completed_scope": structured.get("completed_scope", []),
        "limitations": structured.get("limitations", [])[:5],
        "unverified_items": structured.get("unverified_items", [])[:5],
        "tool_calls": [
            _compact_tool_call(index, tool_call)
            for index, tool_call in enumerate(result.tool_calls, start=1)
        ],
        "usage": result.usage.model_dump(mode="json"),
        "warnings": result.warnings,
        "error": result.error,
    }


def _remember_result(payload: dict[str, Any]) -> None:
    global LAST_RESULT
    with RESULT_LOCK:
        LAST_RESULT = dict(payload)


def _latest_result() -> dict[str, Any] | None:
    with RESULT_LOCK:
        return dict(LAST_RESULT) if LAST_RESULT is not None else None


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validation_path(name: str) -> Path:
    return PROJECT_ROOT / ".openharness" / "validation" / name


def _research_snapshot() -> dict[str, Any]:
    with CHAIN_STATE_LOCK:
        state = dict(CHAIN_STATE)
    summary = state.get("summary") or _read_json_file(_validation_path("full-chain.json"))
    progress = _read_json_file(_validation_path("full-chain-progress.json"))
    report_path = _validation_path("full-chain-report.md")
    receipts = list((summary or {}).get("receipts") or [])
    agents: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or not receipt.get("agent_id"):
            continue
        agent_id = str(receipt["agent_id"])
        agents[agent_id] = {
            "agent_id": agent_id,
            "execution_status": receipt.get("execution_status"),
            "output_status": receipt.get("output_status"),
            "pass": bool(receipt.get("pass")),
            "tool_count": int(receipt.get("tool_count") or 0),
            "total_tokens": int((receipt.get("usage") or {}).get("total_tokens") or 0),
            "warnings": list(receipt.get("warnings") or []),
            "error_type": receipt.get("error_type"),
        }
    if report_path.is_file() and "report_writer" not in agents:
        agents["report_writer"] = {
            "agent_id": "report_writer",
            "execution_status": "succeeded",
            "output_status": "fallback",
            "pass": True,
            "tool_count": 0,
            "total_tokens": 0,
            "warnings": ["由本地兜底程序完成报告交付"],
            "error_type": None,
        }
    return {
        "running": bool(state.get("running")),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "error": state.get("error"),
        "progress": progress,
        "summary": summary,
        "agents": agents,
        "report_available": report_path.is_file(),
        "report_bytes": report_path.stat().st_size if report_path.is_file() else 0,
    }


def _run_full_chain_background() -> None:
    try:
        summary = asyncio.run(ValidationHarness(PROJECT_ROOT).full_chain())
        with CHAIN_STATE_LOCK:
            CHAIN_STATE.update(
                {
                    "running": False,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "summary": summary,
                    "error": None,
                }
            )
    except Exception as exc:
        with CHAIN_STATE_LOCK:
            CHAIN_STATE.update(
                {
                    "running": False,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    finally:
        if CHAIN_LOCK.locked():
            CHAIN_LOCK.release()


def _start_full_chain() -> tuple[int, dict[str, Any]]:
    if not CHAIN_LOCK.acquire(blocking=False):
        return HTTPStatus.CONFLICT, {
            "error": "已有完整研究任务正在运行，请等待当前任务完成。"
        }
    with CHAIN_STATE_LOCK:
        CHAIN_STATE.update(
            {
                "running": True,
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "summary": None,
                "error": None,
            }
        )
    thread = threading.Thread(
        target=_run_full_chain_background,
        name="investment-research-full-chain",
        daemon=True,
    )
    thread.start()
    return HTTPStatus.ACCEPTED, {
        "status": "started",
        "message": "宁德时代七 Agent 完整研究已在后台启动。",
    }


def _run_planner(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    company = str(payload.get("company") or "").strip()
    as_of_text = str(payload.get("as_of_date") or "").strip()
    if not company or len(company) > 120:
        return HTTPStatus.BAD_REQUEST, {"error": "请输入有效的上市公司名称或股票代码。"}
    try:
        as_of_date = date.fromisoformat(as_of_text)
    except ValueError:
        return HTTPStatus.BAD_REQUEST, {"error": "研究基准日必须使用 YYYY-MM-DD 格式。"}

    if not RUN_LOCK.acquire(blocking=False):
        return HTTPStatus.CONFLICT, {"error": "已有 Planner 正在运行，请等待本次任务结束。"}

    short_id = uuid4().hex[:10].upper()
    run_id = f"RUN-WEB-{short_id}"
    try:
        adapter = PlannerRuntimeAdapter()
        request = AgentExecutionRequest(
            agent_id="planner",
            input_payload={
                "protocol_version": "1.0",
                "run_id": run_id,
                "task_id": f"TASK-WEB-{short_id}",
                "objective": f"为{company}生成投研参数卡、竞品建议和任务计划",
                "agent_id": "planner",
                "company_query": company,
                "as_of_date": as_of_date.isoformat(),
                "uploaded_file_refs": [],
            },
            task_prompt=(
                "这是一个低成本网页演示任务。只完成公司身份核验、最近一年与未来六个月"
                "绝对日期、两家竞品推荐和六个下游角色任务计划。最多调用 3 次 "
                "tavily_search 和 2 次 web_fetch；每次搜索最多返回 3 条；不要抓取 PDF；"
                "不要展开财务研究、投资逻辑或最终报告。最终 JSON 保持简洁。"
            ),
            context_package={
                "demo_mode": True,
                "evidence_persistence": "disabled",
                "display_note": "结果仅用于验证真实模型与工具链路。",
            },
            timeout_seconds=240,
            max_repair_attempts=1,
        )
        result = asyncio.run(adapter.execute_agent(request))
        compact_result = _compact_result(run_id, result)
        _remember_result(compact_result)
        return HTTPStatus.OK, compact_result
    except Exception as exc:
        error_result = {
            "run_id": run_id,
            "error": f"Planner 页面运行失败（{type(exc).__name__}）。",
        }
        _remember_result(error_result)
        return HTTPStatus.INTERNAL_SERVER_ERROR, error_result
    finally:
        RUN_LOCK.release()


class PlannerDemoHandler(BaseHTTPRequestHandler):
    server_version = "InvestmentResearchWeb/0.2"

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A browser tab may be refreshed or closed while a long model call
            # is still running. The compact result remains available through
            # /api/planner/last-result.
            return

    def _send_json(self, status: int, payload: Any) -> None:
        self._send_bytes(status, _json_bytes(payload), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/research/status":
            self._send_json(HTTPStatus.OK, _research_snapshot())
            return

        if path in {"/api/research/report", "/api/research/report/download"}:
            report_path = _validation_path("full-chain-report.md")
            if not report_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "尚未生成研究报告。"})
                return
            headers = None
            if path.endswith("/download"):
                headers = {
                    "Content-Disposition": "attachment; filename=investment-research-report.md"
                }
            self._send_bytes(
                HTTPStatus.OK,
                report_path.read_bytes(),
                "text/markdown; charset=utf-8",
                headers=headers,
            )
            return

        if path == "/api/health":
            try:
                preflight = PlannerRuntimeAdapter().preflight()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ready" if preflight.ready_for_real_run else "blocked",
                        "model": preflight.model,
                        "runtime_agent_name": preflight.runtime_agent_name,
                        "allowed_tools": preflight.allowed_tools,
                        "tavily_configured": preflight.tavily_configured,
                        "ready_for_real_run": preflight.ready_for_real_run,
                    },
                )
            except Exception as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"status": "failed", "error": type(exc).__name__},
                )
            return

        if path == "/api/planner/last-result":
            latest = _latest_result()
            if latest is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "尚无运行结果。"})
            else:
                self._send_json(HTTPStatus.OK, latest)
            return

        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        selected = static_files.get(path)
        if selected is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        file_name, content_type = selected
        file_path = WEB_ROOT / file_name
        if not file_path.is_file():
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Web asset missing"})
            return
        self._send_bytes(HTTPStatus.OK, file_path.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/research/run":
            status, response = _start_full_chain()
            self._send_json(status, response)
            return
        if path != "/api/planner/run":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求内容无效。"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求必须是有效 JSON。"})
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求必须是 JSON 对象。"})
            return
        status, response = _run_planner(payload)
        self._send_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep local logs compact and never print request bodies or credentials.
        print(f"[planner-demo] {self.address_string()} {format % args}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local seven-Agent research page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    server = ThreadingHTTPServer((args.host, args.port), PlannerDemoHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Investment research page: {url}")
    print("Press Ctrl+C to stop.")
    if args.open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
