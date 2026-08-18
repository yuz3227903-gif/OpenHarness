"""Pairing and tokens.

The bridge can start Agents on the user's machine, so reaching it must take
more than being able to address ``localhost``. Any page in any tab can send a
request there; what a hostile page cannot do is read the bridge's terminal.

So pairing works like this:

1. A site asks to pair. The bridge prints a short code **in its own terminal**
   and returns only a request id.
2. The user types that code into the site.
3. The bridge issues a token bound to that site's origin.

Every later call needs the token *and* an allowed origin. A stolen token used
from another origin is refused, and an allowed origin without a token is
refused too.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: A pairing code is short enough to retype and short-lived, so brute force has
#: to beat the clock as well as the attempt limit.
PAIR_CODE_TTL_SECONDS = 180.0
PAIR_CODE_MAX_ATTEMPTS = 5
#: Tokens expire so an abandoned pairing does not stay valid forever.
TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60.0


def _now() -> float:
    return time.time()


@dataclass
class PairRequest:
    request_id: str
    code: str
    origin: str
    client_name: str
    created_at: float
    attempts: int = 0

    def expired(self, now: float | None = None) -> bool:
        return (now or _now()) - self.created_at > PAIR_CODE_TTL_SECONDS


@dataclass
class PairedClient:
    token: str
    origin: str
    client_name: str
    created_at: float
    expires_at: float

    def expired(self, now: float | None = None) -> bool:
        return (now or _now()) >= self.expires_at

    def public(self) -> dict[str, Any]:
        """Describe the pairing without leaking the token."""

        data = asdict(self)
        data.pop("token")
        data["token_preview"] = f"{self.token[:6]}…"
        return data


class PairingError(Exception):
    """A pairing step was refused. The message is safe to show a user."""


class PairingStore:
    """Pending pair requests and issued tokens.

    Tokens persist so a browser reload does not force re-pairing; pending
    requests stay in memory because they die in three minutes anyway.
    """

    def __init__(self, state_path: Path | str | None = None, *, allowed_origins: tuple[str, ...] = ()) -> None:
        self._lock = threading.RLock()
        self._requests: dict[str, PairRequest] = {}
        self._clients: dict[str, PairedClient] = {}
        self._allowed_origins = tuple(allowed_origins)
        self._state_path = Path(state_path) if state_path else None
        self._load()

    # ------------------------------------------------------------------ origin

    def origin_allowed(self, origin: str) -> bool:
        """Is this origin permitted to talk to the bridge at all?

        An empty allowlist means "anything on this machine", which is the
        default for a bridge only ever addressed by a workbench on localhost.
        A configured allowlist is exact-match: no wildcards, no suffix rules,
        because ``evil-workbench.local`` must not match ``workbench.local``.
        """

        if not self._allowed_origins:
            return _is_loopback_origin(origin)
        return origin in self._allowed_origins

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return self._allowed_origins

    # ----------------------------------------------------------------- pairing

    def begin_pairing(self, *, origin: str, client_name: str) -> PairRequest:
        if not self.origin_allowed(origin):
            raise PairingError(f"来源不被允许：{origin or '(缺少 Origin)'}")
        request = PairRequest(
            request_id=secrets.token_urlsafe(12),
            code=f"{secrets.randbelow(1_000_000):06d}",
            origin=origin,
            client_name=(client_name or "未命名客户端")[:80],
            created_at=_now(),
        )
        with self._lock:
            self._prune_requests()
            self._requests[request.request_id] = request
        return request

    def confirm_pairing(self, *, request_id: str, code: str, origin: str) -> PairedClient:
        with self._lock:
            self._prune_requests()
            request = self._requests.get(request_id)
            if request is None:
                raise PairingError("配对请求不存在或已过期，请重新发起。")
            if request.origin != origin:
                # The confirming page must be the page that asked.
                raise PairingError("确认来源与发起配对的来源不一致。")
            if request.expired():
                del self._requests[request_id]
                raise PairingError("配对码已过期，请重新发起。")

            request.attempts += 1
            if request.attempts > PAIR_CODE_MAX_ATTEMPTS:
                del self._requests[request_id]
                raise PairingError("尝试次数过多，配对请求已作废。")
            # Compare in constant time so a wrong code leaks nothing by timing.
            if not secrets.compare_digest(str(code or ""), request.code):
                remaining = PAIR_CODE_MAX_ATTEMPTS - request.attempts
                raise PairingError(f"配对码不正确，还可尝试 {max(remaining, 0)} 次。")

            del self._requests[request_id]
            now = _now()
            client = PairedClient(
                token=secrets.token_urlsafe(32),
                origin=origin,
                client_name=request.client_name,
                created_at=now,
                expires_at=now + TOKEN_TTL_SECONDS,
            )
            self._clients[client.token] = client
            self._save()
            return client

    # ------------------------------------------------------------------ tokens

    def authenticate(self, *, token: str, origin: str) -> PairedClient:
        """Return the pairing for this token, or say precisely why not."""

        if not token:
            raise PairingError("缺少配对令牌。")
        with self._lock:
            client = self._clients.get(token)
            if client is None:
                raise PairingError("配对令牌无效，请重新配对。")
            if client.expired():
                del self._clients[client.token]
                self._save()
                raise PairingError("配对令牌已过期，请重新配对。")
            if not self.origin_allowed(origin):
                raise PairingError(f"来源不被允许：{origin or '(缺少 Origin)'}")
            if client.origin != origin:
                # A token lifted from one site is useless on another.
                raise PairingError("配对令牌与当前来源不匹配。")
            return client

    def revoke(self, token: str) -> bool:
        with self._lock:
            existed = self._clients.pop(token, None) is not None
            if existed:
                self._save()
            return existed

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._clients)
            self._clients.clear()
            self._save()
            return count

    def list_clients(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_clients()
            return [client.public() for client in self._clients.values()]

    # ----------------------------------------------------------------- storage

    def _prune_requests(self) -> None:
        now = _now()
        for request_id in [k for k, v in self._requests.items() if v.expired(now)]:
            del self._requests[request_id]

    def _prune_clients(self) -> None:
        now = _now()
        removed = [token for token, client in self._clients.items() if client.expired(now)]
        for token in removed:
            del self._clients[token]
        if removed:
            self._save()

    def _load(self) -> None:
        if not self._state_path or not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A damaged store must not stop the bridge; it just means the user
            # pairs again.
            return
        for item in payload.get("clients", []):
            try:
                client = PairedClient(**item)
            except TypeError:
                continue
            if not client.expired():
                self._clients[client.token] = client

    def _save(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"clients": [asdict(client) for client in self._clients.values()]}
        self._state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            # Tokens are credentials: keep them owner-readable where the OS
            # honours it. Windows ignores this, which is why the file also
            # lives under the user's own data directory.
            self._state_path.chmod(0o600)
        except OSError:
            pass


def _is_loopback_origin(origin: str) -> bool:
    """Accept only pages served from this machine.

    Parsed rather than pattern-matched: ``http://127.0.0.1.evil.com`` contains
    the loopback address as a substring but is not loopback.
    """

    from urllib.parse import urlsplit

    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"}:
        return False
    return parts.hostname in {"127.0.0.1", "localhost", "::1"}


__all__ = [
    "PAIR_CODE_MAX_ATTEMPTS",
    "PAIR_CODE_TTL_SECONDS",
    "TOKEN_TTL_SECONDS",
    "PairRequest",
    "PairedClient",
    "PairingError",
    "PairingStore",
]
