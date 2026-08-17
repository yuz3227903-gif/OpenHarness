"""One conversational turn against Ark.

A discussion turn is not a research run: no tools, no JSON contract, no evidence
budget — an Agent reads what has been said and says the next thing. So this is a
plain chat completion rather than the research adapter, and it is deliberately
small enough to read in one sitting.

Failures are reported as themselves. A turn that could not reach the model must
never look like an Agent that had nothing to say.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from openharness.invest_research.workbench_models import ARK_PLAN_BASE_URL

#: A discussion reply is a few sentences. Capping it keeps one Agent from
#: monologuing through everyone else's turn.
DEFAULT_MAX_TOKENS = 700
DEFAULT_TIMEOUT_SECONDS = 90.0


class ChatModelError(RuntimeError):
    """A turn that did not produce a reply, with a reason a person can act on."""

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ChatTurn:
    """What one Agent said, and what it cost."""

    text: str
    model: str
    key_label: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_finish_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _classify(status: int) -> str:
    if status in {401, 403}:
        return "provider_auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "provider_error"
    return "request_error"


def complete(
    *,
    messages: list[dict[str, str]],
    model: str,
    api_key: str,
    key_label: str = "",
    base_url: str = ARK_PLAN_BASE_URL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.8,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> ChatTurn:
    """Ask one model for one reply.

    ``api_key`` is passed in rather than read from the environment: concurrent
    speakers each hold a different credential, so there is no single ambient key
    that would be correct here.
    """

    if not api_key.strip():
        raise ChatModelError("没有可用的 Ark API Key。", kind="provider_auth")
    if not messages:
        raise ChatModelError("没有可发送的对话内容。", kind="input_error")

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.TimeoutException as exc:
        raise ChatModelError(f"调用模型超时（{timeout:.0f} 秒）。", kind="timeout") from exc
    except httpx.HTTPError as exc:
        raise ChatModelError(f"无法连接模型服务：{exc}", kind="network") from exc
    finally:
        if owned:
            http.close()

    if response.status_code >= 400:
        # The provider's own message is the useful part; keep it, bounded.
        detail = response.text[:400].replace("\n", " ").strip()
        raise ChatModelError(
            f"模型返回 HTTP {response.status_code}：{detail}",
            kind=_classify(response.status_code),
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise ChatModelError("模型返回的不是 JSON。", kind="invalid_json") from exc

    choices = body.get("choices") or []
    if not choices:
        raise ChatModelError("模型没有返回任何回复。", kind="empty_response")
    message = (choices[0] or {}).get("message") or {}
    finish_reason = str((choices[0] or {}).get("finish_reason") or "")
    text = str(message.get("content") or "").strip()
    if not text:
        # A reasoning model can spend the whole budget thinking and return an
        # empty answer with its reasoning attached. That is a token limit, not
        # an Agent with nothing to say, so it must not read as one.
        if message.get("reasoning_content") or finish_reason == "length":
            raise ChatModelError(
                f"模型把 {max_tokens} tokens 全用在推理上，没有留下回复；请调高上限或换一个模型。",
                kind="token_limit",
            )
        raise ChatModelError("模型返回了空回复。", kind="empty_response")

    usage = body.get("usage") or {}
    return ChatTurn(
        text=text,
        model=str(body.get("model") or model),
        key_label=key_label,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        raw_finish_reason=finish_reason,
    )


__all__ = ["ChatModelError", "ChatTurn", "complete", "DEFAULT_MAX_TOKENS"]
