"""lifecycle 模块测试：状态机 INITIAL→MAIN→PERIODIC、initial_rounds=0、
periodic_interval=0、main_group 空、模板、normalize（含 v0.1.6 stages 多阶段降级）。"""

from conftest import make_store
from scheduler.lifecycle import (
    LIFECYCLE_TEMPLATES,
    LifecycleEngine,
    normalize_lifecycle,
    should_trigger_compression,
)
from scheduler.state import SessionState


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


def _store_with(lcs):
    store = make_store()
    store.update("lifecycles", lcs)
    return store


def _decide(store, lc, round_):
    eng = LifecycleEngine(store)
    state = SessionState(umo="u", round=round_)
    return eng.decide_group(lc, state)


def test_initial_then_main():
    store = _store_with([_lc()])
    init0 = _decide(store, _lc(), 0)
    init1 = _decide(store, _lc(), 1)
    # 1 基轮次 t=r+1：initial_rounds=2 → t<=2 (r=0,1) INITIAL；
    # t=3 即紧邻轮(r=2)仍为 MAIN（首个 PERIODIC 在 t=7, r=6）
    main2 = _decide(store, _lc(), 2)
    per6 = _decide(store, _lc(), 6)
    assert init0[1] == "INITIAL" and init0[0] == "g_init"
    assert init1[1] == "INITIAL" and init1[0] == "g_init"
    assert main2[1] == "MAIN" and main2[0] == "g_main"
    assert per6[1] == "PERIODIC" and per6[0] == "g_per"


def test_periodic_insertion():
    store = _store_with([_lc(initial_rounds=2, periodic_interval=5)])
    # 1 基 t=r+1：(t-2) % 5 == 0 → t = 7,12 → r = 6,11 PERIODIC；其余 MAIN
    for r in (2, 3, 4, 5, 7, 8, 9, 10):
        stage = _decide(store, _lc(initial_rounds=2, periodic_interval=5), r)[1]
        assert stage == "MAIN", f"round {r} 应为 MAIN"
    for r in (6, 11):
        res = _decide(store, _lc(initial_rounds=2, periodic_interval=5), r)
        assert res[1] == "PERIODIC" and res[0] == "g_per"


def test_decide_sequence_r0_to_12():
    # 需求示例 B（initial=3 / interval=5）：t=r+1
    # t<=3 INITIAL(r=0,1,2)；t=8 PERIODIC(r=7)；t=13 PERIODIC(r=12)；其余 MAIN
    for r in range(0, 13):
        res = _decide(
            _store_with([_lc(initial_rounds=3, periodic_interval=5)]),
            _lc(initial_rounds=3, periodic_interval=5),
            r,
        )
        if r in (0, 1, 2):
            assert res[1] == "INITIAL" and res[0] == "g_init", f"round {r}: {res}"
        elif r in (7, 12):
            assert res[1] == "PERIODIC" and res[0] == "g_per", f"round {r}: {res}"
        else:
            assert res[1] == "MAIN" and res[0] == "g_main", f"round {r}: {res}"


def test_initial_rounds_zero():
    store = _store_with([_lc(initial_rounds=0, periodic_interval=0)])
    res = _decide(store, _lc(initial_rounds=0, periodic_interval=0), 0)
    assert res[1] == "MAIN"
    assert res[0] == "g_main"


def test_periodic_interval_zero_disabled():
    store = _store_with([_lc(periodic_interval=0, periodic_group="g_per")])
    for r in (0, 3, 7):
        res = _decide(store, _lc(periodic_interval=0), r)
        assert res[1] != "PERIODIC"


def test_main_group_empty_returns_none():
    store = _store_with([_lc(main_group="")])
    res = _decide(store, _lc(main_group=""), 5)
    assert res[0] is None
    assert res[1] == "MAIN"
    assert "no main group" in res[2]


def test_stage_written_to_state():
    store = _store_with([_lc()])
    eng = LifecycleEngine(store)
    state = SessionState(umo="u", round=1)
    eng.decide_group(_lc(), state)
    assert state.stage == "INITIAL"


def test_templates_exist():
    for key in ("balanced", "quality", "cost_saving", "new_conversation"):
        assert key in LIFECYCLE_TEMPLATES
        t = LIFECYCLE_TEMPLATES[key]
        assert isinstance(t["initial_rounds"], int)
        assert isinstance(t["periodic_interval"], int)


def test_normalize_defaults():
    lc = normalize_lifecycle({"name": "X"})
    assert lc["id"].startswith("lc_")
    assert lc["enabled"] is True
    assert lc["initial_group"] == ""
    assert lc["main_group"] == ""
    assert lc["initial_rounds"] == 0
    assert lc["periodic_interval"] == 0


def test_normalize_negative_coerced():
    lc = normalize_lifecycle({"initial_rounds": -3, "periodic_interval": "abc"})
    assert lc["initial_rounds"] == 0
    assert lc["periodic_interval"] == 0


def test_crud():
    store = make_store()
    eng = LifecycleEngine(store)
    created = eng.create({"name": "LC1", "initial_rounds": 3})
    lid = created["id"]
    assert eng.get(lid)["name"] == "LC1"
    upd = eng.update(lid, {"name": "LC2"})
    assert upd["name"] == "LC2"
    dup = eng.duplicate(lid)
    assert dup["name"] == "LC2 (copy)"
    assert eng.delete(lid) is True
    assert eng.delete(lid) is False


# ---- v0.1.6：stages 多阶段降级 / 事件校准 / 压缩检测 ----


def _staged_lc(**kw):
    base = {
        "id": "lc_staged",
        "name": "Staged",
        "enabled": True,
        "stages": [
            {"group_id": "g_a", "rounds": 3},
            {"group_id": "g_b", "rounds": 5},
        ],
        "final_group": "g_final",
        "periodic_group": "",
        "periodic_interval": 0,
    }
    base.update(kw)
    return base


def _decide_g(store, lc, round_, umo="u"):
    """decide_group 便捷封装，返回 (group, stage, reason)。"""
    eng = LifecycleEngine(store)
    state = SessionState(umo=umo, round=round_)
    return eng.decide_group(lc, state)


def test_staged_boundaries():
    # stages=[g_a:3, g_b:5]（总 8）→ round 0/1/2 STAGE_1，3..7 STAGE_2，>=8 耗尽走 final_group。
    lc = _staged_lc()
    store = _store_with([lc])
    assert _decide_g(store, lc, 0)[:2] == ("g_a", "STAGE_1")
    assert _decide_g(store, lc, 1)[:2] == ("g_a", "STAGE_1")
    assert _decide_g(store, lc, 3)[:2] == ("g_b", "STAGE_2")
    assert _decide_g(store, lc, 4)[:2] == ("g_b", "STAGE_2")
    assert _decide_g(store, lc, 8)[:2] == ("g_final", "MAIN")
    assert _decide_g(store, lc, 9)[:2] == ("g_final", "MAIN")


def test_staged_group_and_stage_tags():
    lc = _staged_lc()
    store = _store_with([lc])
    g0, s0, _ = _decide_g(store, lc, 0)
    g3, s3, _ = _decide_g(store, lc, 3)
    g8, s8, _ = _decide_g(store, lc, 8)
    assert (g0, s0) == ("g_a", "STAGE_1")
    assert (g3, s3) == ("g_b", "STAGE_2")
    assert (g8, s8) == ("g_final", "MAIN")


def test_staged_final_group_empty_returns_none():
    # final_group 为空 → 耗尽时返回 (None, "MAIN", "final_group 未配置")
    lc = _staged_lc(final_group="")
    store = _store_with([lc])
    res = _decide_g(store, lc, 9)
    assert res[0] is None
    assert res[1] == "MAIN"
    assert "final_group" in res[2]
    # round 8 已耗尽同样返回 None
    res8 = _decide_g(store, lc, 8)
    assert res8[0] is None and res8[1] == "MAIN"


def test_staged_periodic():
    # staged 模式 periodic 优先：interval=15，round 15/30 命中 PERIODIC，round 16 回阶段组。
    lc = _staged_lc(
        stages=[{"group_id": "g_a", "rounds": 15}, {"group_id": "g_b", "rounds": 100}],
        periodic_group="g_per",
        periodic_interval=15,
    )
    store = _store_with([lc])
    r15 = _decide_g(store, lc, 15)
    r30 = _decide_g(store, lc, 30)
    assert r15[0] == "g_per" and r15[1] == "PERIODIC"
    assert r30[0] == "g_per" and r30[1] == "PERIODIC"
    # round 16：16%15!=0 → 回阶段组（round>=15 → STAGE_2）
    r16 = _decide_g(store, lc, 16)
    assert r16[0] == "g_b" and r16[1] == "STAGE_2"
    # round 0 不满足 round>0，不触发 periodic
    r0 = _decide_g(store, lc, 0)
    assert r0[0] == "g_a" and r0[1] == "STAGE_1"


def test_staged_periodic_at_zero_interval_disabled():
    # interval=0 时不触发 periodic（staged 模式仍正常分阶段）
    lc = _staged_lc(periodic_group="g_per", periodic_interval=0)
    store = _store_with([lc])
    res = _decide_g(store, lc, 6)
    assert res[1] == "STAGE_2"  # round 6 < 8 → STAGE_2
    assert res[0] == "g_b"


def test_staged_stage_written_to_state():
    store = _store_with([_staged_lc()])
    eng = LifecycleEngine(store)
    state = SessionState(umo="u", round=4)
    eng.decide_group(_staged_lc(), state)
    assert state.stage == "STAGE_2"


def test_legacy_regression_unchanged():
    # 回归确认：stages 为空时完全走 legacy 逻辑（initial/periodic 旧公式）。
    store = _store_with([_lc(initial_rounds=2, periodic_interval=5)])
    assert (
        _decide_g(store, _lc(initial_rounds=2, periodic_interval=5), 0)[1] == "INITIAL"
    )
    assert (
        _decide_g(store, _lc(initial_rounds=2, periodic_interval=5), 6)[1] == "PERIODIC"
    )
    assert _decide_g(store, _lc(initial_rounds=2, periodic_interval=5), 4)[1] == "MAIN"


def test_normalize_stages_default_and_fields():
    lc = normalize_lifecycle({"name": "X"})
    assert lc["stages"] == []
    assert lc["final_group"] == ""
    assert lc["calibration_event"] == ""
    assert lc["calibration_group"] == ""
    assert lc["calibration_rounds"] == 0


def test_normalize_stages_drops_invalid_entries():
    raw = {
        "stages": [
            {"group_id": "g_a", "rounds": 3},
            {"group_id": "g_b", "rounds": 0},  # rounds<=0 → 剔除
            {"group_id": "", "rounds": 2},  # 缺 group_id → 剔除
            {"group_id": "g_c"},  # 缺 rounds → 剔除
            {"group_id": "g_d", "rounds": 5},
            "not-a-dict",  # 非 dict → 剔除
        ]
    }
    lc = normalize_lifecycle(raw)
    assert lc["stages"] == [
        {"group_id": "g_a", "rounds": 3},
        {"group_id": "g_d", "rounds": 5},
    ]


def test_normalize_stages_non_list():
    lc = normalize_lifecycle({"stages": "oops"})
    assert lc["stages"] == []


def test_normalize_calibration_event_fallback():
    assert (
        normalize_lifecycle({"calibration_event": "context_compression"})[
            "calibration_event"
        ]
        == "context_compression"
    )
    # 非法值回退为空
    assert (
        normalize_lifecycle({"calibration_event": "something_else"})[
            "calibration_event"
        ]
        == ""
    )


def test_calibration_config_branches():
    eng = LifecycleEngine(make_store())
    # 全部满足 → 返回 dict
    cfg = eng.calibration_config(
        {
            "calibration_event": "context_compression",
            "calibration_group": "g_cal",
            "calibration_rounds": 5,
        }
    )
    assert cfg == {"event": "context_compression", "group_id": "g_cal", "rounds": 5}
    # 事件为空 → None
    assert (
        eng.calibration_config(
            {
                "calibration_event": "",
                "calibration_group": "g_cal",
                "calibration_rounds": 5,
            }
        )
        is None
    )
    # 组为空 → None
    assert (
        eng.calibration_config(
            {
                "calibration_event": "context_compression",
                "calibration_group": "",
                "calibration_rounds": 5,
            }
        )
        is None
    )
    # 轮数<=0 → None
    assert (
        eng.calibration_config(
            {
                "calibration_event": "context_compression",
                "calibration_group": "g_cal",
                "calibration_rounds": 0,
            }
        )
        is None
    )
    # 非 dict → None
    assert eng.calibration_config(None) is None


def test_should_trigger_compression_boundaries():
    # prev<2000 不触发
    assert should_trigger_compression(1999, 100) is False
    # prev=2000：cur < 2000*0.6=1200 才触发；相等不触发
    assert should_trigger_compression(2000, 1200) is False  # 1200 >= 1200 → 不触发
    assert should_trigger_compression(2000, 1199) is True  # 1199 < 1200 → 触发
    # cur >= prev*0.6 不触发（相等边界）
    assert should_trigger_compression(3000, 1800) is False  # 1800 >= 1800
    assert should_trigger_compression(3000, 1799) is True  # 1799 < 1800
    # 骤降触发
    assert should_trigger_compression(5000, 2000) is True
    # 非法输入 False
    assert should_trigger_compression("abc", 100) is False
    assert should_trigger_compression(None, 100) is False
    assert should_trigger_compression(5000, None) is False
