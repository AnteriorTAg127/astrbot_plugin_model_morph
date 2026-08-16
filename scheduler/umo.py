"""umo —— UMO（unified_msg_origin）解析 / 构造 / 换算纯逻辑模块（模块 U，不依赖 astrbot）。

AstrBot 中 Unified Message Origin（会话唯一标识）的官方格式为::

    unified_msg_origin = f"{platform_id}:{message_type.value}:{session_id}"

（见 astrbot/core/platform/message_session.py 的 ``MessageSession.__str__``）：

- ``platform_id``：平台适配器实例唯一标识（如 ``aiocqhttp``、``webchat``、
  ``telegram``），同一类型可有多个实例；
- ``message_type``：``GroupMessage`` / ``FriendMessage`` / ``OtherMessage``
  （astrbot/core/platform/message_type.py 的 ``MessageType`` 枚举值，严格区分大小写）；
- ``session_id``：会话来源 ID——aiocqhttp 群消息 = 群号、私聊 = 发送者 QQ 号
  （aiocqhttp_platform_adapter.py 的 ``abm.session_id`` 逻辑）。

设计要点：

- **session_id 允许包含冒号**（如 webchat 的 session_id 可含其他冒号），因此
  parse 时按「最多 split 成 3 段、前两段为 platform_id 与 message_type、其余合并
  为 session_id」处理，与 AstrBot 源码 ``MessageSession.from_str`` 的
  ``split(":", 2)`` 语义一致；
- parse 严格校验：非字符串 / platform_id 或 session_id 为空 / message_type 不在
  白名单（含大小写不符）均返回 None；
- 本模块为纯逻辑（仅标准库），可离线单测。
"""

from __future__ import annotations

# message_type 白名单（与 MessageType 枚举值保持一致，严格区分大小写）。
MESSAGE_TYPES = ("GroupMessage", "FriendMessage", "OtherMessage")

# 用于 format 时刻的快速合法集合。
_MESSAGE_TYPE_SET = frozenset(MESSAGE_TYPES)


def parse_umo(umo: str) -> tuple[str, str, str] | None:
    """严格解析 UMO 字符串为 (platform_id, message_type, session_id)。

    Args:
        umo: 形如 ``platform_id:message_type:session_id`` 的字符串
            （session_id 可含额外冒号）。

    Returns:
        三元组 (platform_id, message_type, session_id)；任一字段非法（非字符串、
        platform_id 或 session_id 为空、message_type 不在白名单）时返回 None。
    """
    if not isinstance(umo, str):
        return None
    # 最多分 3 段：前两段固定为 platform_id / message_type，其余合并为 session_id。
    parts = umo.split(":", 2)
    if len(parts) < 3:
        return None
    platform_id, message_type, session_id = parts
    if not platform_id or not session_id:
        return None
    if message_type not in _MESSAGE_TYPE_SET:
        return None
    return (platform_id, message_type, session_id)


def format_umo(platform_id: str, message_type: str, session_id: str) -> str:
    """按官方格式拼接 UMO 字符串。

    Args:
        platform_id: 平台适配器实例唯一标识。
        message_type: 消息类型（须在 MESSAGE_TYPES 白名单内）。
        session_id: 会话来源 ID。

    Returns:
        拼接后的 UMO；platform_id / session_id 为空或 message_type 非法时返回空串。
    """
    if not platform_id or not session_id:
        return ""
    if message_type not in _MESSAGE_TYPE_SET:
        return ""
    return f"{platform_id}:{message_type}:{session_id}"


def umo_examples(platform_id: str) -> dict:
    """给定平台 id，返回群聊 / 私聊两条示例 UMO（占位符为真实可读文本）。

    Args:
        platform_id: 平台适配器实例唯一标识。

    Returns:
        ``{"group": "<pid>:GroupMessage:<群号>", "friend": "<pid>:FriendMessage:<QQ号>"}``；
        platform_id 为空时两条均为空串。
    """
    if not platform_id:
        return {"group": "", "friend": ""}
    return {
        "group": f"{platform_id}:GroupMessage:<群号>",
        "friend": f"{platform_id}:FriendMessage:<QQ号>",
    }


def umo_from_ids(
    platform_id: str, group_id: str = "", user_id: str = ""
) -> list[str]:
    """把群号 / QQ 号换算为对应 UMO 列表（去空项、保持顺序）。

    Args:
        platform_id: 平台适配器实例唯一标识。
        group_id: 群号，非空 → 追加 ``<pid>:GroupMessage:<group_id>``。
        user_id: 发送者 QQ 号，非空 → 追加 ``<pid>:FriendMessage:<user_id>``。

    Returns:
        换算出的 UMO 列表（按 group、friend 顺序）；platform_id 为空时返回空列表。
    """
    if not platform_id:
        return []
    result: list[str] = []
    if group_id:
        result.append(f"{platform_id}:GroupMessage:{group_id}")
    if user_id:
        result.append(f"{platform_id}:FriendMessage:{user_id}")
    return result
