"""A channel where several Agents discuss the same topic at once.

A topic posted in a channel is not a task for one Agent — it is something the
room talks about. This module runs that conversation:

* Agents speak in waves. A wave is as wide as the Ark key pool allows, so more
  than one Agent is genuinely mid-call at the same time rather than taking
  polite turns.
* Every speaker sees the same transcript, which is read from **one channel**.
  A channel discussion and a 1:1 conversation never share context: they are
  different channels, so they are different sessions, and that is enforced by
  where the transcript comes from rather than by a convention.
* An Agent may @ another. That does two things — the mentioned Agent speaks in
  the next wave, and a task is recorded so the handover is visible on the board
  instead of only in the chat.

What this module does not do is silence failure. A speaker that could not reach
the model posts why, and the discussion continues without it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from openharness.invest_research.ark_key_pool import (
    ArkKeyPool,
    ArkKeyUnavailable,
    NoArkKeysConfigured,
)
from openharness.invest_research.workbench_chat_model import ChatModelError, ChatTurn
from openharness.invest_research.workbench_models import DEFAULT_MODEL

log = logging.getLogger(__name__)

#: How many waves one topic runs for. Three is enough for a position, a
#: response and a resolution; beyond that a discussion repeats itself.
DEFAULT_ROUNDS = 3
#: How much of the channel an Agent reads before speaking.
TRANSCRIPT_LIMIT = 24
#: Longest a single reply may be, in characters, before it is trimmed for the
#: transcript handed to the next speaker.
TRANSCRIPT_EXCERPT = 600

_MENTION_PATTERN = re.compile(r"@([A-Za-z][A-Za-z0-9_\-]{0,63})")


@dataclass
class Speaker:
    """One participant, resolved from the roster."""

    agent_id: str
    name: str
    role: str
    persona: str
    model: str = DEFAULT_MODEL

    @classmethod
    def from_agent(cls, agent: dict[str, Any]) -> Speaker:
        agent_id = str(agent.get("agent_id") or "")
        return cls(
            agent_id=agent_id,
            name=str(agent.get("name") or agent_id),
            role=str(agent.get("role") or "Agent"),
            persona=str(agent.get("system_prompt") or agent.get("profile") or "").strip(),
            model=str(agent.get("model") or DEFAULT_MODEL),
        )


@dataclass
class DiscussionOutcome:
    """What a finished discussion produced, for the caller to report."""

    channel_id: str
    topic_message_id: str
    rounds: int = 0
    replies: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    max_concurrent: int = 0


def parse_mentions(text: str, known_ids: Sequence[str]) -> list[str]:
    """Which Agents a reply is addressed to.

    Only ids that exist are returned: an Agent writing ``@团队`` is talking, not
    assigning, and inventing a participant from a typo would be worse than
    missing one.
    """

    known = {value.lower(): value for value in known_ids}
    found: list[str] = []
    for raw in _MENTION_PATTERN.findall(text or ""):
        actual = known.get(raw.lower())
        if actual and actual not in found:
            found.append(actual)
    return found


def _excerpt(text: str, limit: int = TRANSCRIPT_EXCERPT) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else f"{body[:limit]}…"


class GroupDiscussion:
    """Runs one topic to completion.

    The store, the event publisher and the model call are injected so this can
    be tested without a network, and so the workbench server owns persistence
    while this owns the conversation.
    """

    def __init__(
        self,
        *,
        store: Any,
        pool: ArkKeyPool,
        complete: Callable[..., ChatTurn],
        publish: Callable[[str, str, dict[str, Any]], None] | None = None,
        rounds: int = DEFAULT_ROUNDS,
    ) -> None:
        self._store = store
        self._pool = pool
        self._complete = complete
        self._publish = publish or (lambda channel_id, event_type, payload: None)
        self._rounds = max(1, rounds)
        self._lock = threading.Lock()
        self._live = 0
        self._peak = 0

    # ------------------------------------------------------------------ prompt

    def _transcript(self, channel_id: str, speakers: dict[str, Speaker]) -> list[str]:
        """The conversation so far, from this channel and no other."""

        lines = []
        for message in self._store.list_messages(channel_id, limit=TRANSCRIPT_LIMIT):
            author = str(message.get("author_id") or "")
            if author == "owner":
                who = "用户"
            elif author in speakers:
                who = speakers[author].name
            elif author == "system":
                continue  # workbench bookkeeping is not part of the conversation
            else:
                who = author
            body = _excerpt(str(message.get("body") or ""))
            if body:
                lines.append(f"{who}：{body}")
        return lines

    def _messages_for(
        self,
        speaker: Speaker,
        *,
        topic: str,
        transcript: list[str],
        others: list[Speaker],
        addressed_by: str = "",
    ) -> list[dict[str, str]]:
        roster = "、".join(f"@{item.agent_id}（{item.name}·{item.role}）" for item in others)
        # The @ rule only appears when there is somebody to name. Telling a lone
        # speaker to hand work over invites it to invent a colleague.
        #
        # No example id either: naming one member inside every persona nudges
        # the whole room to @ that same Agent.
        handoff_rule = (
            "4) 需要别人补充或复核时，用上面名单里的 @id 点名，并说清你要他做什么；"
            if roster else ""
        )
        system = "\n".join(
            part for part in [
                f"你是 {speaker.name}，在一个多 Agent 协作频道里的角色是「{speaker.role}」。",
                speaker.persona,
                f"同一频道里的其他成员：{roster}。" if roster else "",
                "这是一场群聊讨论，不是独立报告。规则：",
                "1) 只说你这个角色该说的部分，不要替别人下结论；",
                "2) 直接说观点，不要复述别人已经说过的内容；",
                "3) 控制在 120 字以内，像同事在群里发言；",
                handoff_rule,
                "5) 不要编造数据或来源，没有依据就说明这是判断还是需要核实。",
            ] if part
        )
        conversation = "\n".join(transcript) or "（还没有人发言）"
        ask = (
            f"{addressed_by} 点名要你回应。" if addressed_by else "请就这个话题发表你的看法。"
        )
        user = f"讨论话题：{topic}\n\n频道记录：\n{conversation}\n\n{ask}"
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # ------------------------------------------------------------------- turns

    def _speak(
        self,
        speaker: Speaker,
        *,
        channel_id: str,
        topic: str,
        topic_message_id: str,
        transcript: list[str],
        others: list[Speaker],
        addressed_by: str = "",
    ) -> dict[str, Any]:
        """One Agent's turn: lease a key, call the model, post what it said."""

        try:
            with self._pool.lease() as lease:
                with self._lock:
                    self._live += 1
                    self._peak = max(self._peak, self._live)
                try:
                    self._publish(channel_id, "agent_speaking", {
                        "agent_id": speaker.agent_id, "key": lease.label,
                    })
                    turn = self._complete(
                        messages=self._messages_for(
                            speaker, topic=topic, transcript=transcript,
                            others=others, addressed_by=addressed_by,
                        ),
                        model=speaker.model,
                        api_key=lease.key,
                        key_label=lease.label,
                    )
                finally:
                    with self._lock:
                        self._live -= 1
        except (ChatModelError, ArkKeyUnavailable) as exc:
            return self._post_failure(channel_id, speaker, exc, topic_message_id)
        except Exception as exc:  # noqa: BLE001 - one speaker must not end the room
            log.exception("group chat turn failed")
            return self._post_failure(channel_id, speaker, exc, topic_message_id)

        mentions = parse_mentions(turn.text, [item.agent_id for item in others])
        message = self._store.add_message(
            channel_id=channel_id,
            author_id=speaker.agent_id,
            author_type="agent",
            message_kind="discussion",
            body=turn.text,
            mentions=mentions,
            metadata={
                "discussion": True,
                "topic_message_id": topic_message_id,
                "model": turn.model,
                # Which credential spoke, never the credential itself.
                "ark_key": turn.key_label,
                "addressed_by": addressed_by,
            },
        )
        self._publish(channel_id, "message_created", {"message": message})
        return {"agent_id": speaker.agent_id, "message": message, "mentions": mentions}

    def _post_failure(
        self, channel_id: str, speaker: Speaker, exc: Exception, topic_message_id: str,
    ) -> dict[str, Any]:
        kind = getattr(exc, "kind", exc.__class__.__name__)
        body = f"（{speaker.name} 这一轮没能发言：{exc}）"
        message = self._store.add_message(
            channel_id=channel_id,
            author_id="system",
            author_type="system",
            message_kind="discussion_error",
            body=body,
            metadata={
                "discussion": True, "topic_message_id": topic_message_id,
                "agent_id": speaker.agent_id, "failure_kind": kind,
            },
        )
        self._publish(channel_id, "message_created", {"message": message})
        return {"agent_id": speaker.agent_id, "error": str(exc), "kind": kind,
                "message": message}

    # -------------------------------------------------------------------- run

    def run(
        self,
        *,
        channel_id: str,
        topic: str,
        topic_message_id: str,
        participants: list[dict[str, Any]],
    ) -> DiscussionOutcome:
        speakers = {
            agent["agent_id"]: Speaker.from_agent(agent)
            for agent in participants if agent.get("agent_id")
        }
        outcome = DiscussionOutcome(channel_id=channel_id, topic_message_id=topic_message_id)
        if not speakers:
            return outcome

        # The first wave is whoever is in the room; later waves are whoever got
        # named, so the discussion follows the conversation instead of a script.
        queue: list[tuple[str, str]] = [(agent_id, "") for agent_id in speakers]
        width = max(1, self._pool.size)

        for round_index in range(self._rounds):
            if not queue:
                break
            wave, queue = queue[:width], queue[width:]
            outcome.rounds = round_index + 1
            transcript = self._transcript(channel_id, speakers)
            results = self._run_wave(
                wave, speakers=speakers, channel_id=channel_id, topic=topic,
                topic_message_id=topic_message_id, transcript=transcript,
            )
            next_up: list[tuple[str, str]] = []
            for result in results:
                if result.get("error"):
                    outcome.failures.append(result)
                    continue
                outcome.replies.append(result)
                for target in result.get("mentions") or []:
                    if target == result["agent_id"] or target not in speakers:
                        continue
                    outcome.handoffs.append(
                        self._record_handoff(
                            channel_id=channel_id,
                            from_agent=result["agent_id"],
                            to_agent=target,
                            message=result["message"],
                            speakers=speakers,
                        )
                    )
                    if all(target != pending for pending, _ in next_up):
                        next_up.append((target, result["agent_id"]))
            # A named Agent answers before anyone who has not spoken yet.
            queue = next_up + queue

        outcome.max_concurrent = self._peak
        return outcome

    def _run_wave(
        self,
        wave: list[tuple[str, str]],
        *,
        speakers: dict[str, Speaker],
        channel_id: str,
        topic: str,
        topic_message_id: str,
        transcript: list[str],
    ) -> list[dict[str, Any]]:
        """Everyone in this wave speaks at the same time."""

        if not wave:
            return []
        with ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="discussion") as pool:
            futures = [
                pool.submit(
                    self._speak,
                    speakers[agent_id],
                    channel_id=channel_id,
                    topic=topic,
                    topic_message_id=topic_message_id,
                    transcript=transcript,
                    others=[item for key, item in speakers.items() if key != agent_id],
                    addressed_by=speakers[addressed_by].name if addressed_by in speakers else "",
                )
                for agent_id, addressed_by in wave
                if agent_id in speakers
            ]
            return [future.result() for future in futures]

    def _record_handoff(
        self,
        *,
        channel_id: str,
        from_agent: str,
        to_agent: str,
        message: dict[str, Any],
        speakers: dict[str, Speaker],
    ) -> dict[str, Any]:
        """An @ in a discussion is a real assignment, so it lands on the board."""

        title = _excerpt(str(message.get("body") or ""), 120)
        task = self._store.create_task(
            channel_id=channel_id,
            created_by=from_agent,
            assignee_id=to_agent,
            title=title or f"{from_agent} 请求 {to_agent} 跟进",
            status="queued",
            metadata={
                "discussion_handoff": True,
                "from_agent": from_agent,
                "root_message_id": message.get("message_id"),
            },
        )
        self._publish(channel_id, "task_created", {"task": task})
        return {
            "task_id": task["task_id"], "from_agent": from_agent, "to_agent": to_agent,
            "to_name": speakers[to_agent].name if to_agent in speakers else to_agent,
        }


__all__ = [
    "DEFAULT_ROUNDS",
    "DiscussionOutcome",
    "GroupDiscussion",
    "NoArkKeysConfigured",
    "Speaker",
    "parse_mentions",
]
