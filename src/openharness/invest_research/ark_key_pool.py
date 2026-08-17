"""The Ark credentials a group discussion speaks through.

Several Agents talk at once, and concurrent calls on one Ark account run into
that account's limits. So each speaker leases a different key for the length of
its turn, and the number of keys *is* the concurrency limit — there is no second
knob to keep in sync with the first.

A lease is held for one turn and returned even when the turn fails, so a crashed
speaker cannot leak a key out of the pool.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

#: Where the workbench looks for the pool. Comma or newline separated.
ARK_KEYS_ENV = "ARK_API_KEYS"
#: The single-key variable the rest of the runtime already uses. It is the
#: fallback so a workspace with one key still works, just one speaker at a time.
ARK_KEY_ENV = "ARK_API_KEY"
#: How long a speaker waits for a free key before giving up. Long enough to sit
#: behind two other turns, short enough that a stuck turn surfaces as an error
#: rather than a hang.
LEASE_TIMEOUT_SECONDS = 180.0


class NoArkKeysConfigured(RuntimeError):
    """Raised when a discussion is asked for without a credential to speak with."""


class ArkKeyUnavailable(RuntimeError):
    """Raised when every key is still busy after waiting."""


@dataclass(frozen=True)
class ArkCredential:
    """One key and where it actually works.

    Keys come from different Ark accounts, and an account is provisioned for
    particular endpoints and models. Carrying that with the key is what lets
    three unrelated accounts serve one discussion: each speaker talks to
    whichever endpoint its own credential is entitled to.

    ``model`` is set only when the account is restricted to one — then it must
    win over the Agent's configured model, because that model does not exist
    on this account. Left empty, the Agent's own choice is used.
    """

    key: str
    base_url: str = ""
    model: str = ""


@dataclass(frozen=True)
class ArkKeyLease:
    """One credential, held for one turn.

    ``index`` is stable and safe to show: it says which of the configured keys
    spoke without revealing any of it.
    """

    index: int
    key: str
    base_url: str = ""
    model: str = ""

    @property
    def label(self) -> str:
        return f"key#{self.index + 1}"

    def masked(self) -> str:
        """A key is never logged or returned whole."""

        tail = self.key[-4:] if len(self.key) >= 4 else "?"
        return f"ark-…{tail}"


def parse_keys(raw: str) -> list[str]:
    """Split configured keys, dropping blanks and duplicates.

    A duplicated key would inflate the concurrency limit while still funnelling
    the calls through one credential, which is the exact thing the pool exists
    to prevent.
    """

    seen: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        key = chunk.strip()
        if key and key not in seen:
            seen.append(key)
    return seen


class ArkKeyPool:
    """Hands out one key per concurrent speaker."""

    def __init__(self, keys: list[str | ArkCredential]) -> None:
        cleaned = [
            item if isinstance(item, ArkCredential) else ArkCredential(key=str(item).strip())
            for item in keys
            if item and (item.key.strip() if isinstance(item, ArkCredential) else str(item).strip())
        ]
        if not cleaned:
            raise NoArkKeysConfigured(
                "没有配置任何 Ark API Key；请设置 ARK_API_KEYS（逗号分隔）或 ARK_API_KEY。"
            )
        self._credentials = cleaned
        self._keys = [item.key for item in cleaned]
        self._available = list(range(len(cleaned)))
        self._condition = threading.Condition()
        self._in_use: set[int] = set()

    @classmethod
    def from_environment(cls) -> ArkKeyPool:
        pooled = parse_keys(os.environ.get(ARK_KEYS_ENV, ""))
        if pooled:
            return cls(pooled)
        single = os.environ.get(ARK_KEY_ENV, "").strip()
        return cls([single] if single else [])

    @property
    def size(self) -> int:
        """How many Agents may speak at the same time."""

        return len(self._keys)

    def busy(self) -> int:
        with self._condition:
            return len(self._in_use)

    def keep_usable(
        self, probe: Callable[[ArkCredential], ArkCredential | None],
    ) -> tuple[ArkKeyPool, list[int]]:
        """Return a pool of the credentials that actually work.

        ``probe`` answers two questions at once: does this key work, and where.
        Keys from different accounts are entitled to different endpoints and
        models, so a probe returns the credential it managed to use — that is
        what the pool then hands out.

        A key that is configured but rejected would otherwise fail every turn it
        was given, making a credential problem look like an Agent problem.
        """

        usable: list[ArkCredential] = []
        dropped: list[int] = []
        for index, credential in enumerate(self._credentials):
            try:
                resolved = probe(credential)
            except Exception:  # noqa: BLE001 - an unreachable provider is not a verdict
                resolved = credential
            if resolved is None:
                dropped.append(index)
            else:
                usable.append(resolved)
        if not usable:
            # Every key failed. Keep the pool as configured rather than leaving
            # the workspace with none: the per-turn error then says why.
            return self, dropped
        return ArkKeyPool(usable), dropped

    @contextmanager
    def lease(self, *, timeout: float = LEASE_TIMEOUT_SECONDS) -> Iterator[ArkKeyLease]:
        index = self._acquire(timeout)
        credential = self._credentials[index]
        try:
            yield ArkKeyLease(
                index=index, key=credential.key,
                base_url=credential.base_url, model=credential.model,
            )
        finally:
            self._release(index)

    def _acquire(self, timeout: float) -> int:
        with self._condition:
            if not self._condition.wait_for(lambda: bool(self._available), timeout=timeout):
                raise ArkKeyUnavailable(
                    f"{timeout:.0f} 秒内没有空闲的 Ark Key（共 {self.size} 个，全部占用中）。"
                )
            index = self._available.pop(0)
            self._in_use.add(index)
            return index

    def _release(self, index: int) -> None:
        with self._condition:
            self._in_use.discard(index)
            # Back to the end of the line: keys are used evenly rather than the
            # first one carrying every turn.
            if index not in self._available:
                self._available.append(index)
            self._condition.notify()


__all__ = [
    "ARK_KEYS_ENV",
    "ARK_KEY_ENV",
    "ArkCredential",
    "ArkKeyLease",
    "ArkKeyPool",
    "ArkKeyUnavailable",
    "NoArkKeysConfigured",
    "parse_keys",
]
