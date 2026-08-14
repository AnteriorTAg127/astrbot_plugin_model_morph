"""temporal 模块测试：时间段 / 跨午夜 / 星期 / 指定日期 / 每规则时区 /
always / 优先级冲突 / 链式替换 / 环防护 / scope 过滤 / 缓存 / validate /
find_conflicts tie 与 shadowed。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import make_store
from scheduler.temporal import (
    PRIORITY_EMERGENCY,
    PRIORITY_GROUP,
    PRIORITY_MANUAL,
    PRIORITY_SCHEDULED,
    TemporalEngine,
    find_conflicts,
    normalize_temporal_rule,
    overlaps,
    parse_hhmm,
    rule_active,
)

TZ = ZoneInfo("Asia/Shanghai")


def _rule(**kw):
    """构造一条默认 model_override 规则（每日 20:00-23:00）。"""
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
        "priority": PRIORITY_SCHEDULED,
        "metadata": {"created_by": "", "created_at": "", "source": ""},
    }
    base.update(kw)
    return base


def _engine(rules=None, adapter=None):
    rules = rules or []
    store = make_store()
    if rules:
        store.save({**store.load(), "temporal_rules": rules})
    return TemporalEngine(store, adapter=adapter), store


def _at(hh, mm, tz=TZ, day=10):
    """构造带时区的判断时刻（默认 2026-06-10 周三）。"""
    return datetime(2026, 6, day, hh, mm, tzinfo=tz)


# ---- parse_hhmm / 基础常量 ----


def test_parse_hhmm_valid():
    assert parse_hhmm("08:30") == (8, 30)
    assert parse_hhmm("00:00") == (0, 0)
    assert parse_hhmm(" 09:05 ") == (9, 5)


def test_parse_hhmm_invalid():
    assert parse_hhmm("24:00") is None
    assert parse_hhmm("08:60") is None
    assert parse_hhmm("abc") is None
    assert parse_hhmm(None) is None
    assert parse_hhmm(123) is None


def test_priority_constants():
    assert PRIORITY_EMERGENCY == 1000
    assert PRIORITY_MANUAL == 500
    assert PRIORITY_SCHEDULED == 200
    assert PRIORITY_GROUP == 100
    assert PRIORITY_EMERGENCY > PRIORITY_MANUAL > PRIORITY_SCHEDULED > PRIORITY_GROUP


def test_normalize_defaults():
    r = normalize_temporal_rule({})
    assert r["id"].startswith("t_")
    assert r["enabled"] is True
    assert r["kind"] == "model_override"
    assert r["priority"] == PRIORITY_SCHEDULED
    assert r["scope"] == {"groups": [], "users": [], "sessions": []}
    assert r["schedule"]["type"] == "daily"
    # 不修改入参
    raw = {}
    normalize_temporal_rule(raw)
    assert raw == {}


# ---- rule_active：普通时间段 ----


def test_rule_active_daily_within():
    r = _rule(
        schedule={
            "type": "daily",
            "start": "20:00",
            "end": "23:00",
            "weekdays": [],
            "date": "",
            "timezone": "Asia/Shanghai",
        }
    )
    hit, reason = rule_active(r, _at(21, 0), TZ)
    assert hit is True
    assert "命中" in reason


def test_rule_active_daily_outside():
    r = _rule()
    hit, _ = rule_active(r, _at(14, 0), TZ)
    assert hit is False


# ---- 跨午夜 ----


def test_rule_active_cross_midnight():
    r = _rule(
        schedule={
            "type": "daily",
            "start": "23:00",
            "end": "08:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    assert rule_active(r, _at(23, 30), TZ)[0] is True
    assert rule_active(r, _at(2, 0), TZ)[0] is True
    assert rule_active(r, _at(14, 0), TZ)[0] is False
    assert rule_active(r, _at(22, 0), TZ)[0] is False


# ---- 星期 ----


def test_rule_active_weekday_filter():
    r = _rule(
        schedule={
            "type": "daily",
            "start": "09:00",
            "end": "11:00",
            "weekdays": [0, 1],
            "date": "",
            "timezone": "",
        }
    )
    # 2026-06-13 是周六(5)，不在 [0,1] → 不生效
    assert rule_active(r, _at(10, 0, day=13), TZ)[0] is False


def test_rule_active_weekly_requires_weekdays():
    r = _rule(
        schedule={
            "type": "weekly",
            "start": "09:00",
            "end": "11:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    hit, reason = rule_active(r, _at(10, 0), TZ)
    assert hit is False
    assert "weekdays" in reason


def test_rule_active_weekly_match():
    r = _rule(
        schedule={
            "type": "weekly",
            "start": "09:00",
            "end": "11:00",
            "weekdays": [2],
            "date": "",
            "timezone": "",
        }
    )
    # 2026-06-10 是周三(=2)
    assert rule_active(r, _at(10, 0), TZ)[0] is True
    assert rule_active(r, _at(10, 0, day=14), TZ)[0] is False  # 周日


# ---- 指定日期 ----


def test_rule_active_exact_date():
    r = _rule(
        schedule={
            "type": "date",
            "start": "08:00",
            "end": "12:00",
            "weekdays": [],
            "date": "2026-06-10",
            "timezone": "",
        }
    )
    assert rule_active(r, _at(9, 0), TZ)[0] is True
    assert rule_active(r, _at(13, 0), TZ)[0] is False
    assert rule_active(r, _at(9, 0, day=11), TZ)[0] is False


# ---- 每规则时区 ----


def test_rule_active_per_rule_timezone_shanghai():
    # 规则指定 Asia/Shanghai，但判断时刻用 Asia/Tokyo（快 1 小时）
    r = _rule(
        schedule={
            "type": "daily",
            "start": "22:00",
            "end": "23:00",
            "weekdays": [],
            "date": "",
            "timezone": "Asia/Shanghai",
        }
    )
    # 东京 23:00 = 上海 22:00 → 命中
    assert rule_active(r, _at(23, 0, tz=ZoneInfo("Asia/Tokyo")), TZ)[0] is True


def test_rule_active_per_rule_timezone_tokyo_vs_shanghai():
    # 窗口 22:00-23:00（Asia/Shanghai，端点含）。同一绝对时刻在不同「墙钟时区」下结果一致：
    # 东京 23:00 = 上海 22:00 → 命中；东京 00:30(次日) = 上海 23:30 → 不命中。
    window = {
        "type": "daily",
        "start": "22:00",
        "end": "23:00",
        "weekdays": [],
        "date": "",
        "timezone": "Asia/Shanghai",
    }
    r = _rule(schedule=dict(window, timezone="Asia/Shanghai"))
    # 东京 23:00 → astimezone 到上海 = 22:00 → 命中
    assert rule_active(r, _at(23, 0, tz=ZoneInfo("Asia/Tokyo")), TZ)[0] is True
    # 东京 00:30(次日) → 上海 23:30 → 不命中
    assert rule_active(r, _at(0, 30, tz=ZoneInfo("Asia/Tokyo"), day=11), TZ)[0] is False
    # 上海本地 23:30 → 同样不命中
    assert rule_active(_rule(schedule=window), _at(23, 30, tz=TZ), TZ)[0] is False


def test_rule_active_invalid_timezone_fallback():
    r = _rule(
        schedule={
            "type": "daily",
            "start": "20:00",
            "end": "23:00",
            "weekdays": [],
            "date": "",
            "timezone": "Not/AZone",
        }
    )
    hit, reason = rule_active(r, _at(21, 0), TZ)
    assert hit is True  # 回退默认时区仍生效
    assert "回退" in reason


# ---- always ----


def test_rule_active_always():
    r = _rule(
        schedule={
            "type": "always",
            "start": "",
            "end": "",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    assert rule_active(r, _at(0, 0), TZ)[0] is True
    assert rule_active(r, _at(23, 59), TZ)[0] is True


# ---- overlaps ----


def test_overlaps_daily_same_window():
    assert overlaps(_rule(), _rule(id="t_y")) is True


def test_overlaps_disjoint_windows():
    a = _rule(
        schedule={
            "type": "daily",
            "start": "08:00",
            "end": "09:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    b = _rule(
        schedule={
            "type": "daily",
            "start": "14:00",
            "end": "15:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    assert overlaps(a, b) is False


def test_overlaps_disjoint_weekdays():
    a = _rule(
        schedule={
            "type": "daily",
            "start": "08:00",
            "end": "09:00",
            "weekdays": [0],
            "date": "",
            "timezone": "",
        }
    )
    b = _rule(
        schedule={
            "type": "daily",
            "start": "08:00",
            "end": "09:00",
            "weekdays": [1],
            "date": "",
            "timezone": "",
        }
    )
    assert overlaps(a, b) is False


def test_overlaps_always_everything():
    always = _rule(
        schedule={
            "type": "always",
            "start": "",
            "end": "",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    other = _rule()
    assert overlaps(always, other) is True


def test_overlaps_cross_midnight():
    a = _rule(
        schedule={
            "type": "daily",
            "start": "23:00",
            "end": "02:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    b = _rule(
        schedule={
            "type": "daily",
            "start": "01:00",
            "end": "03:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    assert overlaps(a, b) is True  # 00:00-02:00 交叉


def test_overlaps_different_dates_conservative_not_detected():
    a = _rule(
        schedule={
            "type": "date",
            "start": "08:00",
            "end": "09:00",
            "weekdays": [],
            "date": "2026-06-10",
            "timezone": "",
        }
    )
    b = _rule(
        schedule={
            "type": "date",
            "start": "08:00",
            "end": "09:00",
            "weekdays": [],
            "date": "2026-06-11",
            "timezone": "",
        }
    )
    assert overlaps(a, b) is False


# ---- find_conflicts：tie 与 shadowed ----


def _fconflict_rules():
    a = _rule(id="a", priority=PRIORITY_SCHEDULED)
    # 同 kind/同组/同 source、时间窗重叠，但去向不同 → 冲突
    b = _rule(id="b", priority=PRIORITY_SCHEDULED, target_provider="qwen")
    return [a, b]


def test_find_conflicts_priority_tie():
    rules = _fconflict_rules()
    conflicts = find_conflicts(rules)
    assert len(conflicts) == 1
    assert {conflicts[0]["a"], conflicts[0]["b"]} == {"a", "b"}
    assert conflicts[0]["note"] == "priority_tie"


def test_find_conflicts_shadowed():
    a = _rule(id="a", priority=PRIORITY_EMERGENCY)
    b = _rule(id="b", priority=PRIORITY_SCHEDULED, target_provider="qwen")
    conflicts = find_conflicts([a, b])
    assert len(conflicts) == 1
    assert conflicts[0]["note"] == "shadowed"


def test_find_conflicts_no_conflict_same_target():
    a = _rule(id="a")
    b = _rule(id="b", target_provider="cheap")
    assert find_conflicts([a, b]) == []


def test_find_conflicts_no_conflict_different_source():
    a = _rule(id="a")
    b = _rule(id="b", source_provider="qwen")
    assert find_conflicts([a, b]) == []


# ---- TemporalEngine CRUD ----


def test_list_order_by_priority():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="lo", priority=PRIORITY_GROUP))
    eng.create(_rule(id="hi", priority=PRIORITY_MANUAL))
    ids = [r["id"] for r in eng.list_()]
    assert ids == ["hi", "lo"]


def test_get_missing():
    store = make_store()
    eng = TemporalEngine(store)
    assert eng.get("t_nope") is None


def test_create_and_get():
    store = make_store()
    eng = TemporalEngine(store)
    created = eng.create(_rule(name="N"))
    assert created["name"] == "N"
    assert eng.get(created["id"])["name"] == "N"
    # 已持久化
    assert eng.list_()[0]["name"] == "N"


def test_create_validation_fails_raises():
    store = make_store()
    eng = TemporalEngine(store)
    bad = _rule(source_provider="", target_provider="")  # 自引用 / 缺字段
    try:
        eng.create(bad)
        raise AssertionError("应抛出 ValueError")
    except ValueError as exc:
        assert isinstance(str(exc), str) and str(exc)
    # 未写入
    assert eng.list_() == []


def test_update_rule():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="up", name="Old"))
    eng.update_rule("up", {"name": "New"})
    assert eng.get("up")["name"] == "New"
    assert eng.update_rule("t_missing", {}) is None


def test_delete():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="del"))
    assert eng.delete("del") is True
    assert eng.delete("del") is False
    assert eng.list_() == []


def test_toggle():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="tg"))
    # 默认 enabled
    assert eng.get("tg")["enabled"] is True
    eng.toggle("tg")  # 取反 → False
    assert eng.get("tg")["enabled"] is False
    eng.toggle("tg", True)
    assert eng.get("tg")["enabled"] is True


# ---- 生效 / 优先级 ----


def test_active_rules_filters_time_and_enabled():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(
        _rule(
            id="in",
            schedule={
                "type": "daily",
                "start": "20:00",
                "end": "23:00",
                "weekdays": [],
                "date": "",
                "timezone": "",
            },
        )
    )
    eng.create(
        _rule(
            id="out",
            schedule={
                "type": "daily",
                "start": "08:00",
                "end": "09:00",
                "weekdays": [],
                "date": "",
                "timezone": "",
            },
        )
    )
    active = eng.active_rules(_at(21, 0), TZ)
    assert [r["id"] for r in active] == ["in"]


def test_resolve_model_priority_higher_wins():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="low", priority=PRIORITY_GROUP))
    eng.create(_rule(id="high", priority=PRIORITY_MANUAL))
    final, matched, chain, _ = eng.resolve_model("", "deepseek", _at(21, 0), TZ, {})
    assert final == "cheap"
    assert matched["id"] == "high"  # 高优先级命中
    assert chain == ["deepseek", "cheap"]


def test_resolve_model_group_match():
    store = make_store()
    eng = TemporalEngine(store)
    # 仅对 default-chat 组生效
    eng.create(_rule(id="g1", group_id="default-chat"))
    # 其他组不受影响
    final, matched, _, _ = eng.resolve_model("other", "deepseek", _at(21, 0), TZ, {})
    assert final == "deepseek"
    assert matched is None


def test_resolve_model_chained():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="ab", source_provider="A", target_provider="B"))
    eng.create(_rule(id="bc", source_provider="B", target_provider="C"))
    final, _, chain, _ = eng.resolve_model("", "A", _at(21, 0), TZ, {})
    assert final == "C"
    assert chain[0] == "A" and chain[-1] == "C"


def test_resolve_model_cycle_stops():
    # 环（A→B、B→A）无法经 create() 写入（validate 拒绝），视为手工/旧配置注入：
    # 直接落到存储，验证运行时 resolve 的环防护（停在某值并标注）。
    eng, _store = _engine(
        [
            _rule(id="ab", source_provider="A", target_provider="B"),
            _rule(id="ba", source_provider="B", target_provider="A"),
        ]
    )
    final, _, _, reason = eng.resolve_model("", "A", _at(21, 0), TZ, {})
    # 环防护：停在某个值并标注
    assert final in ("A", "B")
    assert "替换环" in reason


def test_resolve_group_switch():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="gs", kind="group_switch", group_id="g_a", target_group="g_b"))
    new_group, matched = eng.resolve_group("g_a", _at(21, 0), TZ, {})
    assert new_group == "g_b"
    assert matched is not None
    # 未匹配组保持原值
    assert eng.resolve_group("g_z", _at(21, 0), TZ, {}) == ("g_z", None)


# ---- scope 过滤 ----


def test_scope_group_user_session():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(
        _rule(id="grp", scope={"groups": ["12345"], "users": [], "sessions": []})
    )
    eng.create(_rule(id="usr", scope={"groups": [], "users": ["u1"], "sessions": []}))
    eng.create(_rule(id="ses", scope={"groups": [], "users": [], "sessions": ["s-9"]}))

    r, _, _, _ = eng.resolve_model(
        "12345",
        "deepseek",
        _at(21, 0),
        TZ,
        {"group_id": "12345", "sender_id": "x", "umo": "p"},
    )
    assert r == "cheap"
    # group 不匹配但 user 命中
    r, _, _, _ = eng.resolve_model(
        "nope",
        "deepseek",
        _at(21, 0),
        TZ,
        {"group_id": "nope", "sender_id": "u1", "umo": "p"},
    )
    assert r == "cheap"
    # session 命中
    r, _, _, _ = eng.resolve_model(
        "nope",
        "deepseek",
        _at(21, 0),
        TZ,
        {"group_id": "nope", "sender_id": "x", "umo": "s-9"},
    )
    assert r == "cheap"
    # 全不命中 → 保持原值
    r, _, chain, _ = eng.resolve_model(
        "nope",
        "deepseek",
        _at(21, 0),
        TZ,
        {"group_id": "nope", "sender_id": "x", "umo": "other"},
    )
    assert r == "deepseek"
    assert chain == []


def test_scope_all_empty_unrestricted():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="free", scope={"groups": [], "users": [], "sessions": []}))
    r, _, _, _ = eng.resolve_model(
        "anything",
        "deepseek",
        _at(21, 0),
        TZ,
        {"group_id": "anything", "sender_id": "x", "umo": "p"},
    )
    assert r == "cheap"


# ---- 缓存 ----


def test_cache_same_minute_same_revision_same_object():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="c1"))
    a = eng.active_rules(_at(21, 0), TZ)
    b = eng.active_rules(_at(21, 0), TZ)
    assert a is b  # 同分钟同 revision 返回同对象


def test_cache_invalidates_after_create():
    store = make_store()
    eng = TemporalEngine(store)
    assert eng.active_rules(_at(21, 0), TZ) == []
    eng.create(_rule(id="new1"))
    # create 后缓存失效，新规则立即生效
    ids = [r["id"] for r in eng.active_rules(_at(21, 0), TZ)]
    assert ids == ["new1"]


def test_cache_key_changes_on_minute():
    store = make_store()
    eng = TemporalEngine(store)
    eng.create(_rule(id="m1"))
    first = eng.active_rules(_at(21, 0), TZ)
    second = eng.active_rules(_at(21, 1), TZ)
    assert first is not second  # 分钟变化 → 重算


# ---- validate：各错误分支 ----


def test_validate_empty_fails():
    store = make_store()
    eng = TemporalEngine(store)
    assert eng.validate(None)["ok"] is False
    assert eng.validate({})["ok"] is False


def test_validate_bad_kind():
    store = make_store()
    eng = TemporalEngine(store)
    assert eng.validate(_rule(kind="bogus"))["ok"] is False


def test_validate_missing_fields_and_self_ref():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(source_provider="x", target_provider="x")  # 自引用
    res = eng.validate(r)
    assert res["ok"] is False
    assert any("自引用" in e for e in res["errors"])


def test_validate_bad_hhmm():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(
        schedule={
            "type": "daily",
            "start": "25:00",
            "end": "23:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    res = eng.validate(r)
    assert not res["ok"]
    assert any("start" in e for e in res["errors"])


def test_validate_bad_timezone():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(
        schedule={
            "type": "daily",
            "start": "20:00",
            "end": "23:00",
            "weekdays": [],
            "date": "",
            "timezone": "Mars/Olympus",
        }
    )
    res = eng.validate(r)
    assert not res["ok"]
    assert any("时区" in e for e in res["errors"])


def test_validate_bad_date_format():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(
        schedule={
            "type": "date",
            "start": "08:00",
            "end": "09:00",
            "weekdays": [],
            "date": "06-10-2026",
            "timezone": "",
        }
    )
    res = eng.validate(r)
    assert not res["ok"]
    assert any("YYYY-MM-DD" in e for e in res["errors"])


def test_validate_weekly_requires_weekdays():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(
        schedule={
            "type": "weekly",
            "start": "08:00",
            "end": "09:00",
            "weekdays": [],
            "date": "",
            "timezone": "",
        }
    )
    res = eng.validate(r)
    assert not res["ok"]
    assert any("weekdays" in e for e in res["errors"])


def test_validate_priority_non_int():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(priority="nope")
    res = eng.validate(r)
    assert not res["ok"]
    assert any("整数" in e for e in res["errors"])


def test_validate_unknown_provider_group():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(source_provider="ghost", target_provider="phantom")
    res = eng.validate(r, known_provider_ids={"reala", "realb"})
    assert not res["ok"]
    assert any("source_provider" in e for e in res["errors"])
    assert any("target_provider" in e for e in res["errors"])


def test_validate_unknown_group_ids():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule(kind="group_switch", group_id="g_miss", target_group="g_ok")
    res = eng.validate(r, known_group_ids={"g_ok"})
    assert not res["ok"]
    assert any("group_id" in e for e in res["errors"])


def test_validate_cycle_with_existing():
    eng, _store = _engine([_rule(id="ab", source_provider="A", target_provider="B")])
    # 新规则 B→A 与已有 A→B 成环 → error
    res = eng.validate(_rule(id="new", source_provider="B", target_provider="A"))
    assert not res["ok"]
    assert any("替换环" in e for e in res["errors"])


def test_validate_conflict_warning():
    # 已有 ex 与候选 new：同 source、同时间窗但 target 不同 → warning（非 error）
    eng, _store = _engine(
        [_rule(id="ex", priority=PRIORITY_SCHEDULED, target_provider="qwen")]
    )
    res = eng.validate(_rule(id="new", priority=PRIORITY_MANUAL))
    assert res["ok"] is True  # 冲突非错误
    assert res["warnings"]


def test_validate_valid_no_conflict():
    store = make_store()
    eng = TemporalEngine(store)
    r = _rule()
    res = eng.validate(r)
    assert res["ok"] is True
    assert res["errors"] == []


# ---- 每规则时区独立生效（Asia/Tokyo vs Asia/Shanghai 同时刻不同结果）----


def test_per_rule_timezone_different_activeness():
    store = make_store()
    eng = TemporalEngine(store)
    # 两条规则窗口相同（22:00-23:00），时区一个上海、一个东京
    eng.create(
        _rule(
            id="sh",
            schedule={
                "type": "daily",
                "start": "22:00",
                "end": "23:00",
                "weekdays": [],
                "date": "",
                "timezone": "Asia/Shanghai",
            },
        )
    )
    eng.create(
        _rule(
            id="tk",
            schedule={
                "type": "daily",
                "start": "22:00",
                "end": "23:00",
                "weekdays": [],
                "date": "",
                "timezone": "Asia/Tokyo",
            },
        )
    )
    # 上海 22:30：sh 命中、tk 不命中（东京已 23:30）
    ids = [r["id"] for r in eng.active_rules(_at(22, 30, tz=TZ), TZ)]
    assert ids == ["sh"]
    # 东京 22:30：tk 命中、sh 不命中（上海 21:30）
    ids = [
        r["id"] for r in eng.active_rules(_at(22, 30, tz=ZoneInfo("Asia/Tokyo")), TZ)
    ]
    assert ids == ["tk"]
