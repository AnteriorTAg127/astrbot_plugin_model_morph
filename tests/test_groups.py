"""groups 模块测试：5 策略、max_uses/cooldown/disabled/不存在、normalize、
CRUD、fallback。"""

import random

from conftest import make_store
from scheduler.groups import GROUP_STRATEGIES, ModelGroupManager, normalize_group
from scheduler.state import SessionState


def _group(**kw):
    base = {
        "id": "g1",
        "name": "G",
        "enabled": True,
        "strategy": "priority",
        "allow_auto_fallback": False,
        "providers": [],
        "fallbacks": [],
    }
    base.update(kw)
    return base


def _member(pid, priority=0, weight=1, max_uses=0, cooldown_seconds=0, enabled=True):
    return {
        "provider_id": pid,
        "model_override": "",
        "priority": priority,
        "weight": weight,
        "max_uses": max_uses,
        "cooldown_seconds": cooldown_seconds,
        "enabled": enabled,
        "note": "",
    }


def _select(store, group, available, rng=None):
    mgr = ModelGroupManager(store)
    state = SessionState(umo="u")
    return mgr.select_provider(group, state, set(available), rng=rng)


def test_priority_picks_lowest_priority():
    store = make_store()
    g = _group(
        strategy="priority",
        providers=[
            _member("p1", priority=3),
            _member("p2", priority=1),
            _member("p3", priority=2),
        ],
    )
    pid, reason = _select(store, g, ["p1", "p2", "p3"])
    assert pid == "p2"
    assert "priority" in reason


def test_round_robin_cycles():
    store = make_store()
    g = _group(
        strategy="round_robin",
        providers=[
            _member("p1"),
            _member("p2"),
            _member("p3"),
        ],
    )
    mgr = ModelGroupManager(store)
    state = SessionState(umo="u")
    chosen = []
    for _ in range(3):
        pid, _ = mgr.select_provider(g, state, {"p1", "p2", "p3"})
        chosen.append(pid)
    assert chosen == ["p1", "p2", "p3"]


def test_round_robin_resumes_from_cursor():
    store = make_store()
    g = _group(
        strategy="round_robin",
        providers=[
            _member("p1"),
            _member("p2"),
            _member("p3"),
        ],
    )
    mgr = ModelGroupManager(store)
    state = SessionState(umo="u")
    state.group_cursor[g["id"]] = {"rr": 1, "uses": {}, "cooldown_until": {}}
    pid, _ = mgr.select_provider(g, state, {"p1", "p2", "p3"})
    assert pid == "p2"


def test_weighted_uses_weights_with_rng():
    store = make_store()
    g = _group(
        strategy="weighted",
        providers=[
            _member("p_heavy", weight=100),
            _member("p_light", weight=1),
        ],
    )
    rng = random.Random(42)
    picks = [_select(store, g, ["p_heavy", "p_light"], rng=rng)[0] for _ in range(50)]
    assert picks.count("p_heavy") > picks.count("p_light")


def test_random_returns_available_member():
    store = make_store()
    g = _group(
        strategy="random",
        providers=[
            _member("p1"),
            _member("p2"),
        ],
    )
    rng = random.Random(7)
    pid, _ = _select(store, g, ["p1"], rng=rng)
    assert pid == "p1"  # 只有 p1 可用


def test_fallback_strategy_takes_first_usable():
    store = make_store()
    g = _group(
        strategy="fallback",
        providers=[
            _member("p1"),
            _member("p2"),
            _member("p3"),
        ],
    )
    pid, _ = _select(store, g, ["p2", "p3"])
    assert pid == "p2"  # p1 不在 available，取第一个可用 p2


def test_max_uses_skips_exhausted():
    store = make_store()
    g = _group(
        strategy="priority",
        providers=[
            _member("p1", priority=1, max_uses=1),
            _member("p2", priority=2),
        ],
    )
    mgr = ModelGroupManager(store)
    state = SessionState(umo="u")
    # 先用掉 p1 的 max_uses
    mgr.select_provider(g, state, {"p1", "p2"})
    state.group_cursor[g["id"]]["uses"]["p1"] = 1
    pid, _ = mgr.select_provider(g, state, {"p1", "p2"})
    assert pid == "p2"


def test_cooldown_skips_cooling_provider():
    import time

    store = make_store()
    g = _group(
        strategy="priority",
        providers=[
            _member("p1", priority=1, cooldown_seconds=100),
            _member("p2", priority=2),
        ],
    )
    mgr = ModelGroupManager(store)
    state = SessionState(umo="u")
    state.group_cursor[g["id"]] = {
        "rr": 0,
        "uses": {},
        "cooldown_until": {"p1": time.monotonic() + 1000},
    }
    pid, _ = mgr.select_provider(g, state, {"p1", "p2"})
    assert pid == "p2"


def test_disabled_member_skipped():
    store = make_store()
    g = _group(
        strategy="priority",
        providers=[
            _member("p1", priority=1, enabled=False),
            _member("p2", priority=2),
        ],
    )
    pid, _ = _select(store, g, ["p1", "p2"])
    assert pid == "p2"


def test_missing_provider_skipped():
    store = make_store()
    g = _group(
        strategy="priority",
        providers=[
            _member("p_ok", priority=1),
            _member("p_gone", priority=1),
        ],
    )
    pid, _ = _select(store, g, ["p_ok"])  # p_gone 不在 available → 跳过
    assert pid == "p_ok"


def test_all_candidates_filtered_returns_none():
    store = make_store()
    g = _group(
        strategy="priority",
        providers=[
            _member("p1", max_uses=1),
        ],
    )
    mgr = ModelGroupManager(store)
    state = SessionState(umo="u")
    state.group_cursor[g["id"]] = {"rr": 0, "uses": {"p1": 5}, "cooldown_until": {}}
    pid, reason = mgr.select_provider(g, state, {"p1"})
    assert pid is None
    assert reason


def test_no_candidates_returns_none():
    store = make_store()
    g = _group(strategy="priority", providers=[_member("p1")])
    pid, reason = _select(store, g, ["other"])
    assert pid is None
    assert reason


def test_normalize_defaults():
    g = normalize_group({"name": "X"})
    assert g["id"].startswith("g_")
    assert g["enabled"] is True
    assert g["strategy"] == "priority"
    assert g["allow_auto_fallback"] is False
    assert g["providers"] == []
    assert g["fallbacks"] == []


def test_normalize_invalid_entry_removed():
    g = normalize_group(
        {
            "providers": [
                _member("p1"),
                {"provider_id": ""},  # 非法 → 剔除
                "not-a-dict",  # 非 dict → 剔除
            ]
        }
    )
    assert [p["provider_id"] for p in g["providers"]] == ["p1"]


def test_normalize_invalid_strategy_falls_back():
    g = normalize_group({"strategy": "bogus"})
    assert g["strategy"] == "priority"


def test_normalize_number_coercion():
    g = normalize_group(
        {"providers": [_member("p1", priority="oops", weight="x", max_uses="bad")]}
    )
    p = g["providers"][0]
    assert p["priority"] == 0
    assert p["weight"] == 1
    assert p["max_uses"] == 0


def test_fallback_provider_ids_filters():
    store = make_store()
    mgr = ModelGroupManager(store)
    g = _group(fallbacks=["p1", "dup", "p1", "missing"])
    result = mgr.fallback_provider_ids(g, {"p1", "dup"})
    assert result == ["p1", "dup"]


def test_crud():
    store = make_store()
    mgr = ModelGroupManager(store)
    g = mgr.create(_group(name="A"))
    assert mgr.get(g["id"])["name"] == "A"
    upd = mgr.update_group(g["id"], {"name": "B"})
    assert upd["name"] == "B"
    dup = mgr.duplicate(g["id"])
    assert dup["name"] == "B(copy)"
    assert mgr.delete(g["id"]) is True
    assert mgr.delete(g["id"]) is False
    assert mgr.get(g["id"]) is None


def test_list_tracks_enabled():
    store = make_store()
    mgr = ModelGroupManager(store)
    mgr.create(_group(name="on"))
    mgr.create(_group(name="off", enabled=False))
    allg = mgr.list_()
    en = mgr.list_(only_enabled=True)
    assert len(allg) == 2
    assert all(g["enabled"] for g in en)
    assert len(en) == 1


def test_strategies_constant():
    assert GROUP_STRATEGIES == (
        "priority",
        "round_robin",
        "weighted",
        "random",
        "fallback",
    )
