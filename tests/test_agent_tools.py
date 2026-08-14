"""agent_tools 模块测试（T4，离线，仅测纯逻辑层）。

覆盖：
- 查询工具返回正确结构；
- create/update/delete 模型组与 temporal 规则走 validate 各错误分支
  （非法时间 / 未知 provider / 未知组 / 自引用 / 环）；
- 高危操作 require_preview 语义（agent_confirm True/False 两态）；
- preview → apply 全流程（前置快照、op 顺序执行、审计 entry 齐全）；
- rollback 恢复原配置；
- 「自然语言等效序列」测试：按 spec §27 逐段构造真实工具调用序列并断言最终 config
  结构化结果（时间 / 来源 / 目标 / 优先级）。

temporal 尚未由 T1 交付，此处使用最小 FakeTemporal 替身（仅实现本模块用到的
validate/create/update/delete/toggle/list_/get/active_rules/resolve_model/
find_conflicts/invalidate），测试不依赖 T1 产出。
"""

import copy
import re
from zoneinfo import ZoneInfo

from conftest import make_store
from scheduler.agent_tools import (
    PendingChangeStore,
    ToolContext,
    tool_apply_configuration_change,
    tool_create_lifecycle,
    tool_create_model_group,
    tool_create_model_override,
    tool_create_schedule_rule,
    tool_delete_lifecycle,
    tool_delete_model_group,
    tool_delete_schedule_rule,
    tool_disable_schedule_rule,
    tool_enable_schedule_rule,
    tool_get_lifecycle,
    tool_get_model_group,
    tool_get_scheduler_status,
    tool_list_lifecycles,
    tool_list_model_groups,
    tool_list_models,
    tool_list_rules,
    tool_preview_configuration_change,
    tool_rollback_configuration_change,
    tool_set_default_lifecycle,
    tool_update_lifecycle,
    tool_update_model_group,
    tool_update_schedule_rule,
    tool_validate_configuration,
)
from scheduler.groups import ModelGroupManager
from scheduler.rules import RuleEngine

TZ = ZoneInfo("Asia/Shanghai")

# 测试用 Provider 信息（纯逻辑层只消费结构）。
PROVIDERS = [
    {
        "id": "deepseek",
        "model": "deepseek-chat",
        "type": "chat_completion",
        "enabled": True,
    },
    {"id": "cheap", "model": "kimi-v1", "type": "chat_completion", "enabled": True},
    {"id": "qwen", "model": "qwen-max", "type": "chat_completion", "enabled": True},
]
PROVIDER_IDS = {p["id"] for p in PROVIDERS}


def _is_hhmm(value) -> bool:
    """校验 "HH:MM"（hour<=23, min<=59）。"""
    if not isinstance(value, str):
        return False
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not m:
        return False
    hour, minute = int(m.group(1)), int(m.group(2))
    return hour <= 23 and minute <= 59


def _creates_cycle(existing, candidate) -> bool:
    """判断给定现有规则 + 候选 model_override 是否形成 A→B、B→A 的环。"""
    pairs: dict = {}
    for r in existing:
        if r.get("kind") == "model_override" and r.get("source_provider"):
            pairs[r["source_provider"]] = r.get("target_provider")
    if candidate.get("kind") == "model_override" and candidate.get("source_provider"):
        pairs[candidate["source_provider"]] = candidate.get("target_provider")

    visited, path = set(), set()

    def dfs(node):
        if node in path:
            return True
        if node in visited:
            return False
        visited.add(node)
        path.add(node)
        nxt = pairs.get(node)
        if nxt and dfs(nxt):
            return True
        path.discard(node)
        return False

    return any(dfs(n) for n in list(pairs))


class FakeTemporal:
    """最小 TemporalEngine 替身（内存规则列表，实现 agent_tools 用到的方法）。"""

    def __init__(self):
        self._rules: list[dict] = []
        self._uid = 0
        self._invalidate_calls = 0

    # ---- 内部 ----
    def _next_id(self) -> str:
        self._uid += 1
        return f"t_{self._uid:04d}"

    def _normalize(self, raw: dict) -> dict:
        r = copy.deepcopy(raw or {})
        r.setdefault("id", self._next_id())
        r.setdefault("name", "")
        r.setdefault("enabled", True)
        r.setdefault("kind", "model_override")
        r.setdefault("group_id", "")
        r.setdefault("source_provider", "")
        r.setdefault("target_provider", "")
        r.setdefault("target_group", "")
        r.setdefault("scope", {"groups": [], "users": [], "sessions": []})
        r.setdefault(
            "schedule",
            {
                "type": "daily",
                "start": "",
                "end": "",
                "weekdays": [],
                "date": "",
                "timezone": "",
            },
        )
        r.setdefault("priority", 200)
        r.setdefault("metadata", {"created_by": "", "created_at": "", "source": ""})
        return r

    # ---- CRUD / 查询 ----
    def list_(self, only_enabled: bool = False) -> list[dict]:
        rules = sorted(self._rules, key=lambda r: -int(r.get("priority", 0) or 0))
        if only_enabled:
            rules = [r for r in rules if r.get("enabled")]
        return copy.deepcopy(rules)

    def get(self, rule_id: str) -> dict | None:
        for r in self._rules:
            if r.get("id") == rule_id:
                return copy.deepcopy(r)
        return None

    def create(self, raw: dict) -> dict:
        rule = self._normalize(raw)
        self._rules.append(rule)
        return copy.deepcopy(rule)

    def update_rule(self, rule_id: str, raw: dict) -> dict | None:
        for i, r in enumerate(self._rules):
            if r.get("id") == rule_id:
                merged = copy.deepcopy(r)
                merged.update(copy.deepcopy(raw))
                merged = self._normalize(merged)
                self._rules[i] = merged
                return copy.deepcopy(merged)
        return None

    def delete(self, rule_id: str) -> bool:
        kept = [r for r in self._rules if r.get("id") != rule_id]
        if len(kept) == len(self._rules):
            return False
        self._rules = kept
        return True

    def toggle(self, rule_id: str, enabled: bool | None = None) -> dict | None:
        r = self.get(rule_id)
        if r is None:
            return None
        r["enabled"] = not r.get("enabled", True) if enabled is None else bool(enabled)
        return self.update_rule(rule_id, {"enabled": r["enabled"]})

    def invalidate(self) -> None:
        self._invalidate_calls += 1

    def active_rules(self, now, tz, meta=None) -> list[dict]:
        return copy.deepcopy([r for r in self.list_() if r.get("enabled")])

    def find_conflicts(self, rules) -> list[dict]:
        return []

    def resolve_model(self, group_id, provider_id, now, tz, meta):
        cur = provider_id
        matched = None
        chain: list = []
        for r in self.list_():
            if r.get("kind") != "model_override":
                continue
            if r.get("group_id") and r.get("group_id") != group_id:
                continue
            if r.get("source_provider") == cur:
                matched = r
                chain.append((cur, r.get("target_provider")))
                cur = r.get("target_provider")
        return cur, matched, chain, f"替换 {len(chain)} 级"

    # ---- 校验 ----
    def validate(self, raw, known_provider_ids=None, known_group_ids=None) -> dict:
        raw = raw or {}
        errors: list[str] = []
        warnings: list[str] = []
        kind = raw.get("kind", "model_override")
        sched = raw.get("schedule") or {}
        stype = sched.get("type", "daily")

        if kind not in ("model_override", "group_switch"):
            errors.append(f"非法 kind：{kind}")
        if stype not in ("always", "daily", "weekly", "date"):
            errors.append(f"非法 schedule.type：{stype}")
        if stype in ("daily", "weekly", "date"):
            start, end = sched.get("start", ""), sched.get("end", "")
            if not _is_hhmm(start) or not _is_hhmm(end):
                errors.append(f"非法时间：start={start!r} end={end!r} 须为 HH:MM")
            if stype == "weekly" and not (sched.get("weekdays") or []):
                errors.append("weekly 类型必须提供 weekdays")
            if stype == "date" and not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", str(sched.get("date", ""))
            ):
                errors.append("date 类型必须提供 YYYY-MM-DD 日期")

        if kind == "model_override":
            src, tgt = raw.get("source_provider", ""), raw.get("target_provider", "")
            if not src or not tgt:
                errors.append("model_override 需要 source_provider 与 target_provider")
            elif src == tgt:
                errors.append(
                    f"自引用：source_provider 不能等于 target_provider（{src}）"
                )
            if known_provider_ids is not None:
                if src and src not in known_provider_ids:
                    errors.append(f"未知 source_provider：{src}")
                if tgt and tgt not in known_provider_ids:
                    errors.append(f"未知 target_provider：{tgt}")
            if (
                known_group_ids is not None
                and raw.get("group_id")
                and raw.get("group_id") not in known_group_ids
            ):
                errors.append(f"未知 group_id：{raw.get('group_id')}")
            if not errors and _creates_cycle(self._rules, raw):
                errors.append("替换链形成环")
        else:  # group_switch
            g, tg = raw.get("group_id", ""), raw.get("target_group", "")
            if not g or not tg:
                errors.append("group_switch 需要 group_id 与 target_group")
            elif g == tg:
                errors.append(f"自引用：group_id 不能等于 target_group（{g}）")
            if known_group_ids is not None:
                if g and g not in known_group_ids:
                    errors.append(f"未知 group_id：{g}")
                if tg and tg not in known_group_ids:
                    errors.append(f"未知 target_group：{tg}")

        return {"ok": not errors, "errors": errors, "warnings": warnings}


class FakeLifecycles:
    """最小 LifecycleEngine 替身（内存列表，实现 agent_tools 用到的方法）。

    实现 list_/get/create/update/delete（create/update 返回带 id 的规范化 dict），
    让测试不依赖 A1 的生命周期实现。
    """

    def __init__(self):
        self._items: list[dict] = []
        self._uid = 0

    def _normalize(self, raw: dict) -> dict:
        item = copy.deepcopy(raw or {})
        if not item.get("id"):
            self._uid += 1
            item["id"] = f"lc_{self._uid:03d}"
        item.setdefault("name", "未命名生命周期")
        item.setdefault("enabled", True)
        item.setdefault("stages", [])
        item.setdefault("final_group", "")
        item.setdefault("calibration_event", "")
        item.setdefault("calibration_group", "")
        item.setdefault("calibration_rounds", 0)
        item.setdefault("periodic_group", "")
        item.setdefault("periodic_interval", 0)
        return item

    def list_(self, only_enabled: bool = False) -> list[dict]:
        items = [copy.deepcopy(i) for i in self._items]
        if only_enabled:
            items = [i for i in items if i.get("enabled")]
        return items

    def get(self, lifecycle_id: str) -> dict | None:
        for i in self._items:
            if i.get("id") == lifecycle_id:
                return copy.deepcopy(i)
        return None

    def create(self, raw: dict) -> dict:
        item = self._normalize(raw)
        self._items.append(item)
        return copy.deepcopy(item)

    def update(self, lifecycle_id: str, raw: dict) -> dict | None:
        for idx, i in enumerate(self._items):
            if i.get("id") == lifecycle_id:
                merged = dict(i)
                merged.update(copy.deepcopy(raw))
                merged = self._normalize(merged)
                merged["id"] = lifecycle_id
                self._items[idx] = merged
                return copy.deepcopy(merged)
        return None

    def delete(self, lifecycle_id: str) -> bool:
        kept = [i for i in self._items if i.get("id") != lifecycle_id]
        if len(kept) == len(self._items):
            return False
        self._items = kept
        return True


def _settings(**kw) -> dict:
    base = {
        "enabled": True,
        "debug": False,
        "timezone": "Asia/Shanghai",
        "base_group": "",
        "log_retention": 500,
        "state_persist": True,
        "agent_confirm": True,
    }
    base.update(kw)
    return base


def _make_tc(agent_confirm=True, audit=None, lifecycles=None):
    """组装 ToolContext（真实 groups/rules manager + FakeTemporal + 真实 store + 可选 lifecycles）。"""
    store = make_store()
    groups = ModelGroupManager(store)
    rules = RuleEngine(store)
    temporal = FakeTemporal()
    from scheduler.audit import AuditLog

    audit = audit or AuditLog(retention=200)
    tc = ToolContext(
        store=store,
        groups=groups,
        rules=rules,
        temporal=temporal,
        audit=audit,
        provider_infos=copy.deepcopy(PROVIDERS),
        tz=TZ,
        settings=_settings(agent_confirm=agent_confirm),
        data_dir=store.config_path().parent,
        lifecycles=lifecycles,
        source="subagent",
        operator="admin",
    )
    return tc, store, temporal


def _make_lifecycles_tc(agent_confirm=True):
    """组装带 FakeLifecycles 替身的 ToolContext（未 seed 组），返回 (tc, store, fs)。"""
    fs = FakeLifecycles()
    tc, store, _ = _make_tc(agent_confirm=agent_confirm, lifecycles=fs)
    return tc, store, fs


def _seed_group(tc, group_id="default-chat", name="默认组", providers=None):
    """向模型组管理器预置一个组并返回其 id。"""
    if providers is None:
        providers = [
            {"provider_id": "deepseek", "priority": 0, "enabled": True},
            {"provider_id": "cheap", "priority": 1, "enabled": True},
        ]
    tc.groups.create(
        {"id": group_id, "name": name, "strategy": "priority", "providers": providers}
    )
    return group_id


def _schedule(**kw) -> dict:
    base = {
        "type": "daily",
        "start": "20:00",
        "end": "23:59",
        "weekdays": [],
        "date": "",
        "timezone": "",
    }
    base.update(kw)
    return base


# ---- 查询工具结构 ----


def test_query_tools_structure():
    tc, store, temporal = _make_tc()
    _seed_group(tc)
    # 预置一条 temporal 规则
    temporal.create(
        {
            "name": "peak",
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )

    g = tool_list_model_groups(tc)
    assert g["ok"] is True
    assert g["groups"][0]["id"] == "default-chat"
    assert g["groups"][0]["provider_count"] == 2

    det = tool_get_model_group(tc, name="default-chat")
    assert det["ok"] is True
    assert det["group"]["name"] == "默认组"

    miss = tool_get_model_group(tc, name="不存在的组")
    assert miss["ok"] is False
    assert "未找到" in miss["error"]

    mods = tool_list_models(tc)
    assert mods["ok"] is True
    by_id = {m["id"]: m for m in mods["models"]}
    assert by_id["deepseek"]["group_count"] == 1

    rules = tool_list_rules(tc)
    assert rules["ok"] is True
    assert rules["rules"] == []
    assert len(rules["temporal_rules"]) == 1

    status = tool_get_scheduler_status(tc)
    assert status["ok"] is True
    assert status["temporal_count"] == 1
    assert status["group_count"] == 1
    assert status["provider_count"] == 3


def test_get_model_group_fuzzy_candidates():
    tc, _, _ = _make_tc()
    _seed_group(tc, group_id="g_chat_main", name="主聊天组")
    miss = tool_get_model_group(tc, name="chat")
    assert miss["ok"] is False
    assert "g_chat_main" in miss["candidates"]


# ---- 模型组 create/update/delete 走校验 ----


def test_group_create_rejects_unknown_provider():
    tc, _, _ = _make_tc()
    r = tool_create_model_group(
        tc, spec={"name": "坏组", "providers": [{"provider_id": "no_such_provider"}]}
    )
    assert r["ok"] is False
    assert "未知 Provider" in r["error"]


def test_group_create_success_without_confirm_gate():
    tc, _, _ = _make_tc()
    r = tool_create_model_group(
        tc, spec={"name": "好组", "providers": [{"provider_id": "qwen"}]}
    )
    assert r["ok"] is True
    assert r["group"]["name"] == "好组"


def test_group_update_before_after():
    tc, _, _ = _make_tc()
    _seed_group(tc)
    r = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={
            "providers": [
                {"provider_id": "deepseek", "priority": 0, "enabled": True},
                {"provider_id": "qwen", "priority": 0, "enabled": True},
            ]
        },
    )
    assert r["ok"] is True
    ids = [e["provider_id"] for e in r["group"]["providers"]]
    assert "qwen" in ids


def test_group_update_unknown_provider_rejected():
    tc, _, _ = _make_tc()
    _seed_group(tc)
    r = tool_update_model_group(
        tc, group_id="default-chat", spec={"providers": [{"provider_id": "ghost"}]}
    )
    assert r["ok"] is False
    assert "未知 Provider" in r["error"]


def test_group_update_missing_target():
    tc, _, _ = _make_tc()
    r = tool_update_model_group(tc, group_id="nope", spec={"name": "x"})
    assert r["ok"] is False
    assert "不存在" in r["error"]


# ---- temporal 规则 validate 错误分支 ----


def test_temporal_create_illegal_time():
    tc, _, _ = _make_tc()
    r = tool_create_schedule_rule(
        tc,
        spec={
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": {"type": "daily", "start": "25:99", "end": "10:00"},
        },
    )
    assert r["ok"] is False
    assert "非法时间" in r["error"]


def test_temporal_create_unknown_provider():
    tc, _, _ = _make_tc()
    r = tool_create_schedule_rule(
        tc,
        spec={
            "kind": "model_override",
            "source_provider": "ghost",
            "target_provider": "cheap",
            "schedule": _schedule(),
        },
    )
    assert r["ok"] is False
    assert "未知" in r["error"]


def test_temporal_create_unknown_group():
    tc, _, _ = _make_tc()
    _seed_group(tc)
    r = tool_create_schedule_rule(
        tc,
        spec={
            "kind": "group_switch",
            "group_id": "no_such_group",
            "target_group": "default-chat",
            "schedule": _schedule(),
        },
    )
    assert r["ok"] is False
    assert "未知 group_id" in r["error"]


def test_temporal_create_self_reference():
    tc, _, _ = _make_tc()
    r = tool_create_schedule_rule(
        tc,
        spec={
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "deepseek",
            "schedule": _schedule(),
        },
    )
    assert r["ok"] is False
    assert "自引用" in r["error"]


def test_temporal_create_cycle():
    tc, _, temporal = _make_tc()
    # 现有 A→B
    temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )
    # 新 B→A 形成环
    r = tool_create_schedule_rule(
        tc,
        spec={
            "kind": "model_override",
            "source_provider": "cheap",
            "target_provider": "deepseek",
            "schedule": _schedule(),
        },
    )
    assert r["ok"] is False
    assert "环" in r["error"]


def test_temporal_update_rule_validate():
    tc, _, temporal = _make_tc()
    rule = temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )
    r = tool_update_schedule_rule(
        tc,
        rule_id=rule["id"],
        spec={"target_provider": "deepseek"},  # 自引用
    )
    assert r["ok"] is False
    assert "自引用" in r["error"]


def test_temporal_enable_disable():
    tc, _, temporal = _make_tc()
    rule = temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )
    off = tool_disable_schedule_rule(tc, rule_id=rule["id"])
    assert off["ok"] is True and off["rule"]["enabled"] is False
    on = tool_enable_schedule_rule(tc, rule_id=rule["id"])
    assert on["ok"] is True and on["rule"]["enabled"] is True


# ---- 高危 require_preview 语义 ----


def test_delete_group_requires_preview_when_confirm():
    tc, _, _ = _make_tc(agent_confirm=True)
    _seed_group(tc)
    r = tool_delete_model_group(tc, group_id="default-chat")
    assert r["ok"] is False
    assert r["require_preview"] is True
    assert "preview_configuration_change" in r["error"]
    # 未被删除
    assert tc.groups.get("default-chat") is not None


def test_delete_group_executes_when_no_confirm():
    tc, _, _ = _make_tc(agent_confirm=False)
    _seed_group(tc)
    r = tool_delete_model_group(tc, group_id="default-chat")
    assert r["ok"] is True
    assert r["deleted"] is True
    assert tc.groups.get("default-chat") is None


def test_delete_rule_requires_preview_when_confirm():
    tc, _, temporal = _make_tc(agent_confirm=True)
    rule = temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )
    r = tool_delete_schedule_rule(tc, rule_id=rule["id"])
    assert r["ok"] is False and r["require_preview"] is True
    assert temporal.get(rule["id"]) is not None


def test_delete_rule_executes_when_no_confirm():
    tc, _, temporal = _make_tc(agent_confirm=False)
    rule = temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(),
        }
    )
    r = tool_delete_schedule_rule(tc, rule_id=rule["id"])
    assert r["ok"] is True
    assert temporal.get(rule["id"]) is None


# ---- preview → apply 全流程 ----


def test_preview_apply_flow_and_audit():
    tc, store, temporal = _make_tc()
    _seed_group(tc)

    ops = [
        {
            "action": "create_schedule_rule",
            "data": {
                "kind": "model_override",
                "source_provider": "deepseek",
                "target_provider": "cheap",
                "schedule": _schedule(),
            },
        },
        {
            "action": "create_model_group",
            "data": {"name": "新增组", "providers": [{"provider_id": "qwen"}]},
        },
    ]
    prev = tool_preview_configuration_change(tc, ops=ops)
    assert prev["ok"] is True
    assert prev["require_apply"] is True
    assert len(prev["preview"]) == 2
    # preview 未写库：temporal 仍为空，组仍未新建
    assert len(temporal.list_()) == 0
    assert all(g["id"] != "新增组" for g in tc.groups_())

    # apply：按序执行 + 审计
    ap = tool_apply_configuration_change(tc)
    assert ap["ok"] is True
    assert len(temporal.list_()) == 1
    assert any(g["name"] == "新增组" for g in tc.groups_())

    # pending 已清空，last_snapshot 已保留
    assert tc.pending.get() is None
    assert tc.pending.last_snapshot() is not None

    # 审计 entry 齐全
    entries = tc.audit.recent()
    actions = [e["action"] for e in entries if e["result"] == "success"]
    assert "create_schedule_rule" in actions
    assert "create_model_group" in actions
    assert all(
        e["source"] == "subagent"
        for e in entries
        if e["action"] in ("create_schedule_rule", "create_model_group")
    )
    assert all(
        e["operator"] == "admin"
        for e in entries
        if e["action"] in ("create_schedule_rule", "create_model_group")
    )


def test_preview_rejects_invalid_op_without_staging():
    tc, _, temporal = _make_tc()
    _seed_group(tc)
    ops = [
        {
            "action": "create_schedule_rule",
            "data": {
                "kind": "model_override",
                "source_provider": "ghost",
                "target_provider": "cheap",
                "schedule": _schedule(),
            },
        }
    ]
    prev = tool_preview_configuration_change(tc, ops=ops)
    assert prev["ok"] is False
    assert "errors" in prev
    # 未暂存
    assert tc.pending.get() is None
    assert len(temporal.list_()) == 0


def test_preview_apply_unknown_action_error():
    tc, _, _ = _make_tc()
    _seed_group(tc)
    prev = tool_preview_configuration_change(tc, ops=[{"action": "bogus", "data": {}}])
    assert prev["ok"] is False
    assert "未知操作" in prev["error"]


def test_apply_without_pending_fails():
    tc, _, _ = _make_tc()
    r = tool_apply_configuration_change(tc)
    assert r["ok"] is False
    assert "无待应用的更改" in r["error"]


def test_rollback_restores_original_config():
    tc, store, _ = _make_tc()
    # 原配置：一个组
    _seed_group(tc, group_id="existing", name="既有组")
    before_groups = len(tc.groups_())

    ops = [
        {
            "action": "create_model_group",
            "data": {"name": "临时组", "providers": [{"provider_id": "qwen"}]},
        }
    ]
    assert tool_preview_configuration_change(tc, ops=ops)["ok"] is True
    assert tool_apply_configuration_change(tc)["ok"] is True
    assert len(tc.groups_()) == before_groups + 1

    rb = tool_rollback_configuration_change(tc)
    assert rb["ok"] is True
    # 恢复原配置：新增组消失，既有组保留
    assert len(tc.groups_()) == before_groups
    assert tc.groups.get("existing") is not None
    assert all(g["name"] != "临时组" for g in tc.groups_())
    # 回滚审计
    rollback_entries = [e for e in tc.audit.recent() if e["result"] == "rollback"]
    assert len(rollback_entries) == 1
    assert rollback_entries[0]["source"] == "subagent"
    # 再次回滚无可得快照
    assert tool_rollback_configuration_change(tc)["ok"] is False


def test_rollback_without_snapshot_fails():
    tc, _, _ = _make_tc()
    r = tool_rollback_configuration_change(tc)
    assert r["ok"] is False
    assert "无可用回滚快照" in r["error"]


def test_validate_configuration_reports_errors():
    tc, _, temporal = _make_tc()
    _seed_group(tc)
    # 一条未知 provider 的规则 → 校验报错
    temporal.create(
        {
            "kind": "model_override",
            "source_provider": "deepseek",
            "target_provider": "ghost",
            "schedule": _schedule(),
        }
    )
    r = tool_validate_configuration(tc)
    assert r["ok"] is False  # 配置存在错误
    assert any("ghost" in e for e in r["errors"])

    # 修复后校验通过
    temporal.update_rule(list(temporal.list_())[0]["id"], {"target_provider": "cheap"})
    r2 = tool_validate_configuration(tc)
    assert r2["ok"] is True
    assert r2["errors"] == []


# ---- 自然语言等效序列（spec §27） ----


def test_nl_seq_evening_cheap_model():
    """管理员原话：「晚上八点以后用便宜模型」→ 创建 model_override：晚间把主力换成便宜模型。"""
    tc, _, temporal = _make_tc()
    _seed_group(tc, group_id="default", name="默认")
    r = tool_create_model_override(
        tc,
        spec={
            "name": "晚间便宜模型",
            "group_id": "default",
            "source_provider": "deepseek",
            "target_provider": "cheap",
            "schedule": _schedule(start="20:00", end="23:59"),
        },
    )
    assert r["ok"] is True
    rule = temporal.get(r["rule"]["id"])
    assert rule["kind"] == "model_override"  # 类型
    assert rule["source_provider"] == "deepseek"  # 来源
    assert rule["target_provider"] == "cheap"  # 目标
    assert rule["schedule"]["type"] == "daily"  # 每天
    assert rule["schedule"]["start"] == "20:00"  # 晚上八点开始
    assert rule["priority"] == 200  # 默认优先级


def test_nl_seq_deepseek_peak_avoid():
    """管理员原话：「DeepSeek 高峰期别用」→ 创建 model_override：高峰期 DeepSeek → 备用模型。"""
    tc, _, temporal = _make_tc()
    _seed_group(tc, group_id="default", name="默认")
    r = tool_create_schedule_rule(
        tc,
        spec={
            "name": "DeepSeek 高峰期禁用",
            "kind": "model_override",
            "group_id": "default",
            "source_provider": "deepseek",
            "target_provider": "qwen",
            "schedule": _schedule(start="12:00", end="14:00"),
        },
    )
    assert r["ok"] is True
    rule = temporal.get(r["rule"]["id"])
    assert rule["source_provider"] == "deepseek"  # 高峰来源是 DeepSeek
    assert rule["target_provider"] == "qwen"  # 替换为 Qwen（备用）
    assert rule["schedule"]["start"] == "12:00"  # 高峰时段起点
    assert rule["schedule"]["end"] == "14:00"


def test_nl_seq_replace_deepseek_with_qwen():
    """管理员原话：「把 default-chat 的 DeepSeek 换成 Qwen」→ update_model_group：组内成员替换。"""
    tc, _, _ = _make_tc()
    _seed_group(
        tc,
        group_id="default-chat",
        name="Default Chat",
        providers=[{"provider_id": "deepseek", "priority": 0, "enabled": True}],
    )
    r = tool_update_model_group(
        tc,
        group_id="default-chat",
        spec={"providers": [{"provider_id": "qwen", "priority": 0, "enabled": True}]},
    )
    assert r["ok"] is True
    ids = [e["provider_id"] for e in r["group"]["providers"]]
    assert ids == ["qwen"]  # DeepSeek 已被 Qwen 替换
    assert "deepseek" not in ids


def test_nl_seq_disable_expensive_after_23():
    """管理员原话：「每天 23 点以后关闭高成本模型」→ 创建 model_override：23:00 起低成本。"""
    tc, _, temporal = _make_tc()
    _seed_group(tc, group_id="default", name="默认")
    r = tool_create_model_override(
        tc,
        spec={
            "name": "夜晚省钱",
            "group_id": "default",
            "source_provider": "deepseek",  # 高成本
            "target_provider": "cheap",  # 低成本
            "schedule": _schedule(start="23:00", end="08:00"),  # 跨午夜
        },
    )
    assert r["ok"] is True
    rule = temporal.get(r["rule"]["id"])
    assert rule["schedule"]["start"] == "23:00"
    assert (
        rule["schedule"]["end"] == "08:00"
    )  # end<start 表示跨午夜（当天 23 点至次日 8 点）
    assert rule["source_provider"] == "deepseek"
    assert rule["target_provider"] == "cheap"


def test_nl_seq_tomorrow_evening_temp_switch():
    """管理员原话：「明天下午六点到晚上十点临时切换」→ 创建 date 型临时替换：明天 18:00-22:00。"""
    tc, _, temporal = _make_tc()
    _seed_group(tc, group_id="default", name="默认")
    # 「明天」= 相对于基准日期 2026-06-10 的次日
    tomorrow = {
        "type": "date",
        "start": "18:00",
        "end": "22:00",
        "weekdays": [],
        "date": "2026-06-11",
        "timezone": "",
    }
    r = tool_create_schedule_rule(
        tc,
        spec={
            "name": "明日临时切换",
            "kind": "model_override",
            "group_id": "default",
            "source_provider": "deepseek",
            "target_provider": "qwen",
            "schedule": tomorrow,
        },
    )
    assert r["ok"] is True
    rule = temporal.get(r["rule"]["id"])
    assert rule["schedule"]["type"] == "date"  # 指定日期
    assert rule["schedule"]["date"] == "2026-06-11"  # 明天
    assert rule["schedule"]["start"] == "18:00"  # 下午六点
    assert rule["schedule"]["end"] == "22:00"  # 晚上十点


# ---- 生命周期工具（v0.1.6） ----


def _seed_groups_for_lifecycle(tc):
    """预置两个组（g_main / g_cal）供生命周期校验引用。"""
    _seed_group(tc, group_id="g_main", name="主组")
    _seed_group(tc, group_id="g_cal", name="校准组")


def _stage_spec(gid="g_main", rounds=3):
    """构造一条合法阶段项。"""
    return {"group_id": gid, "rounds": rounds}


def _lc_spec(**kw) -> dict:
    base = {
        "name": "降级预设",
        "enabled": True,
        "stages": [_stage_spec("g_main", 4), _stage_spec("g_cal", 5)],
        "final_group": "g_main",
        "calibration_event": "",
        "calibration_group": "",
        "calibration_rounds": 0,
        "periodic_group": "",
        "periodic_interval": 0,
    }
    base.update(kw)
    return base


# ---- 生命周期工具未注入兜底 ----


def test_lifecycle_tools_guard_when_engine_missing():
    tc, _, _ = _make_tc(lifecycles=None)
    # 未注入引擎时，生命周期写 / 查询工具返回「生命周期引擎未注入」
    assert tool_list_lifecycles(tc)["ok"] is False
    assert "未注入" in tool_list_lifecycles(tc)["error"]
    assert tool_get_lifecycle(tc, name="x")["ok"] is False
    assert tool_create_lifecycle(tc, spec={})["ok"] is False
    assert tool_update_lifecycle(tc, lifecycle_id="lc", spec={})["ok"] is False
    assert tool_delete_lifecycle(tc, lifecycle_id="lc")["ok"] is False
    assert tool_set_default_lifecycle(tc, lifecycle_id="lc")["ok"] is False


# ---- 生命周期 list / get ----


def test_lifecycle_list_get():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    fs.create({"name": "经典", "initial_group": "g_main", "main_group": "g_cal"})

    listed = tool_list_lifecycles(tc)
    assert listed["ok"] is True
    assert len(listed["lifecycles"]) == 2

    by_id = tool_get_lifecycle(tc, name=created["id"])
    assert by_id["ok"] is True and by_id["lifecycle"]["name"] == "降级预设"
    by_name = tool_get_lifecycle(tc, name="经典")
    assert by_name["ok"] is True and by_name["lifecycle"]["name"] == "经典"


def test_lifecycle_get_missing_lists_candidates():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    fs.create({"name": "深 降级A"})
    fs.create({"name": "深 降级B"})
    miss = tool_get_lifecycle(tc, name="降级")
    assert miss["ok"] is False
    assert len(miss["candidates"]) >= 1


# ---- 生命周期 create 校验各错误分支 ----


def test_lifecycle_create_ok():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    r = tool_create_lifecycle(tc, spec=_lc_spec())
    assert r["ok"] is True
    lc = r["lifecycle"]
    assert lc["name"] == "降级预设"
    assert lc["id"].startswith("lc_")
    assert lc["stages"][0]["group_id"] == "g_main"
    # 审计
    assert any(
        e["action"] == "create_lifecycle" and e["result"] == "success"
        for e in tc.audit.recent()
    )


def test_lifecycle_create_name_default_and_enabled_default():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    r = tool_create_lifecycle(tc, spec={"stages": [_stage_spec("g_main", 1)]})
    assert r["ok"] is True
    assert r["lifecycle"]["name"] == "未命名生命周期"
    assert r["lifecycle"]["enabled"] is True


def test_lifecycle_create_rejects_bad_stages():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    # stages 不是数组
    r = tool_create_lifecycle(tc, spec={"stages": "bad"})
    assert r["ok"] is False and "stages 必须为数组" in r["error"]
    # 阶段 group_id 不存在
    r = tool_create_lifecycle(tc, spec={"stages": [_stage_spec("ghost", 1)]})
    assert r["ok"] is False and "「ghost」不存在" in r["error"]
    # 阶段 rounds<=0
    r = tool_create_lifecycle(tc, spec={"stages": [_stage_spec("g_main", 0)]})
    assert r["ok"] is False and "rounds 必须为正整数" in r["error"]


def test_lifecycle_create_rejects_bad_final_group():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    r = tool_create_lifecycle(tc, spec=_lc_spec(final_group="ghost"))
    assert r["ok"] is False and "final_group" in r["error"]


def test_lifecycle_create_rejects_bad_periodic():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    # periodic_group 非空但 interval 非法
    r = tool_create_lifecycle(
        tc, spec=_lc_spec(periodic_group="g_main", periodic_interval=0)
    )
    assert r["ok"] is False and "periodic_interval" in r["error"]
    # periodic_group 不存在
    r = tool_create_lifecycle(
        tc, spec=_lc_spec(periodic_group="ghost", periodic_interval=5)
    )
    assert r["ok"] is False and "不存在" in r["error"]


def test_lifecycle_create_rejects_bad_calibration():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    # 非法 calibration_event
    r = tool_create_lifecycle(tc, spec=_lc_spec(calibration_event="bogus"))
    assert r["ok"] is False and "calibration_event" in r["error"]
    # event 非空但 group 不存在
    r = tool_create_lifecycle(
        tc,
        spec=_lc_spec(
            calibration_event="context_compression",
            calibration_group="ghost",
            calibration_rounds=3,
        ),
    )
    assert r["ok"] is False and "calibration_group" in r["error"]
    # event 非空但 rounds<=0
    r = tool_create_lifecycle(
        tc,
        spec=_lc_spec(
            calibration_event="context_compression",
            calibration_group="g_cal",
            calibration_rounds=0,
        ),
    )
    assert r["ok"] is False and "calibration_rounds" in r["error"]


def test_lifecycle_create_calibration_ok():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    r = tool_create_lifecycle(
        tc,
        spec=_lc_spec(
            calibration_event="context_compression",
            calibration_group="g_cal",
            calibration_rounds=5,
        ),
    )
    assert r["ok"] is True
    assert r["lifecycle"]["calibration_rounds"] == 5


# ---- 生命周期 update / delete ----


def test_lifecycle_update_ok():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    r = tool_update_lifecycle(
        tc,
        lifecycle_id=created["id"],
        spec={"name": "改后"},
    )
    assert r["ok"] is True and r["lifecycle"]["name"] == "改后"
    assert any(
        e["action"] == "update_lifecycle" and e["result"] == "success"
        for e in tc.audit.recent()
    )


def test_lifecycle_update_merge_preserves_fields():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(
        tc,
        spec=_lc_spec(
            calibration_event="context_compression",
            calibration_group="g_cal",
            calibration_rounds=5,
        ),
    )["lifecycle"]
    r = tool_update_lifecycle(tc, lifecycle_id=created["id"], spec={"name": "n"})
    assert r["ok"] is True
    # 未被更新的字段保留
    assert r["lifecycle"]["calibration_rounds"] == 5


def test_lifecycle_update_missing_target():
    tc, _, fs = _make_lifecycles_tc()
    r = tool_update_lifecycle(tc, lifecycle_id="nope", spec={"name": "x"})
    assert r["ok"] is False and "不存在" in r["error"]


def test_lifecycle_update_rejects_bad_after_merge():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    # 更新后 final_group 指向不存在 → 校验失败
    r = tool_update_lifecycle(
        tc, lifecycle_id=created["id"], spec={"final_group": "ghost"}
    )
    assert r["ok"] is False and "final_group" in r["error"]


def test_lifecycle_delete_requires_preview_when_confirm():
    tc, _, fs = _make_lifecycles_tc(agent_confirm=True)
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    r = tool_delete_lifecycle(tc, lifecycle_id=created["id"])
    assert r["ok"] is False
    assert r["require_preview"] is True
    assert "preview_configuration_change" in r["error"]
    # 未删除
    assert fs.get(created["id"]) is not None


def test_lifecycle_delete_executes_when_no_confirm():
    tc, _, fs = _make_lifecycles_tc(agent_confirm=False)
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    r = tool_delete_lifecycle(tc, lifecycle_id=created["id"])
    assert r["ok"] is True and r["deleted"] is True
    assert fs.get(created["id"]) is None
    assert any(
        e["action"] == "delete_lifecycle" and e["result"] == "success"
        for e in tc.audit.recent()
    )


def test_lifecycle_delete_missing_when_no_confirm():
    tc, _, fs = _make_lifecycles_tc(agent_confirm=False)
    r = tool_delete_lifecycle(tc, lifecycle_id="nope")
    assert r["ok"] is False and "不存在" in r["error"]


# ---- set_default_lifecycle ----


def test_set_default_lifecycle_writes_settings_and_audit():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    r = tool_set_default_lifecycle(tc, lifecycle_id=created["id"])
    assert r["ok"] is True and r["default_lifecycle"] == created["id"]
    # settings 已写 store
    assert tc.store.get_settings()["default_lifecycle"] == created["id"]
    # 审计
    entries = [e for e in tc.audit.recent() if e["action"] == "set_default_lifecycle"]
    assert (
        len(entries) == 1 and entries[0]["after"]["default_lifecycle"] == created["id"]
    )


def test_set_default_lifecycle_clears_with_empty():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    assert tool_set_default_lifecycle(tc, lifecycle_id=created["id"])["ok"] is True
    # 空串清除默认
    r = tool_set_default_lifecycle(tc, lifecycle_id="")
    assert r["ok"] is True and r["default_lifecycle"] == ""
    assert tc.store.get_settings()["default_lifecycle"] == ""


def test_set_default_lifecycle_missing_errors():
    tc, _, fs = _make_lifecycles_tc()
    r = tool_set_default_lifecycle(tc, lifecycle_id="nope")
    assert r["ok"] is False and "不存在" in r["error"]


# ---- status / validate 新字段 ----


def test_status_includes_lifecycle_fields():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    created = tool_create_lifecycle(tc, spec=_lc_spec())["lifecycle"]
    tool_set_default_lifecycle(tc, lifecycle_id=created["id"])
    status = tool_get_scheduler_status(tc)
    assert status["ok"] is True
    assert status["lifecycle_count"] == 1
    assert status["default_lifecycle"] == created["id"]


def test_status_lifecycle_fields_empty_without_engine():
    tc, _, _ = _make_tc(lifecycles=None)
    status = tool_get_scheduler_status(tc)
    assert status["lifecycle_count"] == 0
    assert status["default_lifecycle"] == ""


def test_validate_configuration_reports_lifecycle_errors():
    tc, _, fs = _make_lifecycles_tc()
    _seed_groups_for_lifecycle(tc)
    # 预置一个生命周期：final_group 指向不存在组
    fs.create(
        {
            "name": "坏生命周期",
            "stages": [_stage_spec("g_main", 2)],
            "final_group": "ghost",
        }
    )
    r = tool_validate_configuration(tc)
    assert r["ok"] is False
    assert any("ghost" in e for e in r["errors"])

    # 修复后通过（组校验为空，无 temporal/规则错误）
    fs.update(
        list(fs.list_())[0]["id"],
        {"final_group": "g_main"},
    )
    r2 = tool_validate_configuration(tc)
    assert r2["ok"] is True


# ---- PendingChangeStore 单测 ----


def test_pending_store_roundtrip():
    store = make_store()
    ps = PendingChangeStore(store.config_path().parent)
    snapshot = {
        "schema_version": 1,
        "settings": {"enabled": True},
        "groups": [],
        "rules": [],
    }
    pid = ps.stage([{"action": "create_model_group", "data": {}}], snapshot)
    assert ps.get()["pending_id"] == pid
    assert ps.get()["snapshot"] == snapshot
    ps.apply_snapshot()
    assert ps.last_snapshot() == snapshot
    ps.clear()
    assert ps.get() is None
    assert ps.last_snapshot() == snapshot  # apply 快照保留
    ps.mark_rolled_back()
    assert ps.last_snapshot() is None


# ---- v0.1.6：预览/应用管线支持生命周期操作（回归 QA 风险项 2） ----


def _stage_spec_lc(group_id="g_main", rounds=3):
    return {"group_id": group_id, "rounds": rounds}


def test_preview_apply_pipeline_supports_lifecycle_ops():
    """delete_lifecycle 等生命周期 op 应能走 preview → apply 闭环（不再「未知操作类型」）。"""
    tc, store, fs = _make_lifecycles_tc(agent_confirm=True)
    _seed_group(tc, "g_main", "主组")
    _seed_group(tc, "g_cal", "校准组")
    lc = fs.create(
        {
            "name": "降级预设",
            "stages": [_stage_spec_lc("g_main", 4)],
            "final_group": "g_main",
            "calibration_event": "context_compression",
            "calibration_group": "g_cal",
            "calibration_rounds": 5,
        }
    )
    lc_id = lc["id"]

    # preview：create + update + delete 生命周期三项全部通过并 stage
    ops = [
        {
            "action": "create_lifecycle",
            "data": {
                "name": "临时",
                "stages": [_stage_spec_lc("g_main", 1)],
                "final_group": "g_main",
            },
        },
        {
            "action": "update_lifecycle",
            "lifecycle_id": lc_id,
            "data": {"name": "降级预设 v2"},
        },
        {"action": "delete_lifecycle", "lifecycle_id": lc_id},
    ]
    prev = tool_preview_configuration_change(tc, ops=ops)
    assert prev["ok"] is True, prev
    assert len(prev["preview"]) == 3
    # 预览项的 before/after 就位
    upd_item = [p for p in prev["preview"] if p["action"] == "update_lifecycle"][0]
    assert upd_item["before"]["name"] == "降级预设"
    assert upd_item["after"]["name"] == "降级预设 v2"
    del_item = [p for p in prev["preview"] if p["action"] == "delete_lifecycle"][0]
    assert del_item["before"] is not None and del_item["after"] is None

    # apply：真实执行
    applied = tool_apply_configuration_change(tc)
    assert applied["ok"] is True, applied
    ids = {i["id"] for i in fs.list_()}
    assert lc_id not in ids  # 已删除
    remaining = [i for i in fs.list_() if i["name"] == "临时"]
    assert len(remaining) == 1  # 已创建
    # 审计含生命周期动作
    actions = {e["action"] for e in tc.audit.recent(limit=50)}
    assert {"create_lifecycle", "update_lifecycle", "delete_lifecycle"} <= actions

    # 回滚：恢复的是 store 快照（真实 LifecycleEngine 从 store 读，生产环境可完整回滚；
    # 测试用 FakeLifecycles 与 store 解耦，故这里仅断言回滚动作成功 + 审计记录）。
    rolled = tool_rollback_configuration_change(tc)
    assert rolled["ok"] is True, rolled
    assert rolled.get("rolled_back") is True
    rollback_entries = [
        e
        for e in tc.audit.recent(limit=50)
        if e["action"] == "rollback_configuration_change"
    ]
    assert rollback_entries and rollback_entries[0]["result"] == "rollback"


def test_preview_pipeline_lifecycle_validation_error():
    """生命周期 op 校验失败（未知组）时 preview 整体失败且不暂存。"""
    tc, store, fs = _make_lifecycles_tc(agent_confirm=True)
    _seed_group(tc, "g_main", "主组")
    prev = tool_preview_configuration_change(
        tc,
        ops=[
            {
                "action": "create_lifecycle",
                "data": {
                    "name": "坏",
                    "stages": [_stage_spec_lc("ghost_group", 2)],
                    "final_group": "g_main",
                },
            }
        ],
    )
    assert prev["ok"] is False
    assert any("ghost_group" in e for e in prev.get("errors", []))
    assert tc.pending.get() is None  # 校验失败不暂存
