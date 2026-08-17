"""agent_tools —— Agent 结构化工具（模块 T4，纯逻辑，不依赖 astrbot）。

为「配置 Agent」（聊天 SubAgent / Web 助手）提供读写模型组、时间调度规则
（temporal）与配置校验 / 预览 / 应用 / 回滚的结构化工具函数。每个工具函数都是
``(tc: ToolContext, **kwargs) -> dict`` 的同步函数，**绝不抛异常**：任何失败统一
返回 ``{"ok": False, "error": "中文原因"}``，成功返回 ``{"ok": True, ...}``。

设计要点：
- ``ToolContext`` 聚合依赖（store / groups / rules / temporal / audit / provider_infos /
  tz / settings / data_dir），并把来源（``source``）与操作者（``operator``）随审计写入。
- 遵循「先查询再修改」：create/update 前先用 manager 读现状计算 before/after，再落库。
- **分级审批（v1.0.3）**：写操作按风险分两类（仅对 Agent 工具生效）——
  - 低风险（创建类 + 启停 toggle）：直接执行并校验，记审计；
  - 高危（删除 / 修改已有配置 / 规则引擎全部 CRUD）：``agent_confirm=True``（默认）时把
    变更以 ``ops`` 形式存入暂存区（``pending.json``，含人性化 ``summary`` 与 ``staged_at``），
    返回 ``{"ok", "status":"staged", "pending_id", "summary"}``，由管理员执行
    ``/scheduler approve <pending_id>`` / ``reject`` 批准或放弃；``agent_confirm=False`` 时
    直接执行（等同旧行为）。高危分类由模块级 ``_HIGH_RISK_TOOLS`` 描述。
- 分级审批公开入口：``apply_staged`` / ``reject_staged`` / ``pending_view``（契约 C8），
  供 main.py 指令与 web/api.py 端点调用。
- 预览 → 应用 → 回滚（老流程，仍保留兼容 Web 助手）：preview 校验每个 op（不写库）→
  通过才把完整配置快照（``store.export_all()``）暂存 pending；apply 按序真实执行并逐 op
  写审计；rollback 用 last_snapshot 恢复（``store.import_all``）。新增写工具（规则引擎 CRUD
  与高危暂存）不再要求走 preview。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from .groups import normalize_group

logger = logging.getLogger("astrbot_plugin_model_morph")

# 时间调度（temporal）规则 + 模型组 + 生命周期 + 规则引擎（when/then）的工具动作集合。
# 供旧 preview→apply 管线（tool_preview/apply_configuration_change）计数用，
# 也覆盖新增的规则引擎写动作。
_WRITE_ACTIONS = (
    "create_schedule_rule",
    "update_schedule_rule",
    "delete_schedule_rule",
    "create_model_group",
    "update_model_group",
    "delete_model_group",
    "create_lifecycle",
    "update_lifecycle",
    "delete_lifecycle",
    "create_model_override",
    "update_model_override",
    "delete_model_override",
    "create_rule",
    "update_rule",
    "delete_rule",
    "toggle_rule",
)

# 高危工具：agent_confirm=True（默认）时写操作一律进暂存区（pending.json）等待管理员批准
# （/scheduler approve / reject）；agent_confirm=False 时直接执行（等同旧行为）。
# 删除 / 修改已有配置 / 规则引擎全部 CRUD（含 create）均属高危。
_HIGH_RISK_TOOLS = {
    "tool_create_rule",
    "tool_update_rule",
    "tool_delete_rule",
    "tool_toggle_rule",
    "tool_update_model_group",
    "tool_delete_model_group",
    "tool_update_schedule_rule",
    "tool_delete_schedule_rule",
    "tool_update_model_override",
    "tool_delete_model_override",
    "tool_update_lifecycle",
    "tool_delete_lifecycle",
}


def _ok(**payload) -> dict:
    """构造成功返回 dict（固定带 ``ok: True``）。"""
    result = {"ok": True}
    result.update(copy.deepcopy(payload))
    return result


def _fail(message: str, **extra) -> dict:
    """构造失败返回 dict（固定带 ``ok: False, error``）。"""
    result = {"ok": False, "error": message}
    result.update(copy.deepcopy(extra))
    return result


def _ts(tc) -> str:
    """生成带调度时区的时间戳（ISO），供审计 entry 使用。"""
    try:
        return datetime.now(tc.tz).isoformat()
    except Exception:  # noqa: BLE001 - 时间异常兜底
        return ""


def _audit(tc, action, target, before, after, result="success", detail=""):
    """追加一条审计（来源 / 操作者取自 tc；缺省字段由 AuditLog 补齐）。

    Args:
        tc: ``ToolContext``。
        action: 动作名（如 ``create_schedule_rule``）。
        target: 目标标识（组 id / 规则 id / 名称）。
        before: 变更前快照（可为 None）。
        after: 变更后快照（可为 None）。
        result: ``success`` / ``failed`` / ``preview`` / ``rollback``。
        detail: 补充说明。
    """
    try:
        tc.audit.add(
            {
                "time": _ts(tc),
                "operator": tc.operator,
                "source": tc.source,
                "action": action,
                "target": target,
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
                "result": result,
                "detail": detail,
            }
        )
    except Exception:  # noqa: BLE001 - 审计失败不阻断工具
        logger.warning("agent_tools: 写审计失败（忽略）")


def _normalize_target(value: str) -> str:
    """规整组 id / 规则 id（去除空白）。"""
    return "" if value is None else str(value).strip()


class PendingChangeStore:
    """待应用更改的持久化（纯逻辑）。

    在 ``data_dir`` 下维护两个文件：
    - ``pending.json``：待执行的操作列表 + 完整配置快照（``{pending_id, ops, snapshot}``）。
    - ``last_snapshot.json``：最近一次 apply 前的配置快照（供 rollback），保留最近 1 份。

    写入统一走「写 tmp → os.replace」原子替换（参照 persistence.save）。
    """

    def __init__(self, data_dir):
        """初始化待应用存储目录。

        Args:
            data_dir: 插件持久化数据目录（ConfigStore 的 data_dir）。
        """
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pending_path = self._dir / "pending.json"
        self._last_path = self._dir / "last_snapshot.json"

    @staticmethod
    def _write(path: Path, payload) -> None:
        """原子写入（写 tmp → os.replace）。"""
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    @staticmethod
    def _read(path: Path):
        """读取 JSON 对象；文件不存在 / 损坏返回 None（不抛）。"""
        path = Path(path)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 损坏兜底
            logger.warning("agent_tools: 读取 %s 失败，视为空", path.name)
            return None

    def stage(
        self, ops, snapshot, preview=None, summary=None, staging_batch=None
    ) -> str:
        """暂存待应用更改：写入 ``{pending_id, ops, snapshot, preview, summary, staged_at, staging_batch}``。

        Args:
            ops: 待执行的操作列表（apply 时按序真实执行）。
            snapshot: 完整配置快照（``store.export_all()`` 的结果）。
            preview: 可选的预览项列表（``[{action, target, before, after, warnings}]``），
                Web 助手用它渲染「待应用更改」预览卡；apply 仍只消费 ``ops``。
                v1.0.3 新分级审批不再走 preview，仅旧 preview→apply 流程兼容传入。
            summary: 可选的人性化变更描述列表（一 op 一行）。缺省为 []。旧调用不传时
                条目仍含 ``summary: []``（键恒在）。
            staging_batch: 可选的一轮 Agent 对话暂存批次标识（``sub_xxx`` / ``web_xxx``）。
                同批连续高危写由 ``_stage_op`` 合并为同一份 pending（追加 op 与 summary），
                跨批才覆盖并写 ``stale`` 审计。旧 pending.json 无此键时读取
                ``get("staging_batch")`` 为 None，天然兼容。

        Returns:
            ``pending_id``（形如 ``p_xxxxxxxx``）。
        """
        pending_id = "p_" + uuid.uuid4().hex[:8]
        payload: dict = {
            "pending_id": pending_id,
            "ops": copy.deepcopy(ops),
            "snapshot": copy.deepcopy(snapshot),
            "summary": copy.deepcopy(summary) if summary is not None else [],
            "staged_at": datetime.now().astimezone().isoformat(),
        }
        if staging_batch is not None:
            payload["staging_batch"] = staging_batch
        if preview is not None:
            payload["preview"] = copy.deepcopy(preview)
        self._write(self._pending_path, payload)
        return pending_id

    def get(self) -> dict | None:
        """返回当前待应用更改（含 pending_id / ops / snapshot，以及可选 preview），无则 None。"""
        return self._read(self._pending_path)

    def apply_snapshot(self) -> None:
        """apply 成功后把当前 pending 的快照另存为 last_snapshot.json。

        保留最近 1 份；调用前应确保 pending 存在（已在 apply 成功路径已判定）。
        """
        pending = self.get()
        snapshot = (pending or {}).get("snapshot")
        if snapshot is not None:
            self._write(self._last_path, copy.deepcopy(snapshot))

    def clear(self) -> None:
        """清空 pending.json（应用 / 回滚 / 放弃后调用）。"""
        try:
            if self._pending_path.exists():
                os.remove(self._pending_path)
        except OSError:  # 删除失败仅告警，不阻断
            logger.warning("agent_tools: 清理 pending.json 失败")

    def last_snapshot(self) -> dict | None:
        """返回最近一次 apply 前的配置快照（供 rollback），无则 None。"""
        return self._read(self._last_path)

    def mark_rolled_back(self) -> None:
        """回滚完成后清空 last_snapshot.json（快照已被消费）。"""
        try:
            if self._last_path.exists():
                os.remove(self._last_path)
        except OSError:
            logger.warning("agent_tools: 清理 last_snapshot.json 失败")


def _as_list(value) -> list:
    """把值规整为列表（None → []，标量 → [标量]）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _validate_group_provider_ids(tc, providers) -> list[str]:
    """校验组内 provider_id 是否存在（provider_infos 非空时）。

    Args:
        tc: ``ToolContext``。
        providers: 组内成员列表（含 provider_id 键）或 provider id 列表。

    Returns:
        错误信息列表（空即通过）。
    """
    known = tc.provider_ids()
    if not known:
        return []
    errors: list[str] = []
    for pid in _as_list(providers):
        if isinstance(pid, dict):
            pid = pid.get("provider_id", "")
        if pid and pid not in known:
            errors.append(f"未知 Provider：{pid}（已配置 Provider：{sorted(known)}）")
    return errors


class ToolContext:
    """Agent 结构化工具的依赖上下文（聚合 manager / 审计 / 环境信息）。

    便捷只读方法（groups_ / rules_ / temporal_ / providers_ / group_ids /
    provider_ids）每次从对应 manager / 传入对象实时读取，遵循「先查询再修改」。
    """

    def __init__(
        self,
        store,
        groups,
        rules,
        temporal,
        audit,
        provider_infos,
        tz,
        settings,
        data_dir,
        lifecycles=None,
        source="manual",
        operator="admin",
    ):
        """初始化依赖上下文。

        Args:
            store: ``persistence.ConfigStore`` 实例（提供 export_all / import_all /
                get_settings / get_groups / get_rules 等）。
            groups: ``groups.ModelGroupManager`` 实例。
            rules: ``rules.RuleEngine`` 实例。
            temporal: ``temporal.TemporalEngine`` 实例（按契约公开方法调用）。
            audit: ``audit.AuditLog`` 实例。
            provider_infos: ``compat.get_provider_info_list`` 的结果（仅消费结构）。
            tz: 时区（``zoneinfo.ZoneInfo``）。
            settings: 插件设置 dict（副本，含 ``agent_confirm`` 等）。
            data_dir: 插件持久化数据目录（pending 快照 / last_snapshot 落盘于此）。
            lifecycles: ``lifecycle.LifecycleEngine`` 实例（v0.1.6 可选；None 时未注入，
                生命周期工具返回「生命周期引擎未注入」）。
            source: 审计来源（``web_agent`` / ``subagent`` / ``wizard`` / ``preset`` /
                ``manual`` / ``system``）。
            operator: 操作者标识（如 ``admin`` 或发送者 id）。
            staging_batch: 可选的一轮 Agent 对话暂存批次标识；默认 None。由 agent.py
                每个 agent 循环开始时（``tool_loop_agent`` / ``run_web_agent`` /
                ``run_web_agent_stream``）更新，用于 ``_stage_op`` 区分「同一轮对话的
                连续高危写 → 合并」与「跨轮对话 → 覆盖」。注意：**不能**在 __init__
                一次性设置（``tool_ctx`` 是 main.py 的单例，跨请求复用）。
        """
        self.store = store
        self.groups = groups
        self.rules = rules
        self.temporal = temporal
        self.lifecycles = lifecycles
        self.audit = audit
        self.provider_infos = provider_infos or []
        self.tz = tz
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.source = source
        self.operator = operator
        self.staging_batch = None
        self.pending = PendingChangeStore(self.data_dir)

    # ---- 便捷只读 ----

    def groups_(self) -> list[dict]:
        """实时读取模型组列表。"""
        return self.groups.list_()

    def rules_(self) -> list[dict]:
        """实时读取现有 rules 列表。"""
        return self.rules.list_()

    def temporal_(self) -> list[dict]:
        """实时读取全部 temporal 规则列表。"""
        return self.temporal.list_()

    def lifecycles_(self) -> list[dict]:
        """实时读取全部生命周期列表；未注入引擎时返回 []（工具内再兜底提示）。"""
        if self.lifecycles is None:
            return []
        return self.lifecycles.list_()

    def providers_(self) -> list[dict]:
        """返回 Provider 信息列表（含 id/model/type/enabled）。"""
        return copy.deepcopy(self.provider_infos)

    def group_ids(self) -> set:
        """返回现有模型组 id 集合。"""
        return {g.get("id") for g in self.groups_()}

    def provider_ids(self) -> set:
        """返回已配置 Provider id 集合。"""
        return {p.get("id") for p in self.providers_() if p.get("id")}


def _resolve_group(tc, name: str):
    """按组 id 或名称定位模型组（模糊不命中时返回 None）。

    Args:
        tc: ``ToolContext``。
        name: 组 id 或组名。

    Returns:
        匹配的规范化组 dict；未命中返回 None。
    """
    key = str(name or "").strip()
    if not key:
        return None
    for g in tc.groups_():
        if g.get("id") == key or g.get("name") == key:
            return copy.deepcopy(g)
    return None


def _similar_group_ids(tc, name: str) -> list[str]:
    """给出与 name 相似（名称 / id 包含匹配）的候选组 id，供查询提示。"""
    key = str(name or "").strip().lower()
    if not key:
        return []
    out = []
    for g in tc.groups_():
        cand = str(g.get("id", "")).lower() + " " + str(g.get("name", "")).lower()
        if key in cand:
            out.append(g.get("id"))
    return out


def _is_positive_int(value) -> bool:
    """判断输入是否为大于 0 的整数（非法 / 非数返回 False）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False
    return n > 0


def _similar_lifecycle_ids(tc, name: str) -> list[str]:
    """给出与 name 相似（名称 / id 包含匹配）的候选生命周期 id，供查询提示。

    复用 :func:`_similar_group_ids` 的思路：按名称 / id 的子串命中收集候选。
    """
    key = str(name or "").strip().lower()
    if not key:
        return []
    out = []
    for item in tc.lifecycles_():
        cand = str(item.get("id", "")).lower() + " " + str(item.get("name", "")).lower()
        if key in cand:
            out.append(item.get("id"))
    return out


def _validate_lifecycle_spec(tc, spec) -> list[str]:
    """校验生命周期结构（自实现，不依赖 A1 的 normalize 内部），返回错误列表。

    检查项（与 v0.1.6 契约一致）：
    - ``stages`` 必须是数组，且每条含存在的 ``group_id`` 与正整数 ``rounds``；
    - ``final_group`` 非空时须为已存在的组；
    - ``periodic_group`` 非空时 ``periodic_interval`` 须为正整数；
    - ``calibration_event`` 只能是 ``""`` 或 ``"context_compression"``；
      ``calibration_event`` 非空时 ``calibration_group`` 须存在且
      ``calibration_rounds`` 为正整数。

    Args:
        tc: ``ToolContext``。
        spec: 待校验的生命周期 dict。

    Returns:
        错误信息列表（空即通过）。
    """
    if not isinstance(spec, dict):
        return ["生命周期 spec 必须是对象（dict）"]
    errors: list[str] = []
    known = tc.group_ids()

    stages = spec.get("stages", [])
    if not isinstance(stages, (list, tuple)):
        errors.append("stages 必须为数组（多阶段列表）")
    else:
        for i, st in enumerate(stages):
            if not isinstance(st, dict):
                errors.append(f"阶段 {i}: 必须是对象")
                continue
            gid = str(st.get("group_id") or "")
            if not gid or gid not in known:
                errors.append(f"阶段 {i}: group_id「{gid}」不存在")
            if not _is_positive_int(st.get("rounds")):
                errors.append(f"阶段 {i}: rounds 必须为正整数")

    final_group = str(spec.get("final_group") or "")
    if final_group and final_group not in known:
        errors.append(f"final_group「{final_group}」不存在")

    periodic_group = str(spec.get("periodic_group") or "")
    if periodic_group:
        if periodic_group not in known:
            errors.append(f"periodic_group「{periodic_group}」不存在")
        if not _is_positive_int(spec.get("periodic_interval")):
            errors.append("periodic_interval 必须为正整数（periodic_group 非空时）")

    calibration_event = spec.get("calibration_event", "")
    if calibration_event not in ("", "context_compression"):
        errors.append("calibration_event 只能是空字符串或 context_compression")
    if calibration_event:
        calibration_group = str(spec.get("calibration_group") or "")
        if not calibration_group or calibration_group not in known:
            errors.append(f"calibration_group「{calibration_group}」不存在")
        if not _is_positive_int(spec.get("calibration_rounds")):
            errors.append("calibration_rounds 必须为正整数（calibration_event 非空时）")

    return errors


def _temporal_validate(tc, raw):
    """委托 temporal 校验并返回 ``(ok, result_dict)``。

    Args:
        tc: ``ToolContext``。
        raw: 待校验的 temporal 规则 dict。

    Returns:
        ``(ok, result)``；``result`` 为 temporal.validate 的返回（含 errors / warnings）。
    """
    try:
        result = tc.temporal.validate(
            raw,
            known_provider_ids=tc.provider_ids(),
            known_group_ids=tc.group_ids(),
        )
        return bool(result.get("ok")), result
    except Exception as exc:  # noqa: BLE001 - temporal 异常兜底
        logger.warning("agent_tools: temporal.validate 异常 %r", exc)
        return False, {"ok": False, "errors": [f"校验异常：{exc}"], "warnings": []}


# ---- 查询工具 ----


def tool_list_model_groups(tc) -> dict:
    """列出全部模型组（含 id/name/enabled/providers 概要）。"""
    groups = []
    for g in tc.groups_():
        groups.append(
            {
                "id": g.get("id"),
                "name": g.get("name"),
                "desc": g.get("desc"),
                "enabled": g.get("enabled"),
                "strategy": g.get("strategy"),
                "provider_count": len(g.get("providers") or []),
            }
        )
    return _ok(groups=groups)


def tool_get_model_group(tc, name="") -> dict:
    """按 id 或名称查询模型组；未见时给出相似候选。"""
    group = _resolve_group(tc, name)
    if group is None:
        sims = _similar_group_ids(tc, name)
        return _fail(f"未找到模型组「{name}」", candidates=sims)
    return _ok(group=copy.deepcopy(group))


def tool_list_models(tc) -> dict:
    """列出已配置 Provider（含 id/model/type/enabled 与所属组统计）。"""
    group_count: dict = {}
    for g in tc.groups_():
        for entry in g.get("providers") or []:
            pid = entry.get("provider_id")
            if pid:
                group_count[pid] = group_count.get(pid, 0) + 1
    models = []
    for p in tc.providers_():
        item = copy.deepcopy(p)
        item["group_count"] = group_count.get(p.get("id"), 0)
        models.append(item)
    return _ok(models=models)


def tool_list_rules(tc) -> dict:
    """列出规则：``rules`` 为现有规则，``temporal_rules`` 为时间调度规则。"""
    return _ok(rules=tc.rules_(), temporal_rules=tc.temporal_())


def tool_get_active_rules(tc) -> dict:
    """列出当前时刻生效的 temporal 规则。"""
    try:
        active = tc.temporal.active_rules(None, tc.tz, None)
    except Exception:  # noqa: BLE001 - temporal 读取兜底
        logger.warning("agent_tools: active_rules 调用异常", exc_info=True)
        active = []
    return _ok(active_rules=copy.deepcopy(active))


def tool_get_scheduled_rules(tc) -> dict:
    """列出全部 temporal 调度规则。"""
    return _ok(scheduled_rules=tc.temporal_())


def tool_get_scheduler_status(tc) -> dict:
    """返回调度器当前状态（开关 / 时区 / 各类数量 / base_group / 冲突）。"""
    settings = tc.settings or {}
    groups = tc.groups_()
    rules = tc.rules_()
    temporals = tc.temporal_()
    lifecycles = tc.lifecycles_()
    conflicts: list = []
    try:
        if hasattr(tc.temporal, "find_conflicts"):
            conflicts = copy.deepcopy(tc.temporal.find_conflicts(temporals))
    except Exception:  # noqa: BLE001 - 冲突检测兜底
        logger.warning("agent_tools: find_conflicts 调用异常", exc_info=True)
    return _ok(
        enabled=bool(settings.get("enabled", True)),
        tz=str(tc.tz),
        group_count=len(groups),
        rule_count=len(rules),
        temporal_count=len(temporals),
        provider_count=len(tc.providers_()),
        base_group=settings.get("base_group", ""),
        lifecycle_count=len(lifecycles),
        default_lifecycle=settings.get("default_lifecycle", ""),
        agent_confirm=bool(settings.get("agent_confirm", True)),
        conflicts=conflicts,
    )


def tool_get_runtime_routing(tc, group_id="") -> dict:
    """只读推演当前时刻某模型组的最终路由：组选择 → 组内 provider → temporal 替换链。

    不动任何状态：选择过程用确定性「最高优先级的可用 provider」，再叠加
    temporal.resolve_model 的替换链。
    """
    now = datetime.now(tc.tz)
    meta = {"group_id": group_id or (tc.settings or {}).get("base_group", "")}
    chosen_group = group_id or meta["group_id"] or ""
    group = _resolve_group(tc, chosen_group)
    provider = ""
    reason_parts: list[str] = []
    if group is None:
        reason_parts.append(f"组「{chosen_group}」不存在")
    else:
        available = tc.provider_ids()
        candidates = [
            e
            for e in (group.get("providers") or [])
            if e.get("enabled") and e.get("provider_id") in available
        ]
        if not candidates:
            reason_parts.append("组内无可用 Provider")
        else:
            chosen = min(candidates, key=lambda e: e.get("priority", 0))
            provider = chosen.get("provider_id")
            reason_parts.append(f"组内选择 provider={provider}")
    # 叠加 temporal 替换链
    replacement_chain: list = []
    matched = None
    try:
        final_provider, matched, chain, treason = tc.temporal.resolve_model(
            group.get("id", "") if group else (meta["group_id"] or ""),
            provider,
            now,
            tc.tz,
            meta,
        )
        replacement_chain = list(chain or [])
        if final_provider != provider:
            reason_parts.append(f"temporal 替换: {treason}")
        provider = final_provider
    except Exception:  # noqa: BLE001 - temporal 推演异常兜底
        logger.warning("agent_tools: resolve_model 推演异常", exc_info=True)
    return _ok(
        group_id=meta["group_id"],
        provider=provider,
        temporal_matched_id=(matched or {}).get("id") if matched else None,
        replacement_chain=replacement_chain,
        reason="；".join(reason_parts) if reason_parts else "无",
    )


# ---- 分级审批：summary 生成 / 暂存 / 直接执行 ----


def _schedule_period_desc(schedule: dict) -> str:
    """把 temporal schedule 缩成人类可读的时间段描述（含跨午夜 / 星期 / 日期）。

    Args:
        schedule: temporal 规则的 ``schedule`` 字段。

    Returns:
        中文时间段描述。
    """
    if not isinstance(schedule, dict):
        return "时间未指定"
    stype = schedule.get("type", "daily")
    start = schedule.get("start") or ""
    end = schedule.get("end") or ""
    period = ""
    if stype == "daily" and start and end:
        period = f"每天 {start}-{end}" + ("（跨午夜）" if start > end else "")
    elif stype == "weekly":
        weekdays = schedule.get("weekdays") or []
        if start and end:
            period = f"每周[{','.join(str(w) for w in weekdays)}] {start}-{end}"
        else:
            period = f"每周[{','.join(str(w) for w in weekdays)}]"
    elif stype == "date":
        period = (
            f"指定日期 {schedule.get('date')} {start}-{end}"
            if start and end
            else (f"指定日期 {schedule.get('date')}")
        )
    elif stype == "always":
        period = "始终"
    else:
        period = "时间未指定"
    return period


def _model_keyword_desc(cond: dict) -> str:
    """把 ``model_keyword`` 条件描述为人类可读文本。"""
    keywords = [str(k) for k in (cond.get("keywords") or [])]
    mode = cond.get("mode", "any")
    if mode == "all":
        mode_text = "全部"
    elif mode == "min_n":
        mode_text = f"至少 {cond.get('min_n', 2)} 个"
    else:
        mode_text = "任意 1 个"
    return f"模型名含 [{', '.join(keywords)}]（{mode_text}）"


def _rule_then_desc(then: dict) -> str:
    """把规则 then 动作描述为人类可读文本。"""
    if not isinstance(then, dict):
        return "无动作"
    action = then.get("action", "")
    if action == "replace_model":
        return f"替换为 {then.get('provider_id') or '?'} @ {then.get('model') or '?'}"
    if action == "switch_group":
        return f"切换组 {then.get('group_id') or '?'}"
    if action == "switch_provider":
        return f"切换 Provider {then.get('provider_id') or '?'}"
    if action == "apply_lifecycle":
        return f"应用生命周期 {then.get('lifecycle_id') or '?'}"
    if action == "unlock":
        return "解锁会话"
    return f"触发动作 {action or '?'}"


def _summarize_op(op: dict) -> str:
    """把单个 op 生成一条人类可读变更描述（中文）。"""
    action = str(op.get("action", ""))
    data = op.get("data")
    if not isinstance(data, dict):
        data = {}

    # ---- 规则引擎（when/then）----
    if action == "create_rule":
        name = data.get("name") or "未命名规则"
        when = data.get("when") or {}
        n_cond = len((when.get("conditions") or []) if isinstance(when, dict) else [])
        kw_parts = []
        for c in (when.get("conditions") or []) if isinstance(when, dict) else []:
            if isinstance(c, dict) and c.get("type") == "model_keyword":
                kw_parts.append(_model_keyword_desc(c))
        when_text = "，".join(kw_parts) if kw_parts else f"{n_cond} 个条件"
        then_text = _rule_then_desc(data.get("then"))
        return f"新建条件规则「{name}」：当{when_text} → {then_text}，优先级 {data.get('priority', 0)}"
    if action == "update_rule":
        name = data.get("name") or op.get("rule_id") or "规则"
        then_text = _rule_then_desc(data.get("then"))
        return f"修改规则「{name}」：动作 {then_text}，优先级 {data.get('priority', 0)}"
    if action == "delete_rule":
        return f"删除条件规则「{op.get('rule_id') or '?'}」（不可恢复）"
    if action == "toggle_rule":
        new_enabled = bool(data.get("enabled"))
        verb = "启用" if new_enabled else "停用"
        return f"{verb}条件规则「{op.get('rule_id') or '?'}」"

    # ---- 模型组 ----
    if action == "create_model_group":
        return f"新建模型组「{data.get('name') or '?'}」（含 {len(data.get('providers') or [])} 个成员）"
    if action == "update_model_group":
        return f"修改模型组「{op.get('group_id') or '?'}」"
    if action == "delete_model_group":
        name = data.get("name") or op.get("group_id") or "?"
        n = len(data.get("providers") or [])
        return f"删除模型组「{name}」（含 {n} 个成员，不可恢复）"

    # ---- 时间调度规则 ----
    if action in ("create_schedule_rule", "create_model_override"):
        kind = data.get("kind", "model_override")
        if kind == "group_switch":
            return (
                f"新建时间规则「{data.get('name') or '?'}」：{_schedule_period_desc(data.get('schedule'))}，"
                f"组 {data.get('group_id') or '全局'} 切到组 {data.get('target_group') or '?'}，"
                f"优先级 {data.get('priority', 200)}"
            )
        return (
            f"新建时间规则「{data.get('name') or '?'}」：{_schedule_period_desc(data.get('schedule'))}，"
            f"把 {data.get('group_id') or '全局'} 内 {data.get('source_provider') or '?'} 替换为 "
            f"{data.get('target_provider') or '?'}，优先级 {data.get('priority', 200)}"
        )
    if action in ("update_schedule_rule", "update_model_override"):
        return f"修改时间规则「{op.get('rule_id') or '?'}」"
    if action == "delete_schedule_rule":
        return f"删除时间规则「{op.get('rule_id') or '?'}」（不可恢复）"

    # ---- 生命周期 ----
    if action == "create_lifecycle":
        return (
            f"新建生命周期「{data.get('name') or '?'}」：共 {len(data.get('stages') or [])} 个阶段，"
            f"最终组 {data.get('final_group') or '（无）'}"
        )
    if action == "update_lifecycle":
        return f"修改生命周期「{op.get('lifecycle_id') or '?'}」"
    if action == "delete_lifecycle":
        return f"删除生命周期「{op.get('lifecycle_id') or '?'}」（不可恢复）"

    return f"变更：{action}"


def _summarize_ops(ops) -> list[str]:
    """把一批 op 翻译为人读变更描述列表（一 op 一行，中文）。

    Args:
        ops: 操作列表（每个含 action / data 等字段）。

    Returns:
        中文描述列表。
    """
    out: list[str] = []
    for op in _as_list(ops):
        if not isinstance(op, dict):
            continue
        text = _summarize_op(op)
        if text:
            out.append(text)
    return out


def _stage_op(tc, op, summary=None) -> dict:
    """高危写：把单个 op 存入暂存区并返回 C2 格式 ``{"ok", "status":"staged", "pending_id", "summary", "approval_hint"}``。

    暂存即”未写 config，等待管理员批准“。**同轮合并语义（v1.0.3 修复）**：
    - 若当前 pending 存在且其 ``staging_batch`` 与 ``tc.staging_batch`` 相同（同一轮 Agent
      对话的连续高危写）：**追加** op 与 summary 而非覆盖，合并后仍为唯一一份 pending
      （stage 会生成新 pending_id；管理员用「无参」或「当前唯一」审批都能命中全部 ops），
      并**不写** ``stale`` 审计（同轮合并不是覆盖）；
    - 否则（跨批 / 首次）：覆盖旧 pending 并写 ``stale`` 审计（原逻辑）。
    来源 / 操作者沿用 tc 并写 ``stage`` 审计。``approval_hint`` 按来源区分批准方式：
    Web 助手（``source=web_agent``）提示点击页面审批卡按钮（WebUI 无指令输入框）；
    聊天 SubAgent 及其他来源提示执行 ``/scheduler approve|reject`` 指令。

    Args:
        tc: ``ToolContext``。
        op: 单个操作 dict（apply_staged 时按序执行）。
        summary: 可选的人性化摘要；缺省由 :func:`_summarize_ops` 生成。

    Returns:
        C2 格式 dict（含 ``approval_hint``）。
    """
    op_summary = summary if summary is not None else _summarize_ops([op])
    old = tc.pending.get()
    snapshot = tc.store.export_all()
    batch = getattr(tc, "staging_batch", None)
    is_same_batch = (
        batch is not None and old is not None and old.get("staging_batch") == batch
    )
    if is_same_batch:
        # 同批合并：追加 op 与 summary，仍写为唯一 pending。
        # 合并后 pending 唯一，管理员用「无参」或「当前唯一」审批都能命中全部 ops，
        # 因此 stage 生成新 pending_id 无碍（旧 id 失效后会提示「不匹配」引导无参批准）。
        merged_ops = (old.get("ops") or []) + [op]
        merged_summary = (old.get("summary") or []) + op_summary
        pending_id = tc.pending.stage(
            merged_ops,
            snapshot,
            summary=merged_summary,
            staging_batch=batch,
        )
        result_summary = merged_summary
        audit_detail = "同轮多变更合并暂存"
    else:
        # 跨批 / 首次：覆盖旧 pending 并写 stale 审计。
        pending_id = tc.pending.stage(
            [op], snapshot, summary=op_summary, staging_batch=batch
        )
        if old is not None:
            _audit(
                tc,
                "stale",
                old.get("pending_id"),
                None,
                pending_id,
                detail="新暂存覆盖旧暂存",
            )
        result_summary = op_summary
        audit_detail = "暂存待批准"
    _audit(tc, "stage", pending_id, None, [copy.deepcopy(op)], detail=audit_detail)
    approval_hint = _approval_hint(tc, pending_id)
    return _ok(
        status="staged",
        pending_id=pending_id,
        summary=result_summary,
        approval_hint=approval_hint,
    )


def _approval_hint(tc, pending_id: str) -> str:
    """按来源生成「如何批准」的提示语（供 LLM 原样转述给管理员）。

    - Web 助手（``source=web_agent``）：前端没有指令输入框，批准方式是点击页面
      审批卡上的「批准 / 拒绝」按钮；
    - 聊天 SubAgent 及其他来源：管理员在聊天中执行审批指令。

    Args:
        tc: ``ToolContext``（含 ``source``）。
        pending_id: 暂存区 id（如 ``p_xxxxxxxx``）。

    Returns:
        中文提示语。
    """
    if str(getattr(tc, "source", "") or "") == "web_agent":
        return (
            f"该变更已暂存（{pending_id}），不会立即生效。请在页面下方的"
            "「待批准更改」卡片中点击『批准』按钮生效，或点击『拒绝』按钮放弃；"
            "也可展开卡片查看原始数据核对。"
        )
    return (
        f"该变更已暂存（{pending_id}），不会立即生效。请管理员执行 "
        f"`/scheduler approve {pending_id}` 批准生效，或执行 "
        f"`/scheduler reject {pending_id}` 放弃（仅管理员）。"
    )


def _run_low_risk(tc, op) -> dict:
    """低风险写：直接执行单个 op（``_apply_op`` 已含写库 + 审计），返回通用结果。"""
    ok, msg = _apply_op(tc, op)
    if not ok:
        return _fail(msg)
    return _ok(message=msg)


# ---- 规则引擎（when/then 条件规则）查询 / 写工具 ----
#
# 查询工具（list/get_rule）直接返回；写工具（create/update/delete/toggle）一律走分级审批：
# agent_confirm=True 时暂存（staged），=False 时直接执行。规则写操作不经 preview→apply 老流程。


def _resolve_rule(tc, rule_id: str) -> dict | None:
    """按 id 定位规则引擎中的一条规则；未命中返回 None。"""
    rule_id = _normalize_target(rule_id)
    if not rule_id:
        return None
    for r in tc.rules_():
        if r.get("id") == rule_id:
            return copy.deepcopy(r)
    return None


def _similar_rule_ids(tc, rule_id: str) -> list[str]:
    """给出与 rule_id 相似的候选规则 id（id / name 子串命中），供查询提示。"""
    key = str(rule_id or "").strip().lower()
    if not key:
        return []
    out = []
    for r in tc.rules_():
        cand = str(r.get("id", "")).lower() + " " + str(r.get("name", "")).lower()
        if key in cand:
            out.append(r.get("id"))
    return out


def _validate_rule_spec(tc, rule) -> list[str]:
    """校验一条完整规则，返回错误列表（空=合法）。

    优先委托 Agent-R 的 ``rules.validate_rule``（依赖契约 C9），并在其上补充工具侧
    的 Provider 存在性校验（``validate_rule`` 本身只要求 provider_id 非空，不感知
    已配置 Provider 集合）。若 Agent-R 的 validate_rule 尚未交付（ImportError /
    AttributeError），则以本模块的兜底校验兜底。

    Args:
        tc: ``ToolContext``。
        rule: 待校验的完整规则 dict（经 normalize 补齐字段）。

    Returns:
        错误信息列表（空即通过）。
    """
    from .rules import validate_rule  # 依赖 Agent-R 的 validate_rule（C9）

    errors: list[str] = []
    try:
        errors = [str(e) for e in (validate_rule(rule) or [])]
    except (ImportError, AttributeError):
        # validate_rule 未就绪（Agent-R 并行开发中）→ 本模块基础兜底
        logger.info("agent_tools: rules.validate_rule 未就绪，使用兜底校验")
    except Exception as exc:  # noqa: BLE001 - 校验异常时回退兜底
        logger.warning(
            "agent_tools: rules.validate_rule 调用异常，回退兜底校验: %r", exc
        )

    if errors:
        return errors

    # ---- 补充校验（不依赖 validate_rule 的返回值，始终执行） ----

    then = rule.get("then") if isinstance(rule, dict) else None
    if isinstance(then, dict) and then.get("action") == "replace_model":
        pid = str(then.get("provider_id") or "")
        if not pid:
            errors.append("replace_model 动作缺少 provider_id")
        elif pid not in tc.provider_ids():
            errors.append(f"replace_model 的 provider_id「{pid}」不存在")
        if not str(then.get("model") or "").strip():
            errors.append("replace_model 动作缺少 model（模型名非空）")

    return errors


def _build_rule_write_op(tc, action, rule_id, spec) -> dict | None:
    """构造规则写操作 op（含校验）；非法时返回 None（错误已通过在 op 里标记）。

    Args:
        tc: ``ToolContext``。
        action: ``create_rule`` / ``update_rule`` / ``toggle_rule``。
        rule_id: update/toggle 时的目标 id（create 为空）。
        spec: 规则 spec。

    Returns:
        规范化 op dict；校验失败返回带 ``_validation_error`` 键的 dict。
    """
    if action == "create_rule":
        data = copy.deepcopy(spec)
    elif action == "update_rule":
        data = copy.deepcopy(spec)
    else:  # toggle_rule
        data = copy.deepcopy(spec)
    op = {"action": action, "data": data}
    if rule_id:
        op["rule_id"] = rule_id

    # 构造完整规则用于校验：create 用 normalize；update 合并现有后再 normalize；toggle 仅翻转 enabled。
    from .rules import normalize_rule  # 相对导入（结构约束）

    if action == "create_rule":
        candidate = normalize_rule(copy.deepcopy(data))
    elif action == "update_rule":
        existing = _resolve_rule(tc, rule_id)
        if existing is None:
            op["_validation_error"] = f"规则不存在：{rule_id}"
            return op
        merged = copy.deepcopy(existing)
        merged.update(copy.deepcopy(data))
        candidate = normalize_rule(merged)
    else:  # toggle
        existing = _resolve_rule(tc, rule_id)
        if existing is None:
            op["_validation_error"] = f"规则不存在：{rule_id}"
            return op
        data["enabled"] = not bool(existing.get("enabled", True))
        candidate = normalize_rule(copy.deepcopy(existing))
        candidate["enabled"] = data["enabled"]

    errors = _validate_rule_spec(tc, candidate)
    if errors:
        op["_validation_error"] = "；".join(errors)
    return op


def tool_get_rule(tc, rule_id="") -> dict:
    """按 id 查询单条条件规则细节；未命中时给出相似候选。"""
    rule_id = _normalize_target(rule_id)
    if not rule_id:
        return _fail("参数缺失：rule_id")
    rule = _resolve_rule(tc, rule_id)
    if rule is None:
        sims = _similar_rule_ids(tc, rule_id)
        return _fail(f"未找到条件规则「{rule_id}」", candidates=sims)
    return _ok(rule=copy.deepcopy(rule))


def tool_create_rule(tc, spec=None) -> dict:
    """创建条件规则（高危，暂存）。spec 结构同 rules.normalize（when/then/priority/scope/enabled）。"""
    spec = spec if isinstance(spec, dict) else {}
    if not spec:
        return _fail("参数缺失：请提供规则 spec")
    op = _build_rule_write_op(tc, "create_rule", "", spec)
    if op.get("_validation_error"):
        return _fail(op["_validation_error"])
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    return _run_rule_direct(tc, op)


def tool_update_rule(tc, rule_id="", spec=None) -> dict:
    """修改条件规则（高危，暂存）。rule_id 为目标 id；spec 为要更新的字段（结构同规则）。"""
    rule_id = _normalize_target(rule_id)
    spec = spec if isinstance(spec, dict) else {}
    if not rule_id:
        return _fail("参数缺失：rule_id")
    if not spec:
        return _fail("参数缺失：请提供要更新的规则字段 spec")
    op = _build_rule_write_op(tc, "update_rule", rule_id, spec)
    if op.get("_validation_error"):
        return _fail(op["_validation_error"])
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    return _run_rule_direct(tc, op)


def tool_delete_rule(tc, rule_id="") -> dict:
    """删除条件规则（高危，暂存）。"""
    rule_id = _normalize_target(rule_id)
    if not rule_id:
        return _fail("参数缺失：rule_id")
    if _resolve_rule(tc, rule_id) is None:
        return _fail(f"规则不存在：{rule_id}")
    op = {"action": "delete_rule", "rule_id": rule_id, "data": {}}
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    return _run_rule_direct(tc, op)


def tool_toggle_rule(tc, rule_id="") -> dict:
    """启停条件规则（高危，暂存）。翻转 enabled：读现有规则状态取反。"""
    rule_id = _normalize_target(rule_id)
    if not rule_id:
        return _fail("参数缺失：rule_id")
    existing = _resolve_rule(tc, rule_id)
    if existing is None:
        return _fail(f"规则不存在：{rule_id}")
    target = not bool(existing.get("enabled", True))
    op = {"action": "toggle_rule", "rule_id": rule_id, "data": {"enabled": target}}
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    return _run_rule_direct(tc, op)


def _run_rule_direct(tc, op) -> dict:
    """规则写操作直接执行（agent_confirm=False 时）：调 _apply_op 写库 + 审计。"""
    ok, msg = _apply_op(tc, op)
    if not ok:
        return _fail(msg)
    return _ok(message=msg, rule_id=op.get("rule_id", ""), action=op.get("action", ""))


# ---- 模型组写工具 ----


def tool_create_model_group(tc, spec=None) -> dict:
    """创建模型组：校验 provider 存在性后落库，返回新组。"""
    spec = spec if isinstance(spec, dict) else {}
    if not spec:
        return _fail("参数缺失：请提供模型组 spec（至少含 name / providers）")
    errors = _validate_group_provider_ids(tc, spec.get("providers"))
    if errors:
        return _fail("；".join(errors))
    try:
        group = tc.groups.create(copy.deepcopy(spec))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"创建模型组失败:{exc}")
    _audit(tc, "create_model_group", group.get("id"), None, group)
    return _ok(group=copy.deepcopy(group))


def tool_update_model_group(tc, group_id="", spec=None) -> dict:
    """更新模型组（高危，暂存）：先用 manager 读现状计算 before/after，校验后落库 / 暂存。

    agent_confirm=True 时把变更暂存等待批准；=False 时直接执行（等同旧行为）。
    """
    group_id = _normalize_target(group_id)
    spec = spec if isinstance(spec, dict) else {}
    if not group_id:
        return _fail("参数缺失：group_id")
    before = tc.groups.get(group_id)
    if before is None:
        return _fail(f"模型组不存在：{group_id}")
    errors = _validate_group_provider_ids(tc, spec.get("providers"))
    if errors:
        return _fail("；".join(errors))
    op = {
        "action": "update_model_group",
        "group_id": group_id,
        "data": copy.deepcopy(spec),
    }
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    try:
        after = tc.groups.update_group(group_id, copy.deepcopy(spec))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"更新模型组失败:{exc}")
    _audit(tc, "update_model_group", group_id, before, after)
    return _ok(group=copy.deepcopy(after))


def tool_delete_model_group(tc, group_id="") -> dict:
    """删除模型组（高危，暂存）。agent_confirm=True 时暂存等待批准；=False 时直接执行。"""
    group_id = _normalize_target(group_id)
    if not group_id:
        return _fail("参数缺失：group_id")
    before = tc.groups.get(group_id)
    if before is None:
        return _fail(f"模型组不存在：{group_id}")
    op = {
        "action": "delete_model_group",
        "group_id": group_id,
        "data": copy.deepcopy(before),
    }
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    try:
        ok = tc.groups.delete(group_id)
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"删除模型组失败:{exc}")
    if not ok:
        return _fail(f"模型组不存在：{group_id}")
    _audit(tc, "delete_model_group", group_id, before, None)
    return _ok(deleted=True, group_id=group_id)


# ---- temporal 规则写工具（内部装配） ----


def _prepare_temporal_write(tc, raw, existing=None):
    """校验 temporal 规则并返回 ``(ok, target_id, before, after_dict, errors)``。

    不落库，供 create / update / preview 复用同一套校验。

    Args:
        tc: ``ToolContext``。
        raw: 待写入的规则 dict。
        existing: 更新场景下的既有规则（create 为 None）。

    Returns:
        ``(ok, target_id, before, after_dict, errors)``。
    """
    raw = raw if isinstance(raw, dict) else {}
    ok, result = _temporal_validate(tc, raw)
    if not ok:
        return False, None, None, None, list(result.get("errors") or [])
    return True, None, None, copy.deepcopy(raw), []


def tool_create_schedule_rule(tc, spec=None) -> dict:
    """创建一条 temporal 调度规则（kind=model_override 或 group_switch）。"""
    spec = spec if isinstance(spec, dict) else {}
    if not spec:
        return _fail("参数缺失：请提供调度规则 spec")
    ok, _, _, after_dict, errors = _prepare_temporal_write(tc, spec)
    if not ok:
        return _fail("；".join(errors) if errors else "调度规则校验失败")
    try:
        rule = tc.temporal.create(copy.deepcopy(after_dict))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"创建调度规则失败:{exc}")
    tc.temporal.invalidate()
    _audit(tc, "create_schedule_rule", rule.get("id"), None, rule)
    return _ok(rule=copy.deepcopy(rule))


def tool_update_schedule_rule(tc, rule_id="", spec=None) -> dict:
    """更新一条 temporal 调度规则（高危，暂存）：先校验后落库 / 暂存。"""
    rule_id = _normalize_target(rule_id)
    spec = spec if isinstance(spec, dict) else {}
    if not rule_id:
        return _fail("参数缺失：rule_id")
    before = tc.temporal.get(rule_id)
    if before is None:
        return _fail(f"调度规则不存在：{rule_id}")
    merged = copy.deepcopy(before)
    merged.update(copy.deepcopy(spec))
    ok, _, _, after_dict, errors = _prepare_temporal_write(tc, merged)
    if not ok:
        return _fail("；".join(errors) if errors else "调度规则校验失败")
    op = {
        "action": "update_schedule_rule",
        "rule_id": rule_id,
        "data": copy.deepcopy(spec),
    }
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    try:
        after = tc.temporal.update_rule(rule_id, copy.deepcopy(spec))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"更新调度规则失败:{exc}")
    tc.temporal.invalidate()
    _audit(tc, "update_schedule_rule", rule_id, before, after)
    return _ok(rule=copy.deepcopy(after))


def tool_delete_schedule_rule(tc, rule_id="") -> dict:
    """删除一条 temporal 调度规则（高危，暂存）。agent_confirm=True 时暂存；=False 直接执行。"""
    rule_id = _normalize_target(rule_id)
    if not rule_id:
        return _fail("参数缺失：rule_id")
    before = tc.temporal.get(rule_id)
    if before is None:
        return _fail(f"调度规则不存在：{rule_id}")
    op = {
        "action": "delete_schedule_rule",
        "rule_id": rule_id,
        "data": copy.deepcopy(before),
    }
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    try:
        ok = tc.temporal.delete(rule_id)
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"删除调度规则失败:{exc}")
    if not ok:
        return _fail(f"调度规则不存在：{rule_id}")
    tc.temporal.invalidate()
    _audit(tc, "delete_schedule_rule", rule_id, before, None)
    return _ok(deleted=True, rule_id=rule_id)


def tool_enable_schedule_rule(tc, rule_id="") -> dict:
    """启用一条 temporal 调度规则。"""
    return _set_schedule_enabled(tc, rule_id, enabled=True)


def tool_disable_schedule_rule(tc, rule_id="") -> dict:
    """停用一条 temporal 调度规则。"""
    return _set_schedule_enabled(tc, rule_id, enabled=False)


def _set_schedule_enabled(tc, rule_id: str, enabled: bool) -> dict:
    """内部：设置规则启用/停用状态。"""
    rule_id = _normalize_target(rule_id)
    if not rule_id:
        return _fail("参数缺失：rule_id")
    before = tc.temporal.get(rule_id)
    if before is None:
        return _fail(f"调度规则不存在：{rule_id}")
    try:
        after = tc.temporal.toggle(rule_id, enabled)
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"切换规则状态失败:{exc}")
    tc.temporal.invalidate()
    verb = "enable_schedule_rule" if enabled else "disable_schedule_rule"
    _audit(tc, verb, rule_id, before, after)
    return _ok(rule=copy.deepcopy(after))


def tool_create_model_override(tc, spec=None) -> dict:
    """创建一条 model_override 调度规则（= create_schedule_rule(kind=model_override)）。"""
    spec = spec if isinstance(spec, dict) else {}
    spec["kind"] = "model_override"
    return tool_create_schedule_rule(tc, spec=spec)


def tool_update_model_override(tc, rule_id="", spec=None) -> dict:
    """更新一条 model_override 调度规则（= update_schedule_rule）。"""
    spec = spec if isinstance(spec, dict) else {}
    spec["kind"] = "model_override"
    return tool_update_schedule_rule(tc, rule_id=rule_id, spec=spec)


def tool_delete_model_override(tc, rule_id="") -> dict:
    """删除一条 model_override 调度规则（= delete_schedule_rule）。"""
    return tool_delete_schedule_rule(tc, rule_id=rule_id)


# ---- 生命周期工具（v0.1.6） ----
#
# 生命周期（含多阶段降级 / 周期校准）的查询与读写工具。对未注入 lifecycles 引擎
# （``tc.lifecycles is None``）显式返回中文错误，保证工具不抛异常。


def tool_list_lifecycles(tc) -> dict:
    """列出全部生命周期（含多阶段 / 周期校准字段概要）。"""
    if tc.lifecycles is None:
        return _fail("生命周期引擎未注入")
    lifecycles = [copy.deepcopy(i) for i in tc.lifecycles_()]
    return _ok(lifecycles=lifecycles)


def tool_get_lifecycle(tc, name="") -> dict:
    """按 id 或名称查询生命周期；不命中时列出相似 id 候选。"""
    if tc.lifecycles is None:
        return _fail("生命周期引擎未注入")
    key = str(name or "").strip()
    if not key:
        return _fail("参数缺失：请提供生命周期 id 或名称")
    found = None
    for item in tc.lifecycles_():
        if item.get("id") == key or item.get("name") == key:
            found = copy.deepcopy(item)
            break
    if found is None:
        sims = _similar_lifecycle_ids(tc, key)
        return _fail(f"未找到生命周期「{key}」", candidates=sims)
    return _ok(lifecycle=found)


def tool_create_lifecycle(tc, spec=None) -> dict:
    """创建生命周期：结构校验（多阶段 / 周期校准）通过后落库，返回新生命周期。"""
    if tc.lifecycles is None:
        return _fail("生命周期引擎未注入")
    spec = spec if isinstance(spec, dict) else {}
    errors = _validate_lifecycle_spec(tc, spec)
    if errors:
        return _fail("；".join(errors))
    payload = copy.deepcopy(spec)
    payload.setdefault("name", "未命名生命周期")
    payload.setdefault("enabled", True)
    try:
        item = tc.lifecycles.create(payload)
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"创建生命周期失败:{exc}")
    _audit(tc, "create_lifecycle", item.get("id"), None, item)
    return _ok(lifecycle=copy.deepcopy(item))


def tool_update_lifecycle(tc, lifecycle_id="", spec=None) -> dict:
    """合并更新生命周期（高危，暂存）：校验后落库 / 暂存；不存在返回 error。"""
    if tc.lifecycles is None:
        return _fail("生命周期引擎未注入")
    lifecycle_id = _normalize_target(lifecycle_id)
    if not lifecycle_id:
        return _fail("参数缺失：lifecycle_id")
    before = tc.lifecycles.get(lifecycle_id)
    if before is None:
        return _fail(f"生命周期不存在：{lifecycle_id}")
    spec = spec if isinstance(spec, dict) else {}
    merged = copy.deepcopy(before)
    merged.update(copy.deepcopy(spec))
    errors = _validate_lifecycle_spec(tc, merged)
    if errors:
        return _fail("；".join(errors))
    op = {
        "action": "update_lifecycle",
        "lifecycle_id": lifecycle_id,
        "data": copy.deepcopy(spec),
    }
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    try:
        after = tc.lifecycles.update(lifecycle_id, copy.deepcopy(spec))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"更新生命周期失败:{exc}")
    _audit(tc, "update_lifecycle", lifecycle_id, before, after)
    return _ok(lifecycle=copy.deepcopy(after))


def tool_delete_lifecycle(tc, lifecycle_id="") -> dict:
    """删除生命周期（高危，暂存）。agent_confirm=True 时暂存；=False 直接执行。"""
    if tc.lifecycles is None:
        return _fail("生命周期引擎未注入")
    lifecycle_id = _normalize_target(lifecycle_id)
    if not lifecycle_id:
        return _fail("参数缺失：lifecycle_id")
    before = tc.lifecycles.get(lifecycle_id)
    if before is None:
        return _fail(f"生命周期不存在：{lifecycle_id}")
    op = {
        "action": "delete_lifecycle",
        "lifecycle_id": lifecycle_id,
        "data": copy.deepcopy(before),
    }
    if (tc.settings or {}).get("agent_confirm", True):
        return _stage_op(tc, op)
    try:
        ok = tc.lifecycles.delete(lifecycle_id)
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"删除生命周期失败:{exc}")
    if not ok:
        return _fail(f"生命周期不存在：{lifecycle_id}")
    _audit(tc, "delete_lifecycle", lifecycle_id, before, None)
    return _ok(deleted=True, lifecycle_id=lifecycle_id)


def tool_set_default_lifecycle(tc, lifecycle_id="") -> dict:
    """设置全局默认生命周期：写入 settings.default_lifecycle（空串=清除）。

    description 语义：这是「全局启用某生命周期 / 降级预设」的方式，引擎在会话没有
    显式绑定生命周期时回退到该默认生命周期；传空串表示清除默认（不启用）。
    """
    lifecycle_id = _normalize_target(lifecycle_id)
    if lifecycle_id:
        if tc.lifecycles is None:
            return _fail("生命周期引擎未注入")
        if tc.lifecycles.get(lifecycle_id) is None:
            return _fail(f"生命周期不存在：{lifecycle_id}")
    before = copy.deepcopy(tc.settings or {})
    settings = dict(tc.settings or {})
    settings["default_lifecycle"] = lifecycle_id
    try:
        tc.store.update("settings", settings)
    except Exception as exc:  # noqa: BLE001 - 保存异常兜底
        return _fail(f"设置默认生命周期失败:{exc}")
    tc.settings = copy.deepcopy(settings)
    _audit(tc, "set_default_lifecycle", lifecycle_id, before, settings)
    return _ok(default_lifecycle=lifecycle_id)


# ---- 校验 / 重载 ----


def tool_validate_configuration(tc) -> dict:
    """全量校验当前配置（模型组 / 规则 / temporal），返回错误、警告与冲突。"""
    errors: list[str] = []
    warnings: list[str] = []
    # 校验每个 temporal 规则（沿用 manager validate）
    for rule in tc.temporal_():
        ok, result = _temporal_validate(tc, rule)
        if not ok:
            errors.extend(
                f"规则 {rule.get('id')}: {e}" for e in (result.get("errors") or [])
            )
        warnings.extend(result.get("warnings") or [])
    # 校验模型组 provider 存在性
    for g in tc.groups_():
        errs = _validate_group_provider_ids(
            tc, [e.get("provider_id") for e in (g.get("providers") or [])]
        )
        for e in errs:
            errors.append(f"组 {g.get('id')}: {e}")
    # 校验生命周期结构（多阶段 / 周期校准字段）
    for item in tc.lifecycles_():
        errs = _validate_lifecycle_spec(tc, item)
        for e in errs:
            errors.append(f"生命周期 {item.get('id')}: {e}")
    conflicts: list = []
    try:
        if hasattr(tc.temporal, "find_conflicts"):
            conflicts = copy.deepcopy(tc.temporal.find_conflicts(tc.temporal_()))
    except Exception:  # noqa: BLE001 - 冲突检测兜底
        logger.warning("agent_tools: find_conflicts 异常", exc_info=True)
    return _ok(
        ok=not errors,
        errors=list(dict.fromkeys(errors)),
        warnings=list(dict.fromkeys(warnings)),
        conflicts=conflicts,
    )


def tool_reload_scheduler(tc) -> dict:
    """使 temporal 缓存失效并返回调度状态（配置变更后调用以立即生效）。"""
    try:
        tc.temporal.invalidate()
    except Exception:  # noqa: BLE001 - 失效兜底
        logger.warning("agent_tools: invalidate 异常", exc_info=True)
    status = tool_get_scheduler_status(tc)
    return _ok(status=status, reloaded=True)


# ---- 预览 / 应用 / 回滚 ----


def _dry_run_op(tc, op):
    """对单个 op 做与对应工具相同的校验（不写库），产出预览项。

    预览项：``{"action", "target", "before", "after", "warnings"}``。

    校验失败时返回 ``{"error": 原因}``，供 preview 汇总。

    Args:
        tc: ``ToolContext``。
        op: 操作 dict，含 ``action`` 与 ``data``（/ ``rule_id`` / ``group_id``）。

    Returns:
        预览项 dict（含 error 键表示该校验失败）。
    """
    action = str(op.get("action", ""))
    data = op.get("data")
    if not isinstance(data, dict):
        data = {}
    item = {
        "action": action,
        "target": "",
        "before": None,
        "after": None,
        "warnings": [],
    }

    if action in ("create_schedule_rule", "create_model_override"):
        ok, _, _, after_dict, errors = _prepare_temporal_write(tc, data)
        if not ok:
            item["error"] = "；".join(errors) if errors else "调度规则校验失败"
            return item
        item["after"] = after_dict
        return item

    if action in ("update_schedule_rule", "update_model_override"):
        rule_id = _normalize_target(op.get("rule_id"))
        item["target"] = rule_id
        before = tc.temporal.get(rule_id)
        if before is None:
            item["error"] = f"调度规则不存在：{rule_id}"
            return item
        merged = copy.deepcopy(before)
        merged.update(copy.deepcopy(data))
        ok, _, _, after_dict, errors = _prepare_temporal_write(tc, merged)
        if not ok:
            item["error"] = "；".join(errors) if errors else "调度规则校验失败"
            return item
        item["before"] = before
        item["after"] = after_dict
        return item

    if action == "delete_schedule_rule":
        rule_id = _normalize_target(op.get("rule_id"))
        item["target"] = rule_id
        before = tc.temporal.get(rule_id)
        if before is None:
            item["error"] = f"调度规则不存在：{rule_id}"
            return item
        item["before"] = before
        item["after"] = None
        return item

    if action == "create_model_group":
        errs = _validate_group_provider_ids(tc, data.get("providers"))
        if errs:
            item["error"] = "；".join(errs)
            return item
        item["after"] = normalize_group(copy.deepcopy(data))
        return item

    if action == "update_model_group":
        group_id = _normalize_target(op.get("group_id"))
        item["target"] = group_id
        before = tc.groups.get(group_id)
        if before is None:
            item["error"] = f"模型组不存在：{group_id}"
            return item
        errs = _validate_group_provider_ids(tc, data.get("providers"))
        if errs:
            item["error"] = "；".join(errs)
            return item
        merged = copy.deepcopy(before)
        merged.update(copy.deepcopy(data))
        merged["id"] = group_id
        item["before"] = before
        item["after"] = normalize_group(merged)
        return item

    if action == "delete_model_group":
        group_id = _normalize_target(op.get("group_id"))
        item["target"] = group_id
        before = tc.groups.get(group_id)
        if before is None:
            item["error"] = f"模型组不存在：{group_id}"
            return item
        item["before"] = before
        item["after"] = None
        return item

    # ---- v0.1.6：生命周期操作（预览管线支持，Agent 删除生命周期不再卡「未知操作类型」） ----

    if action == "create_lifecycle":
        errs = _validate_lifecycle_spec(tc, data)
        if errs:
            item["error"] = "；".join(errs)
            return item
        item["after"] = copy.deepcopy(data)
        return item

    if action == "update_lifecycle":
        lifecycle_id = _normalize_target(op.get("lifecycle_id"))
        item["target"] = lifecycle_id
        if tc.lifecycles is None:
            item["error"] = "生命周期引擎未注入"
            return item
        before = tc.lifecycles.get(lifecycle_id)
        if before is None:
            item["error"] = f"生命周期不存在：{lifecycle_id}"
            return item
        merged = copy.deepcopy(before)
        merged.update(copy.deepcopy(data))
        errs = _validate_lifecycle_spec(tc, merged)
        if errs:
            item["error"] = "；".join(errs)
            return item
        item["before"] = before
        item["after"] = merged
        return item

    if action == "delete_lifecycle":
        lifecycle_id = _normalize_target(op.get("lifecycle_id"))
        item["target"] = lifecycle_id
        if tc.lifecycles is None:
            item["error"] = "生命周期引擎未注入"
            return item
        before = tc.lifecycles.get(lifecycle_id)
        if before is None:
            item["error"] = f"生命周期不存在：{lifecycle_id}"
            return item
        item["before"] = before
        item["after"] = None
        return item

    item["error"] = f"未知操作类型：{action}"
    return item


def _apply_op(tc, op):
    """真实执行单个 op（写库）并写审计；返回 ``(ok, message)``。

    Args:
        tc: ``ToolContext``。
        op: 操作 dict。

    Returns:
        ``(ok, message)``；message 供汇总展示。
    """
    action = str(op.get("action", ""))
    data = op.get("data")
    if not isinstance(data, dict):
        data = {}
    rule_id = _normalize_target(op.get("rule_id"))
    group_id = _normalize_target(op.get("group_id"))

    if action in ("create_schedule_rule", "create_model_override"):
        spec = copy.deepcopy(data)
        spec["kind"] = (
            "model_override"
            if action == "create_model_override"
            else spec.get("kind", "model_override")
        )
        rule = tc.temporal.create(copy.deepcopy(spec))
        tc.temporal.invalidate()
        _audit(tc, action, rule.get("id"), None, rule)
        return True, f"创建规则 {rule.get('id')}"

    if action in ("update_schedule_rule", "update_model_override"):
        if not rule_id:
            return False, "更新调度规则缺少 rule_id"
        before = tc.temporal.get(rule_id)
        after = tc.temporal.update_rule(rule_id, copy.deepcopy(data))
        tc.temporal.invalidate()
        _audit(tc, action, rule_id, before, after)
        return True, f"更新规则 {rule_id}"

    if action == "delete_schedule_rule":
        if not rule_id:
            return False, "删除调度规则缺少 rule_id"
        before = tc.temporal.get(rule_id)
        ok = tc.temporal.delete(rule_id)
        tc.temporal.invalidate()
        _audit(tc, action, rule_id, before, None)
        return (
            (True, f"删除规则 {rule_id}") if ok else (False, f"删除规则 {rule_id} 失败")
        )

    if action == "create_model_group":
        group = tc.groups.create(copy.deepcopy(data))
        _audit(tc, action, group.get("id"), None, group)
        return True, f"创建模型组 {group.get('id')}"

    if action == "update_model_group":
        if not group_id:
            return False, "更新模型组缺少 group_id"
        before = tc.groups.get(group_id)
        after = tc.groups.update_group(group_id, copy.deepcopy(data))
        _audit(tc, action, group_id, before, after)
        return True, f"更新模型组 {group_id}"

    if action == "delete_model_group":
        if not group_id:
            return False, "删除模型组缺少 group_id"
        before = tc.groups.get(group_id)
        ok = tc.groups.delete(group_id)
        _audit(tc, action, group_id, before, None)
        return (
            (True, f"删除模型组 {group_id}")
            if ok
            else (False, f"删除模型组 {group_id} 失败")
        )

    # ---- v1.0.3：规则引擎（when/then）操作 ----

    if action == "create_rule":
        rule = tc.rules.create_rule(copy.deepcopy(data))
        _audit(tc, action, rule.get("id"), None, rule)
        return True, f"创建条件规则 {rule.get('id')}"

    if action == "update_rule":
        if not rule_id:
            return False, "更新条件规则缺少 rule_id"
        before = _resolve_rule(tc, rule_id)
        after = tc.rules.update_rule(rule_id, copy.deepcopy(data))
        if after is None:
            return False, f"更新条件规则 {rule_id} 失败（不存在）"
        _audit(tc, action, rule_id, before, after)
        return True, f"更新条件规则 {rule_id}"

    if action == "delete_rule":
        if not rule_id:
            return False, "删除条件规则缺少 rule_id"
        before = _resolve_rule(tc, rule_id)
        ok = tc.rules.delete(rule_id)
        _audit(tc, action, rule_id, before, None)
        return (
            (True, f"删除条件规则 {rule_id}")
            if ok
            else (False, f"删除条件规则 {rule_id} 失败")
        )

    if action == "toggle_rule":
        if not rule_id:
            return False, "切换条件规则缺少 rule_id"
        before = _resolve_rule(tc, rule_id)
        if before is None:
            return False, f"切换条件规则 {rule_id} 失败（不存在）"
        new_enabled = bool(data.get("enabled", not bool(before.get("enabled", True))))
        after = tc.rules.update_rule(rule_id, {"enabled": new_enabled})
        _audit(tc, action, rule_id, before, after)
        verb = "启用" if new_enabled else "停用"
        return True, f"{verb}条件规则 {rule_id}"

    # ---- v0.1.6：生命周期操作 ----

    if tc.lifecycles is None:
        return False, "生命周期引擎未注入"

    if action == "create_lifecycle":
        lc = tc.lifecycles.create(copy.deepcopy(data))
        _audit(tc, action, lc.get("id"), None, lc)
        return True, f"创建生命周期 {lc.get('id')}"

    if action == "update_lifecycle":
        lifecycle_id = _normalize_target(op.get("lifecycle_id"))
        if not lifecycle_id:
            return False, "更新生命周期缺少 lifecycle_id"
        before = tc.lifecycles.get(lifecycle_id)
        after = tc.lifecycles.update(lifecycle_id, copy.deepcopy(data))
        _audit(tc, action, lifecycle_id, before, after)
        return True, f"更新生命周期 {lifecycle_id}"

    if action == "delete_lifecycle":
        lifecycle_id = _normalize_target(op.get("lifecycle_id"))
        if not lifecycle_id:
            return False, "删除生命周期缺少 lifecycle_id"
        before = tc.lifecycles.get(lifecycle_id)
        ok = tc.lifecycles.delete(lifecycle_id)
        _audit(tc, action, lifecycle_id, before, None)
        return (
            (True, f"删除生命周期 {lifecycle_id}")
            if ok
            else (False, f"删除生命周期 {lifecycle_id} 失败")
        )

    return False, f"未知操作类型：{action}"


def tool_preview_configuration_change(tc, ops=None) -> dict:
    """预览一批配置更改：逐个校验（不写库），全部通过才 stage。

    Args:
        tc: ``ToolContext``。
        ops: 操作列表（每个含 action / data / rule_id / group_id）。

    Returns:
        全部通过 → ``{"ok", "preview": [...], "pending_id", "require_apply": True}``；
        任一校验失败 → ``{"ok", "errors": [...]}``（不暂存）。
    """
    ops = [o for o in _as_list(ops) if isinstance(o, dict)]
    if not ops:
        return _fail("参数缺失：ops 为空，请先规划要执行的配置更改")
    preview: list = []
    errors: list = []
    for op in ops:
        item = _dry_run_op(tc, op)
        if item.get("error"):
            errors.append(f"操作 {item['action']}: {item['error']}")
        else:
            preview.append(item)
    if errors:
        return _fail("；".join(errors), preview=preview, errors=errors)
    snapshot = tc.store.export_all()
    # preview 一并随 pending 暂存，供 Web 助手在「待应用更改」预览卡渲染 before/after；
    # apply 仍只消费 pending.ops（真实操作 dict），二者互不干扰。
    pending_id = tc.pending.stage(ops, snapshot, preview=preview)
    _audit(
        tc, "preview_configuration_change", pending_id, None, preview, result="preview"
    )
    return _ok(
        preview=preview,
        pending_id=pending_id,
        require_apply=True,
        message=f"预览通过，共 {len(preview)} 项更改，请调用 apply_configuration_change 应用",
    )


def tool_apply_configuration_change(tc) -> dict:
    """应用待执行的配置更改：按序真实写库并逐 op 写审计，完成后落盘回滚快照。

    无待应用更改时返回错误。
    """
    pending = tc.pending.get()
    if not pending:
        return _fail("无待应用的更改：请先调用 preview_configuration_change")
    ops = pending.get("ops") or []
    try:
        for op in ops:
            ok, msg = _apply_op(tc, op)
            if not ok:
                return _fail(f"应用中途失败（已应用前面步骤）：{msg}")
    except Exception as exc:  # noqa: BLE001 - 兜底
        return _fail(f"应用异常：{exc}")
    tc.pending.apply_snapshot()
    tc.pending.clear()
    return _ok(
        applied=len([o for o in ops if o.get("action") in _WRITE_ACTIONS]),
        message=f"已应用 {len(ops)} 项更改，可随时调用 rollback_configuration_change 回滚",
    )


def tool_rollback_configuration_change(tc) -> dict:
    """用最近一次 apply 前的配置快照恢复（``store.import_all``），并写 rollback 审计。"""
    snapshot = tc.pending.last_snapshot()
    if not snapshot:
        return _fail("无可用回滚快照：尚未执行过 apply_configuration_change")
    try:
        restored = tc.store.import_all(copy.deepcopy(snapshot))
    except Exception as exc:  # noqa: BLE001 - 恢复失败兜底
        return _fail(f"回滚失败：{exc}")
    try:
        tc.temporal.invalidate()
    except Exception:  # noqa: BLE001 - 失效兜底
        pass
    _audit(
        tc, "rollback_configuration_change", "config", restored, None, result="rollback"
    )
    tc.pending.clear()
    tc.pending.mark_rolled_back()
    return _ok(
        rolled_back=True,
        restored_group_count=len(restored.get("groups") or []),
        restored_rule_count=len(restored.get("rules") or []),
        message="已回滚到应用前的配置快照",
    )


# ---- v1.0.3：分级审批公开入口（契约 C8） ----
#
# main.py 的 approve/reject/pending 指令与 web/api.py 的 agent/approve|reject|pending
# 端点只调用这三个函数，不在外部重复实现 ops 应用逻辑。函数内部完成 pending 存在性、
# id 匹配（调用方传入 pending_id 时）、遍历 ops 应用 / 丢弃、写审计。


def apply_staged(tc, pending_id="") -> dict:
    """应用暂存区中的变更：校验 pending 存在（id 匹配或当前唯一）→ 按序真实写库 → 记审计。

    Args:
        tc: ``ToolContext``。
        pending_id: 可选的期望暂存 id；传空表示批准当前唯一暂存。

    Returns:
        ``{"ok", "applied": N, "summary": [...]}``；无暂存或 id 不匹配返回错误。
    """
    pending = tc.pending.get()
    if not pending:
        return _fail("无待批准的暂存更改")
    target_id = pending.get("pending_id")
    if pending_id:
        pid = _normalize_target(str(pending_id))
        if pid and pid != target_id:
            return _fail(
                f"暂存 id 不匹配：{pid}（当前暂存为 {target_id}，可能已被同轮新变更更新，"
                "请重新批准或使用无参批准）"
            )
    ops = pending.get("ops") or []
    applied = 0
    try:
        for op in ops:
            ok, msg = _apply_op(tc, op)
            if not ok:
                return _fail(f"应用暂存中途失败（已应用前面 {applied} 项）：{msg}")
            applied += 1
    except Exception as exc:  # noqa: BLE001 - 应用异常兜底
        return _fail(f"应用暂存异常：{exc}")
    tc.pending.apply_snapshot()
    tc.pending.clear()
    summary = pending.get("summary") or []
    _audit(
        tc,
        "approve",
        target_id,
        None,
        {"applied": applied, "summary": summary},
        detail="管理员批准暂存变更",
    )
    return _ok(applied=applied, summary=summary)


def reject_staged(tc, pending_id="") -> dict:
    """丢弃暂存区中的变更（不写库），并记审计。

    Args:
        tc: ``ToolContext``。
        pending_id: 可选的期望暂存 id；传空表示拒绝当前唯一暂存。

    Returns:
        ``{"ok", "discarded": bool}``；无暂存或 id 不匹配返回错误。
    """
    pending = tc.pending.get()
    if not pending:
        return _fail("无待放弃的暂存更改")
    target_id = pending.get("pending_id")
    if pending_id:
        pid = _normalize_target(str(pending_id))
        if pid and pid != target_id:
            return _fail(
                f"暂存 id 不匹配：{pid}（当前暂存为 {target_id}，可能已被同轮新变更更新，"
                "请重新批准或使用无参批准）"
            )
    tc.pending.clear()
    _audit(
        tc,
        "reject",
        target_id,
        None,
        None,
        detail="管理员放弃暂存变更",
    )
    return _ok(discarded=True)


def pending_view(tc) -> dict | None:
    """返回挂起的暂存条目（含 pending_id / ops / snapshot / preview / summary / staged_at），无则 None。

    Args:
        tc: ``ToolContext``。

    Returns:
        暂存条目 dict；无暂存返回 None。
    """
    pending = tc.pending.get()
    if not pending:
        return None
    return {
        "pending_id": pending.get("pending_id"),
        "ops": pending.get("ops"),
        "snapshot": pending.get("snapshot"),
        "preview": pending.get("preview"),
        "summary": pending.get("summary") or [],
        "staged_at": pending.get("staged_at"),
        "staging_batch": pending.get("staging_batch"),
    }
