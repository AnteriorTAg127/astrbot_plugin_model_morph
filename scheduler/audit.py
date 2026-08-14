"""audit —— 审计日志（模块 T2，纯逻辑，不依赖 astrbot）。

记录管理员 / Agent 对调度配置的每一次变更（谁、何时、来源、做了什么、改动前/后、
结果与详情），供 WebUI 审计页查看与后续追溯。内存侧使用 ``collections.deque``
（``maxlen=retention``）作为环形缓冲，复用 ``scheduler/logs.py`` 的思路与注释风格：
- ``add`` 先补齐默认字段再深拷贝追加；
- ``recent`` 最新在前并按 source / action 筛选；
- 持久化走 ``save_to`` / ``load_from``（json 原子写：写 tmp → os.replace；损坏兜底）。

设计要点：
- ``AUDIT_SOURCES`` 声明合法来源，写入方应遵守，但 ``add`` 不强校验（宽松接收）。
- ``time`` 缺省由写入方自定；本模块仅在缺省时补 ``""``，不擅自注入系统时钟
  （与 logs.py 保持一致，保证离线测试确定性）。
- 持久化到插件 ``ConfigStore`` 的 data_dir 下 ``audit.json``，禁止系统 tempfile。
"""

from __future__ import annotations

import copy
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot_plugin_model_morph")

# 审计来源常量：Web 助手 / 聊天子代理 / 配置向导 / 预设 / 手动 / 系统。
AUDIT_SOURCES = ("web_agent", "subagent", "wizard", "preset", "manual", "system")

# add 时兜底补齐的默认字段（缺省用空字符串 / 固定值，保证筛选与序列化不因缺键出错）。
_DEFAULTS = {
    "time": "",
    "operator": "",
    "source": "",
    "action": "",
    "target": "",
    "before": None,
    "after": None,
    "result": "",
    "detail": "",
}


class AuditLog:
    """审计日志的环形缓冲。

    条目为 dict，必含 ``time``（ISO 字符串），其余字段（operator / source / action /
    target / before / after / result / detail）由写入方自由携带，``add`` 会对缺省字段补齐。
    """

    def __init__(self, retention: int = 500):
        """初始化审计缓冲。

        Args:
            retention: 内存环形缓冲上限条目数（默认 500，至少为 1）。
        """
        n = int(retention) if retention else 500
        self._entries: deque[dict[str, Any]] = deque(maxlen=max(1, n))

    def add(self, entry: dict) -> None:
        """追加一条审计（环形截断，超出 retention 自动丢弃最旧）。

        Args:
            entry: 审计条目 dict；缺省字段自动补齐，非 dict 直接忽略。
        """
        if not isinstance(entry, dict):
            return
        item = copy.deepcopy(entry)
        # 兜底补全必备字段，保证筛选 / 持久化 / Web 展示不因字段缺失出错
        for key, default in _DEFAULTS.items():
            item.setdefault(key, default)
        self._entries.append(item)

    def recent(
        self, limit: int = 100, source: str = "", action: str = ""
    ) -> list[dict]:
        """返回最近的审计子集（最新在前）并做筛选。

        Args:
            limit: 最多返回条数（默认 100）。
            source: 非空时仅返回该来源（如 subagent）的条目。
            action: 非空时仅返回该动作（如 update_rule）的条目。

        Returns:
            符合筛选的最近审计条目列表（深拷贝）。
        """
        limit = max(0, int(limit)) if limit else 0
        result: list[dict] = []
        # 逆序遍历得到最新在前
        for item in reversed(self._entries):
            if source and str(item.get("source", "")) != source:
                continue
            if action and str(item.get("action", "")) != action:
                continue
            result.append(copy.deepcopy(item))
            if limit and len(result) >= limit:
                break
        return result

    def clear(self) -> None:
        """清空所有审计条目。"""
        self._entries.clear()

    def to_list(self) -> list[dict]:
        """导出全部审计（按时间正序：最旧在前），供持久化 / Web 展示。"""
        return [copy.deepcopy(item) for item in self._entries]

    def load_entries(self, entries: list[dict]) -> None:
        """载入持久化条目（插件启动时恢复），追加到缓冲尾部。

        Args:
            entries: 审计条目列表（依次按 ``add`` 语义进入缓冲）。
        """
        if not isinstance(entries, list):
            return
        for item in entries:
            if isinstance(item, dict):
                self.add(item)

    def save_to(self, path: Path) -> None:
        """把当前全部审计条目原子写入 JSON 文件（写 tmp → os.replace）。

        Args:
            path: 目标 JSON 文件路径（应为插件 data_dir 下的 ``audit.json``）。

        Raises:
            RuntimeError: 写入失败时抛出，调用方应转为错误响应。
        """
        path = Path(path)
        payload = self.to_list()
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - 转为 RuntimeError
            logger.error("audit.save_to: 写入审计日志失败", exc_info=True)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"保存审计日志失败: {exc}") from exc

    def load_from(self, path: Path) -> int:
        """从 JSON 文件读取审计条目并载入缓冲。

        文件不存在或内容损坏（JSON 解析失败 / 结构非列表）时返回 0 且不抛异常，
        仅在损坏时记录告警日志。

        Args:
            path: 审计 JSON 文件路径。

        Returns:
            成功载入并追加的条目数。
        """
        path = Path(path)
        if not path.exists():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 损坏文件兜底
            logger.warning("audit.load_from: 审计日志解析失败 %r，跳过载入", exc)
            return 0
        if not isinstance(raw, list):
            logger.warning("audit.load_from: 审计日志顶层结构非法（非列表），跳过载入")
            return 0
        before = len(self._entries)
        self.load_entries(raw)
        return len(self._entries) - before
