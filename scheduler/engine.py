"""engine —— 调度引擎与决策轨迹（模块 E，纯逻辑，不依赖 astrbot）。

``SchedulerEngine.resolve`` 是每次调用 LLM 前的总入口：综合锁定、规则、生命周期、
base_group 四级作用域，决定当前会话（UMO）应使用的 Provider，并调用注入的
``adapter.set_provider`` 完成切换（adapter 即 compat.RuntimeAdapter，由 main.py 组装注入）。

设计要点：
- 引擎只 import scheduler 内纯逻辑模块（persistence/state/groups/rules/lifecycle/logs），
  绝不 import astrbot / compat，从而可在离线 pytest 中配 FakeAdapter 全流程测试。
- 每个决策都产出 ``DecisionTrace``（命中规则、被拒规则、理由、耗时）。
- resolve 全程 try/except 兜底：任何异常都转为 ``skipped_reason="error"`` 的 trace，
  绝不向上抛出，从而不阻断消息流。

v0.1.6 优先级全序（写进 _decide docstring）：
``会话锁定 > 命中规则动作 > 校准阶段(calibration_rounds_left>0) > 生命周期
(periodic > stages/final > legacy) > default_lifecycle > base_group > 不干预``；
校准覆盖仅在组来自生命周期解析时生效，其选出的 provider 仍可被 temporal 层替换。
"""

from __future__ import annotations

import copy
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from .groups import ModelGroupManager
from .lifecycle import LifecycleEngine
from .logs import SchedulerLog
from .persistence import ConfigStore
from .rules import RuleContext, RuleEngine
from .state import SessionState, SessionStateStore
from .temporal import TemporalEngine


@dataclass
class DecisionTrace:
    """一次调度决策的可解释轨迹。"""

    umo: str
    changed: bool
    final_provider_id: str | None
    final_group_id: str | None
    stage: str | None
    matched_rule: dict | None
    rejected_rules: list[dict]
    condition_results: list[dict]
    reason: str
    elapsed_ms: float = 0.0
    skipped_reason: str = ""
    temporal_matched: dict | None = None
    temporal_group_match: dict | None = None
    replacement_chain: list = field(default_factory=list)
    temporal_reason: str = ""

    def to_dict(self) -> dict:
        """转 dict（供 WebUI / 调试日志 / last_trace 持久化使用）。"""
        return {
            "umo": self.umo,
            "changed": self.changed,
            "final_provider_id": self.final_provider_id,
            "final_group_id": self.final_group_id,
            "stage": self.stage,
            "matched_rule": self.matched_rule,
            "rejected_rules": self.rejected_rules,
            "condition_results": self.condition_results,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "skipped_reason": self.skipped_reason,
            "temporal_matched": self.temporal_matched,
            "temporal_group_match": self.temporal_group_match,
            "replacement_chain": list(self.replacement_chain),
            "temporal_reason": self.temporal_reason,
        }


class SchedulerEngine:
    """模型调度引擎：负责整合各模块完成一次会话的 Provider 决策与切换。"""

    def __init__(
        self,
        store: ConfigStore,
        states: SessionStateStore,
        groups: ModelGroupManager,
        rules: RuleEngine,
        lifecycles: LifecycleEngine,
        slog: SchedulerLog,
        adapter,
        logger=None,
        temporal=None,
    ):
        """初始化。

        Args:
            store: 配置存储（读写 settings / base_group 等）。
            states: 会话状态存储（per-UMO 隔离）。
            groups: 模型组管理器（组内选 provider 与 fallback）。
            rules: 规则引擎（条件评估）。
            lifecycles: 生命周期引擎（阶段推演）。
            slog: 调度日志（环形缓冲）。
            adapter: 兼容运行时的适配器，需提供 ``provider_ids()`` / ``is_local()`` /
                ``get_timezone()`` / ``async get_conversation_id(umo)`` /
                ``async set_provider(id, umo)`` / ``current_provider_id(umo)`` / ``now()``。
            logger: 日志记录器；缺省时使用插件命名空间的 logging 记录器。
            temporal: 时间强制调度引擎（可选）；为 None 时内部自建
                ``TemporalEngine(store, adapter)``。
        """
        self._store = store
        self._states = states
        self._groups = groups
        self._rules = rules
        self._lifecycles = lifecycles
        self._slog = slog
        self._adapter = adapter
        self._logger = logger or logging.getLogger("astrbot_plugin_model_morph")
        self._temporal = (
            temporal if temporal is not None else TemporalEngine(store, adapter)
        )

    # ------------------------------------------------------------------ #

    async def resolve(self, meta: dict) -> DecisionTrace:
        """对一个消息会话执行一次完整调度决策（调度器主流程）。

        Args:
            meta: 会话元数据 dict，结构见 ``compat.get_session_meta``（含
                ``umo`` / ``group_id`` / ``sender_id`` / ``message_str`` /
                ``message_type`` / ``at_bot`` / ``platform_id`` 等）。

        Returns:
            DecisionTrace，绝不抛出异常。
        """
        t0 = time.perf_counter()
        umo = str(meta.get("umo", "") or "")
        settings = self._store.get_settings()
        debug = self._adapter.is_debug()
        trace = DecisionTrace(
            umo=umo,
            changed=False,
            final_provider_id=None,
            final_group_id=None,
            stage=None,
            matched_rule=None,
            rejected_rules=[],
            condition_results=[],
            reason="",
        )

        def _now_iso() -> str:
            try:
                return datetime.now(self._adapter.get_timezone()).isoformat()
            except Exception:  # noqa: BLE001 - 时区取不到时用本地
                return datetime.now().isoformat()

        try:
            # 1. 总开关关闭（AstrBot 原生配置面板 enabled，adapter 实时读取）→ 不干预
            if not self._adapter.is_enabled():
                trace.skipped_reason = "disabled"
                trace.reason = "调度器已禁用"
                return self._finish(trace, t0, debug, umo)

            # 2. 第三方 Agent Runner → 本地调度无效
            if not self._adapter.is_local():
                trace.skipped_reason = "third_party_runner"
                trace.reason = "第三方 Agent Runner，跳过本地调度"
                return self._finish(trace, t0, debug, umo)

            # 3. 读取会话状态（per-UMO 锁内）
            state = await self._states.get(umo)
            old_round = state.round
            old_cid = state.conversation_id

            # 4. 重置检测：pending_reset 或 conversation_id 变化
            try:
                cid = await self._adapter.get_conversation_id(umo)
            except Exception:  # noqa: BLE001 - 读取失败视为无变化
                cid = None
            lifecycle_event = ""
            if state.pending_reset:
                await self._states.reset(umo, cid)
                state = await self._states.get(umo)
                lifecycle_event = "reset"
                reset_event = "reset"
                self._slog.add(
                    {
                        "time": _now_iso(),
                        "umo": umo,
                        "type": "reset",
                        "event": reset_event,
                        "reason": f"pending_reset 标志触发重置 (round {old_round})",
                    }
                )
            elif (
                cid is not None
                and state.conversation_id is not None
                and cid != state.conversation_id
            ):
                # 会话中途更换 conversation_id 视为「新会话」；首条（round=0）不算事件
                lifecycle_event = "new" if old_round > 0 else ""
                reset_event = lifecycle_event or "reset"
                await self._states.reset(umo, cid)
                state = await self._states.get(umo)
                self._slog.add(
                    {
                        "time": _now_iso(),
                        "umo": umo,
                        "type": "reset",
                        "event": reset_event,
                        "reason": f"conversation_id 变化: {old_cid} -> {cid}",
                    }
                )

            # 5. 上下文长度估算（简单模型：round * 512 + 消息长度）
            context_length = state.round * 512 + len(str(meta.get("message_str") or ""))

            # 6. 构造 RuleContext 并评估规则
            tz = self._adapter.get_timezone()
            rctx = RuleContext(
                now=self._adapter.now(),
                tz=tz,
                umo=umo,
                platform_id=str(meta.get("platform_id") or ""),
                platform_name=str(meta.get("platform_name") or ""),
                group_id=str(meta.get("group_id") or ""),
                sender_id=str(meta.get("sender_id") or ""),
                is_group=bool(meta.get("is_group", False)),
                message_type=str(meta.get("message_type") or ""),
                message_str=str(meta.get("message_str") or ""),
                at_bot=bool(meta.get("at_bot", False)),
                round=state.round,
                context_length=context_length,
                lifecycle_event=lifecycle_event,
            )
            eval_result = self._rules.evaluate(rctx)
            matched_rule = eval_result.get("matched_rule")
            trace.matched_rule = copy.deepcopy(matched_rule)
            trace.rejected_rules = copy.deepcopy(eval_result.get("rejected", []))
            trace.condition_results = copy.deepcopy(eval_result.get("results", []))

            available_ids = self._adapter.provider_ids()

            # 7/8/9. 覆盖顺序 → 确定 group 与 provider
            group_id, direct_provider, reason, stage, lifecycle_used = self._decide(
                state, matched_rule, settings, meta
            )

            # 9a. 校准覆盖：仅当组来自生命周期解析（未被锁定/规则直选覆盖）且会话
            #     处于校准阶段时，强制切到校准组。校准仅在真正应用的那一轮递减轮数。
            group_id, stage, reason, calibration_applied = self._calibration_override(
                state, group_id, stage, reason, lifecycle_used
            )
            if calibration_applied:
                state.calibration_rounds_left = max(
                    0, int(state.calibration_rounds_left) - 1
                )

            # 10. 最终 provider 决定；若提供了 provider 且与当前不同 → 切换
            final_provider_id: str | None = None
            last_rule_id: str | None = state.last_rule_id
            changed = False
            old_pid = None
            if direct_provider is not None:
                # switch_provider 直选：校验 provider 可用性
                if direct_provider in available_ids:
                    final_provider_id = direct_provider
                    reason = reason or f"直接指定 Provider {direct_provider}"
                else:
                    self._slog.add(
                        {
                            "time": _now_iso(),
                            "umo": umo,
                            "type": "error",
                            "level": "error",
                            "reason": f"直选 Provider {direct_provider} 不可用",
                        }
                    )
                    final_provider_id = None
                    reason = reason or f"直选 Provider {direct_provider} 不可用"
            elif group_id is not None:
                group = self._groups.get(group_id)
                provider, sel_reason = self._select_from_group(
                    group, state, available_ids
                )
                if provider is not None:
                    final_provider_id = provider
                    reason = reason or sel_reason
                else:
                    reason = reason or sel_reason or "模型组无可用 Provider"

            trace.final_group_id = group_id
            trace.final_provider_id = final_provider_id
            trace.stage = stage or state.stage

            # 10a. temporal 时间强制调度层（运行时替换，不改 0.1.x 决策语义；异常不阻断主流）。
            temporal_rule_id: str | None = None
            temporal_chain: list = []
            if final_provider_id is not None or (
                group_id is not None and direct_provider is None
            ):
                try:
                    now_t = self._adapter.now()
                except Exception:  # noqa: BLE001
                    now_t = None

                # a1. 组来自组选择（group_id 非空且 direct_provider 为空）→ 先做整组切换。
                if group_id is not None and direct_provider is None:
                    try:
                        new_g, g_match = self._temporal.resolve_group(
                            group_id, now_t, tz, meta
                        )
                        if g_match is not None:
                            trace.temporal_group_match = g_match
                            if new_g and new_g != group_id:
                                reselected, resel_reason = self._select_from_group(
                                    self._groups.get(new_g), state, available_ids
                                )
                                if reselected is not None:
                                    group_id = new_g
                                    final_provider_id = reselected
                                    if resel_reason:
                                        reason = (
                                            reason + "；" if reason else ""
                                        ) + resel_reason
                                    reason = (reason + "；" if reason else "") + (
                                        f"temporal 组切换至 {new_g}"
                                    )
                                else:
                                    # 新组无可用成员 → 回退原组，保持原选中的 provider。
                                    reason = (reason + "；" if reason else "") + (
                                        resel_reason
                                        or "temporal 目标组无可用 Provider，回退原组"
                                    )
                    except Exception:  # noqa: BLE001 - temporal 异常不影响主流程
                        self._logger.exception(
                            "SchedulerEngine resolve temporal group_switch 异常"
                        )
                        self._add_error_log(umo, "temporal group_switch 异常")

                # a2. 对最终 provider（无论来源）执行模型替换（model_override）。
                if final_provider_id is not None:
                    try:
                        final_p, t_rule, chain, t_reason = self._temporal.resolve_model(
                            group_id or "", final_provider_id, now_t, tz, meta
                        )
                        if t_rule is not None:
                            final_provider_id = final_p
                            trace.temporal_matched = t_rule
                            trace.replacement_chain = list(chain)
                            trace.temporal_reason = t_reason
                            temporal_rule_id = t_rule.get("id")
                            temporal_chain = list(chain)
                            if t_reason:
                                reason = (reason + "；" if reason else "") + t_reason
                    except Exception:  # noqa: BLE001 - temporal 异常不影响主流程
                        self._logger.exception(
                            "SchedulerEngine resolve temporal model_override 异常"
                        )
                        self._add_error_log(umo, "temporal model_override 异常")

            trace.final_group_id = group_id
            trace.final_provider_id = final_provider_id

            # 10b. 执行切换（只在实际 provider 与当前不同时）
            if final_provider_id is not None:
                try:
                    old_pid = self._adapter.current_provider_id(umo)
                except Exception:  # noqa: BLE001
                    old_pid = None
                if old_pid != final_provider_id:
                    await self._adapter.set_provider(final_provider_id, umo)
                    changed = True
                    if matched_rule and matched_rule.get("id"):
                        last_rule_id = matched_rule["id"]
                    self._slog.add(
                        {
                            "time": _now_iso(),
                            "umo": umo,
                            "type": "switch",
                            "old": old_pid,
                            "new": final_provider_id,
                            "group": group_id,
                            "rule": last_rule_id,
                            "temporal": temporal_rule_id or "",
                            "chain": temporal_chain,
                            "round": state.round,
                            "reason": reason or "",
                        }
                    )
            trace.changed = changed
            trace.reason = reason or (
                "inherit native behavior" if not final_provider_id else ""
            )

            # 11. 轮数 +1 并把本次可能发生的全部状态变更持久化（不依赖局部克隆能被感知，
            #     必须把变更字段显式写入 store 内的权威状态）
            state.round += 1
            changes: dict = {
                "round": state.round,
                "stage": state.stage,
                "group_cursor": state.group_cursor,
                "lifecycle_id": state.lifecycle_id,
                "lock_group_id": state.lock_group_id,
                "lock_provider_id": state.lock_provider_id,
                # 校准三字段：轮数仅在实际应用校准的那一轮递减（见 9a）；其余字段原样持久化。
                "calibration_rounds_left": state.calibration_rounds_left,
                "calibration_group_id": state.calibration_group_id,
                "calibration_reason": state.calibration_reason,
            }
            if changed:
                changes["current_provider_id"] = final_provider_id
                changes["current_group_id"] = group_id
                changes["last_switch_at"] = time.time()
                changes["last_rule_id"] = last_rule_id
                changes["last_trace"] = dict(trace.to_dict())
            await self._states.update(umo, **changes)

            return self._finish(trace, t0, debug, umo)

        except Exception:  # noqa: BLE001 - 调度异常绝不外泄
            self._logger.exception("SchedulerEngine.resolve 异常：%r", umo)
            try:
                self._slog.add(
                    {
                        "time": datetime.now().isoformat(),
                        "umo": umo,
                        "type": "error",
                        "level": "error",
                        "reason": "resolve 调度异常",
                    }
                )
            except Exception:  # noqa: BLE001 - 日志失败忽略
                pass
            trace.skipped_reason = "error"
            trace.reason = "resolve 调度异常"
            return self._finish(trace, t0, debug, umo)

    # ------------------------------------------------------------------ #

    def _decide(
        self, state: SessionState, matched_rule, settings: dict, meta: dict | None = None
    ) -> tuple[str | None, str | None, str, str | None, bool]:
        """按覆盖顺序确定本次应使用的模型组或直选 Provider。

        优先级（v0.1.6 全序，v1.0.1 生命周期分支扩展）：
        ``(unlock 解除锁) > 命中规则动作 > 会话锁定 > 校准阶段(在 resolve 判定) >
        生命周期 > default_lifecycle > base_group > 不干预``。
        生命周期分支 v1.0.1 起按「限定群组」二段式自动选择：
        ``限定命中(match_scoped) > 会话已绑定策略 > 全局(match_global，按 priority) >
        default_lifecycle``。校准阶段不在这里判定，而是由 ``_calibration_override``
        在组已落入生命周期分支时覆盖（因此组在本方法中仍按 lifecycle/base_group 求值）。

        Args:
            state: 会话状态（锁定字段 / lifecycle_id 会被读写）。
            matched_rule: 命中的最高优先级规则（可为 None）。
            settings: 插件设置（含 base_group / default_lifecycle）。
            meta: 会话上下文 dict（``group_id`` / ``sender_id`` / ``umo``），
                供生命周期限定群组匹配；None 时按空处理。

        Returns:
            ``(group_id, direct_provider, reason, stage, lifecycle_used)``。
            ``group_id`` / ``direct_provider`` 至多一个非空；全空代表不干预（继承原生行为）。
            ``lifecycle_used`` 表示本次组来自生命周期解析（``state.lifecycle_id`` /
            规则 ``apply_lifecycle`` / ``default_lifecycle``），供 resolve/simulate
            判断是否适用校准覆盖。
        """
        group_id: str | None = None
        direct_provider: str | None = None
        reason = ""
        stage: str | None = None
        lifecycle_used = False
        then = (matched_rule or {}).get("then") or {}
        action = then.get("action", "") if matched_rule else ""

        # unlock 动作即使会话已锁定也优先执行：解除锁定，恢复自动调度。
        if action == "unlock":
            state.lock_group_id = None
            state.lock_provider_id = None
            reason = "规则解锁会话锁定"
        # 否则若已锁定：锁定优先，跳过（非 unlock）规则（已评估结果保留在 trace）。
        elif state.lock_group_id or state.lock_provider_id:
            group_id = state.lock_group_id
            direct_provider = state.lock_provider_id
            reason = "会话已锁定"
            if group_id:
                return group_id, None, reason, state.stage, False
            if direct_provider:
                return None, direct_provider, reason, state.stage, False

        # 命中规则的其它动作（仅未锁定时执行）
        if matched_rule and action in (
            "switch_group",
            "switch_provider",
            "apply_lifecycle",
        ):
            if action == "switch_group":
                group_id = then.get("group_id")
                reason = f"规则 {matched_rule.get('name', matched_rule.get('id'))} 切换到组 {group_id}"
            elif action == "switch_provider":
                direct_provider = then.get("provider_id")
                reason = f"规则 {matched_rule.get('name', matched_rule.get('id'))} 直选 Provider {direct_provider}"
            elif action == "apply_lifecycle":
                lid = then.get("lifecycle_id")
                if lid:
                    state.lifecycle_id = lid
                    group_id, stage, reason = self._resolve_lifecycle(lid, state)
                    lifecycle_used = True

        # 未命中任何组/直选：按 生命周期 → default_lifecycle → base_group → 不干预
        if group_id is None and direct_provider is None:
            # v1.0.1 二段式：限定命中(match_scoped) > 会话已绑定策略 > 全局(match_global，
            # 按 priority) > default_lifecycle（旧回退）。限定命中覆盖已绑定的全局策略。
            scoped_lc = self._lifecycles.match_scoped(meta)
            if scoped_lc is not None:
                state.lifecycle_id = scoped_lc["id"]
                group_id, stage, reason = self._resolve_lifecycle(
                    scoped_lc["id"], state
                )
                lifecycle_used = True
            elif state.lifecycle_id:
                group_id, stage, reason = self._resolve_lifecycle(
                    state.lifecycle_id, state
                )
                lifecycle_used = True
            else:
                global_lc = self._lifecycles.match_global()
                if global_lc is not None:
                    state.lifecycle_id = global_lc["id"]
                    group_id, stage, reason = self._resolve_lifecycle(
                        global_lc["id"], state
                    )
                    lifecycle_used = True
                elif settings.get("default_lifecycle"):
                    dfl = str(settings["default_lifecycle"] or "")
                    if dfl:
                        # 全局默认生命周期：记住到 state.lifecycle_id（随 changes 持久化），
                        # 直接以 dfl 为入参解析（_resolve_lifecycle 不依赖 state.lifecycle_id）。
                        state.lifecycle_id = dfl
                        group_id, stage, reason = self._resolve_lifecycle(dfl, state)
                        if group_id is not None:
                            lifecycle_used = True
                        else:
                            # 组不存在/禁用 → 说明原因并落到 base_group 分支，不算生命周期命中。
                            fallback_note = f"default_lifecycle {dfl} 无法使用：{reason}"
                            if settings.get("base_group"):
                                bg = str(settings["base_group"])
                                group_id = bg
                                reason = f"{fallback_note}；回退 base_group {bg}"
                            else:
                                reason = f"{fallback_note}；无 base_group，继承原生行为"
                elif settings.get("base_group"):
                    group_id = settings["base_group"]
                    reason = f"使用 base_group {group_id}"
                else:
                    reason = "inherit native behavior"

        return group_id, direct_provider, reason, stage, lifecycle_used

    def _calibration_override(
        self,
        state: SessionState,
        group_id: str | None,
        stage: str | None,
        reason: str,
        lifecycle_used: bool,
    ) -> tuple[str | None, str | None, str, bool]:
        """校准覆盖：若组来自生命周期解析且会话处于校准阶段，则切换到校准组。

        仅当 ``lifecycle_used`` 为 True（组源自生命周期解析，未被锁定/规则直选覆盖）、
        ``state.calibration_rounds_left > 0`` 且 ``state.calibration_group_id`` 非空时
        应用校准：``group_id`` 换为校准组、``stage`` 记为 ``"CALIBRATION"``、
        ``reason`` 追加「校准阶段(剩余 N 轮, 原因)」。

        Args:
            state: 会话状态（读取校准三字段）。
            group_id: 生命周期/base_group/锁定选出的组（可能为 None）。
            stage: 当前阶段标签。
            reason: 当前决策原因。
            lifecycle_used: 组是否来自生命周期解析。

        Returns:
            ``(group_id, stage, reason, applied)``；``applied`` 表示本次确实应用了校准，
            调用方仅在 True 时递减 ``calibration_rounds_left``。
        """
        if (
            lifecycle_used
            and state.calibration_rounds_left > 0
            and state.calibration_group_id
        ):
            note = (
                f"校准阶段(剩余 {int(state.calibration_rounds_left)} 轮, "
                f"{state.calibration_reason or 'unknown'})"
            )
            reason = (reason + "；" if reason else "") + note
            return state.calibration_group_id, "CALIBRATION", reason, True
        return group_id, stage, reason, False

    def _resolve_lifecycle(self, lifecycle_id: str, state: SessionState):
        """解析生命周期并决定当前组；失败时返回 None 组。"""
        lc = self._lifecycles.get(lifecycle_id)
        if lc is None:
            return None, None, f"lifecycle {lifecycle_id} 不存在"
        if not lc.get("enabled", True):
            return None, None, f"lifecycle {lifecycle_id} 已禁用"
        return self._lifecycles.decide_group(lc, state)

    def _select_from_group(self, group, state: SessionState, available_ids: set[str]):
        """从模型组选 provider；组不可用且 allow_auto_fallback 时用 fallbacks[]。"""
        if group is None:
            return None, "模型组不存在"
        if not group.get("enabled", True):
            return None, "模型组已禁用"
        provider, reason = self._groups.select_provider(group, state, available_ids)
        if provider is not None:
            return provider, reason
        # 组内无可用成员且允许自动降级 → 依次尝试 fallbacks
        if group.get("allow_auto_fallback", False):
            for pid in self._groups.fallback_provider_ids(group, available_ids):
                if pid in available_ids:
                    return pid, f"fallback 到 Provider {pid}"
            return None, (reason or "组内无可用 Provider") + "，fallback 也无可用"
        return None, (reason or "组内无可用 Provider")

    # ------------------------------------------------------------------ #

    async def simulate(self, payload: dict) -> dict:
        """模拟（Dry Run）一次调度，不触碰真实状态、不切换 Provider。

        Args:
            payload: ``{"time_iso", "group_id", "sender_id", "umo", "round",
                "lifecycle_event", "message_str", "message_type", "at_bot"}``。

        Returns:
            ``DecisionTrace.to_dict()``。
        """
        umo = str(payload.get("umo") or "")
        r = int(payload.get("round", 0) or 0)
        # 构造临时副本状态，仅用于推演，不持久化；可携带校准字段以与 resolve 语义一致。
        temp = SessionState(umo=umo, round=r)
        if "calibration_rounds_left" in payload:
            temp.calibration_rounds_left = int(
                payload.get("calibration_rounds_left") or 0
            )
        if payload.get("calibration_group_id"):
            temp.calibration_group_id = str(payload.get("calibration_group_id"))
        if payload.get("calibration_reason"):
            temp.calibration_reason = str(payload.get("calibration_reason"))

        tz = self._adapter.get_timezone()
        try:
            now = datetime.fromisoformat(str(payload.get("time_iso") or ""))
        except (TypeError, ValueError):
            now = self._adapter.now()
        if now.tzinfo is None:
            try:
                now = now.replace(tzinfo=tz)
            except Exception:  # noqa: BLE001 - 时区附加失败则保持原值
                pass

        context_length = r * 512 + len(str(payload.get("message_str") or ""))
        rctx = RuleContext(
            now=now,
            tz=tz,
            umo=umo,
            group_id=str(payload.get("group_id") or ""),
            sender_id=str(payload.get("sender_id") or ""),
            message_str=str(payload.get("message_str") or ""),
            message_type=str(payload.get("message_type") or ""),
            at_bot=bool(payload.get("at_bot", False)),
            round=r,
            context_length=context_length,
            lifecycle_event=str(payload.get("lifecycle_event") or ""),
        )
        eval_result = self._rules.evaluate(rctx)
        settings = self._store.get_settings()
        available_ids = self._adapter.provider_ids()

        # sim_meta：限定群组（scope）匹配用上下文（group_id/sender_id/umo 取自 payload）。
        sim_meta = {
            "group_id": str(payload.get("group_id") or ""),
            "sender_id": str(payload.get("sender_id") or ""),
            "umo": umo,
        }

        group_id, direct_provider, reason, stage, lifecycle_used = self._decide(
            temp, eval_result.get("matched_rule"), settings, sim_meta
        )
        # 校准覆盖（simulate 为只读推演，不递减轮数，与 resolve 的选组语义一致）。
        group_id, stage, reason, _applied = self._calibration_override(
            temp, group_id, stage, reason, lifecycle_used
        )

        final_provider_id: str | None = None
        if direct_provider is not None and direct_provider in available_ids:
            final_provider_id = direct_provider
        elif group_id is not None:
            provider, _ = self._select_from_group(
                self._groups.get(group_id), temp, available_ids
            )
            final_provider_id = provider

        # temporal 层：与 resolve 相同的两层（整组切换 → 模型替换），用 payload 的 now/meta 推演。
        temporal_matched: dict | None = None
        temporal_group_match: dict | None = None
        replacement_chain: list = []
        temporal_reason = ""
        try:
            # s1. 组来自组选择 → 先做整组切换。
            if group_id is not None and direct_provider is None:
                new_g, g_match = self._temporal.resolve_group(
                    group_id, now, tz, sim_meta
                )
                if g_match is not None:
                    temporal_group_match = g_match
                    if new_g and new_g != group_id:
                        reselected, resel_reason = self._select_from_group(
                            self._groups.get(new_g), temp, available_ids
                        )
                        if reselected is not None:
                            group_id = new_g
                            final_provider_id = reselected
                            if resel_reason:
                                reason = (
                                    reason + "；" if reason else ""
                                ) + resel_reason
                            reason = (reason + "；" if reason else "") + (
                                f"temporal 组切换至 {new_g}"
                            )
                        else:
                            reason = (reason + "；" if reason else "") + (
                                resel_reason
                                or "temporal 目标组无可用 Provider，回退原组"
                            )
            # s2. 对最终 provider 执行模型替换（model_override）。
            if final_provider_id is not None:
                final_p, t_rule, chain, t_reason = self._temporal.resolve_model(
                    group_id or "", final_provider_id, now, tz, sim_meta
                )
                if t_rule is not None:
                    final_provider_id = final_p
                    temporal_matched = t_rule
                    replacement_chain = list(chain)
                    temporal_reason = t_reason
                    if t_reason:
                        reason = (reason + "；" if reason else "") + t_reason
        except Exception:  # noqa: BLE001 - temporal 异常不影响模拟结果
            self._logger.exception("SchedulerEngine simulate temporal 异常")

        trace = DecisionTrace(
            umo=umo,
            changed=final_provider_id is not None,
            final_provider_id=final_provider_id,
            final_group_id=group_id,
            stage=stage or temp.stage,
            matched_rule=copy.deepcopy(eval_result.get("matched_rule")),
            rejected_rules=copy.deepcopy(eval_result.get("rejected", [])),
            condition_results=copy.deepcopy(eval_result.get("results", [])),
            reason=reason or "inherit native behavior",
            temporal_matched=temporal_matched,
            temporal_group_match=temporal_group_match,
            replacement_chain=replacement_chain,
            temporal_reason=temporal_reason,
        )
        return trace.to_dict()

    # ------------------------------------------------------------------ #

    def dashboard(self) -> dict:
        """Web 仪表盘摘要数据。"""
        groups = self._store.get_groups()
        rules = self._store.get_rules()
        lifecycles = self._store.get_lifecycles()
        settings = self._store.get_settings()
        try:
            tz_name = str(self._adapter.get_timezone())
        except Exception:  # noqa: BLE001 - 时区名取不到时留空
            tz_name = ""
        provider_count = len(self._adapter.provider_ids())
        # 会话数用同步的 snapshot() 统计（all_states 为协程，dashboard 为同步方法，不能用）
        snap_method = getattr(self._states, "snapshot", None)
        try:
            snap = snap_method() if callable(snap_method) else {}
            session_count = len(snap)
            # 校准阶段会话数：快照中 calibration_rounds_left>0 的数量。
            calibration_sessions = sum(
                1
                for st in snap.values()
                if isinstance(st, dict)
                and int(st.get("calibration_rounds_left") or 0) > 0
            )
        except Exception:  # noqa: BLE001 - 状态读取失败计 0
            session_count = 0
            calibration_sessions = 0

        # temporal 层：规则总数 + 当前生效规则浅层展示（最多 20 条；异常不影响主数据）。
        temporal_rule_count = 0
        active_temporal_rules: list[dict] = []
        try:
            temporal_rule_count = len(self._store.get_temporal_rules())
            now_t = self._adapter.now()
            tz_t = self._adapter.get_timezone()
            active = self._temporal.active_rules(now_t, tz_t)
        except Exception:  # noqa: BLE001 - temporal 数据读取失败按空处理
            active = []
        for rule in active[:20]:
            sch = rule.get("schedule") or {}
            active_temporal_rules.append(
                {
                    "id": rule.get("id", ""),
                    "name": rule.get("name", ""),
                    "kind": rule.get("kind", ""),
                    "group_id": rule.get("group_id", ""),
                    "source_provider": rule.get("source_provider", ""),
                    "target_provider": rule.get("target_provider", ""),
                    "target_group": rule.get("target_group", ""),
                    "scope": copy.deepcopy(rule.get("scope") or {}),
                    "schedule_type": sch.get("type", ""),
                    "schedule_start": sch.get("start", ""),
                    "schedule_end": sch.get("end", ""),
                    "priority": rule.get("priority", 0),
                }
            )
        # 默认生命周期名（供前端展示；未设置/不存在 → 空串）。
        default_lifecycle_id = str(settings.get("default_lifecycle", "") or "")
        default_lifecycle_name = ""
        if default_lifecycle_id:
            for lc in lifecycles:
                if lc.get("id") == default_lifecycle_id:
                    default_lifecycle_name = str(lc.get("name") or lc.get("id") or "")
                    break
        return {
            "enabled": self._adapter.is_enabled(),
            "debug": self._adapter.is_debug(),
            "timezone": tz_name,
            "provider_count": provider_count,
            "group_count": len(groups),
            "rule_count": len(rules),
            "lifecycle_count": len(lifecycles),
            "default_lifecycle": default_lifecycle_id,
            "default_lifecycle_name": default_lifecycle_name,
            "calibration_sessions": calibration_sessions,
            "temporal_rule_count": temporal_rule_count,
            "active_temporal_rules": active_temporal_rules,
            "session_count": session_count,
            "recent_switches": self._slog.recent(10),
            "recent_errors": self._slog.recent(10, level="error"),
        }

    # ------------------------------------------------------------------ #

    def _finish(
        self, trace: DecisionTrace, t0: float, debug: bool, umo: str
    ) -> DecisionTrace:
        """结算耗时、按 debug 记录完整决策轨迹并返回 trace。"""
        trace.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if debug:
            self._logger.info(
                "ModelMorph resolve umo=%s -> %s",
                umo,
                json.dumps(trace.to_dict(), ensure_ascii=False, default=str),
            )
        return trace

    def _add_error_log(self, umo: str, reason: str) -> None:
        """向调度日志追加一条错误记录（用于 temporal 层异常，不向上抛）。"""
        try:
            self._slog.add(
                {
                    "time": self._now_iso(),
                    "umo": str(umo or ""),
                    "type": "error",
                    "level": "error",
                    "reason": reason,
                }
            )
        except Exception:  # noqa: BLE001 - 日志失败忽略
            pass

    def _now_iso(self) -> str:
        """返回当前时刻 ISO 字符串（带插件时区；取不到时用本地时间）。"""
        try:
            return datetime.now(self._adapter.get_timezone()).isoformat()
        except Exception:  # noqa: BLE001 - 时区取不到时用本地
            return datetime.now().isoformat()
