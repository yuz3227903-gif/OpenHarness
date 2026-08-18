"""在频道里 @ 一个 Agent 时，它到底被要求做什么。

以前所有 @ 都走同一条路：当成一份正式研究任务，跑研究合约，几十秒后给一份
按格式产出的交付——哪怕你说的只是"重新做一遍"或者"停下"。结果是等半天拿到
一句"用户问题与授权研究对象错配"。

所以先分意图，再决定走哪条路。分类是纯函数，不调模型：这几类意图靠词就能认
准，用模型反而慢、贵、还不稳定。认不出来的一律落到 ``reply``——聊天回答比
错误地启动一个几十秒的研究任务代价小得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Intent = Literal["redo", "stop", "research", "reply"]

#: 重做：让它把刚才那件事再做一遍
_REDO = (
    "重做", "重新做", "再做一遍", "再做一次", "重新执行", "重新跑", "再跑一遍",
    "重来", "重新来", "redo", "rerun", "re-run", "retry", "重新分析", "重新复核",
)
#: 终止：停下手上的活
_STOP = (
    "终止", "停止", "停下", "别做了", "取消", "中止", "打住", "先停",
    "stop", "cancel", "abort", "halt",
)
#: 研究：明确要求做一份正式的调研产出
_RESEARCH = (
    "研究", "调研", "分析一下", "出一份", "写一份", "报告", "尽调", "评估",
    "测算", "复核", "核查", "查一下", "查证", "对比", "拆解",
)
#: 这些说法带研究词但其实是闲聊或追问，不该启动正式研究
_NOT_RESEARCH = (
    "怎么看", "什么意思", "为什么", "解释", "说说", "简单说", "总结一下",
    "你觉得", "是不是", "对吗", "行不行",
)

_MENTION = re.compile(r"@[A-Za-z0-9_\-]+")


@dataclass(frozen=True)
class MentionIntent:
    """一条 @ 消息的意图，以及去掉 @ 之后真正说的话。"""

    intent: Intent
    text: str

    @property
    def is_conversational(self) -> bool:
        """这一类意图应该在对话里当场回答，而不是排一个任务。"""

        return self.intent in {"redo", "stop", "reply"}


def strip_mentions(text: str) -> str:
    """去掉 @xxx，留下真正的内容。"""

    return _MENTION.sub(" ", str(text or "")).strip()


def classify_mention(text: str) -> MentionIntent:
    """判断 @ 一个 Agent 时要它做什么。

    顺序有讲究：终止和重做是对"当前这件事"的指令，优先级高于内容里出现的
    研究词——"停下，别再重新分析了"要认成终止，不是重做也不是研究。
    """

    body = strip_mentions(text)
    lowered = body.lower()

    if any(word in lowered for word in _STOP):
        return MentionIntent("stop", body)
    if any(word in lowered for word in _REDO):
        return MentionIntent("redo", body)
    if any(word in body for word in _NOT_RESEARCH):
        return MentionIntent("reply", body)
    if any(word in body for word in _RESEARCH):
        return MentionIntent("research", body)
    return MentionIntent("reply", body)


#: 在频道里直接喊停：``@终止``、``@停止``、``@stop``。
#:
#: 这类 @ 后面跟的不是 Agent 名字，所以 ``_parse_mentions`` 那条只认 ASCII 标识
#: 符的正则根本看不见它——单独认一次。
_STOP_MENTION = re.compile(
    r"@\s*(?:全部)?\s*(终止|停止|停下|中止|结束讨论|结束|stop|halt|abort)",
    re.IGNORECASE,
)


def is_stop_command(text: str) -> bool:
    """这条消息是不是在频道里喊停当前这一轮。"""

    return bool(_STOP_MENTION.search(str(text or "")))


#: 频道里"换个课题"的说法。命中之后当前讨论会被打断，Agent 不再继续聊上一个。
_NEW_TOPIC = (
    "新课题", "新的课题", "换个课题", "换一个课题", "重新开一个", "另一个课题",
    "新话题", "新的话题", "换个话题", "接下来研究", "现在研究", "改成研究",
)


def looks_like_new_topic(text: str) -> bool:
    """这条消息是不是在宣布换课题。

    只用来决定"要不要先把上一场讨论停掉"。判断错了的代价是对称的：认错了会
    白停一场讨论，漏认了会让 Agent 继续聊上一个课题——后者更糟，所以这里
    宁可宽一点。
    """

    body = strip_mentions(text)
    return any(word in body for word in _NEW_TOPIC)


__all__ = [
    "MentionIntent",
    "classify_mention",
    "is_stop_command",
    "looks_like_new_topic",
    "strip_mentions",
]
