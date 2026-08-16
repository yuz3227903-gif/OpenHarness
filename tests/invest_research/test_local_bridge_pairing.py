"""Pairing is the bridge's only defence, so it is tested as such."""

from __future__ import annotations

import json
import time

import pytest

from openharness.local_bridge.pairing import (
    PAIR_CODE_MAX_ATTEMPTS,
    PairingError,
    PairingStore,
)

ORIGIN = "http://127.0.0.1:8788"
OTHER = "http://127.0.0.1:9999"


@pytest.fixture
def store(tmp_path):
    return PairingStore(tmp_path / "pairings.json")


def pair(store, origin=ORIGIN):
    request = store.begin_pairing(origin=origin, client_name="workbench")
    return store.confirm_pairing(request_id=request.request_id, code=request.code, origin=origin)


class TestOriginPolicy:
    def test_loopback_pages_are_allowed_by_default(self, store):
        for origin in ("http://127.0.0.1:8788", "http://localhost:3000", "https://localhost"):
            assert store.origin_allowed(origin), origin

    def test_a_remote_page_is_refused(self, store):
        for origin in ("http://example.com", "https://evil.test", ""):
            assert not store.origin_allowed(origin), origin

    def test_loopback_lookalikes_are_refused(self, store):
        """Substring matching would accept these; parsing rejects them."""
        for origin in (
            "http://127.0.0.1.evil.com",
            "http://localhost.evil.com",
            "http://evil.com/?x=127.0.0.1",
            "http://evil.com#localhost",
        ):
            assert not store.origin_allowed(origin), origin

    def test_a_non_http_scheme_is_refused(self, store):
        assert not store.origin_allowed("file://localhost")
        assert not store.origin_allowed("ws://localhost:8788")

    def test_an_explicit_allowlist_is_exact(self, tmp_path):
        store = PairingStore(tmp_path / "p.json", allowed_origins=("https://workbench.local",))
        assert store.origin_allowed("https://workbench.local")
        # A suffix rule would let this through.
        assert not store.origin_allowed("https://evil-workbench.local")
        assert not store.origin_allowed("https://workbench.local.evil.com")
        # The allowlist replaces the loopback default rather than adding to it.
        assert not store.origin_allowed("http://127.0.0.1:8788")


class TestPairingHandshake:
    def test_a_pair_request_does_not_return_the_code(self, store):
        request = store.begin_pairing(origin=ORIGIN, client_name="workbench")
        # The code reaches the user through the bridge terminal only; that is
        # what proves they can see this machine.
        assert request.code not in request.request_id
        assert len(request.code) == 6 and request.code.isdigit()

    def test_the_right_code_issues_a_token(self, store):
        client = pair(store)
        assert client.token
        assert client.origin == ORIGIN
        assert store.authenticate(token=client.token, origin=ORIGIN) is not None

    def test_a_wrong_code_is_refused(self, store):
        request = store.begin_pairing(origin=ORIGIN, client_name="workbench")
        wrong = "000000" if request.code != "000000" else "111111"
        with pytest.raises(PairingError, match="不正确"):
            store.confirm_pairing(request_id=request.request_id, code=wrong, origin=ORIGIN)

    def test_guessing_is_capped(self, store):
        request = store.begin_pairing(origin=ORIGIN, client_name="workbench")
        wrong = "000000" if request.code != "000000" else "111111"
        for _ in range(PAIR_CODE_MAX_ATTEMPTS):
            with pytest.raises(PairingError):
                store.confirm_pairing(request_id=request.request_id, code=wrong, origin=ORIGIN)
        # The request is destroyed, so the correct code no longer helps.
        with pytest.raises(PairingError):
            store.confirm_pairing(request_id=request.request_id, code=request.code, origin=ORIGIN)

    def test_another_origin_cannot_confirm_someone_elses_request(self, store):
        request = store.begin_pairing(origin=ORIGIN, client_name="workbench")
        with pytest.raises(PairingError, match="来源"):
            store.confirm_pairing(request_id=request.request_id, code=request.code, origin=OTHER)

    def test_an_expired_code_is_refused(self, store, monkeypatch):
        request = store.begin_pairing(origin=ORIGIN, client_name="workbench")
        monkeypatch.setattr(
            "openharness.local_bridge.pairing._now", lambda: time.time() + 10_000
        )
        with pytest.raises(PairingError, match="过期"):
            store.confirm_pairing(request_id=request.request_id, code=request.code, origin=ORIGIN)

    def test_a_request_cannot_be_reused(self, store):
        request = store.begin_pairing(origin=ORIGIN, client_name="workbench")
        store.confirm_pairing(request_id=request.request_id, code=request.code, origin=ORIGIN)
        with pytest.raises(PairingError):
            store.confirm_pairing(request_id=request.request_id, code=request.code, origin=ORIGIN)

    def test_a_remote_origin_cannot_even_start_pairing(self, store):
        with pytest.raises(PairingError, match="来源"):
            store.begin_pairing(origin="http://evil.com", client_name="x")


class TestTokenAuthentication:
    def test_a_missing_token_is_refused(self, store):
        with pytest.raises(PairingError, match="缺少"):
            store.authenticate(token="", origin=ORIGIN)

    def test_an_unknown_token_is_refused(self, store):
        with pytest.raises(PairingError, match="无效"):
            store.authenticate(token="not-a-real-token", origin=ORIGIN)

    def test_a_token_is_bound_to_its_origin(self, store):
        client = pair(store)
        # Lifting the token onto another page must not work, even though that
        # page is also on loopback.
        with pytest.raises(PairingError, match="不匹配"):
            store.authenticate(token=client.token, origin=OTHER)

    def test_an_expired_token_is_refused_and_dropped(self, store, monkeypatch):
        client = pair(store)
        monkeypatch.setattr(
            "openharness.local_bridge.pairing._now", lambda: time.time() + 400 * 24 * 3600
        )
        with pytest.raises(PairingError, match="过期"):
            store.authenticate(token=client.token, origin=ORIGIN)
        assert store.list_clients() == []

    def test_revoking_ends_access(self, store):
        client = pair(store)
        assert store.revoke(client.token) is True
        with pytest.raises(PairingError):
            store.authenticate(token=client.token, origin=ORIGIN)
        assert store.revoke(client.token) is False

    def test_revoke_all_clears_every_pairing(self, store):
        pair(store)
        pair(store, origin="http://localhost:3000")
        assert store.revoke_all() == 2
        assert store.list_clients() == []


class TestPersistence:
    def test_tokens_survive_a_restart(self, tmp_path):
        path = tmp_path / "pairings.json"
        client = pair(PairingStore(path))
        # A browser reload must not force the user to pair again.
        assert PairingStore(path).authenticate(token=client.token, origin=ORIGIN)

    def test_a_listing_never_exposes_the_token(self, tmp_path):
        store = PairingStore(tmp_path / "pairings.json")
        client = pair(store)
        listed = store.list_clients()
        assert len(listed) == 1
        assert client.token not in json.dumps(listed)
        assert listed[0]["token_preview"].endswith("…")

    def test_a_corrupt_store_does_not_stop_the_bridge(self, tmp_path):
        path = tmp_path / "pairings.json"
        path.write_text("{ this is not json", encoding="utf-8")
        store = PairingStore(path)
        assert store.list_clients() == []
        assert pair(store).token

    def test_an_expired_token_is_not_reloaded(self, tmp_path):
        path = tmp_path / "pairings.json"
        client = pair(PairingStore(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["clients"][0]["expires_at"] = time.time() - 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(PairingError):
            PairingStore(path).authenticate(token=client.token, origin=ORIGIN)
