"""engine 模块 temporal 层集成测试（模块 T5）。

覆盖：引擎自建 TemporalEngine、运行时 model_override 替换、跨午夜、group_switch 换组、
优先级高者生效、替换环防护、scope.sessions 过滤、缓存失效、simulate 叠加 temporal、
DecisionTrace 新字段序列化、dashboard temporal 字段。
"""

from conftest import FakeAdapter, make_engine, make_store, meta_f, run


def _mk(group_id="g1"):
    """构造一个带组（deepseek 优先，cheap 备选）与 base_group 的引擎。

    Returns:
        ``(engine, adapter, store, states, slog)``；adapter 当前 provider 为 deepseek。
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
            "providers": [
                {
                    "provider_id": "deepseek",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                },
                {
                    "provider_id": "cheap",
                    "priority": 2,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                },
            ],
        },
    ]
    cfg["settings"]["base_group"] = group_id
    store.save(cfg)
    adapter = FakeAdapter(["deepseek", "cheap"], current="deepseek")
    return make_engine(store, adapter=adapter)


def _override_rule(name, source, target, start, end, **extra):
    """构造一条规范化的 model_override 规则。"""
    rule = {
        "name": name,
        "enabled": True,
        "kind": "model_override",
        "group_id": "",
        "source_provider": source,
        "target_provider": target,
        "target_group": "",
        "scope": {"groups": [], "users": [], "sessions": []},
        "schedule": {
            "type": "daily",
            "start": start,
            "end": end,
            "weekdays": [],
            "date": "",
            "timezone": "",
        },
        "priority": 200,
        "metadata": {"created_by": "", "created_at": "", "source": "test"},
    }
    rule.update(extra)
    return rule


def _group_switch_rule(name, src_group, target_group, start, end, **extra):
    """构造一条规范化的 group_switch 规则。"""
    rule = {
        "name": name,
        "enabled": True,
        "kind": "group_switch",
        "group_id": src_group,
        "source_provider": "",
        "target_provider": "",
        "target_group": target_group,
        "scope": {"groups": [], "users": [], "sessions": []},
        "schedule": {
            "type": "daily",
            "start": start,
            "end": end,
            "weekdays": [],
            "date": "",
            "timezone": "",
        },
        "priority": 200,
        "metadata": {"created_by": "", "created_at": "", "source": "test"},
    }
    rule.update(extra)
    return rule


def _at(adapter, hour, minute=0, day=10):
    """设置 FakeAdapter 时间到 2026-06-{day} {hour}:{minute}（Asia/Shanghai）。"""
    adapter.now_dt = adapter.now_dt.replace(
        day=day, hour=hour, minute=minute, second=0, microsecond=0
    )
    return adapter.now_dt


# ---------------------------------------------------------------------- #


def test_model_override_peak_hours():
    """20:00-23:00 deepseek→cheap：21:00 命中替换为 cheap，19:00 恢复为 deepseek。"""
    engine, adapter, store, states, slog = _mk()
    engine._temporal.create(
        _override_rule("Peak", "deepseek", "cheap", "20:00", "23:00")
    )

    _at(adapter, 21)  # 21:00 命中
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "cheap"
    assert t.temporal_matched is not None
    assert t.temporal_matched["target_provider"] == "cheap"
    assert t.replacement_chain == ["deepseek", "cheap"]

    _at(adapter, 19)  # 19:00 未命中
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "deepseek"
    assert t.temporal_matched is None
    assert t.replacement_chain == []


def test_model_override_cross_midnight():
    """跨午夜规则 22:00-02:00：23:30 与 01:00 命中，12:00 不命中。"""
    engine, adapter, store, states, slog = _mk()
    engine._temporal.create(
        _override_rule("Night", "deepseek", "cheap", "22:00", "02:00")
    )

    _at(adapter, 23, 30)
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "cheap"
    assert t.temporal_matched is not None

    _at(adapter, 1)  # 次日 01:00（day 不变仅改时间）
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "cheap"
    assert t.temporal_matched is not None

    _at(adapter, 12)
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "deepseek"
    assert t.temporal_matched is None


def test_group_switch_changes_group():
    """group_switch g1→g2：命中时换组并按 g2 策略选 provider，时间外不切换。"""
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {
            "id": "g1",
            "name": "G1",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "deepseek",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
        {
            "id": "g2",
            "name": "G2",
            "enabled": True,
            "strategy": "priority",
            "allow_auto_fallback": False,
            "fallbacks": [],
            "providers": [
                {
                    "provider_id": "cheap",
                    "priority": 1,
                    "weight": 1,
                    "max_uses": 0,
                    "cooldown_seconds": 0,
                    "enabled": True,
                }
            ],
        },
    ]
    cfg["settings"]["base_group"] = "g1"
    store.save(cfg)
    adapter = FakeAdapter(["deepseek", "cheap"], current="deepseek")
    engine, adapter, store, states, slog = make_engine(store, adapter=adapter)
    engine._temporal.create(_group_switch_rule("Switch", "g1", "g2", "20:00", "23:00"))

    _at(adapter, 21)  # 命中：g1→g2，g2 内选 cheap
    t = run(engine.resolve(meta_f()))
    assert t.final_group_id == "g2"
    assert t.final_provider_id == "cheap"
    assert t.temporal_group_match is not None
    assert t.temporal_group_match["target_group"] == "g2"

    _at(adapter, 19)  # 时间外：保持 g1 → deepseek
    t = run(engine.resolve(meta_f()))
    assert t.final_group_id == "g1"
    assert t.final_provider_id == "deepseek"
    assert t.temporal_group_match is None


def test_overlapping_rules_priority_highest_wins():
    """两条重叠规则不同 priority → 高者生效，trace.temporal_matched id 正确。"""
    engine, adapter, store, states, slog = _mk()
    engine._temporal.create(
        _override_rule(
            "Low", "deepseek", "cheap", "20:00", "23:00", **{"priority": 200}
        )
    )
    high = engine._temporal.create(
        _override_rule(
            "High", "deepseek", "prov-c", "20:00", "23:00", **{"priority": 500}
        )
    )
    # 需要 prov-c 在 available 中（即便替换后也要可用；此处仅校验 trace 命中 id）
    adapter._ids.add("prov-c")

    _at(adapter, 21)
    t = run(engine.resolve(meta_f()))
    assert t.temporal_matched is not None
    assert t.temporal_matched["id"] == high["id"]
    assert t.temporal_matched["target_provider"] == "prov-c"


def test_replacement_cycle_guard():
    """A→B、B→A 环：resolve 不崩溃、链有限、reason 标注环。"""
    engine, adapter, store, states, slog = _mk()
    a_rule = _override_rule("A2B", "deepseek", "cheap", "20:00", "23:00")
    b_rule = _override_rule("B2A", "cheap", "deepseek", "20:00", "23:00")
    # 直接写库绕过 create 的环校验，模拟历史/外部配置残留的环。
    store.update("temporal_rules", [a_rule, b_rule])
    engine._temporal.invalidate()

    _at(adapter, 21)
    t = run(engine.resolve(meta_f()))
    # 从 deepseek 出发：deepseek→cheap→deepseek，检测到环回到 deepseek。
    assert t.final_provider_id == "deepseek"
    assert t.replacement_chain == ["deepseek", "cheap", "deepseek"]
    assert "环" in (t.temporal_reason or "")


def test_scope_sessions_filter():
    """scope.sessions 命中 meta.umo 才生效，否则保持原 provider。"""
    engine, adapter, store, states, slog = _mk()
    engine._temporal.create(
        _override_rule(
            "Scoped",
            "deepseek",
            "cheap",
            "20:00",
            "23:00",
            **{"scope": {"groups": [], "users": [], "sessions": ["umo:group:TARGET"]}},
        )
    )

    _at(adapter, 21)
    t = run(engine.resolve(meta_f(umo="umo:group:TARGET")))
    assert t.final_provider_id == "cheap"
    assert t.temporal_matched is not None

    t = run(engine.resolve(meta_f(umo="umo:group:OTHER")))
    assert t.final_provider_id == "deepseek"
    assert t.temporal_matched is None


def test_cache_invalidation_after_create():
    """在已解析过（形成缓存）后新建规则 → 立即 resolve 反映新规则（缓存失效）。"""
    engine, adapter, store, states, slog = _mk()
    _at(adapter, 21)
    # 先跑一次，形成 active_rules 缓存（无规则命中）。
    t0 = run(engine.resolve(meta_f()))
    assert t0.final_provider_id == "deepseek"
    assert t0.temporal_matched is None

    engine._temporal.create(
        _override_rule("Late", "deepseek", "cheap", "20:00", "23:00")
    )
    t = run(engine.resolve(meta_f()))
    assert t.final_provider_id == "cheap"
    assert t.temporal_matched is not None


def test_simulate_applies_temporal():
    """simulate 带 time_iso 的 payload 同样叠加 temporal 两层。"""
    engine, adapter, store, states, slog = _mk()
    engine._temporal.create(
        _override_rule("Peak", "deepseek", "cheap", "20:00", "23:00")
    )

    out = run(
        engine.simulate(
            {
                "time_iso": "2026-06-10T21:00:00+08:00",
                "group_id": "g1",
                "sender_id": "user1",
                "umo": "umo:sim:1",
                "round": 1,
            }
        )
    )
    assert out["final_provider_id"] == "cheap"
    assert out["temporal_matched"] is not None
    assert out["final_group_id"] == "g1"

    out = run(
        engine.simulate(
            {
                "time_iso": "2026-06-10T19:00:00+08:00",
                "group_id": "g1",
                "sender_id": "user1",
                "umo": "umo:sim:1",
                "round": 1,
            }
        )
    )
    assert out["final_provider_id"] == "deepseek"
    assert out["temporal_matched"] is None


def test_decision_trace_to_dict_includes_temporal_fields():
    """DecisionTrace.to_dict() 输出新 temporal 字段。"""
    engine, adapter, store, states, slog = _mk()
    engine._temporal.create(
        _override_rule("Peak", "deepseek", "cheap", "20:00", "23:00")
    )
    _at(adapter, 21)
    t = run(engine.resolve(meta_f()))
    d = t.to_dict()
    assert "temporal_matched" in d
    assert "temporal_group_match" in d
    assert "replacement_chain" in d
    assert "temporal_reason" in d
    assert d["temporal_matched"] is not None
    assert d["replacement_chain"] == ["deepseek", "cheap"]
    assert isinstance(d["temporal_reason"], str)


def test_switch_log_has_temporal_fields():
    """执行切换的日志 entry 含 temporal 与 chain 字段。"""
    engine, adapter, store, states, slog = _mk()
    rule = engine._temporal.create(
        _override_rule("Peak", "deepseek", "cheap", "20:00", "23:00")
    )
    _at(adapter, 21)
    run(engine.resolve(meta_f()))
    entries = [e for e in slog.to_list() if e.get("type") == "switch"]
    assert entries, "应有一条切换日志"
    e = entries[0]
    assert e.get("temporal") == rule["id"]
    assert e.get("chain") == ["deepseek", "cheap"]


def test_dashboard_has_temporal_fields():
    """dashboard() 输出 temporal_rule_count 与 active_temporal_rules。"""
    engine, adapter, store, states, slog = _mk()
    engine._temporal.create(
        _override_rule("Peak", "deepseek", "cheap", "20:00", "23:00")
    )
    _at(adapter, 21)
    dash = engine.dashboard()
    assert dash["temporal_rule_count"] == 1
    assert len(dash["active_temporal_rules"]) == 1
    item = dash["active_temporal_rules"][0]
    for field in (
        "id",
        "name",
        "kind",
        "group_id",
        "source_provider",
        "target_provider",
        "target_group",
        "schedule_type",
        "schedule_start",
        "schedule_end",
        "priority",
    ):
        assert field in item
    assert item["target_provider"] == "cheap"
    assert item["schedule_type"] == "daily"
