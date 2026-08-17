"""rules 模块 —— 新增 model_keyword 条件 + replace_model 动作 + validate_rule 校验测试。

覆盖：
- model_keyword 条件：all / any / min_n 三模式命中与不命中（含大小写不敏感、
  min_n 边界、keywords 空、model_name 空、非法 mode）；
- replace_model 动作归一化（normalize_rule 默认补 provider_id/model）；
- validate_rule：合法规则返回空错误；缺失关键词 / 空 model / 非法 mode /
  越界 min_n / 空 provider_id 各返回错误。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import make_store
from scheduler.rules import (
    ACTIONS,
    RuleContext,
    RuleEngine,
    normalize_rule,
    validate_rule,
)

TZ = ZoneInfo("Asia/Shanghai")


def _ctx(**kw):
    base = {
        "now": datetime(2026, 6, 10, 14, 0, tzinfo=TZ),
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
        "model_name": "",
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


def _ev(conditions, ctx, then=None, **kw):
    store = make_store()
    r = _rule(conditions, **kw)
    if then is not None:
        r["then"] = then
    store.update("rules", [r])
    return RuleEngine(store).evaluate(ctx)


def _matched(res):
    m = res.get("matched_rule")
    return m.get("id") if m else None


# ---- all 模式 ----


def test_all_all_keywords_match():
    r = _ev(
        [{"type": "model_keyword", "keywords": ["flash", "turbo"], "mode": "all"}],
        _ctx(model_name="deepseek-chat-flash-turbo"),
    )
    assert _matched(r) == "r1"


def test_all_missing_one_fails():
    r = _ev(
        [{"type": "model_keyword", "keywords": ["flash", "turbo"], "mode": "all"}],
        _ctx(model_name="deepseek-chat-flash"),
    )
    assert _matched(r) is None


# ---- any 模式 ----


def test_any_one_keyword_matches():
    r = _ev(
        [{"type": "model_keyword", "keywords": ["flash", "turbo"], "mode": "any"}],
        _ctx(model_name="deepseek-chat-turbo"),
    )
    assert _matched(r) == "r1"


def test_any_none_match_fails():
    r = _ev(
        [{"type": "model_keyword", "keywords": ["flash", "turbo"], "mode": "any"}],
        _ctx(model_name="deepseek-chat"),
    )
    assert _matched(r) is None


# ---- min_n 模式（边界 =min_n 命中、<min_n 不命中）----


def test_min_n_boundary_equal():
    # 模型名含 flash+mini 两个关键词，min_n=2 → 命中
    r = _ev(
        [
            {
                "type": "model_keyword",
                "keywords": ["flash", "turbo", "mini"],
                "mode": "min_n",
                "min_n": 2,
            }
        ],
        _ctx(model_name="deepseek-flash-mini"),
    )
    assert _matched(r) == "r1"


def test_min_n_under_boundary_fails():
    # 只命中 1 个，min_n=2 → 不命中
    r = _ev(
        [
            {
                "type": "model_keyword",
                "keywords": ["flash", "turbo", "mini"],
                "mode": "min_n",
                "min_n": 2,
            }
        ],
        _ctx(model_name="deepseek-flash"),
    )
    assert _matched(r) is None


def test_min_n_three_of_four_matches():
    r = _ev(
        [
            {
                "type": "model_keyword",
                "keywords": ["a", "b", "c", "d"],
                "mode": "min_n",
                "min_n": 3,
            }
        ],
        _ctx(model_name="model-a-b-c"),
    )
    assert _matched(r) == "r1"


# ---- 大小写不敏感 ----


def test_case_insensitive():
    r = _ev(
        [{"type": "model_keyword", "keywords": ["FLASH"], "mode": "any"}],
        _ctx(model_name="DeepSeek-Chat-Flash"),
    )
    assert _matched(r) == "r1"


# ---- keywords 空 / model_name 空 ----


def test_empty_keywords_no_match():
    r = _ev(
        [{"type": "model_keyword", "keywords": [], "mode": "any"}],
        _ctx(model_name="deepseek-chat"),
    )
    assert _matched(r) is None
    assert any("无关键词" in cr.get("reason", "") for cr in r["results"])


def test_empty_model_name_no_match():
    r = _ev(
        [{"type": "model_keyword", "keywords": ["flash"], "mode": "any"}],
        _ctx(model_name=""),
    )
    assert _matched(r) is None
    assert any("无法确定模型名" in cr.get("reason", "") for cr in r["results"])


# ---- 非法 mode ----


def test_invalid_mode_no_match_with_reason():
    r = _ev(
        [{"type": "model_keyword", "keywords": ["flash"], "mode": "bogus"}],
        _ctx(model_name="some-flash-model"),
    )
    assert _matched(r) is None
    assert any("非法 mode" in cr.get("reason", "") for cr in r["results"])


# ---- replace_model 动作 ----


def test_replace_model_in_actions():
    assert "replace_model" in ACTIONS


def test_replace_model_works_when_matched():
    # 命中 model_keyword → 匹配规则 then 为 replace_model（命中判定与动作无关）
    res = _ev(
        [{"type": "model_keyword", "keywords": ["flash"], "mode": "any"}],
        _ctx(model_name="deepseek-chat-flash"),
        then={
            "action": "replace_model",
            "provider_id": "openai",
            "model": "gpt-5-mini",
        },
    )
    assert _matched(res) == "r1"
    assert res["matched_rule"]["then"]["action"] == "replace_model"
    assert res["matched_rule"]["then"]["provider_id"] == "openai"
    assert res["matched_rule"]["then"]["model"] == "gpt-5-mini"


def test_normalize_rule_replace_model_defaults():
    raw = {"then": {"action": "replace_model"}}
    r = normalize_rule(raw)
    assert r["then"]["action"] == "replace_model"
    assert r["then"]["provider_id"] == ""
    assert r["then"]["model"] == ""


def test_normalize_rule_non_replace_keeps_old_default():
    raw = {"then": {}}
    r = normalize_rule(raw)
    assert r["then"]["action"] == "switch_group"
    assert r["then"]["group_id"] == ""


# ---- validate_rule ----


def _valid_rule():
    return {
        "id": "r1",
        "name": "R",
        "enabled": True,
        "priority": 10,
        "scope": {
            "groups": [],
            "users": [],
            "sessions": [],
            "platforms": [],
            "exclude_groups": [],
            "exclude_users": [],
        },
        "when": {
            "op": "and",
            "conditions": [
                {"type": "model_keyword", "keywords": ["flash", "turbo"], "mode": "any"}
            ],
        },
        "then": {
            "action": "replace_model",
            "provider_id": "openai",
            "model": "gpt-5-mini",
        },
    }


def test_validate_valid_rule_empty_errors():
    assert validate_rule(_valid_rule()) == []


def test_validate_missing_keywords_error():
    rule = _valid_rule()
    rule["when"]["conditions"][0]["keywords"] = []
    errs = validate_rule(rule)
    assert any("keywords" in e for e in errs)


def test_validate_empty_provider_id_error():
    rule = _valid_rule()
    rule["then"]["provider_id"] = ""
    errs = validate_rule(rule)
    assert any("provider_id" in e for e in errs)


def test_validate_empty_model_error():
    rule = _valid_rule()
    rule["then"]["model"] = ""
    errs = validate_rule(rule)
    assert any("model" in e for e in errs)


def test_validate_invalid_mode_error():
    rule = _valid_rule()
    rule["when"]["conditions"][0]["mode"] = "bogus"
    errs = validate_rule(rule)
    assert any("mode" in e for e in errs)


def test_validate_out_of_range_min_n_error():
    # len(keywords)=2，min_n=3 越界
    rule = _valid_rule()
    rule["when"]["conditions"][0].update({"mode": "min_n", "min_n": 3})
    errs = validate_rule(rule)
    assert any("min_n" in e for e in errs)


def test_validate_min_n_zero_error():
    rule = _valid_rule()
    rule["when"]["conditions"][0].update({"mode": "min_n", "min_n": 0})
    errs = validate_rule(rule)
    assert any("min_n" in e for e in errs)


def test_validate_min_n_default_fills_to_two_ok():
    # 缺省 min_n：默认 2；keywords 2 个 → 校验通过
    rule = _valid_rule()
    rule["when"]["conditions"][0].update({"mode": "min_n"})
    assert validate_rule(rule) == []


def test_validate_unknown_action_error():
    rule = _valid_rule()
    rule["then"]["action"] = "bogus_action"
    errs = validate_rule(rule)
    assert any("action" in e for e in errs)


# ---- G-5：_eval_model_keyword 非 str 关键词求值防御 ----
# 实现：求值层先过滤掉非 str 元素（isinstance(kw, str)），剩余为空则视为「无关键词」不命中。


def test_non_str_keywords_filtered_any_hits_str():
    """G-5：keywords=[123, "flash"] → 求值层只保留 str（"flash"）；
    mode=any 且模型名含 "flash" → 命中（视为 1 个关键词）。"""
    r = _ev(
        [{"type": "model_keyword", "keywords": [123, "flash"], "mode": "any"}],
        _ctx(model_name="deepseek-chat-flash"),
    )
    assert _matched(r) == "r1"


def test_non_str_keywords_all_filtered_no_match():
    """G-5：keywords=[123] 全为非 str → 过滤后为空 → 视为「无关键词」不命中。"""
    r = _ev(
        [{"type": "model_keyword", "keywords": [123], "mode": "any"}],
        _ctx(model_name="deepseek-chat"),
    )
    assert _matched(r) is None
    assert any("无关键词" in cr.get("reason", "") for cr in r["results"])


# ---- G-6：validate_rule 对 switch_provider 的确认性用例 ----
# 已按代码确认：validate_rule 的最小校验仅覆盖 replace_model（provider_id/model）
# 与 switch_group（group_id），**不校验 switch_provider 的 provider_id 非空**；
# 工具侧补充校验也只覆盖 replace_model。以下用例固化该现状。


def test_validate_switch_provider_does_not_require_provider_id():
    """G-6（确认性用例，固化现状）：switch_provider + 空 provider_id → validate_rule
    返回空错误（当前最小校验设计不校验该项）。若未来实现收紧该校验，本条需同步改为
    断言返回错误。"""
    rule = _valid_rule()
    rule["then"] = {"action": "switch_provider", "provider_id": ""}
    assert validate_rule(rule) == []
