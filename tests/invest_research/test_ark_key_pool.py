"""The credential pool that bounds how many Agents talk at once."""

from __future__ import annotations

import threading

import pytest

from openharness.invest_research.ark_key_pool import (
    ArkKeyPool,
    ArkKeyUnavailable,
    NoArkKeysConfigured,
    parse_keys,
)


class TestParsingConfiguration:
    def test_a_comma_separated_list_becomes_a_pool(self):
        assert parse_keys("a, b ,c") == ["a", "b", "c"]

    def test_newlines_are_accepted_too(self):
        assert parse_keys("a\nb\n") == ["a", "b"]

    def test_blanks_are_dropped(self):
        assert parse_keys(" , a, ,, b ") == ["a", "b"]

    def test_a_repeated_key_is_counted_once(self):
        # Two copies of one key would raise the concurrency limit while still
        # sending every call through the same credential — the exact thing the
        # pool exists to avoid.
        assert parse_keys("a,a,b") == ["a", "b"]

    def test_nothing_configured_is_an_empty_pool(self):
        assert parse_keys("   ") == []


class TestPoolConstruction:
    def test_a_pool_needs_at_least_one_key(self):
        with pytest.raises(NoArkKeysConfigured):
            ArkKeyPool([])

    def test_blank_keys_do_not_count(self):
        with pytest.raises(NoArkKeysConfigured):
            ArkKeyPool(["", "   "])

    def test_the_pool_size_is_the_concurrency_limit(self):
        assert ArkKeyPool(["a", "b", "c"]).size == 3

    def test_the_environment_pool_wins_over_the_single_key(self, monkeypatch):
        monkeypatch.setenv("ARK_API_KEYS", "one,two,three")
        monkeypatch.setenv("ARK_API_KEY", "legacy")
        assert ArkKeyPool.from_environment().size == 3

    def test_a_single_key_still_works(self, monkeypatch):
        monkeypatch.delenv("ARK_API_KEYS", raising=False)
        monkeypatch.setenv("ARK_API_KEY", "legacy")
        pool = ArkKeyPool.from_environment()
        assert pool.size == 1

    def test_no_credential_at_all_is_refused(self, monkeypatch):
        monkeypatch.delenv("ARK_API_KEYS", raising=False)
        monkeypatch.delenv("ARK_API_KEY", raising=False)
        with pytest.raises(NoArkKeysConfigured):
            ArkKeyPool.from_environment()


class TestLeasing:
    def test_a_lease_hands_out_a_real_key(self):
        pool = ArkKeyPool(["alpha"])
        with pool.lease() as lease:
            assert lease.key == "alpha"
            assert lease.index == 0
            assert lease.label == "key#1"

    def test_concurrent_speakers_never_share_a_key(self):
        pool = ArkKeyPool(["a", "b", "c"])
        seen: list[str] = []
        release = threading.Event()
        lock = threading.Lock()
        ready = threading.Barrier(4, timeout=5)

        def speak():
            with pool.lease() as lease:
                with lock:
                    seen.append(lease.key)
                ready.wait()
                release.wait(3)

        threads = [threading.Thread(target=speak, daemon=True) for _ in range(3)]
        for thread in threads:
            thread.start()
        ready.wait()
        # All three are mid-turn right now, so this is the real overlap.
        assert sorted(seen) == ["a", "b", "c"]
        release.set()
        for thread in threads:
            thread.join(5)

    def test_a_fourth_speaker_waits_for_a_key(self):
        pool = ArkKeyPool(["a", "b", "c"])
        release = threading.Event()
        entered = threading.Barrier(4, timeout=5)
        fourth_started = threading.Event()
        fourth_got_key = threading.Event()

        def hold():
            with pool.lease():
                entered.wait()
                release.wait(5)

        holders = [threading.Thread(target=hold, daemon=True) for _ in range(3)]
        for thread in holders:
            thread.start()
        entered.wait()

        def fourth():
            fourth_started.set()
            with pool.lease(timeout=5):
                fourth_got_key.set()

        thread = threading.Thread(target=fourth, daemon=True)
        thread.start()
        assert fourth_started.wait(2)
        # The pool is full, so the fourth speaker must not be talking yet.
        assert not fourth_got_key.wait(0.4)
        assert pool.busy() == 3

        release.set()
        assert fourth_got_key.wait(5)
        thread.join(5)
        for holder in holders:
            holder.join(5)

    def test_a_failed_turn_returns_its_key(self):
        pool = ArkKeyPool(["only"])
        with pytest.raises(RuntimeError, match="boom"):
            with pool.lease():
                raise RuntimeError("boom")
        assert pool.busy() == 0
        # A leaked key would make every later turn time out.
        with pool.lease(timeout=1) as lease:
            assert lease.key == "only"

    def test_waiting_too_long_is_an_error_not_a_hang(self):
        pool = ArkKeyPool(["only"])
        with pool.lease():
            with pytest.raises(ArkKeyUnavailable):
                with pool.lease(timeout=0.2):
                    pass

    def test_keys_are_used_evenly(self):
        pool = ArkKeyPool(["a", "b"])
        used = []
        for _ in range(4):
            with pool.lease() as lease:
                used.append(lease.key)
        # Round-robin rather than always the first key, so one credential does
        # not absorb every request.
        assert used == ["a", "b", "a", "b"]


class TestDroppingDeadKeys:
    def test_a_rejected_key_is_removed_from_the_pool(self):
        pool = ArkKeyPool(["good", "dead", "also-good"])
        usable, dropped = pool.keep_usable(lambda key: key != "dead")
        assert usable.size == 2
        assert dropped == [1]

    def test_a_pool_of_only_dead_keys_is_kept_as_configured(self):
        # Emptying it would leave nothing to fail against, and the per-turn
        # error is what tells the user their credentials are rejected.
        pool = ArkKeyPool(["dead"])
        usable, dropped = pool.keep_usable(lambda _key: False)
        assert (usable.size, dropped) == (1, [0])

    def test_an_unreachable_provider_does_not_condemn_a_key(self):
        def exploding(_key):
            raise OSError("network down")

        usable, dropped = ArkKeyPool(["a", "b"]).keep_usable(exploding)
        assert (usable.size, dropped) == (2, [])

    def test_the_surviving_keys_are_the_working_ones(self):
        usable, _ = ArkKeyPool(["dead", "live"]).keep_usable(lambda key: key == "live")
        with usable.lease() as lease:
            assert lease.key == "live"


class TestSecrecy:
    def test_a_lease_can_be_reported_without_leaking_the_key(self):
        with ArkKeyPool(["ark-secret-value-1234"]).lease() as lease:
            # Which key spoke is useful to show; the key itself never is.
            assert "secret" not in lease.masked()
            assert lease.masked().endswith("1234")
            assert lease.label == "key#1"
