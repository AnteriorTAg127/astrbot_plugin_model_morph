"""agent_tools —— Agent 结构化工具（模块 T4，纯逻辑，不依赖 astrbot）。

为「配置 Agent」（聊天 SubAgent / Web 助手）提供读写模型组、时间调度规则
（temporal）与配置校验 / 预览 / 应用 / 回滚的结构化工具函数。每个工具函数都是
``(tc: ToolContext, **kwargs) -> dict`` 的同步函数，**绝不抛异常**：任何失败统一
返回 ``{"ok": False, "error": "中文原因"}``，成功返回 ``{"ok": True, ...}``。

设计要点：
- ``ToolContext`` 聚合依赖（store / groups / rules / temporal / audit / provider_infos /
  tz / settings / data_dir），并把来源（``source``）与操作者（``operator``）随审计写入。
- 遵循「先查询再修改」：create/update 前先用 manager 读现状计算 before/after，再落库。
- 高危判定（``settings.agent_confirm = True`` 时）：删除模型组 / 删除调度规则 /
  单批 ≥3 条写操作 → 要求先 ``preview_configuration_change`` 预览再 ``apply``。
  预览/应用/回滚流程本身不受此限。
- 预览 → 应用 → 回滚：preview 校验每个 op（不写库）→ 通过才把完整配置快照
  （``store.export_all()``）暂存 pending；apply 按序真实执行并逐 op 写审计；
  rollback 用 last_snapshot 恢复（``store.import_all``）。
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

# 时间调度（temporal）规则 + 模型组 + 生命周期的工具动作集合。
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
)

# 高危动作：删除类操作在 agent_confirm=True 时须先预览。
_HIGH_RISK_ACTIONS = (
    "delete_model_group",
    "delete_schedule_rule",
    "delete_lifecycle",
)


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


def _pending_require_preview() -> dict:
    """返回高危操作要求先预览的固定结果。"""
    return _fail(
        "高危操作：请先调用 preview_configuration_change 生成预览，"
        "再调用 apply_configuration_change 应用",
        require_preview=True,
    )


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

    def stage(self, ops, snapshot, preview=None) -> str:
        """暂存待应用更改：写入 ``{pending_id, ops, snapshot, preview}``。

        Args:
            ops: 待执行的操作列表（apply 时按序真实执行）。
            snapshot: 完整配置快照（``store.export_all()`` 的结果）。
            preview: 可选的预览项列表（``[{action, target, before, after, warnings}]``），
                Web 助手用它渲染「待应用更改」预览卡；apply 仍只消费 ``ops``。

        Returns:
            ``pending_id``（形如 ``p_xxxxxxxx``）。
        """
        pending_id = "p_" + uuid.uuid4().hex[:8]
        payload: dict = {
            "pending_id": pending_id,
            "ops": copy.deepcopy(ops),
            "snapshot": copy.deepcopy(snapshot),
        }
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
    """更新模型组：先用 manager 读现状计算 before/after，再落库。"""
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
    try:
        after = tc.groups.update_group(group_id, copy.deepcopy(spec))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"更新模型组失败:{exc}")
    _audit(tc, "update_model_group", group_id, before, after)
    return _ok(group=copy.deepcopy(after))


def tool_delete_model_group(tc, group_id="") -> dict:
    """删除模型组（高危）。agent_confirm=True 时须先 preview 再 apply。"""
    group_id = _normalize_target(group_id)
    if not group_id:
        return _fail("参数缺失：group_id")
    if (tc.settings or {}).get("agent_confirm", True):
        return _pending_require_preview()
    before = tc.groups.get(group_id)
    if before is None:
        return _fail(f"模型组不存在：{group_id}")
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
    """更新一条 temporal 调度规则：先校验后落库。"""
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
    try:
        after = tc.temporal.update_rule(rule_id, copy.deepcopy(after_dict))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"更新调度规则失败:{exc}")
    tc.temporal.invalidate()
    _audit(tc, "update_schedule_rule", rule_id, before, after)
    return _ok(rule=copy.deepcopy(after))


def tool_delete_schedule_rule(tc, rule_id="") -> dict:
    """删除一条 temporal 调度规则（高危）。agent_confirm=True 时须先预览。"""
    rule_id = _normalize_target(rule_id)
    if not rule_id:
        return _fail("参数缺失：rule_id")
    if (tc.settings or {}).get("agent_confirm", True):
        return _pending_require_preview()
    before = tc.temporal.get(rule_id)
    if before is None:
        return _fail(f"调度规则不存在：{rule_id}")
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
    """合并更新生命周期（校验后落库）；不存在返回 error。"""
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
    try:
        after = tc.lifecycles.update(lifecycle_id, copy.deepcopy(spec))
    except Exception as exc:  # noqa: BLE001 - 落库异常兜底
        return _fail(f"更新生命周期失败:{exc}")
    _audit(tc, "update_lifecycle", lifecycle_id, before, after)
    return _ok(lifecycle=copy.deepcopy(after))


def tool_delete_lifecycle(tc, lifecycle_id="") -> dict:
    """删除生命周期（高危）。agent_confirm=True 时要求先 preview 再 apply。"""
    if tc.lifecycles is None:
        return _fail("生命周期引擎未注入")
    lifecycle_id = _normalize_target(lifecycle_id)
    if not lifecycle_id:
        return _fail("参数缺失：lifecycle_id")
    if (tc.settings or {}).get("agent_confirm", True):
        return _pending_require_preview()
    before = tc.lifecycles.get(lifecycle_id)
    if before is None:
        return _fail(f"生命周期不存在：{lifecycle_id}")
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
