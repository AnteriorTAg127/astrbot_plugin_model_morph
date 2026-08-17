"""engine 模块 —— v1.0.3 决策全序集成测试（强锁模型 + 关键词替换层）。

覆盖：
- 强锁模型（lock_provider_id + lock_model）：最终 provider 为锁定的 provider，
  trace 显示模型锁定；规则 / 关键词 / temporal 均不生效；
- 优先级全序：强锁 > 关键词替换 > 锁组 > 规则 > temporal 端到端断言；
- model_keyword 三模式（all / any / min_n）在引擎端生效；
- 名义模型名解析（C7）：direct_provider 场景与 group_id 场景（成员 model_override）；
- 无法确定模型名 → 关键词层不命中、流程继续；
- simulate 可见性（强锁 / 关键词层）。
"""

from conftest import FakeAdapter, make_engine, make_store, meta_f, run


def _group_provider_entry(provider_id, **kw):
    """构造一组内成员条目（含 model_override 默认空，与 groups 默认一致）。"""
    entry = {
        "provider_id": provider_id,
        "model_override": "",
        "priority": 1,
        "weight": 1,
        "max_uses": 0,
        "cooldown_seconds": 0,
        "enabled": True,
    }
    entry.update(kw)
    return entry


def _mk_group_engine(provider="prov-a", group_providers=None):
    """构造带 base_group（g1）的引擎；组成员默认 prov-a。

    Returns:
        ``(engine, adapter, store, states, slog)``。
    """
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g1",
            "name": "Main",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": group_providers
            if group_providers is not None
            else [_group_provider_entry(provider)],
        }
    ]
    cfg["settings"]["base_group"] = "g1"
    store.save(cfg)
    adapter = FakeAdapter(["prov-a", "prov-b", "prov-c"], current="prov-a")
    adapter.models = {
        "prov-a": "deepseek-v3-flash",
        "prov-b": "turbo-x",
        "prov-c": "gpt-5-mini",
    }
    return make_engine(store, adapter=adapter)


def _kw_rule(cond, then=None, priority=100, **kw):
    """构造一条规范化 keyword/replace_model 规则（默认命中后换到 prov-c @ gpt-5-mini）。"""
    rule = {
        "id": "r_kw",
        "name": "KW",
        "enabled": True,
        "priority": priority,
        "scope": {
            "groups": [],
            "users": [],
            "sessions": [],
            "platforms": [],
            "exclude_groups": [],
            "exclude_users": [],
        },
        "when": {"op": "and", "conditions": [cond]},
        "then": (
            then
            if then is not None
            else {
                "action": "replace_model",
                "provider_id": "prov-c",
                "model": "gpt-5-mini",
            }
        ),
    }
    rule.update(kw)
    return rule


def _kw_cond(keywords, mode="any", min_n=2):
    """构造一条 model_keyword 条件。"""
    cond = {"type": "model_keyword", "keywords": keywords, "mode": mode}
    if mode == "min_n":
        cond["min_n"] = min_n
    return cond


def _resolve(engine, states, **state_kw):
    """在会话上写入可选状态字段并 resolve 一次。"""
    if state_kw:
        run(states.update(meta_f()["umo"], **state_kw))
    return run(engine.resolve(meta_f()))


# ---------------------------------------------------------------------- #
# 1. 强锁模型
# ---------------------------------------------------------------------- #


def test_strong_lock_uses_locked_provider_model():
    """lock_provider + lock_model → 最终 provider 为锁定值，trace 显示模型锁定。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    t = _resolve(engine, states, lock_provider_id="prov-b", lock_model="turbo-x")
    assert t.final_provider_id == "prov-b"
    assert t.lock_model == "turbo-x"
    assert "模型锁定" in t.reason
    assert t.keyword_matched_rule is None
    assert t.final_group_id is None
    # 切换到锁定 provider。
    assert adapter.calls == [("prov-b", meta_f()["umo"])]


def test_strong_lock_beats_keyword_and_rule():
    """强锁 + 命中关键词规则 + 命中普通规则 → 强锁赢（规则/关键词/锁组/temporal 均不生效）。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    # 命中关键词的 replace 规则（模型名 deepseek-v3-flash 含 flash）。
    store.update(
        "rules",
        [
            _kw_rule(_kw_cond(["flash"], mode="any")),
            _kw_rule(
                _kw_cond(["zzz-none"]),
                then={"action": "switch_group", "group_id": "g1"},
                priority=500,
            ),
        ],
    )
    # 强锁到 prov-b（与关键词目标 prov-c 冲突 → 强锁赢）。
    t = _resolve(engine, states, lock_provider_id="prov-b", lock_model="turbo-x")
    assert t.final_provider_id == "prov-b"
    assert t.lock_model == "turbo-x"
    assert t.keyword_matched_rule is None
    assert t.matched_rule is None  # 规则层未评估
    assert t.temporal_matched is None


def test_strong_lock_skips_temporal():
    """强锁 > temporal：即使有命中的 temporal model_override 也不生效。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    # temporal deepseek(=prov-a 的模型语境) → 但强锁是 prov-b，与 temporal 无关。
    engine._temporal.create(
        {
            "name": "Peak",
            "enabled": True,
            "kind": "model_override",
            "group_id": "",
            "source_provider": "prov-a",
            "target_provider": "prov-b",
            "target_group": "",
            "scope": {"groups": [], "users": [], "sessions": []},
            "schedule": {
                "type": "daily",
                "start": "20:00",
                "end": "23:00",
                "weekdays": [],
                "date": "",
                "timezone": "",
            },
            "priority": 200,
        }
    )
    adapter.now_dt = adapter.now_dt.replace(hour=21)
    # 强锁到 prov-c @ gpt-5-mini。
    t = _resolve(engine, states, lock_provider_id="prov-c", lock_model="gpt-5-mini")
    assert t.final_provider_id == "prov-c"
    assert t.temporal_matched is None


# ---------------------------------------------------------------------- #
# 2. 优先级：强锁 > 关键词 > 锁组 > 规则 > temporal
# ---------------------------------------------------------------------- #


def test_keyword_beats_group_lock():
    """关键词替换 > 锁组：锁定组 + 命中关键词规则 → 关键词替换赢（provider 变为目标）。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    store.update("rules", [_kw_rule(_kw_cond(["flash"], mode="any"))])
    # 只锁组（lock_group_id 单独非空，无 lock_model）→ 走旧锁组路径。
    t = _resolve(engine, states, lock_group_id="g1")
    assert t.final_provider_id == "prov-c"  # 关键词替换覆盖锁组选中的 prov-a
    assert t.keyword_matched_rule is not None
    assert t.keyword_matched_rule.get("then", {}).get("provider_id") == "prov-c"
    assert "关键词替换" in t.keyword_reason


def test_keyword_beats_rule_and_temporal():
    """关键词替换 > 规则 > temporal：命中关键词 → 替换；未命中 → temporal 路径不变。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    # temporal：prov-a(deepseek 语境) → 命中时换到 prov-b。
    engine._temporal.create(
        {
            "name": "Peak",
            "enabled": True,
            "kind": "model_override",
            "group_id": "",
            "source_provider": "prov-a",
            "target_provider": "prov-b",
            "target_group": "",
            "scope": {"groups": [], "users": [], "sessions": []},
            "schedule": {
                "type": "daily",
                "start": "20:00",
                "end": "23:00",
                "weekdays": [],
                "date": "",
                "timezone": "",
            },
            "priority": 200,
        }
    )
    adapter.now_dt = adapter.now_dt.replace(hour=21)

    # 命中关键词 → 替换到 prov-c（覆盖 base_group 的 prov-a 与 temporal 的 prov-b）。
    store.update("rules", [_kw_rule(_kw_cond(["flash"], mode="any"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-c"
    assert t.keyword_matched_rule is not None
    assert t.temporal_matched is None  # temporal 在关键词之后，未再覆盖

    # 未命中关键词 → 关键词层不命中，走 temporal：prov-a → prov-b。
    store.update("rules", [_kw_rule(_kw_cond(["nonexistent"], mode="any"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-b"
    assert t.temporal_matched is not None
    assert t.keyword_matched_rule is None
    assert "未命中" in t.keyword_reason


def test_no_keyword_match_keeps_temporal_path():
    """仅普通规则 + temporal（无关键词规则）：行为与 v1.0.2 一致（temporal 生效）。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    engine._temporal.create(
        {
            "name": "Peak",
            "enabled": True,
            "kind": "model_override",
            "group_id": "",
            "source_provider": "prov-a",
            "target_provider": "prov-b",
            "target_group": "",
            "scope": {"groups": [], "users": [], "sessions": []},
            "schedule": {
                "type": "daily",
                "start": "20:00",
                "end": "23:00",
                "weekdays": [],
                "date": "",
                "timezone": "",
            },
            "priority": 200,
        }
    )
    adapter.now_dt = adapter.now_dt.replace(hour=21)
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-b"  # temporal 替换 deepseek→cheap 语义
    assert t.temporal_matched is not None
    assert t.keyword_matched_rule is None


# ---------------------------------------------------------------------- #
# 3. model_keyword 三模式在引擎端生效
# ---------------------------------------------------------------------- #


def test_engine_model_keyword_any():
    """any：模型名含任一关键词 → 替换。"""
    engine, adapter, store, states, slog = (
        _mk_group_engine()
    )  # prov-a 模型 deepseek-v3-flash
    store.update("rules", [_kw_rule(_kw_cond(["flash", "nope"], mode="any"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-c"
    assert t.keyword_matched_rule is not None


def test_engine_model_keyword_all():
    """all：模型名含全部关键词 → 替换；缺任一无替换。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    store.update("rules", [_kw_rule(_kw_cond(["deepseek", "flash"], mode="all"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-c"
    assert t.keyword_matched_rule is not None

    # 缺一个关键词 → 不替换。
    store.update("rules", [_kw_rule(_kw_cond(["deepseek", "gpt"], mode="all"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-a"
    assert t.keyword_matched_rule is None


def test_engine_model_keyword_min_n():
    """min_n：命中数 ≥ min_n → 替换；< min_n 不替换。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    # 模型 deepseek-v3-flash 含 deep 与 flash（2 个），min_n=2 → 替换。
    store.update(
        "rules",
        [_kw_rule(_kw_cond(["deep", "flash", "turbo"], mode="min_n", min_n=2))],
    )
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-c"
    assert t.keyword_matched_rule is not None

    # min_n=3（仅 2 个命中）→ 不替换。
    store.update(
        "rules",
        [_kw_rule(_kw_cond(["deep", "flash", "turbo"], mode="min_n", min_n=3))],
    )
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-a"
    assert t.keyword_matched_rule is None


def test_keyword_rule_priority_highest_wins():
    """同匹配多关键词规则：高 priority 的 replace_model 生效。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    low = _kw_rule(_kw_cond(["flash"], mode="any"), priority=10)
    low["id"] = "r_kw_low"
    low["then"] = {
        "action": "replace_model",
        "provider_id": "prov-b",
        "model": "turbo-x",
    }
    high = _kw_rule(_kw_cond(["flash"], mode="any"), priority=300)
    high["id"] = "r_kw_high"
    store.update("rules", [low, high])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-c"  # high 的目标
    assert t.keyword_matched_rule is not None
    assert t.keyword_matched_rule.get("id") == "r_kw_high"


def test_keyword_target_provider_missing_is_ignored():
    """命中但目标 Provider 不可用 → 忽略该规则，流程继续（回落到 base_group）。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    # 目标 prov-zzz 不在 available_ids → 忽略。
    store.update(
        "rules",
        [
            _kw_rule(
                _kw_cond(["flash"], mode="any"),
                then={
                    "action": "replace_model",
                    "provider_id": "prov-zzz",
                    "model": "x",
                },
            )
        ],
    )
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-a"
    assert t.keyword_matched_rule is None
    assert "不可用" in t.keyword_reason


# ---------------------------------------------------------------------- #
# 4. 名义模型名解析（C7）
# ---------------------------------------------------------------------- #


def test_nominal_model_group_member_override():
    """group_id 场景：首个可用成员的 model_override 作为名义模型名。"""
    engine, adapter, store, states, slog = _mk_group_engine(
        group_providers=[
            _group_provider_entry("prov-a", model_override="custom-flash-tuning"),
        ]
    )
    # 声明 model_override 含 "flash" → 关键词层按它命中。
    store.update("rules", [_kw_rule(_kw_cond(["flash"], mode="any"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-c"
    assert t.keyword_matched_rule is not None
    assert "custom-flash-tuning" in t.keyword_reason


def test_nominal_model_group_provider_model():
    """group_id 场景：成员无 override 时用 provider 默认模型名（adapter.models）。"""
    engine, adapter, store, states, slog = (
        _mk_group_engine()
    )  # prov-a 模型 deepseek-v3-flash
    store.update("rules", [_kw_rule(_kw_cond(["deepseek"], mode="any"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-c"
    assert t.keyword_matched_rule is not None
    assert "deepseek-v3-flash" in t.keyword_reason


def test_nominal_model_direct_provider():
    """direct_provider 场景：直选 switch_provider → adapter 提供模型名。"""
    store = make_store()
    cfg = store.load()
    adapter = FakeAdapter(["prov-a", "prov-b", "prov-c"], current="prov-a")
    adapter.models = {
        "prov-a": "deepseek-v3-flash",
        "prov-b": "turbo-x",
        "prov-c": "gpt-5-mini",
    }
    # 直接规则：always 命中 → direct_provider = prov-b（模型 turbo-x）。
    switch_rule = {
        "id": "r_switch",
        "name": "Switch",
        "enabled": True,
        "priority": 50,
        "scope": {
            "groups": [],
            "users": [],
            "sessions": [],
            "platforms": [],
            "exclude_groups": [],
            "exclude_users": [],
        },
        "when": {"op": "and", "conditions": []},  # 空条件 = 恒命中
        "then": {"action": "switch_provider", "provider_id": "prov-b"},
    }
    cfg["rules"] = [
        switch_rule,
        _kw_rule(_kw_cond(["turbo"], mode="any"), priority=100),
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(store, adapter=adapter)
    t = run(engine.resolve(meta_f()))
    assert (
        t.final_provider_id == "prov-c"
    )  # direct_provider=prov-b 的模型含 turbo → 替换
    assert t.keyword_matched_rule is not None
    assert "turbo-x" in t.keyword_reason


def test_unknown_model_name_skips_keyword():
    """无法确定名义模型名 → 关键词层不命中、流程继续（final 为组内 provider）。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    adapter.models = {}  # 清空模型名映射 → 名义模型名无法确定
    store.update("rules", [_kw_rule(_kw_cond(["flash"], mode="any"))])
    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-a"
    assert t.keyword_matched_rule is None
    assert "无法确定名义模型名" in t.keyword_reason


# ---------------------------------------------------------------------- #
# 5. simulate 可见性
# ---------------------------------------------------------------------- #


def test_simulate_shows_strong_lock():
    """simulate 携带锁字段 → 输出可见「模型锁定」。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    out = run(
        engine.simulate(
            {
                "umo": "umo:sim:lock",
                "round": 3,
                "lock_provider_id": "prov-c",
                "lock_model": "gpt-5-mini",
                "group_id": "g1",
            }
        )
    )
    assert out["final_provider_id"] == "prov-c"
    assert out["lock_model"] == "gpt-5-mini"
    assert "模型锁定" in out["reason"]
    assert out["keyword_matched_rule"] is None


def test_simulate_shows_keyword_layer():
    """simulate 命中关键词规则 → 输出可见 keyword_matched_rule。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    store.update("rules", [_kw_rule(_kw_cond(["flash"], mode="any"))])
    out = run(
        engine.simulate(
            {
                "umo": "umo:sim:kw",
                "round": 2,
                "group_id": "g1",
                "sender_id": "user1",
            }
        )
    )
    assert out["final_provider_id"] == "prov-c"
    assert out["keyword_matched_rule"] is not None
    assert "关键词替换" in out["keyword_reason"]


def test_decision_trace_to_dict_includes_v103_fields():
    """DecisionTrace.to_dict() 输出 v1.0.3 新字段且向后兼容。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    store.update("rules", [_kw_rule(_kw_cond(["flash"], mode="any"))])
    t = _resolve(engine, states)
    d = t.to_dict()
    assert "lock_model" in d
    assert "keyword_matched_rule" in d
    assert "keyword_reason" in d
    assert d["keyword_matched_rule"] is not None
    assert isinstance(d["keyword_reason"], str)
    # 向后兼容：原有字段仍存在。
    for key in (
        "umo",
        "changed",
        "final_provider_id",
        "final_group_id",
        "stage",
        "matched_rule",
        "rejected_rules",
        "reason",
        "temporal_matched",
        "replacement_chain",
    ):
        assert key in d


# ---------------------------------------------------------------------- #
# 6. _provider_model_name getattr 兜底（G-3）
# ---------------------------------------------------------------------- #


def test_provider_model_name_missing_method_falls_back(monkeypatch):
    """G-3：adapter 完全无 provider_model_name 方法（getattr 兜底）→ 名义模型名空、
    关键词层不命中、流程正常无异常（最终走 base_group/temporal 路径）。"""
    engine, adapter, store, states, slog = _mk_group_engine()
    # 命中关键词规则（若名义模型名可解析则必命中 flash）。
    store.update("rules", [_kw_rule(_kw_cond(["flash"], mode="any"))])
    # 从类上移除 provider_model_name 方法：模拟 adapter 无模型名查询能力
    # （engine 内 getattr(self._adapter, "provider_model_name", None) 返回 None）。
    monkeypatch.delattr(type(adapter), "provider_model_name")
    assert not hasattr(adapter, "provider_model_name")

    t = _resolve(engine, states)
    assert t.final_provider_id == "prov-a"  # 回落到 base_group 路径
    assert t.keyword_matched_rule is None  # 名义模型名空 → 关键词层不命中
    assert "无法确定名义模型名" in t.keyword_reason
