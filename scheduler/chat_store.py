"""chat_store —— AI 配置助手的会话存储（模块 A1，纯逻辑，不依赖 astrbot）。

负责把 Web AI 配置助手的会话（conversations）以 JSON 形式原子读写到
``data_dir/agent_chats.json``，供会话列表 / 切换 / 删除 / 刷新后找回历史。

设计要点：
- 内存为权威状态，每次变更立即原子落盘（写 tmp → os.replace，参照 persistence.save）。
- 上限：最多保留 ``MAX_CONVERSATIONS`` 个会话（超出删除最旧）；每会话最多
  ``MAX_MESSAGES`` 条消息（超出丢弃最旧）。
- 读取损坏文件时先备份为 ``agent_chats.json.bak`` 再用空列表，保证不崩溃。
- 时间字段一律用注入的 ``now_fn``（默认 ``datetime.now().isoformat()``），
  离线测试可传入固定时间时钟。
- 返回给调用方的一律为深拷贝，避免外部改值污染内存状态。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger("astrbot_plugin_model_morph")

_LAST_PREVIEW_LEN = 60  # 会话概要里 last_preview 的截断字符数

# 顶层 JSON 缓冲用 TEMP 后缀（写 tmp → os.replace）。
_TMP_SUFFIX = ".tmp"
_BAK_SUFFIX = ".bak"


class ChatStore:
    """AI 配置助手的会话存储（纯逻辑，不 import astrbot）。

    持久化到 ``data_dir/agent_chats.json``；内存为权威状态，每次变更立即原子落盘
    （写 tmp → os.replace，参照 persistence.save）；加载失败/损坏 → 备份 .bak 并用空列表。
    """

    MAX_CONVERSATIONS = 50
    MAX_MESSAGES = 200
    TITLE_LEN = 30

    def __init__(self, data_dir: Path, now_fn: Callable[[], str] | None = None):
        """初始化会话存储，并立即从 ``data_dir/agent_chats.json`` 恢复历史。

        Args:
            data_dir: 插件持久化数据目录（``data/plugin_data/astrbot_plugin_model_morph``）。
            now_fn: 时间戳生成函数（默认 ``datetime.now().isoformat()``），离线测试注入固定时钟。
        """
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._chats_path = self._data_dir / "agent_chats.json"
        self._now_fn = now_fn or (lambda: datetime.now().isoformat())
        # 内存权威状态：{ "conversations": [ {id,title,created_at,updated_at,messages} ] }
        self._conversations: list[dict] = []
        self.load_from(self._chats_path)

    # ---- 路径 / 时间 ----

    def config_path(self) -> Path:
        """会话数据文件路径（``data_dir/agent_chats.json``）。"""
        return self._chats_path

    def _now(self) -> str:
        """当前时间戳（ISO 字符串），由注入的 ``now_fn`` 生成。"""
        return self._now_fn()

    # ---- 概要 / 读取 ----

    def list_conversations(self) -> list[dict]:
        """按 ``updated_at`` 降序返回会话概要列表。

        Returns:
            概要列表：``[{id,title,created_at,updated_at,message_count,last_preview}]``，
            其中 ``last_preview`` 为最后一条消息内容截断 60 字符后的值（深拷贝）。
        """
        sorted_convos = sorted(
            self._conversations,
            key=lambda c: c.get("updated_at", ""),
            reverse=True,
        )
        result = []
        for conv in sorted_convos:
            messages = conv.get("messages", [])
            last_preview = ""
            if messages:
                last_preview = str(messages[-1].get("content", ""))[:_LAST_PREVIEW_LEN]
            result.append(
                {
                    "id": conv.get("id", ""),
                    "title": conv.get("title", ""),
                    "created_at": conv.get("created_at", ""),
                    "updated_at": conv.get("updated_at", ""),
                    "message_count": len(messages),
                    "last_preview": last_preview,
                }
            )
        return result

    def get_conversation(self, cid: str) -> dict | None:
        """返回指定会话的完整副本（含 messages）；不存在返回 None。

        Args:
            cid: 会话 id。

        Returns:
            会话 dict 的深拷贝；找不到时返回 None。
        """
        conv = self._find(cid)
        return copy.deepcopy(conv) if conv is not None else None

    def _find(self, cid: str) -> dict | None:
        """按 id 查找内存中的会话对象（返回内部引用，调用方须自行深拷贝）。"""
        for conv in self._conversations:
            if conv.get("id") == cid:
                return conv
        return None

    # ---- 写操作 ----

    def new_conversation(self, first_user_content: str) -> dict:
        """创建新会话并立即保存。

        Args:
            first_user_content: 首条用户消息内容，用于生成标题。

        Returns:
            新会话的深拷贝。
        """
        content = (first_user_content or "").strip()
        title = content[: self.TITLE_LEN] if content else self._new_title()
        now = self._now()
        conv: dict = {
            "id": "c_" + uuid.uuid4().hex[:8],
            "title": title,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        self._conversations.append(conv)
        # 超过上限删除最旧（按 created_at 最旧），新会话必然保留。
        if len(self._conversations) > self.MAX_CONVERSATIONS:
            oldest = sorted(self._conversations, key=lambda c: c.get("created_at", ""))
            for extra in oldest[: len(self._conversations) - self.MAX_CONVERSATIONS]:
                self._discard(extra)
        self.save()
        return copy.deepcopy(conv)

    def append_message(self, cid: str, role: str, content: str) -> dict | None:
        """向指定会话追加一条消息（user/assistant）并更新 updated_at 后保存。

        Args:
            cid: 会话 id。
            role: 仅允许 ``"user"`` / ``"assistant"``；非法 role 拒绝。
            content: 消息正文。

        Returns:
            更新后会话的深拷贝；会话不存在或 role 非法时返回 None。
        """
        if role not in ("user", "assistant"):
            logger.warning(
                "chat_store.append_message: 非法 role %r（仅允许 user/assistant），已拒绝",
                role,
            )
            return None
        conv = self._find(cid)
        if conv is None:
            return None
        messages = conv.setdefault("messages", [])
        messages.append({"role": role, "content": content, "time": self._now()})
        # 超过 MAX_MESSAGES 丢弃最旧。
        if len(messages) > self.MAX_MESSAGES:
            del messages[: len(messages) - self.MAX_MESSAGES]
        conv["updated_at"] = self._now()
        self.save()
        return copy.deepcopy(conv)

    def delete(self, cid: str) -> bool:
        """删除指定会话并保存。

        Args:
            cid: 会话 id。

        Returns:
            是否成功删除（会话存在返回 True，不存在返回 False）。
        """
        conv = self._find(cid)
        if conv is None:
            return False
        self._discard(conv)
        self.save()
        return True

    def _discard(self, conv: dict) -> None:
        """从内存列表移除指定会话对象（不落盘，由调用方负责 save）。"""
        if conv in self._conversations:
            self._conversations.remove(conv)

    @staticmethod
    def _new_title() -> str:
        """无首条内容时的兜底标题。"""
        return "新对话"

    # ---- 持久化 ----

    def save(self) -> None:
        """原子写入 ``agent_chats.json``（写 tmp → os.replace）。

        写盘异常吞掉并记 warning，绝不向外抛出（保证消息流不被阻断）。
        """
        payload = {"conversations": copy.deepcopy(self._conversations)}
        tmp = self._chats_path.with_suffix(self._chats_path.suffix + _TMP_SUFFIX)
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._chats_path)
        except Exception as exc:  # noqa: BLE001 - 异常兜底
            logger.warning(
                "chat_store.save: 写入 %s 失败: %r（内存状态不变）",
                self._chats_path,
                exc,
            )
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass

    def load_from(self, path: Path) -> int:
        """从指定文件读回会话数并替换内存状态。

        文件不存在或损坏（JSON 解析失败 / 结构非法）→ 备份为 ``.bak`` 并用空列表，
        返回 0 且不抛异常。

        Args:
            path: 读取的 JSON 文件路径。

        Returns:
            恢复的会话数；文件不存在 / 损坏时返回 0。
        """
        path = Path(path)
        if not path.exists():
            self._conversations = []
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(
                raw.get("conversations"), list
            ):
                raise ValueError("agent_chats.json 结构非法")
            conversations = _validated_conversations(raw["conversations"])
        except Exception as exc:  # noqa: BLE001 - 损坏文件兜底
            logger.warning(
                "chat_store.load_from: 读取 %s 失败 %r，备份为 .bak 并使用空列表",
                path,
                exc,
            )
            self._backup_corrupt(path)
            self._conversations = []
            return 0
        self._conversations = conversations
        return len(self._conversations)

    def _backup_corrupt(self, path: Path) -> None:
        """把损坏文件备份为 ``.bak``（尽力而为，失败仅记 warning）。"""
        backup = path.with_suffix(path.suffix + _BAK_SUFFIX)
        try:
            os.replace(path, backup)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat_store._backup_corrupt: 备份 %s 失败: %r", path, exc)


def _validated_conversations(items: list) -> list:
    """结构化校验并归一化读取到的会话列表（丢弃结构非法的条目，保留合法者）。

    Args:
        items: 磁盘读到的会话列表。

    Returns:
        合法会话的深拷贝列表。
    """
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        messages = item.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        conv = {
            "id": str(item.get("id", "")),
            "title": str(item.get("title", "")),
            "created_at": str(item.get("created_at", "")),
            "updated_at": str(item.get("updated_at", "")),
            "messages": [copy.deepcopy(m) for m in messages if isinstance(m, dict)],
        }
        if not conv["id"]:
            continue
        result.append(conv)
    return result
