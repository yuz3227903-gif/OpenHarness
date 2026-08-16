"""The bridge over HTTP: what an unpaired page can and cannot reach."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from openharness.local_bridge.server import build_bridge

ORIGIN = "http://127.0.0.1:8788"
EVIL = "http://evil.example.com"


@pytest.fixture
def bridge(tmp_path):
    server = build_bridge(0, state_path=tmp_path / "pairings.json")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server
    server.shutdown()
    server.server_close()


def call(base, path, *, method="GET", payload=None, origin=ORIGIN, token=None):
    headers = {}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body or "{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body or "{}"), dict(exc.headers)


def pair(base, capsys=None, origin=ORIGIN):
    """Complete the handshake the way a user does."""
    status, started, _ = call(base, "/pair/request", method="POST",
                              payload={"client_name": "workbench"}, origin=origin)
    assert status == 200
    # The code is only ever printed by the bridge, so a test reads it the same
    # way a user does — from the process output.
    printed = capsys.readouterr().out
    code = next(line.split("：")[1].strip() for line in printed.splitlines() if "配对码" in line)
    status, confirmed, _ = call(base, "/pair/confirm", method="POST",
                                payload={"request_id": started["request_id"], "code": code},
                                origin=origin)
    assert status == 200, confirmed
    return confirmed["token"]


class TestDiscovery:
    def test_health_answers_without_a_token(self, bridge):
        base, _ = bridge
        status, body, _ = call(base, "/health")
        assert status == 200
        assert body["service"] == "openharness-local-bridge"
        assert body["paired"] is False

    def test_health_does_not_leak_what_is_installed(self, bridge):
        base, _ = bridge
        _, body, _ = call(base, "/health")
        # Which Agents exist is information about this machine; it stays behind
        # the token. Only the provider catalogue the bridge supports is public.
        assert "agents" not in body
        assert set(body["providers"]) >= {"codex", "openclaw"}

    def test_health_reports_whether_this_origin_may_pair(self, bridge):
        base, _ = bridge
        assert call(base, "/health")[1]["origin_allowed"] is True
        assert call(base, "/health", origin=EVIL)[1]["origin_allowed"] is False


class TestUnpairedAccess:
    @pytest.mark.parametrize("path", ["/agents/detect", "/pairings"])
    def test_protected_routes_refuse_without_a_token(self, bridge, path):
        base, _ = bridge
        status, body, _ = call(base, path)
        assert status == 401
        assert body["paired"] is False

    def test_a_forged_token_is_refused(self, bridge):
        base, _ = bridge
        status, _, _ = call(base, "/agents/detect", token="made-up-token")
        assert status == 401

    def test_a_remote_page_cannot_start_pairing(self, bridge):
        base, _ = bridge
        status, body, _ = call(base, "/pair/request", method="POST",
                               payload={"client_name": "evil"}, origin=EVIL)
        assert status == 403
        assert "来源" in body["error"]

    def test_no_cors_header_is_sent_to_a_disallowed_origin(self, bridge):
        base, _ = bridge
        _, _, headers = call(base, "/health", origin=EVIL)
        # Without this header the page cannot read the response even though the
        # request reached the bridge.
        assert "Access-Control-Allow-Origin" not in headers

    def test_preflight_is_refused_for_a_disallowed_origin(self, bridge):
        base, _ = bridge
        status, _, _ = call(base, "/agents/detect", method="OPTIONS", origin=EVIL)
        assert status == 403

    def test_cors_is_never_a_wildcard(self, bridge):
        base, _ = bridge
        _, _, headers = call(base, "/health")
        assert headers.get("Access-Control-Allow-Origin") == ORIGIN


class TestPairedAccess:
    def test_pairing_then_detecting(self, bridge, capsys):
        base, _ = bridge
        token = pair(base, capsys)
        status, body, _ = call(base, "/agents/detect", token=token)
        assert status == 200
        providers = {item["provider"] for item in body["agents"]}
        assert providers >= {"codex", "openclaw"}
        for item in body["agents"]:
            assert item["status"] in {"available", "not_installed", "error"}

    def test_health_reports_paired_once_paired(self, bridge, capsys):
        base, _ = bridge
        token = pair(base, capsys)
        assert call(base, "/health", token=token)[1]["paired"] is True

    def test_a_token_does_not_work_from_another_origin(self, bridge, capsys):
        base, _ = bridge
        token = pair(base, capsys)
        status, _, _ = call(base, "/agents/detect", token=token,
                            origin="http://localhost:3000")
        assert status == 401

    def test_a_wrong_code_does_not_pair(self, bridge):
        base, _ = bridge
        _, started, _ = call(base, "/pair/request", method="POST",
                             payload={"client_name": "workbench"})
        status, body, _ = call(base, "/pair/confirm", method="POST",
                               payload={"request_id": started["request_id"], "code": "000000"})
        assert status == 403
        assert "配对码" in body["error"]

    def test_unpairing_ends_access(self, bridge, capsys):
        base, _ = bridge
        token = pair(base, capsys)
        status, body, _ = call(base, "/pair", method="DELETE", token=token)
        assert (status, body["revoked"]) == (200, True)
        assert call(base, "/agents/detect", token=token)[0] == 401

    def test_listing_pairings_hides_tokens(self, bridge, capsys):
        base, _ = bridge
        token = pair(base, capsys)
        _, body, _ = call(base, "/pairings", token=token)
        assert token not in json.dumps(body)


class TestMalformedRequests:
    def test_an_unknown_route_is_a_404(self, bridge):
        base, _ = bridge
        assert call(base, "/nope")[0] == 404

    def test_invalid_json_is_rejected(self, bridge):
        base, _ = bridge
        request = urllib.request.Request(
            base + "/pair/request", data=b"{not json",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("expected a rejection")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_an_oversized_body_is_rejected(self, bridge):
        base, _ = bridge
        payload = {"client_name": "x" * 200_000}
        status, _, _ = call(base, "/pair/request", method="POST", payload=payload)
        assert status == 400

    def test_the_bridge_binds_loopback_only(self, bridge):
        _, server = bridge
        # Binding a routable address would expose the user's Agents to the LAN.
        assert server.server_address[0] == "127.0.0.1"
