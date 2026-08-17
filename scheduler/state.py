"""会话状态 —— 按 UMO 隔离的调度运行时状态（模块 B，纯逻辑，不依赖 astrbot）。

本模块提供 ``SessionState``（单会话状态快照）与 ``SessionStateStore``（per-UMO 状态存取 +
并发控制）。调度器所有可变状态（轮数 round、生命周期 stage、当前组/Provider、组内轮换游标
等）都存放在这里，禁止使用全局变量保存当前模型。

并发模型：
- 每个 UMO 一个 ``asyncio.Lock``（``self._locks``），所有对该 UMO 的读写都在对应锁内执行；
- 锁字典自身的并发创建由总锁 ``self._master`` 保护（双重检查建锁）；
- ``snapshot`` / ``restore``（同步方法）整体读写状态字典，供主入口在启动/终止时一次性快照。
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger("astrbot_plugin_model_morph")

# 生命周期可能处于的阶段（与 lifecycle 模块约定一致）。
STAGE_NEW = "NEW"
STAGE_INITIAL = "INITIAL"
STAGE_MAIN = "MAIN"
STAGE_PERIODIC = "PERIODIC"


@dataclass
class SessionState:
    """单个会话（UMO）的调度运行时状态。

    Attributes:
        umo: 会话唯一标识（unified_msg_origin）。
        conversation_id: 会话当前的 conversation id，用于重置检测兜底。
        round: 会话内进入 LLM 的轮数（/new、/reset、conversation_id 变化时归零）。
        stage: 生命周期阶段（NEW|INITIAL|MAIN|PERIODIC）。
        lifecycle_id: 会话当前绑定的生命周期策略 id。
        current_group_id / current_provider_id: 最近一次调度选中的组 / Provider。
        lock_group_id / lock_provider_id / lock_model: 手动锁定的组 / Provider / 模型名
            （非 None 表示已锁定）。lock_provider_id + lock_model 同时非空表示「强锁模型」，
            优先级最高；lock_group_id 单独非空表示旧版「锁组」。
        pending_reset: 标记会话待重置（检测到 /new、/reset 后由下一次调度消费）。
        last_switch_at: 最近一次切换的时间戳（time.time()）。
        last_rule_id / last_trace: 最近命中的规则 id 与决策轨迹。
        group_cursor: 组内轮换游标（``group_id -> {"rr", "uses", "cooldown_until"}``），
            由模型组模块（groups.select_provider）读写。
        calibration_rounds_left: 剩余校准轮数（>0 表示当前处于校准阶段）。
        calibration_group_id: 校准阶段使用的模型组 id。
        calibration_reason: 触发原因（如 ``"context_compression"``）。
    """

    umo: str
    conversation_id: str | None = None
    round: int = 0
    stage: str = STAGE_NEW
    lifecycle_id: str | None = None
    current_group_id: str | None = None
    current_provider_id: str | None = None
    lock_group_id: str | None = None  # 手动锁定（None=未锁）
    lock_provider_id: str | None = None
    lock_model: str | None = None  # 强锁模型名（配合 lock_provider_id，v1.0.3 新增）
    pending_reset: bool = False
    last_switch_at: float = 0.0
    last_rule_id: str | None = None
    last_trace: dict | None = None
    group_cursor: dict = field(
        default_factory=dict
    )  # group_id -> {"rr": int, "uses": {pid: int}, "cooldown_until": {pid: ts}}
    # 校准阶段状态（v0.1.6）：回合数据骤降等事件触发后在限定轮数内固定使用校准组。
    calibration_rounds_left: int = 0  # 剩余校准轮数（>0 = 处于校准阶段）
    calibration_group_id: str | None = None  # 校准使用的模型组
    calibration_reason: str = ""  # 触发原因（"context_compression"）

    def to_dict(self) -> dict:
        """序列化为普通 dict（用于持久化快照 / Web API 展示）。"""
        return {
            "umo": self.umo,
            "conversation_id": self.conversation_id,
            "round": self.round,
            "stage": self.stage,
            "lifecycle_id": self.lifecycle_id,
            "current_group_id": self.current_group_id,
            "current_provider_id": self.current_provider_id,
            "lock_group_id": self.lock_group_id,
            "lock_provider_id": self.lock_provider_id,
            "lock_model": self.lock_model,
            "pending_reset": self.pending_reset,
            "last_switch_at": self.last_switch_at,
            "last_rule_id": self.last_rule_id,
            "last_trace": copy.deepcopy(self.last_trace)
            if self.last_trace is not None
            else None,
            "group_cursor": copy.deepcopy(self.group_cursor),
            "calibration_rounds_left": self.calibration_rounds_left,
            "calibration_group_id": self.calibration_group_id,
            "calibration_reason": self.calibration_reason,
        }

    @staticmethod
    def from_dict(data: dict, umo: str) -> SessionState:
        """从 dict 反序列化（容忍缺字段，缺省用数据类型默认值）。

        Args:
            data: ``to_dict`` 产生的字段映射（可能是首次创建/旧快照，字段可能缺失）。
            umo: 会话唯一标识。

        Returns:
            恢复后的 ``SessionState`` 实例。
        """
        d = data or {}
        gcur = d.get("group_cursor")
        if not isinstance(gcur, dict):
            gcur = {}
        return SessionState(
            umo=umo,
            conversation_id=d.get("conversation_id"),
            round=int(d.get("round", 0) or 0),
            stage=str(d.get("stage", STAGE_NEW) or STAGE_NEW),
            lifecycle_id=d.get("lifecycle_id"),
            current_group_id=d.get("current_group_id"),
            current_provider_id=d.get("current_provider_id"),
            lock_group_id=d.get("lock_group_id"),
            lock_provider_id=d.get("lock_provider_id"),
            lock_model=d.get("lock_model"),
            pending_reset=bool(d.get("pending_reset", False)),
            last_switch_at=float(d.get("last_switch_at", 0.0) or 0.0),
            last_rule_id=d.get("last_rule_id"),
            last_trace=(
                copy.deepcopy(d.get("last_trace"))
                if isinstance(d.get("last_trace"), dict)
                else None
            ),
            group_cursor=copy.deepcopy(gcur),
            # 校准阶段字段：缺省用默认值，兼容旧快照（v0.1.6 之前无这些键）。
            calibration_rounds_left=int(d.get("calibration_rounds_left", 0) or 0),
            calibration_group_id=d.get("calibration_group_id"),
            calibration_reason=str(d.get("calibration_reason", "") or ""),
        )


class SessionStateStore:
    """per-UMO 调度状态的存取与并发控制容器。

    所有返回给调用方的状态均为深拷贝，防止外部直接改内部状态；对外修改一律经
    ``update`` / ``reset`` / ``mark_pending_reset`` 等方法在持有对应 UMO 锁的情况下完成，
    并触发 ``on_change`` 回调（try/except 包裹，回调异常不阻断主流程）。
    """

    def __init__(self, on_change: Callable[[SessionState], None] | None = None):
        """初始化状态容器。

        Args:
            on_change: 每次状态变更（update/reset/mark_pending_reset）后可选回调，
                入参为变更后的 ``SessionState``（深拷贝）。
        """
        self._states: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._master = asyncio.Lock()  # 保护 _locks 字典本身的创建
        self._on_change = on_change

    async def _lock_for(self, umo: str) -> asyncio.Lock:
        """获取（或按需创建）指定 UMO 的锁；创建动作由总锁 ``_master`` 保护，避免并发建锁竞态。"""
        if umo not in self._locks:
            async with self._master:
                # 双重检查：等待总锁期间可能已被其他协程创建
                if umo not in self._locks:
                    self._locks[umo] = asyncio.Lock()
        return self._locks[umo]

    async def get(self, umo: str) -> SessionState:
        """返回指定会话的状态；不存在则创建默认状态（锁内完成）。

        Args:
            umo: 会话唯一标识。

        Returns:
            该会话的 ``SessionState`` 深拷贝。
        """
        async with await self._lock_for(umo):
            state = self._states.get(umo)
            if state is None:
                state = SessionState(umo=umo)
                self._states[umo] = state
            return copy.deepcopy(state)

    async def update(self, umo: str, **changes) -> SessionState:
        """按关键字更新指定会话的状态字段，返回变更后的状态。

        Args:
            umo: 会话唯一标识。
            changes: 需更新的字段（如 ``round=5``、``stage="MAIN"``）。

        Returns:
            更新后的 ``SessionState`` 深拷贝。
        """
        async with await self._lock_for(umo):
            state = self._states.get(umo)
            if state is None:
                state = SessionState(umo=umo)
                self._states[umo] = state
            for key, value in changes.items():
                if hasattr(state, key):
                    setattr(state, key, value)
            self._notify(state)
            return copy.deepcopy(state)

    async def reset(self, umo: str, conversation_id: str | None) -> SessionState:
        """重置会话调度状态（round/stage/lifecycle/游标等归零），保留手动锁定。

        重置语义（与契约一致）：
        - ``round=0``、``stage="NEW"``、``lifecycle_id=None``、``pending_reset=False``；
        - ``group_cursor={}``、``last_rule_id=None``、``last_trace=None``；
        - 校准阶段三字段清空（``calibration_rounds_left=0``、``calibration_group_id=None``、
          ``calibration_reason=""``）；
        - ``lock_group_id`` / ``lock_provider_id`` / ``lock_model`` 保留（手动锁跨重置保留）。

        Args:
            umo: 会话唯一标识。
            conversation_id: 重置后记录的 conversation id（可能为 None）。

        Returns:
            重置后的 ``SessionState`` 深拷贝。
        """
        async with await self._lock_for(umo):
            state = self._states.get(umo)
            if state is None:
                state = SessionState(umo=umo)
                self._states[umo] = state
            state.conversation_id = conversation_id
            state.round = 0
            state.stage = STAGE_NEW
            state.lifecycle_id = None
            state.pending_reset = False
            state.group_cursor = {}
            state.last_rule_id = None
            state.last_trace = None
            # 清空校准阶段三字段
            state.calibration_rounds_left = 0
            state.calibration_group_id = None
            state.calibration_reason = ""
            # lock_group_id / lock_provider_id / lock_model 保留（手动锁跨重置保留）
            self._notify(state)
            return copy.deepcopy(state)

    async def mark_pending_reset(self, umo: str) -> None:
        """将指定会话标记为「待重置」（检测到 /new、/reset 后的上游钩子调用）。

        Args:
            umo: 会话唯一标识。
        """
        async with await self._lock_for(umo):
            st = self._states.get(umo)
            if st is None:
                st = SessionState(umo=umo)
                self._states[umo] = st
            st.pending_reset = True
            self._notify(st)

    async def all_states(self) -> list[SessionState]:
        """返回当前全部会话状态（各为深拷贝，供 WebUI / 统计使用）。"""
        result: list[SessionState] = []
        for umo in list(self._states.keys()):
            async with await self._lock_for(umo):
                st = self._states.get(umo)
                if st is not None:
                    result.append(copy.deepcopy(st))
        return result

    async def remove(self, umo: str) -> bool:
        """移除指定会话的状态，返回是否确实存在过。

        Args:
            umo: 会话唯一标识。

        Returns:
            ``True`` 表示该会话原有状态已被移除，否则 ``False``。
        """
        existed = False
        lock = self._locks.get(umo)
        if lock is None:
            existed = umo in self._states
            self._states.pop(umo, None)
            return existed
        async with lock:
            existed = umo in self._states
            self._states.pop(umo, None)
        # 移除后清掉对应锁，避免无限增长
        self._locks.pop(umo, None)
        return existed

    def snapshot(self) -> dict:
        """导出全部状态快照（``{umo: state.to_dict()}``），供持久化使用。"""
        snap: dict[str, dict] = {}
        for umo, st in self._states.items():
            snap[umo] = st.to_dict()
        return snap

    def restore(self, snap: dict) -> None:
        """从快照恢复全部状态（整体替换内部状态字典）。

        Args:
            snap: ``snapshot`` 产生的 ``{umo: dict}`` 映射。
        """
        restored: dict[str, SessionState] = {}
        for umo, data in (snap or {}).items():
            try:
                restored[str(umo)] = SessionState.from_dict(data, str(umo))
            except Exception:  # noqa: BLE001 - 单条恢复失败跳过，不影响其余
                logger.warning("state.restore: 恢复会话 %s 的状态失败，跳过", umo)
        self._states = restored
        self._locks = {}

    def _notify(self, state: SessionState) -> None:
        """触发 on_change 回调（try/except 包裹，回调异常不得阻断主流程）。"""
        if self._on_change is None:
            return
        try:
            self._on_change(copy.deepcopy(state))
        except Exception:  # noqa: BLE001 - 回调内不应让插件崩溃
            logger.exception("state.on_change 回调执行失败")
