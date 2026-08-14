"""rules 模块测试：time_range/date_weekday/scope/keyword/command/at_bot/
message_type/round_gte/context_length_gte/lifecycle_event、AND/OR、优先级、
空条件、未知类型。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import make_store
from scheduler.rules import RuleContext, RuleEngine

TZ = ZoneInfo("Asia/Shanghai")


def _ctx(**kw):
    base = {
        "now": datetime(2026, 6, 10, 14, 0, tzinfo=TZ),  # 周三 14:00
        "tz": TZ,
        "umo": "u1",
        "platform_id": "pi",
        "platform_name": "aiocqhttp",
        "group_id": "12345",
        "sender_id": "user1",
        "is_group": True,
        "message_type": "group",
        "message_str": "",
        "at_bot": False,
        "round": 0,
        "context_length": 0,
        "lifecycle_event": "",
    }
    base.update(kw)
    return RuleContext(**base)


def _rule(conditions, op="and", **kw):
    base = {
        "id": "r1",
        "name": "R",
        "enabled": True,
        "priority": 0,
        "scope": {
            "groups": [],
            "users": [],
            "sessions": [],
            "platforms": [],
            "exclude_groups": [],
            "exclude_users": [],
        },
        "when": {"op": op, "conditions": conditions},
        "then": {"action": "switch_group", "group_id": "g1"},
    }
    base.update(kw)
    return base


def _store_with(rules):
    store = make_store()
    store.update("rules", rules)
    return store


def _ev(rules, ctx):
    store = _store_with(rules)
    return RuleEngine(store).evaluate(ctx)


def _matched(eval_res):
    m = eval_res.get("matched_rule")
    return m.get("id") if m else None


def test_time_range_match():
    ctx = _ctx(now=datetime(2026, 6, 10, 10, 0, tzinfo=TZ))
    r = _rule(
        [{"type": "time_range", "start": "09:00", "end": "11:00", "weekdays": []}]
    )
    assert _matched(_ev([r], ctx)) == "r1"


def test_time_range_no_match():
    ctx = _ctx(now=datetime(2026, 6, 10, 14, 0, tzinfo=TZ))
    r = _rule(
        [{"type": "time_range", "start": "09:00", "end": "11:00", "weekdays": []}]
    )
    assert _matched(_ev([r], ctx)) is None


def test_time_range_cross_midnight():
    # 23:00-08:00 跨午夜：23:00 及次日 0-7 命中；22:00 与 14:00 不命中
    r = _rule(
        [{"type": "time_range", "start": "23:00", "end": "08:00", "weekdays": []}]
    )
    for hour, expected in [(23, True), (2, True), (22, False), (14, False)]:
        ctx = _ctx(now=datetime(2026, 6, 10, hour, 30, tzinfo=TZ))
        assert (_matched(_ev([r], ctx)) == "r1") is expected


def test_time_range_weekday_filter():
    ctx = _ctx(now=datetime(2026, 6, 13, 10, 0, tzinfo=TZ))  # 周六
    r = _rule(
        [{"type": "time_range", "start": "09:00", "end": "11:00", "weekdays": [0, 1]}]
    )  # 仅周一二
    assert _matched(_ev([r], ctx)) is None


def test_date_weekday_workday():
    r = _rule([{"type": "date_weekday", "mode": "workday"}])
    work = _ctx(now=datetime(2026, 6, 10, 10, 0, tzinfo=TZ))  # 周三
    weekend = _ctx(now=datetime(2026, 6, 13, 10, 0, tzinfo=TZ))  # 周六
    assert _matched(_ev([r], work)) == "r1"
    assert _matched(_ev([r], weekend)) is None


def test_date_weekday_weekend():
    r = _rule([{"type": "date_weekday", "mode": "weekend"}])
    weekend = _ctx(now=datetime(2026, 6, 13, 10, 0, tzinfo=TZ))
    assert _matched(_ev([r], weekend)) == "r1"


def test_date_weekday_exact_date():
    r = _rule([{"type": "date_weekday", "date": "2026-06-10"}])
    match = _ctx(now=datetime(2026, 6, 10, 1, 0, tzinfo=TZ))
    miss = _ctx(now=datetime(2026, 6, 11, 1, 0, tzinfo=TZ))
    assert _matched(_ev([r], match)) == "r1"
    assert _matched(_ev([r], miss)) is None


def test_date_weekday_days_list():
    r = _rule([{"type": "date_weekday", "days": [2]}])  # 周三=2
    ctx = _ctx(now=datetime(2026, 6, 10, 10, 0, tzinfo=TZ))
    assert _matched(_ev([r], ctx)) == "r1"


def test_keyword_contains_case_insensitive():
    r = _rule([{"type": "keyword", "mode": "contains", "keywords": ["HeLLo"]}])
    ctx = _ctx(message_str="say hello world")
    assert _matched(_ev([r], ctx)) == "r1"
    ctx2 = _ctx(message_str="goodbye")
    assert _matched(_ev([r], ctx2)) is None


def test_keyword_prefix():
    r = _rule([{"type": "keyword", "mode": "prefix", "keywords": ["morph"]}])
    assert _matched(_ev([r], _ctx(message_str="morph now"))) == "r1"
    assert _matched(_ev([r], _ctx(message_str="xxmorph"))) is None


def test_command():
    r = _rule([{"type": "command", "commands": ["/tool"]}])
    # 单词边界前缀匹配：消息==命令 或 命令后接空白边界 → 命中
    assert _matched(_ev([r], _ctx(message_str="/tool"))) == "r1"
    assert (
        _matched(_ev([r], _ctx(message_str="/tool run"))) == "r1"
    )  # 带参数，空格边界命中
    assert _matched(_ev([r], _ctx(message_str="/tool   "))) == "r1"
    assert (
        _matched(_ev([r], _ctx(message_str="/tools"))) is None
    )  # 前缀但非单词边界，不命中
    assert (
        _matched(_ev([r], _ctx(message_str="/toolx"))) is None
    )  # 前缀但非单词边界，不命中
    assert _matched(_ev([r], _ctx(message_str="note: /tool"))) is None


def test_at_bot():
    r_true = _rule([{"type": "at_bot", "value": True}])
    assert _matched(_ev([r_true], _ctx(at_bot=True))) == "r1"
    assert _matched(_ev([r_true], _ctx(at_bot=False))) is None


def test_message_type():
    r = _rule([{"type": "message_type", "value": "private"}])
    assert _matched(_ev([r], _ctx(message_type="private"))) == "r1"
    assert _matched(_ev([r], _ctx(message_type="group"))) is None


def test_round_gte():
    r = _rule([{"type": "round_gte", "value": 5}])
    assert _matched(_ev([r], _ctx(round=5)))  # 等于达标
    assert _matched(_ev([r], _ctx(round=4))) is None


def test_context_length_gte():
    r = _rule([{"type": "context_length_gte", "value": 1000}])
    assert _matched(_ev([r], _ctx(context_length=2000))) == "r1"
    assert _matched(_ev([r], _ctx(context_length=500))) is None


def test_lifecycle_event():
    r = _rule([{"type": "lifecycle_event", "event": "reset"}])
    assert _matched(_ev([r], _ctx(lifecycle_event="reset"))) == "r1"
    assert _matched(_ev([r], _ctx(lifecycle_event="new"))) is None
    assert _matched(_ev([r], _ctx(lifecycle_event=""))) is None


def test_scope_include_group():
    r = _rule([{"type": "scope", "groups": ["12345"]}])
    assert _matched(_ev([r], _ctx(group_id="12345"))) == "r1"
    assert _matched(_ev([r], _ctx(group_id="999"))) is None


def test_scope_include_user():
    r = _rule([{"type": "scope", "users": ["user1"]}])
    assert _matched(_ev([r], _ctx(sender_id="user1"))) == "r1"


def test_scope_include_session():
    r = _rule([{"type": "scope", "sessions": ["u1"]}])
    assert _matched(_ev([r], _ctx(umo="u1"))) == "r1"


def test_scope_include_platform():
    r = _rule([{"type": "scope", "platforms": ["aiocqhttp"]}])
    assert _matched(_ev([r], _ctx(platform_name="aiocqhttp"))) == "r1"
    assert _matched(_ev([r], _ctx(platform_id="pi"))) == "r1"  # platform_id 也匹配


def test_scope_exclude():
    r = _rule([{"type": "scope", "exclude_groups": ["12345"]}])
    assert _matched(_ev([r], _ctx(group_id="12345"))) is None
    assert _matched(_ev([r], _ctx(group_id="888"))) == "r1"  # include 全空=不限制


def test_scope_all_empty_unrestricted():
    r = _rule(
        [{"type": "scope", "groups": [], "users": [], "sessions": [], "platforms": []}]
    )
    assert _matched(_ev([r], _ctx(group_id="anything"))) == "r1"


def test_and_all_conditions_must_match():
    r = _rule(
        [{"type": "at_bot", "value": True}, {"type": "message_type", "value": "group"}],
        op="and",
    )
    assert _matched(_ev([r], _ctx(at_bot=True, message_type="group"))) == "r1"
    assert _matched(_ev([r], _ctx(at_bot=False, message_type="group"))) is None


def test_or_any_condition_matches():
    r = _rule(
        [
            {"type": "at_bot", "value": True},
            {"type": "message_type", "value": "private"},
        ],
        op="or",
    )
    assert _matched(_ev([r], _ctx(at_bot=True, message_type="group"))) == "r1"
    assert _matched(_ev([r], _ctx(at_bot=False, message_type="group"))) is None


def test_priority_higher_wins():
    low = _rule([{"type": "at_bot", "value": True}], id="low_p", priority=1)
    high = _rule([{"type": "at_bot", "value": True}], id="high_p", priority=100)
    assert _matched(_ev([low, high], _ctx(at_bot=True))) == "high_p"


def test_empty_conditions_matches():
    r = _rule([], op="and")
    assert _matched(_ev([r], _ctx())) == "r1"


def test_unknown_condition_not_matched():
    r = _rule([{"type": "bogus_type"}])
    res = _ev([r], _ctx())
    assert _matched(res) is None
    assert any("unknown" in cr.get("reason", "") for cr in res["results"])


def test_disabled_rule_skipped():
    r = _rule([{"type": "at_bot", "value": True}], enabled=False)
    assert _matched(_ev([r], _ctx(at_bot=True))) is None


def test_rule_level_scope_rejects():
    r = _rule(
        [{"type": "at_bot", "value": True}],
        scope={
            "groups": ["999"],
            "users": [],
            "sessions": [],
            "platforms": [],
            "exclude_groups": [],
            "exclude_users": [],
        },
    )
    # 规则级 scope 未命中（群 12345 ∉ [999]）→ 拒绝且不评 when
    res = _ev([r], _ctx(at_bot=True, group_id="12345"))
    assert _matched(res) is None
    assert len(res["rejected"]) == 1


def test_rule_scope_exclude_rejects():
    r = _rule(
        [{"type": "at_bot", "value": True}],
        scope={
            "groups": [],
            "users": [],
            "sessions": [],
            "platforms": [],
            "exclude_groups": ["12345"],
            "exclude_users": [],
        },
    )
    res = _ev([r], _ctx(at_bot=True, group_id="12345"))
    assert _matched(res) is None


def test_rejected_reasons_present():
    r = _rule([{"type": "at_bot", "value": True}])
    res = _ev([r], _ctx(at_bot=False))
    assert res["matched_rule"] is None
    assert len(res["rejected"]) == 1
    assert res["rejected"][0]["results"]
