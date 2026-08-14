"""groups —— 模型组管理（模块 C，纯逻辑，不依赖 astrbot）。

负责模型组（Model Group）的增删改查、字段规范化，以及组内 Provider 的
选择策略（priority / round_robin / weighted / random / fallback）。

设计要点：
- 组内选择是「纯函数式」：选中游标（``rr`` / ``uses`` / ``cooldown_until``）
  存放在 ``SessionState.group_cursor[group_id]``，由调度引擎（engine.py）传入
  状态对象，本模块不直接接触会话存储。
- ``select_provider`` 只依赖标准库（random / time），离线可测；对 ``SessionState``
  仅作类型注解（运行时不强依赖，见 ``TYPE_CHECKING``）。
"""

from __future__ import annotations

import copy
import logging
import random
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型注解使用，避免模块级强依赖 state.py，保持离线独立可测
    pass

logger = logging.getLogger("astrbot_plugin_model_morph")

# 合法调度策略元组。
GROUP_STRATEGIES = ("priority", "round_robin", "weighted", "random", "fallback")

# 组字段默认值（normalize_group 补齐用）。
_DEFAULT_GROUP_FIELDS: dict[str, Any] = {
    "name": "",
    "desc": "",
    "enabled": True,
    "strategy": "priority",
    "allow_auto_fallback": False,
    "providers": [],
    "fallbacks": [],
}

# 组内成员条目的默认值。
_PROVIDER_ENTRY_DEFAULTS: dict[str, Any] = {
    "provider_id": "",
    "model_override": "",
    "priority": 0,
    "weight": 1,
    "max_uses": 0,  # 0 = 不限
    "cooldown_seconds": 0,  # 0 = 无冷却
    "enabled": True,
    "note": "",
}


def _fresh_provider_entry(raw: Any) -> dict | None:
    """规范化单个组内成员条目。

    Args:
        raw: 原始条目（dict 或可被 dict() 转换的对象）。

    Returns:
        补齐默认字段后的条目副本；条目非法（非 dict / ``provider_id`` 为空）时返回 ``None``。
    """
    if not isinstance(raw, dict):
        logger.warning("groups: 组内条目非对象，剔除: %r", raw)
        return None
    entry = copy.deepcopy(_PROVIDER_ENTRY_DEFAULTS)
    for key in _PROVIDER_ENTRY_DEFAULTS:
        if key in raw:
            entry[key] = copy.deepcopy(raw[key])
    if not isinstance(entry["provider_id"], str) or not entry["provider_id"].strip():
        logger.warning("groups: 组内条目 provider_id 为空，剔除: %r", raw)
        return None
    # 数值字段容错：无法转 int 的视为默认。
    for int_key in ("priority", "max_uses", "cooldown_seconds"):
        try:
            entry[int_key] = int(entry[int_key])
        except (TypeError, ValueError):
            entry[int_key] = _PROVIDER_ENTRY_DEFAULTS[int_key]
    try:
        entry["weight"] = int(entry["weight"])
    except (TypeError, ValueError):
        entry["weight"] = 1
    entry["enabled"] = bool(entry["enabled"])
    return entry


def normalize_group(raw: dict) -> dict:
    """生成 / 补齐模型组字段，返回一个规范化的深拷贝。

    - 自动生成 ``id``（缺省为 ``"g_" + uuid4().hex[:8]``）；``id`` 存在则保留。
    - 补齐 ``name/desc/enabled/strategy/allow_auto_fallback/providers/fallbacks``。
    - ``strategy`` 非法时回退 ``"priority"``。
    - 逐条规范化 ``providers``，剔除 ``provider_id`` 为空等非法条目。
    - ``fallbacks`` 为字符串 provider_id 列表，保留存在者并按序去重。

    Args:
        raw: 原始模型组 dict。

    Returns:
        规范化后的模型组 dict。
    """
    group = copy.deepcopy(_DEFAULT_GROUP_FIELDS)
    if not isinstance(raw, dict):
        logger.warning("groups: normalize_group 收到非 dict，按默认组处理")
        return group

    raw_id = raw.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        group["id"] = raw_id.strip()
    else:
        group["id"] = "g_" + uuid.uuid4().hex[:8]

    group["name"] = str(raw.get("name", "") or "")
    group["desc"] = str(raw.get("desc", "") or "")
    group["enabled"] = bool(raw.get("enabled", True))
    group["allow_auto_fallback"] = bool(raw.get("allow_auto_fallback", False))

    strategy = raw.get("strategy", "priority")
    if strategy not in GROUP_STRATEGIES:
        logger.warning(
            "groups: 组 %s 的策略 %r 非法，回退 priority", group["id"], strategy
        )
        strategy = "priority"
    group["strategy"] = strategy

    # 组内成员规范化：非法条目剔除并提示。
    providers: list[dict] = []
    raw_providers = raw.get("providers") or []
    for item in raw_providers:
        entry = _fresh_provider_entry(item)
        if entry is not None:
            providers.append(entry)
    group["providers"] = providers

    # fallbacks 过滤 + 去重（保序）。
    seen: set[str] = set()
    fallbacks: list[str] = []
    for fb in raw.get("fallbacks") or []:
        if not isinstance(fb, str) or not fb.strip():
            continue
        fid = fb.strip()
        if fid in seen:
            continue
        seen.add(fid)
        fallbacks.append(fid)
    group["fallbacks"] = fallbacks

    return group


class ModelGroupManager:
    """模型组管理：CRUD + 组内 Provider 选择策略。"""

    def __init__(self, store):
        """用 ConfigStore 实例初始化。

        Args:
            store: ``persistence.ConfigStore`` 实例（提供 get_groups / update 等）。
        """
        self._store = store

    # ---- 查询 / CRUD ----

    def list_(self, only_enabled: bool = False) -> list[dict]:
        """返回模型组列表（深拷贝）。

        Args:
            only_enabled: 为 True 时仅返回 ``enabled`` 的组。

        Returns:
            模型组 dict 列表。
        """
        groups = [normalize_group(g) for g in self._store.get_groups()]
        if only_enabled:
            groups = [g for g in groups if g["enabled"]]
        return groups

    def get(self, group_id: str) -> dict | None:
        """按 id 查模型组（深拷贝）；不存在返回 ``None``。"""
        for g in self._store.get_groups():
            if g.get("id") == group_id:
                return normalize_group(g)
        return None

    def create(self, raw: dict) -> dict:
        """创建模型组：规范化后追加并保存，返回新组。"""
        group = normalize_group(raw)
        groups = self._store.get_groups()
        groups.append(copy.deepcopy(group))
        self._store.update("groups", groups)
        return group

    def update_group(self, group_id: str, raw: dict) -> dict | None:
        """合并更新模型组：在既有 id/字段基础上用 ``raw`` 覆盖。

        归一化的 ``raw`` 会保留其 id（若 ``raw`` 无 id 则沿用 ``group_id``），再替换原组。
        目标组不存在时返回 ``None``。
        """
        groups = self._store.get_groups()
        for i, g in enumerate(groups):
            if g.get("id") == group_id:
                merged = copy.deepcopy(g)
                for k, v in raw.items():
                    merged[k] = copy.deepcopy(v)
                merged.setdefault("id", group_id)
                updated = normalize_group(merged)
                groups[i] = updated
                self._store.update("groups", groups)
                return updated
        return None

    def delete(self, group_id: str) -> bool:
        """删除模型组；存在并删除返回 True，不存在返回 False。"""
        groups = self._store.get_groups()
        remaining = [g for g in groups if g.get("id") != group_id]
        if len(remaining) == len(groups):
            return False
        self._store.update("groups", remaining)
        return True

    def duplicate(self, group_id: str) -> dict | None:
        """复制模型组：深拷贝，生成新 id，name 追加 ``(copy)``；返回新组。"""
        src = self.get(group_id)
        if src is None:
            return None
        clone = normalize_group(src)
        clone["id"] = "g_" + uuid.uuid4().hex[:8]
        clone["name"] = f"{clone['name']}(copy)" if clone["name"] else "(copy)"
        self.create(clone)
        return clone

    # ---- 组内选择策略 ----

    def select_provider(
        self,
        group: dict,
        state,
        available_ids: set[str],
        rng: random.Random | None = None,
    ) -> tuple[str | None, str]:
        """按组策略在组成员中选择一个可用 Provider。

        返回 ``(provider_id | None, reason)``。``reason`` 恒为人类可读字符串，
        解释选中 / 跳过 / 无候选的原因。选中后会写入 ``state.group_cursor`` 的
        ``uses`` 与 ``cooldown_until``（非持久化成本模块职责，engine 随后统一保存）。

        Args:
            group: 规范化模型组。
            state: SessionState（组游标存于 ``group_cursor[group_id]``）。
            available_ids: 当前可用且 enabled 的 provider id 集合。
            rng: 可选 ``random.Random``，加权/随机策略的随机源（测试注入用）。

        Returns:
            ``(provider_id | None, reason)``。
        """
        rand = rng or random
        cursor = state.group_cursor.setdefault(
            group["id"], {"rr": 0, "uses": {}, "cooldown_until": {}}
        )
        uses: dict = cursor["uses"]
        cooldown_until: dict = cursor["cooldown_until"]
        now = time.monotonic()
        strategy = group["strategy"]

        # 候选（enabled 且 available），带原顺序下标。
        members = group["providers"]
        candidates = [
            (i, entry["provider_id"])
            for i, entry in enumerate(members)
            if entry["enabled"] and entry["provider_id"] in available_ids
        ]
        if not candidates:
            return (
                None,
                f"组内无 enabled 且存在于可用Provider集合的成员（strategy={strategy}）",
            )

        # 计算每个候选的可用性原因（供过滤提示）。
        def _skip_reason(pid: str) -> str | None:
            until = cooldown_until.get(pid)
            if until and until > now:
                return f"Provider {pid} 处于冷却中（至 {until:.0f}）"
            entry = next((e for e in members if e["provider_id"] == pid), None)
            max_uses = (entry or {}).get("max_uses", 0)
            if max_uses and max_uses > 0 and uses.get(pid, 0) >= max_uses:
                return f"Provider {pid} 已达 max_uses={max_uses}"
            return None

        if strategy == "fallback":
            # 只取第一个可用成员；不可用返回 None（fallbacks 由 engine 处理）。
            for _, pid in candidates:
                reason = _skip_reason(pid)
                if reason is None:
                    entry = self._member_entry(members, pid)
                    self._record_use(cursor, entry, now)
                    return (pid, f"fallback 取第一个可用成员 {pid}")
                logger.debug("groups: fallback 跳过 %s: %s", pid, reason)
            return (None, "fallback 策略：所有成员均不可用（冷却或达 max_uses）")

        if strategy == "priority":
            chosen: str | None = None
            chosen_order = -1
            for order, pid in candidates:
                if _skip_reason(pid) is not None:
                    continue
                # priority 越小越优先；同优先级按原顺序。
                pri = members[order].get("priority", 0)
                if chosen is None or pri < members[chosen_order].get("priority", 0):
                    chosen = pid
                    chosen_order = order
            if chosen is None:
                return (
                    None,
                    "priority 策略：有 enabled 成员但均被冷却 / max_uses 过滤",
                )
            entry = members[chosen_order]
            self._record_use(cursor, entry, now)
            pri = entry.get("priority", 0)
            return (chosen, f"priority 策略选中 priority={pri} 的 {chosen}")

        if strategy == "round_robin":
            rr = int(cursor.get("rr") or 0)
            n = len(candidates)
            for step in range(n):
                order, pid = candidates[(rr + step) % n]
                if _skip_reason(pid) is not None:
                    continue
                # 命中后 rr 指向其后一个下标（按 providers 原顺序，含被跳过下标）。
                cursor["rr"] = (order + 1) % len(members) if members else 0
                entry = self._member_entry(members, pid)
                self._record_use(cursor, entry, now)
                return (pid, f"round_robin 选中 {pid}")
            return (None, "round_robin 策略：有 enabled 成员但均被冷却 / max_uses 过滤")

        # weighted / random：对可用候选做随机选择。
        usable = [pid for _, pid in candidates if _skip_reason(pid) is None]
        if not usable:
            return (None, f"{strategy} 策略：有 enabled 成员但均被冷却 / max_uses 过滤")

        if strategy == "weighted":
            weights = {
                pid: max(1, int(self._member_entry(members, pid).get("weight", 1)))
                for pid in usable
            }
            pids = list(weights.keys())
            wlist = [weights[p] for p in pids]
            chosen = rand.choices(pids, weights=wlist, k=1)[0]
            self._record_use(cursor, self._member_entry(members, chosen), now)
            return (chosen, f"weighted 策略按权重 {weights} 随机选中 {chosen}")

        chosen = rand.choice(usable)
        self._record_use(cursor, self._member_entry(members, chosen), now)
        return (chosen, f"random 策略等权随机选中 {chosen}")

    @staticmethod
    def _member_entry(members: list[dict], pid: str) -> dict:
        """按 provider_id 查找成员条目；找不到返回空 dict（不应发生，防御性）。"""
        for entry in members:
            if entry.get("provider_id") == pid:
                return entry
        return {}

    def _record_use(self, cursor: dict, entry: dict, now: float) -> None:
        """选中后更新游标：``uses[pid] += 1``；``cooldown_seconds > 0`` 时登记冷却时间。

        Args:
            cursor: 组游标 ``{"rr","uses","cooldown_until"}``。
            entry: 选中的组内成员条目（含 ``cooldown_seconds``）。
            now: 单调时钟当前值。
        """
        pid = entry.get("provider_id", "")
        uses: dict = cursor["uses"]
        cooldown_until: dict = cursor["cooldown_until"]
        uses[pid] = uses.get(pid, 0) + 1
        cooldown = int(entry.get("cooldown_seconds", 0) or 0)
        if cooldown > 0:
            cooldown_until[pid] = now + cooldown
        else:
            cooldown_until[pid] = now

    def fallback_provider_ids(self, group: dict, available_ids: set[str]) -> list[str]:
        """返回组内 fallbacks 中「存在且可用」的 provider_id 列表（按序去重）。

        Args:
            group: 规范化模型组。
            available_ids: 当前可用 provider id 集合。

        Returns:
            provider_id 列表（保序、去重、已过滤可用性）。
        """
        seen: set[str] = set()
        result: list[str] = []
        for pid in group.get("fallbacks") or []:
            if not isinstance(pid, str) or not pid:
                continue
            if pid in seen or pid not in available_ids:
                continue
            seen.add(pid)
            result.append(pid)
        return result
