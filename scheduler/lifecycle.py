"""lifecycle —— 生命周期策略状态机（模块 E，纯逻辑，不依赖 astrbot）。

管理「新会话 → 初始轮（initial_group x initial_rounds）→ 主组（main_group）→
每 periodic_interval 轮插入一次 periodic_group → 主组 …」的轮转策略。

v0.1.6 扩展（多阶段降级 + 事件校准）：
- ``stages``：多阶段轮转，按累计 rounds 逐段定位（``round < s1→STAGE_1``，
  ``round < s1+s2→STAGE_2`` …耗尽走 ``final_group``），staged 模式下
  ``periodic_group`` 优先于阶段定位（用于周期校准）。
- ``calibration_event`` / ``calibration_group`` / ``calibration_rounds``：
  事件触发后的校准配置（如「上下文压缩后切到某组校准 N 轮」）。
- ``should_trigger_compression``：纯函数启发式，用 LLM 输入 token 骤降判定
  上下文压缩发生（AstrBot 不暴露压缩事件，见 v0.1.6 分工文档）。

v1.0.1 扩展（限定群组 + 优先级）：
- ``scope`` / ``priority``：与 temporal 共用「限定群组」语义（见 scheduler/scope.py）。
  三键全空 = 全局策略；``match_scoped``（限定命中）优先于 ``match_global``
  （全局按 priority 降序），供 engine 在未绑定策略时自动选择。

设计要点：
- ``LIFECYCLE_TEMPLATES`` 仅提供四套预设的 name / initial_rounds / periodic_interval，
  组字段（initial_group/main_group/periodic_group）留空，由用户在 WebUI 上一键载入后填写。
- ``normalize_lifecycle`` 补齐缺失字段，保证与 WebUI / config.json 读写一致。
- ``decide_group`` 根据 ``state.round`` 计算当前应使用的模型组并回写 ``state.stage``；
  ``stages`` 非空时走 staged 模式，否则完全沿用 legacy 逻辑。
"""

from __future__ import annotations

import copy
import logging
import uuid

from .persistence import ConfigStore
from .scope import normalize_scope, scope_is_empty, scope_match
from .state import SessionState

logger = logging.getLogger("astrbot_plugin_model_morph")

# 生命周期状态机允许的阶段。
LIFECYCLE_STAGES = ("NEW", "INITIAL", "MAIN", "PERIODIC")

# 上下文压缩检测启发式常量（见 should_trigger_compression）：
# AstrBot 不暴露「上下文压缩」事件，只能以 LLM 输入 token 相对骤降近似判定。
# 至少 _COMPRESS_MIN_TOKENS 词、且本轮 < 前一轮 * _COMPRESS_DROP_RATIO 视为压缩发生。
_COMPRESS_MIN_TOKENS = 2000
_COMPRESS_DROP_RATIO = 0.6

# 生命周期预设模板（仅 name / initial_rounds / periodic_interval；
# 组字段 initial_group/main_group/periodic_group 留空字符串，载入后再由用户填写）。
# v1.0.1：scope 三键全空 = 全局策略，priority 0。
LIFECYCLE_TEMPLATES: dict[str, dict] = {
    "balanced": {
        "name": "Balanced",
        "initial_rounds": 2,
        "periodic_interval": 5,
        "initial_group": "",
        "main_group": "",
        "periodic_group": "",
        "scope": {"groups": [], "users": [], "sessions": []},
        "priority": 0,
    },
    "quality": {
        "name": "Quality",
        "initial_rounds": 5,
        "periodic_interval": 5,
        "initial_group": "",
        "main_group": "",
        "periodic_group": "",
        "scope": {"groups": [], "users": [], "sessions": []},
        "priority": 0,
    },
    "cost_saving": {
        "name": "Cost Saving",
        "initial_rounds": 1,
        "periodic_interval": 10,
        "initial_group": "",
        "main_group": "",
        "periodic_group": "",
        "scope": {"groups": [], "users": [], "sessions": []},
        "priority": 0,
    },
    "new_conversation": {
        "name": "New Conversation",
        "initial_rounds": 3,
        "periodic_interval": 5,
        "initial_group": "",
        "main_group": "",
        "periodic_group": "",
        "scope": {"groups": [], "users": [], "sessions": []},
        "priority": 0,
    },
}


def normalize_lifecycle(raw: dict) -> dict:
    """补齐 / 校正一个生命周期策略字典，返回规范结构的新字典。

    字段：``id``（缺省生成）、``name``、``enabled``、``initial_group``、
    ``initial_rounds``、``main_group``、``periodic_group``、``periodic_interval``、
    ``stages``（多阶段降级）、``final_group``、``calibration_event``、
    ``calibration_group``、``calibration_rounds``、``scope``（v1.0.1 限定群组，
    三键全空=全局）、``priority``（v1.0.1，int，非法回 0）。

    Args:
        raw: 待规范化的原始字典（可由 WebUI 或模板提供）。

    Returns:
        补齐所有字段后的规范生命周期字典。
    """
    src = raw if isinstance(raw, dict) else {}
    calibration_event = str(src.get("calibration_event") or "")
    if calibration_event not in ("", "context_compression"):
        logger.warning(
            "lifecycle.normalize: 非法 calibration_event=%r，回退为空",
            calibration_event,
        )
        calibration_event = ""
    try:
        priority = int(src.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    result: dict = {
        "id": str(src.get("id") or "lc_" + uuid.uuid4().hex[:8]),
        "name": str(src.get("name") or "未命名生命周期"),
        "enabled": bool(src.get("enabled", True)),
        "initial_group": str(src.get("initial_group") or ""),
        "initial_rounds": _as_non_neg_int(src.get("initial_rounds", 0)),
        "main_group": str(src.get("main_group") or ""),
        "periodic_group": str(src.get("periodic_group") or ""),
        "periodic_interval": _as_non_neg_int(src.get("periodic_interval", 0)),
        "stages": _normalize_stages(src.get("stages", [])),
        "final_group": str(src.get("final_group") or ""),
        "calibration_event": calibration_event,
        "calibration_group": str(src.get("calibration_group") or ""),
        "calibration_rounds": _as_non_neg_int(src.get("calibration_rounds", 0)),
        # v1.0.1：限定群组 + 优先级（与 temporal 共享 scope 语义）。
        "scope": normalize_scope(src.get("scope")),
        "priority": priority,
    }
    return result


def _as_non_neg_int(value) -> int:
    """把任意输入安全转成非负整数（非法输入返回 0）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _as_positive_int(value) -> int:
    """把任意输入安全转成正整数（非法或 <=0 返回 0）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _normalize_stages(value) -> list[dict]:
    """规范化多阶段降级 ``stages``：仅保留含非空 group_id 与正整数 rounds 的条目。

    非法条目（非 dict / 缺 group_id / rounds 非正整数）一律剔除并告警；
    非法输入（非 list）返回空列表。
    """
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            logger.warning("lifecycle.normalize: stages[%d] 非 dict，剔除", i)
            continue
        group_id = str(entry.get("group_id") or "")
        rounds = _as_positive_int(entry.get("rounds"))
        if not group_id:
            logger.warning("lifecycle.normalize: stages[%d] 缺 group_id，剔除", i)
            continue
        if rounds <= 0:
            logger.warning(
                "lifecycle.normalize: stages[%d] rounds 非正整数(%r)，剔除",
                i,
                entry.get("rounds"),
            )
            continue
        out.append({"group_id": group_id, "rounds": rounds})
    return out


class LifecycleEngine:
    """生命周期策略的 CRUD 与阶段推演。通过 ``ConfigStore`` 持久化 lifecycles。"""

    def __init__(self, store: ConfigStore):
        """初始化。

        Args:
            store: 配置存储（提供 ``get_lifecycles`` / ``update`` 等接口）。
        """
        self._store = store

    # ---- CRUD ----

    def _raw_list(self) -> list[dict]:
        """读取 store 中的 lifecycles 原始列表。"""
        return self._store.get_lifecycles()

    def _save(self, items: list[dict]) -> None:
        """写入 lifecycles 列表到 store。"""
        self._store.update("lifecycles", items)

    def get(self, lifecycle_id: str) -> dict | None:
        """按 id 获取生命周期；不存在返回 None。"""
        for item in self._raw_list():
            if item.get("id") == lifecycle_id:
                return copy.deepcopy(item)
        return None

    def list_(self, only_enabled: bool = False) -> list[dict]:
        """返回全部（或仅启用）生命周期列表。"""
        return [
            copy.deepcopy(i)
            for i in self._raw_list()
            if not only_enabled or i.get("enabled", True)
        ]

    # ---- v1.0.1：限定群组自动选择 ----

    def _ordered_enabled(self) -> list[dict]:
        """返回启用策略，按 priority 降序（同 priority 保持存储顺序）。"""
        items = [
            normalize_lifecycle(i)
            for i in self._raw_list()
            if i.get("enabled", True)
        ]
        return sorted(
            items, key=lambda i: (int(i.get("priority", 0) or 0),), reverse=True
        )

    def match_scoped(self, meta: dict | None) -> dict | None:
        """返回限定命中（scope 非空且匹配 meta）的启用策略中 priority 最高者。

        Args:
            meta: 上下文 dict（``group_id`` / ``sender_id`` / ``umo``）。

        Returns:
            命中的生命周期深拷贝，无命中返回 None。
        """
        for item in self._ordered_enabled():
            scope = item.get("scope")
            if scope_is_empty(scope):
                continue
            if scope_match(scope, meta):
                return copy.deepcopy(item)
        return None

    def match_global(self) -> dict | None:
        """返回全局（scope 全空）启用策略中 priority 最高者，无则 None。"""
        for item in self._ordered_enabled():
            if scope_is_empty(item.get("scope")):
                return copy.deepcopy(item)
        return None

    def create(self, raw: dict) -> dict:
        """新建一个生命周期并持久化，返回规范化后的完整对象。"""
        item = normalize_lifecycle(raw)
        items = self._raw_list()
        items.append(item)
        self._save(items)
        return copy.deepcopy(item)

    def update(self, lifecycle_id: str, raw: dict) -> dict | None:
        """按 id 合并更新；不存在返回 None。

        以 ``raw`` 覆盖同名字段，保留版本内未提及字段的既有值。
        """
        items = self._raw_list()
        for i, item in enumerate(items):
            if item.get("id") == lifecycle_id:
                merged = dict(item)
                merged.update(raw if isinstance(raw, dict) else {})
                items[i] = normalize_lifecycle(merged)
                self._save(items)
                return copy.deepcopy(items[i])
        return None

    def delete(self, lifecycle_id: str) -> bool:
        """删除指定生命周期；成功返回 True，不存在返回 False。"""
        items = self._raw_list()
        kept = [i for i in items if i.get("id") != lifecycle_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def duplicate(self, lifecycle_id: str) -> dict | None:
        """深拷贝指定生命周期为新 id，名称追加 ``(copy)``；不存在返回 None。"""
        src = self.get(lifecycle_id)
        if src is None:
            return None
        clone = copy.deepcopy(src)
        clone["id"] = "lc_" + uuid.uuid4().hex[:8]
        clone["name"] = str(src.get("name", "")) + " (copy)"
        items = self._raw_list()
        items.append(clone)
        self._save(items)
        return copy.deepcopy(clone)

    # ---- 阶段推演 ----

    def decide_group(
        self, lifecycle: dict, state: SessionState
    ) -> tuple[str | None, str, str]:
        """根据会话轮数与生命周期配置，决定当前应使用的模型组。

        轮次在 legacy 模式采用 1 基计数：``t = round + 1``（round 为 0 基的已完成轮数）。
        决策分两种模式：

        **staged 模式（``stages`` 非空）**，按 0 基 ``round`` 直接定位：
        1. periodic 优先：``periodic_group`` 非空、``periodic_interval>0`` 且
           ``round>0`` 且 ``round % periodic_interval == 0`` → ``PERIODIC``。
        2. 否则按 stages 累计 rounds 定位：``round < s1 → STAGE_1``、
           ``round < s1+s2 → STAGE_2`` …（k 从 1 起）。
        3. 全部耗尽 → ``final_group``（stage=``"MAIN"``；``final_group`` 空 →
           ``(None, "MAIN", "final_group 未配置")``）。

        **legacy 模式（``stages`` 为空）**：完全保留既有逻辑——
        ``t <= initial_rounds`` → ``INITIAL``；否则 periodic 用旧公式
        ``(t - initial_rounds) % periodic_interval == 0`` → ``PERIODIC``；否则 ``MAIN``。

        计算出的 stage 一律写回 ``state.stage``。

        Args:
            lifecycle: 规范化后的生命周期字典。
            state: 会话状态（读取 ``round``，写入 ``stage``）。

        Returns:
            ``(group_id | None, stage, reason)``；无可用组时 group_id 为 None。
        """
        lc = lifecycle if isinstance(lifecycle, dict) else {}
        r = _as_non_neg_int(state.round)

        # staged 模式：stages 非空
        stages = lc.get("stages")
        if isinstance(stages, list) and stages:
            return self._decide_staged(lc, state, r, stages)

        # ---------- legacy 模式（stages 为空）：完全保留既有逻辑 ----------
        initial_group = str(lc.get("initial_group") or "")
        main_group = str(lc.get("main_group") or "")
        periodic_group = str(lc.get("periodic_group") or "")
        initial_rounds = _as_non_neg_int(lc.get("initial_rounds", 0))
        periodic_interval = _as_non_neg_int(lc.get("periodic_interval", 0))
        t = r + 1  # 1 基轮次

        # 初始阶段：t <= initial_rounds 内使用 initial_group
        if t <= initial_rounds and initial_group and initial_rounds > 0:
            stage = "INITIAL"
            group_id: str | None = initial_group
            reason = f"initial_group 第 {t} 条（共 {initial_rounds} 条）"
        # 周期阶段：初始阶段后每 periodic_interval 条插入一次 periodic_group
        elif (
            periodic_group
            and periodic_interval > 0
            and t > initial_rounds
            and (t - initial_rounds) % periodic_interval == 0
        ):
            stage = "PERIODIC"
            group_id = periodic_group
            reason = f"第 {t} 条触发 periodic_group（每 {periodic_interval} 条）"
        # 主阶段
        else:
            stage = "MAIN"
            group_id = main_group if main_group else None
            reason = "no main group" if not group_id else f"第 {t} 条使用 main_group"

        state.stage = stage
        return group_id, stage, reason

    def _decide_staged(
        self, lc: dict, state: SessionState, r: int, stages: list[dict]
    ) -> tuple[str | None, str, str]:
        """staged 模式决策：periodic 优先，否则按 stages 累计 rounds 定位，耗尽走 final_group。

        Args:
            lc: 生命周期字典。
            state: 会话状态（写入 ``stage``）。
            r: 0 基已完成轮数。
            stages: 已规范化的阶段列表（每条含 ``group_id`` / ``rounds``>0）。

        Returns:
            ``(group_id | None, stage, reason)``。
        """
        periodic_group = str(lc.get("periodic_group") or "")
        periodic_interval = _as_non_neg_int(lc.get("periodic_interval", 0))
        final_group = str(lc.get("final_group") or "")

        # periodic 优先：periodic_group 非空、interval>0、round>0、round%interval==0
        if (
            periodic_group
            and periodic_interval > 0
            and r > 0
            and r % periodic_interval == 0
        ):
            stage = "PERIODIC"
            state.stage = stage
            reason = (
                f"每 {periodic_interval} 轮用 {periodic_group} 校准（第 {r} 轮命中）"
            )
            return periodic_group, stage, reason

        # 按 stages 累计 rounds 定位阶段
        cumulative = 0
        for k, entry in enumerate(stages, start=1):
            erounds = _as_non_neg_int(entry.get("rounds", 0))
            cumulative += erounds
            if r < cumulative:
                stage = f"STAGE_{k}"
                state.stage = stage
                reason = f"第 {k} 阶段（前 {erounds} 轮内，round {r}）使用 {entry.get('group_id')}"
                return str(entry.get("group_id") or ""), stage, reason

        # stages 全部耗尽 → final_group
        if final_group:
            stage = "MAIN"
            state.stage = stage
            reason = f"stages 均已耗尽（总 {cumulative} 轮），使用 final_group"
            return final_group, stage, reason

        stage = "MAIN"
        state.stage = stage
        return None, stage, "final_group 未配置"

    def calibration_config(self, lifecycle: dict) -> dict | None:
        """返回事件校准配置；条件不满足（无事件 / 无组 / 轮数<=0）返回 None。

        Args:
            lifecycle: 生命周期字典。

        Returns:
            ``{"event": str, "group_id": str, "rounds": int}``，或 None（禁用校准）。
        """
        lc = lifecycle if isinstance(lifecycle, dict) else {}
        event = str(lc.get("calibration_event") or "")
        group_id = str(lc.get("calibration_group") or "")
        rounds = _as_non_neg_int(lc.get("calibration_rounds", 0))
        if not event or not group_id or rounds <= 0:
            return None
        return {"event": event, "group_id": group_id, "rounds": int(rounds)}


def should_trigger_compression(prev_tokens, cur_tokens) -> bool:
    """启发式判断「上下文压缩」是否发生（供 main.py 在 on_llm_response 采样调用）。

    AstrBot 不暴露「上下文压缩」事件，这里用两次实际 LLM 调用的输入 token 数
    相对骤降近似判定：``prev >= _COMPRESS_MIN_TOKENS`` 且 ``cur < prev * _COMPRESS_DROP_RATIO``
    视为压缩发生。输入非法（无法 int 化）返回 False；数值自动截断为整数。

    Args:
        prev_tokens: 上一次实际 LLM 调用的输入 token 数。
        cur_tokens: 本次实际 LLM 调用的输入 token 数。

    Returns:
        是否判定为发生了上下文压缩。
    """
    try:
        prev = int(prev_tokens)
        cur = int(cur_tokens)
    except (TypeError, ValueError):
        return False
    if prev < _COMPRESS_MIN_TOKENS:
        return False
    return cur < prev * _COMPRESS_DROP_RATIO
