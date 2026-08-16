"""The bridge HTTP service.

Runs on the user's machine, default port 18789. Routes fall into two groups:

* ``/health`` and ``/pair/*`` — reachable before a token exists, because a site
  has to be able to find the bridge and start pairing.
* everything else — requires a paired token whose origin matches the caller.

``/health`` deliberately reveals nothing beyond "a bridge is here and whether
you are paired". Which Agents are installed is already information about the
user's machine, so it sits behind the token.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openharness.local_bridge.adapters.registry import available_providers, detect_all
from openharness.local_bridge.pairing import (
    PAIR_CODE_TTL_SECONDS,
    PairingError,
    PairingStore,
)

log = logging.getLogger(__name__)

DEFAULT_PORT = 18789
BRIDGE_VERSION = "1"
MAX_REQUEST_BYTES = 64 * 1024


def default_state_path() -> Path:
    from openharness.config.paths import get_data_dir

    return get_data_dir() / "local-bridge" / "pairings.json"


class BridgeServer(ThreadingHTTPServer):
    """Carries the pairing store so handlers stay stateless."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 *, pairing: PairingStore) -> None:
        super().__init__(address, handler)
        self.pairing = pairing


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "OpenHarnessLocalBridge/0.1"
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------------------- plumbing

    @property
    def pairing(self) -> PairingStore:
        return self.server.pairing  # type: ignore[attr-defined]

    def _origin(self) -> str:
        return self.headers.get("Origin", "") or ""

    def _token(self) -> str:
        raw = self.headers.get("Authorization", "") or ""
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return ""

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Echo the caller's origin only when it is allowed. A wildcard here
        # would let any page read bridge responses.
        origin = self._origin()
        if origin and self.pairing.origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            return None
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _require_pairing(self) -> bool:
        """Gate a route. Returns False once a refusal has been sent."""

        try:
            self.pairing.authenticate(token=self._token(), origin=self._origin())
        except PairingError as exc:
            self._send(401, {"error": str(exc), "paired": False})
            return False
        return True

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("bridge %s", format % args)

    # --------------------------------------------------------------- routing

    def do_OPTIONS(self) -> None:
        origin = self._origin()
        if origin and self.pairing.origin_allowed(origin):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.end_headers()
            return
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            # Unauthenticated on purpose: a site must be able to discover the
            # bridge. It answers only that a bridge exists and whether this
            # caller is already paired.
            paired = False
            try:
                self.pairing.authenticate(token=self._token(), origin=self._origin())
                paired = True
            except PairingError:
                paired = False
            self._send(200, {
                "service": "openharness-local-bridge",
                "version": BRIDGE_VERSION,
                "paired": paired,
                "origin_allowed": self.pairing.origin_allowed(self._origin()),
                "providers": available_providers(),
            })
            return
        if path == "/agents/detect":
            if not self._require_pairing():
                return
            self._send(200, {"agents": detect_all()})
            return
        if path == "/pairings":
            if not self._require_pairing():
                return
            self._send(200, {"clients": self.pairing.list_clients()})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        if body is None:
            self._send(400, {"error": "invalid JSON request"})
            return

        if path == "/pair/request":
            try:
                request = self.pairing.begin_pairing(
                    origin=self._origin(),
                    client_name=str(body.get("client_name") or ""),
                )
            except PairingError as exc:
                self._send(403, {"error": str(exc)})
                return
            # The code goes to the terminal, never into the response: proving
            # the user can read it is the whole point of the handshake.
            print(
                f"\n>>> 配对请求来自 {request.origin} ({request.client_name})\n"
                f">>> 配对码：{request.code}\n"
                f">>> 请在网页中输入该配对码（3 分钟内有效）\n",
                flush=True,
            )
            self._send(200, {
                "request_id": request.request_id,
                "expires_in": int(PAIR_CODE_TTL_SECONDS),
            })
            return

        if path == "/pair/confirm":
            try:
                client = self.pairing.confirm_pairing(
                    request_id=str(body.get("request_id") or ""),
                    code=str(body.get("code") or ""),
                    origin=self._origin(),
                )
            except PairingError as exc:
                self._send(403, {"error": str(exc)})
                return
            print(f">>> 已与 {client.origin} 配对成功\n", flush=True)
            self._send(200, {
                "token": client.token,
                "origin": client.origin,
                "expires_at": client.expires_at,
            })
            return

        self._send(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path == "/pair":
            # Unpairing needs the token being revoked, not an existing session:
            # a user who wants out should always be able to get out.
            token = self._token()
            revoked = self.pairing.revoke(token) if token else False
            self._send(200, {"revoked": revoked})
            return
        self._send(404, {"error": "not found"})


def build_bridge(
    port: int = DEFAULT_PORT,
    *,
    allowed_origins: tuple[str, ...] = (),
    state_path: Path | str | None = None,
) -> BridgeServer:
    pairing = PairingStore(
        state_path if state_path is not None else default_state_path(),
        allowed_origins=allowed_origins,
    )
    # Loopback only: the bridge is for this machine, not the network.
    return BridgeServer(("127.0.0.1", port), BridgeHandler, pairing=pairing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenHarness Local Agent Bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--allow-origin", action="append", default=[],
        help="允许的网页来源，可重复；不指定时只允许本机页面",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = build_bridge(args.port, allowed_origins=tuple(args.allow_origin))
    allowed = ", ".join(args.allow_origin) or "本机页面（loopback）"
    print(f"OpenHarness Local Bridge: http://127.0.0.1:{args.port}/", flush=True)
    print(f"允许的来源：{allowed}", flush=True)
    print("等待网页发起配对…\n", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        print("\n正在关闭 Bridge…", flush=True)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["DEFAULT_PORT", "BridgeHandler", "BridgeServer", "build_bridge", "main"]
