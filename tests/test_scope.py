"""v1.0.1 scope（限定群组）语义测试。

覆盖：
- scheduler.scope 工具（normalize / is_empty / match / parse_scope_text）；
- temporal 二段式：限定命中优先于全局（即使全局 priority 更高），段内按 priority；
- temporal group_switch 二段式；find_conflicts 豁免「限定 × 全局」对、保留同段冲突；
- lifecycle normalize scope/priority 与 match_scoped / match_global（priority 排序、禁用过滤）；
- engine 集成：限定生命周期覆盖已绑定全局、全局生命周期按 priority 生效、
  dashboard default_lifecycle_name。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import FakeAdapter, make_engine, make_store, meta_f, run
from scheduler.lifecycle import LifecycleEngine, normalize_lifecycle
from scheduler.scope import (
    normalize_scope,
    parse_scope_text,
    scope_is_empty,
    scope_match,
)
from scheduler.temporal import PRIORITY_EMERGENCY, TemporalEngine, find_conflicts

TZ = ZoneInfo("Asia/Shanghai")


def _at(hh, mm):
    return datetime(2026, 6, 10, hh, mm, tzinfo=TZ)


def _temporal_rule(**kw):
    """构造一条默认 model_override 规则（每日 20:00-23:00，deepseek → cheap）。"""
    base = {
        "id": "t_x",
        "name": "R",
        "enabled": True,
        "kind": "model_override",
        "group_id": "",
        "source_provider": "deepseek",
        "target_provider": "cheap",
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
        "metadata": {"created_by": "", "created_at": "", "source": ""},
    }
    base.update(kw)
    return base


def _lc(**kw):
    base = {
        "id": "lc1",
        "name": "LC",
        "enabled": True,
        "initial_group": "g_init",
        "initial_rounds": 2,
        "main_group": "g_main",
        "periodic_group": "g_per",
        "periodic_interval": 5,
    }
    base.update(kw)
    return base


def _group(gid, provider_id):
    return {
        "id": gid,
        "name": gid,
        "enabled": True,
        "strategy": "priority",
        "allow_auto_fallback": False,
        "fallbacks": [],
        "providers": [
            {
                "provider_id": provider_id,
                "priority": 1,
                "weight": 1,
                "max_uses": 0,
                "cooldown_seconds": 0,
                "enabled": True,
            }
        ],
    }


# ---- scheduler.scope 工具 ----


def test_scope_helpers():
    assert scope_is_empty({}) is True
    assert scope_is_empty(None) is True
    assert scope_is_empty({"groups": [], "users": [], "sessions": []}) is True
    assert scope_is_empty({"groups": ["g1"]}) is False
    assert scope_match({"groups": ["g1"]}, {"group_id": "g1"}) is True
    assert scope_match({"users": ["u1"]}, {"sender_id": "u1"}) is True
    assert scope_match({"sessions": ["s1"]}, {"umo": "s1"}) is True
    assert scope_match({"groups": ["g1"]}, {"group_id": "g2"}) is False
    norm = normalize_scope({"groups": "g1"})
    assert norm == {"groups": ["g1"], "users": [], "sessions": []}
    assert parse_scope_text("g1, g2", "", " s1 ") == {
        "groups": ["g1", "g2"],
        "users": [],
        "sessions": ["s1"],
    }


# ---- temporal 二段式优先级 ----


def test_temporal_scoped_beats_global_priority():
    """限定命中规则 priority 低也优先于全局高 priority 规则。"""
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(
        _temporal_rule(
            id="g", priority=PRIORITY_EMERGENCY, target_provider="global_t"
        )
    )
    eng.create(
        _temporal_rule(
            id="s",
            priority=100,
            scope={"groups": ["12345"], "users": [], "sessions": []},
            target_provider="scoped_t",
        )
    )
    meta = {"group_id": "12345", "sender_id": "x", "umo": "p"}
    r, matched, chain, _ = eng.resolve_model("", "deepseek", _at(21, 0), TZ, meta)
    assert r == "scoped_t"
    assert matched["id"] == "s"
    assert chain == ["deepseek", "scoped_t"]
    # 限定不命中的会话 → 走全局段
    meta2 = {"group_id": "999", "sender_id": "x", "umo": "p"}
    r2, m2, _, _ = eng.resolve_model("", "deepseek", _at(21, 0), TZ, meta2)
    assert r2 == "global_t"
    assert m2["id"] == "g"


def test_temporal_scoped_tier_priority_order():
    """限定段内仍按 priority 降序。"""
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(
        _temporal_rule(
            id="s1",
            priority=100,
            scope={"groups": ["12345"]},
            target_provider="s1_t",
        )
    )
    eng.create(
        _temporal_rule(
            id="s2",
            priority=500,
            scope={"groups": ["12345"]},
            target_provider="s2_t",
        )
    )
    meta = {"group_id": "12345", "sender_id": "x", "umo": "p"}
    r, matched, _, _ = eng.resolve_model("", "deepseek", _at(21, 0), TZ, meta)
    assert r == "s2_t"
    assert matched["id"] == "s2"


def test_temporal_group_switch_scoped_beats_global():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(
        _temporal_rule(
            id="g1",
            kind="group_switch",
            group_id="g_a",
            target_group="g_glob",
            priority=PRIORITY_EMERGENCY,
        )
    )
    eng.create(
        _temporal_rule(
            id="g2",
            kind="group_switch",
            group_id="g_a",
            target_group="g_scop",
            priority=100,
            scope={"groups": ["12345"]},
        )
    )
    meta = {"group_id": "12345", "sender_id": "x", "umo": "p"}
    new_g, matched = eng.resolve_group("g_a", _at(21, 0), TZ, meta)
    assert new_g == "g_scop"
    assert matched["id"] == "g2"
    # 限定不命中 → 全局段
    new_g2, matched2 = eng.resolve_group(
        "g_a", _at(21, 0), TZ, {"group_id": "999", "sender_id": "x", "umo": "p"}
    )
    assert new_g2 == "g_glob"
    assert matched2["id"] == "g1"


def test_find_conflicts_scoped_global_exempt():
    """限定 × 全局分属不同段 → 豁免冲突。"""
    a = _temporal_rule(id="a", scope={"groups": ["12345"]}, target_provider="x1")
    b = _temporal_rule(id="b", target_provider="x2")
    assert find_conflicts([a, b]) == []


def test_find_conflicts_same_tier_still_detected():
    """同段（同为限定/同为全局）仍判冲突。"""
    a = _temporal_rule(id="a", scope={"groups": ["12345"]}, target_provider="x1")
    b = _temporal_rule(id="b", scope={"groups": ["12345"]}, target_provider="x2")
    conflicts = find_conflicts([a, b])
    assert len(conflicts) == 1
    assert conflicts[0]["note"] in ("priority_tie", "shadowed")


# ---- lifecycle scope / priority ----


def test_normalize_lifecycle_scope_priority():
    lc = normalize_lifecycle(_lc())
    assert lc["scope"] == {"groups": [], "users": [], "sessions": []}
    assert lc["priority"] == 0
    lc2 = normalize_lifecycle(_lc(scope={"groups": ["g1"]}, priority=7))
    assert lc2["scope"]["groups"] == ["g1"]
    assert lc2["priority"] == 7
    assert normalize_lifecycle(_lc(priority="x"))["priority"] == 0


def test_match_scoped_and_global_priority():
    store = make_store()
    store.update(
        "lifecycles",
        [
            _lc(id="lc_glob_hi", priority=500),
            _lc(id="lc_glob_lo", priority=10),
            _lc(id="lc_scop_hi", priority=9, scope={"groups": ["12345"]}),
            _lc(id="lc_scop_lo", priority=3, scope={"groups": ["12345"]}),
            _lc(id="lc_disabled", enabled=False, priority=999),
        ],
    )
    eng = LifecycleEngine(store)
    meta = {"group_id": "12345", "sender_id": "u1", "umo": "s1"}
    assert eng.match_scoped(meta)["id"] == "lc_scop_hi"
    assert eng.match_global()["id"] == "lc_glob_hi"
    assert (
        eng.match_scoped({"group_id": "other", "sender_id": "u", "umo": "s"})
        is None
    )
    # 全禁用 → 无匹配
    store.update(
        "lifecycles", [_lc(id="x", enabled=False, scope={"groups": ["12345"]})]
    )
    eng2 = LifecycleEngine(store)
    assert eng2.match_scoped(meta) is None
    assert eng2.match_global() is None


# ---- engine 集成 ----


def _simple_lc(lc_id, main_group, **kw):
    """只走 MAIN 的简单生命周期：main_group 恒生效。"""
    lc = _lc(
        id=lc_id,
        main_group=main_group,
        initial_group="",
        initial_rounds=0,
        periodic_group="",
        periodic_interval=0,
    )
    lc.update(kw)
    return lc


def test_engine_scoped_lifecycle_overrides_bound_global():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [_group("g_scoped", "prov-a"), _group("g_glob", "prov-b")]
    cfg["lifecycles"] = [
        _simple_lc("lc_glob", "g_glob"),
        _simple_lc(
            "lc_scoped",
            "g_scoped",
            scope={"groups": ["12345"], "users": [], "sessions": []},
        ),
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(
        store, adapter=FakeAdapter(["prov-a", "prov-b"])
    )
    # 限定不命中 → 绑定全局策略
    t1 = run(engine.resolve(meta_f(group_id="999")))
    assert t1.final_group_id == "g_glob"
    assert run(states.get("umo:group:1")).lifecycle_id == "lc_glob"
    # 同一会话落入限定群组 → 限定策略覆盖已绑定全局
    t2 = run(engine.resolve(meta_f(group_id="12345")))
    assert t2.final_group_id == "g_scoped"
    st2 = run(states.get("umo:group:1"))
    assert st2.lifecycle_id == "lc_scoped"


def test_engine_global_lifecycle_by_priority():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [_group("g_hi", "prov-a"), _group("g_lo", "prov-b")]
    cfg["lifecycles"] = [
        _simple_lc("lc_lo", "g_lo", priority=10),
        _simple_lc("lc_hi", "g_hi", priority=100),
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(
        store, adapter=FakeAdapter(["prov-a", "prov-b"])
    )
    t = run(engine.resolve(meta_f()))
    assert t.final_group_id == "g_hi"
    assert run(states.get("umo:group:1")).lifecycle_id == "lc_hi"


def test_engine_scoped_user_and_session_match():
    """限定用户 / 限定会话（UMO）同样参与生命周期选择。"""
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [_group("g_user", "prov-a"), _group("g_ses", "prov-b")]
    cfg["lifecycles"] = [
        _simple_lc("lc_user", "g_user", scope={"users": ["user1"]}),
        _simple_lc("lc_ses", "g_ses", scope={"sessions": ["umo:group:1"]}),
    ]
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(
        store, adapter=FakeAdapter(["prov-a", "prov-b"])
    )
    # sender_id=user1 命中 lc_user（priority 相同，列表顺序靠前）
    t = run(engine.resolve(meta_f(group_id="999")))
    assert t.final_group_id == "g_user"
    assert run(states.get("umo:group:1")).lifecycle_id == "lc_user"


def test_dashboard_default_lifecycle_name():
    store = make_store()
    cfg = store.load()
    cfg["lifecycles"] = [_lc(id="lc_x", name="自定义策略")]
    cfg["settings"]["default_lifecycle"] = "lc_x"
    store.save(cfg)
    engine, adapter, store, states, slog = make_engine(
        store, adapter=FakeAdapter(["p1"])
    )
    d = engine.dashboard()
    assert d["default_lifecycle"] == "lc_x"
    assert d["default_lifecycle_name"] == "自定义策略"
    assert d["temporal_rule_count"] == 0
