"""engine 模块校准集成测试（v0.1.6 模块 B1）。

覆盖：default_lifecycle 回退（首轮即用全局生命周期组、不存在时回退 base_group、
state.lifecycle_id 被记住）、校准覆盖与递减、锁定优先于校准、校准 + temporal 替换、
旧快照 restore（无校准字段）、dashboard 新字段、v1.0.1 全局生命周期按优先级
优先于 default_lifecycle 指针。
"""

from conftest import FakeAdapter, make_engine, make_store, meta_f, run

UMO = "umo:group:1"

PROVIDERS = ["prov-a", "prov-b", "prov-c", "prov-d"]


def _override_like(name, source, target, start, end, **extra):
    """构造一条规范化的 model_override temporal 规则。"""
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


def _group(gid, provider_id, **extra):
    """构造一个无 fallback、priority 单成员、直接出该 provider 的模型组。"""
    group = {
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
    group.update(extra)
    return group


def _staged_lifecycle():
    """返回一个 staged 生命周期：round<2→g_s1，round<5→g_s2，之后→g_final。"""
    return {
        "id": "lc_staged",
        "name": "Staged",
        "enabled": True,
        "initial_group": "",
        "initial_rounds": 0,
        "main_group": "",
        "periodic_group": "",
        "periodic_interval": 0,
        "stages": [
            {"group_id": "g_s1", "rounds": 2},
            {"group_id": "g_s2", "rounds": 3},
        ],
        "final_group": "g_final",
        "calibration_event": "context_compression",
        "calibration_group": "g_cal",
        "calibration_rounds": 5,
    }


def _setup_engine(default_lifecycle=None, base_group="", lifecycles=None):
    """返回一个带 staged 生命周期与若干组（g_s1/g_s2/g_cal/g_final/g_locked）
    的引擎；default_lifecycle / base_group 可配置。

    v1.0.1：``lifecycles`` 可显式指定（None = 默认带一个全局 staged 生命周期；
    [] = 无生命周期，用于测试 default_lifecycle/base_group 兜底路径）。
    """
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        _group("g_s1", "prov-a"),
        _group("g_s2", "prov-b"),
        _group("g_cal", "prov-c"),
        _group("g_final", "prov-d"),
        _group("g_locked", "prov-a"),
    ]
    cfg["lifecycles"] = [_staged_lifecycle()] if lifecycles is None else lifecycles
    if default_lifecycle:
        cfg["settings"]["default_lifecycle"] = default_lifecycle
    if base_group:
        cfg["settings"]["base_group"] = base_group
    store.save(cfg)
    adapter = FakeAdapter(PROVIDERS)
    return make_engine(store, adapter=adapter)


# ---------------------------------------------------------------------- #


def test_default_lifecycle_used_and_remembered():
    """default_lifecycle 生效：无锁无规则首轮即用该生命周期组，且 lifecycle_id 被记住。"""
    engine, adapter, store, states, slog = _setup_engine(default_lifecycle="lc_staged")

    t = run(engine.resolve(meta_f(umo=UMO)))
    # round=0 → STAGE_1/g_s1 → prov-a
    assert t.final_group_id == "g_s1"
    assert t.final_provider_id == "prov-a"
    assert t.stage == "STAGE_1"
    # default_lifecycle 已被记住到 state.lifecycle_id
    st = run(states.get(UMO))
    assert st.lifecycle_id == "lc_staged"


def test_default_lifecycle_falls_back_to_base_group():
    """default_lifecycle 指向不存在的组且无全局生命周期 → 说明原因并回退 base_group。"""
    engine, adapter, store, states, slog = _setup_engine(
        default_lifecycle="lc_nonexist", base_group="g_s2", lifecycles=[]
    )
    t = run(engine.resolve(meta_f(umo=UMO)))
    assert t.final_group_id == "g_s2"
    assert t.final_provider_id == "prov-b"
    assert "default_lifecycle lc_nonexist" in t.reason
    assert "回退 base_group" in t.reason
    # 未绑定不存在的 lifecycle
    st = run(states.get(UMO))
    assert st.lifecycle_id == "lc_nonexist"


def test_default_lifecycle_nonexistent_and_no_base_inherits():
    """default_lifecycle 不存在、无全局生命周期且无 base_group → 不干预（继承原生），不崩溃。"""
    engine, adapter, store, states, slog = _setup_engine(
        default_lifecycle="lc_nonexist", lifecycles=[]
    )
    t = run(engine.resolve(meta_f(umo=UMO)))
    assert t.final_provider_id is None
    assert "继承原生" in t.reason


def test_global_lifecycle_priority_over_default_lifecycle():
    """v1.0.1：存在全局生命周期时按其生效，优先级高于 default_lifecycle 指针。"""
    engine, adapter, store, states, slog = _setup_engine(
        default_lifecycle="lc_nonexist", base_group="g_s2"
    )
    t = run(engine.resolve(meta_f(umo=UMO)))
    # match_global → lc_staged（全局、priority 0）→ round 0 → g_s1
    assert t.final_group_id == "g_s1"
    assert t.final_provider_id == "prov-a"
    st = run(states.get(UMO))
    assert st.lifecycle_id == "lc_staged"


def test_calibration_override_and_decrement():
    """校准覆盖：选校准组、stage=CALIBRATION、reason 含校准、轮数递减。"""
    engine, adapter, store, states, slog = _setup_engine(default_lifecycle="lc_staged")

    async def _main():
        await states.update(
            UMO,
            calibration_rounds_left=5,
            calibration_group_id="g_cal",
            calibration_reason="context_compression",
        )
        t = await engine.resolve(meta_f(umo=UMO))
        st = await states.get(UMO)
        return t, st

    t, st = run(_main())
    assert t.final_group_id == "g_cal"
    assert t.final_provider_id == "prov-c"
    assert t.stage == "CALIBRATION"
    assert "校准阶段(剩余 5 轮, context_compression)" in t.reason
    assert st.calibration_rounds_left == 4
    assert st.calibration_group_id == "g_cal"
    assert st.calibration_reason == "context_compression"


def test_calibration_exhausts_then_returns_to_normal():
    """连续 5 轮校准后轮数耗尽，回到生命周期正常阶段组且 stage 恢复。"""
    engine, adapter, store, states, slog = _setup_engine(default_lifecycle="lc_staged")

    async def _main():
        await states.update(
            UMO,
            calibration_rounds_left=5,
            calibration_group_id="g_cal",
            calibration_reason="context_compression",
        )
        groups = []
        for i in range(5):
            t = await engine.resolve(meta_f(umo=UMO))
            groups.append(t.final_group_id)
            assert t.final_group_id == "g_cal"
            st = await states.get(UMO)
            assert st.calibration_rounds_left == 5 - (i + 1)
        # 第 6 轮：校准耗尽 → 回 lifecycle（round=5 → MAIN/g_final）
        t = await engine.resolve(meta_f(umo=UMO))
        st = await states.get(UMO)
        return groups, t, st

    groups, t, st = run(_main())
    assert groups == ["g_cal"] * 5
    assert t.final_group_id == "g_final"
    assert t.stage != "CALIBRATION"
    assert t.stage == "MAIN"
    assert st.calibration_rounds_left == 0


def test_calibration_not_applied_when_locked():
    """锁定优先于校准：锁定后校准不生效且不递减。"""
    engine, adapter, store, states, slog = _setup_engine(default_lifecycle="lc_staged")

    async def _main():
        await states.update(
            UMO,
            lock_group_id="g_locked",
            calibration_rounds_left=5,
            calibration_group_id="g_cal",
            calibration_reason="context_compression",
        )
        t = await engine.resolve(meta_f(umo=UMO))
        st = await states.get(UMO)
        return t, st

    t, st = run(_main())
    assert t.final_group_id == "g_locked"
    assert t.final_provider_id == "prov-a"
    assert t.stage != "CALIBRATION"
    assert "校准阶段" not in (t.reason or "")
    assert st.calibration_rounds_left == 5  # 未递减


def test_calibration_plus_temporal_model_override():
    """校准 + temporal：g_cal 选出的 provider 被 model_override 规则替换。"""
    engine, adapter, store, states, slog = _setup_engine(default_lifecycle="lc_staged")
    # 20:00-23:00 把 prov-c → prov-b
    rule = engine._temporal.create(
        _override_like("Peak", "prov-c", "prov-b", "20:00", "23:00")
    )
    adapter.now_dt = adapter.now_dt.replace(hour=21, minute=0)

    async def _main():
        await states.update(
            UMO,
            calibration_rounds_left=5,
            calibration_group_id="g_cal",
            calibration_reason="context_compression",
        )
        t = await engine.resolve(meta_f(umo=UMO))
        st = await states.get(UMO)
        return t, st

    t, st = run(_main())
    assert t.final_group_id == "g_cal"  # 组仍是校准组
    assert t.final_provider_id == "prov-b"  # 被 model_override 替换
    assert t.temporal_matched is not None
    assert t.temporal_matched["id"] == rule["id"]
    assert t.replacement_chain == ["prov-c", "prov-b"]


def test_old_snapshot_restore_without_calibration_fields():
    """旧快照（state.json 无校准字段）restore 后 resolve 正常。"""
    engine, adapter, store, states, slog = _setup_engine(default_lifecycle="lc_staged")
    old_snap = {
        UMO: {
            "umo": UMO,
            "round": 3,
            "stage": "STAGE_2",
            "lifecycle_id": "lc_staged",
            # 无 calibration_* 字段（旧版本快照）
        }
    }
    engine._states.restore(old_snap)
    t = run(engine.resolve(meta_f(umo=UMO)))
    # 不报错，正常走生命周期（round=3 → STAGE_2/g_s2）
    assert t.final_group_id == "g_s2"
    assert t.final_provider_id == "prov-b"
    st = run(states.get(UMO))
    assert st.calibration_rounds_left == 0
    assert st.calibration_group_id is None
    assert st.calibration_reason == ""


def test_dashboard_calibration_fields():
    """dashboard 输出 default_lifecycle 与 calibration_sessions。"""
    engine, adapter, store, states, slog = _setup_engine(default_lifecycle="lc_staged")

    async def _main():
        await states.update(
            UMO,
            calibration_rounds_left=3,
            calibration_group_id="g_cal",
            calibration_reason="context_compression",
        )
        await states.get("umo:group:2")  # 造一个无校准会话
        return engine.dashboard()

    dash = run(_main())
    assert dash["default_lifecycle"] == "lc_staged"
    assert dash["calibration_sessions"] == 1
