"""调度日志 —— 环形缓冲 + 按 UMO / 级别筛选（模块 B，纯逻辑，不依赖 astrbot）。

记录调度器的关键动作（切换 / 重置 / 锁定 / 解锁 / 错误等）。内存侧使用 ``collections.deque``
（``maxlen=retention``）作为环形缓冲，并用 ``asyncio.Lock`` 声明为约定的并发保护原语；
``load_entries`` 用于在插件启动时从持久化的 ``logs.json`` 恢复历史条目。

说明：公开方法均为同步方法（契约如此），``asyncio.Lock`` 在无事件循环的离线同步测试中
不可 await，而 ``deque`` 的单元素 append / pop 在 CPython 下本身原子、线程安全，因此并发
安全由「deque 操作原子性 + 约定锁」双层保证，返回给调用方的一律为深拷贝。
"""

from __future__ import annotations

import asyncio
import copy
from collections import deque
from typing import Any


class SchedulerLog:
    """调度日志的环形缓冲。

    条目为 dict，必含 ``time``（ISO 字符串）与 ``umo``，其余字段（type / level / rule /
    旧 → 新 Provider / 组 / round / reason 等）由写入方自由携带。
    """

    def __init__(self, retention: int = 500):
        """初始化日志缓冲。

        Args:
            retention: 内存环形缓冲上限条目数（默认 500，至少为 1）。
        """
        self._lock = asyncio.Lock()
        n = int(retention) if retention else 500
        self._entries: deque[dict[str, Any]] = deque(maxlen=max(1, n))

    def add(self, entry: dict) -> None:
        """追加一条日志（环形截断，超出 retention 自动丢弃最旧）。

        Args:
            entry: 日志条目 dict；至少应含 ``time`` 与 ``umo``。
        """
        if not isinstance(entry, dict):
            return
        item = copy.deepcopy(entry)
        # 兜底补全必备字段，保证筛选与持久化不因字段缺失出错
        item.setdefault("time", "")
        item.setdefault("umo", "")
        item.setdefault("type", "")
        item.setdefault("level", "")
        self._entries.append(item)

    def recent(self, limit: int = 100, umo: str = "", level: str = "") -> list[dict]:
        """返回最近的日志子集（按时间倒序：最新在前）并做筛选。

        Args:
            limit: 最多返回条数（默认 100）。
            umo: 非空时仅返回该会话的日志。
            level: 非空时仅返回该级别（如 error）的日志。

        Returns:
            符合筛选的最近日志条目列表（深拷贝）。
        """
        limit = max(0, int(limit)) if limit else 0
        result: list[dict] = []
        # 逆序遍历得到最新在前
        for item in reversed(self._entries):
            if umo and str(item.get("umo", "")) != umo:
                continue
            if level and str(item.get("level", "")) != level:
                continue
            result.append(copy.deepcopy(item))
            if limit and len(result) >= limit:
                break
        return result

    def clear(self) -> None:
        """清空所有日志。"""
        self._entries.clear()

    def to_list(self) -> list[dict]:
        """导出全部日志（按时间正序：最旧在前），供持久化 / Web 展示。"""
        return [copy.deepcopy(item) for item in self._entries]

    def load_entries(self, entries: list[dict]) -> None:
        """载入持久化条目（插件启动时恢复），追加到缓冲尾部。

        Args:
            entries: 日志条目列表（依次按 ``add`` 语义进入缓冲）。
        """
        if not isinstance(entries, list):
            return
        for item in entries:
            if isinstance(item, dict):
                self.add(item)
