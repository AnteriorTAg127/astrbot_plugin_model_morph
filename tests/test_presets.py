"""presets 模块测试：5 个预设均可构建出合法 temporal rule 结构（字段级断言），
peak_valley 两条互补，参数缺失抛 ValueError，未知 preset 抛 KeyError。"""

import pytest

from scheduler.presets import PRESETS, build_preset_rules

# 各预设所需的一组合理参数（字段名参照 v0.1.5 契约）。
_COMMON = {
    "group_id": "g_default",
    "source_provider": "premium-model",
    "target_provider": "cheap-model",
}


def _params(preset_id, **overrides):
    p = dict(_COMMON)
    if preset_id in ("force_replace", "maintenance"):
        p["start"] = "20:00"
        p["end"] = "23:00"
    base = {
        "peak_valley": {"start": "18:00", "end": "23:00"},
        "night_saving": {"start": "23:00", "end": "08:00"},
        "workday_performance": {"start": "09:00", "end": "18:00"},
    }.get(preset_id, {})
    p.update(base)
    p.update(overrides)
    return p


_REQ_KEYS = {
    "id",
    "name",
    "enabled",
    "kind",
    "group_id",
    "source_provider",
    "target_provider",
    "target_group",
    "scope",
    "schedule",
    "priority",
    "metadata",
}
_SCHED_KEYS = {"type", "start", "end", "weekdays", "date", "timezone"}
_SCOPE_KEYS = {"groups", "users", "sessions"}


def _assert_valid_rule(rule, kind, expected_schedule_type):
    # 字段齐全
    assert _REQ_KEYS <= rule.keys()
    assert rule["enabled"] is True
    assert rule["kind"] == kind
    assert rule["group_id"] == "g_default"
    assert rule["source_provider"]
    assert rule["target_provider"]
    assert rule["target_group"] == ""
    # id 前缀 t_
    assert rule["id"].startswith("t_")
    # scope 三键齐全
    assert set(rule["scope"]) >= _SCOPE_KEYS
    # schedule 结构
    assert set(rule["schedule"]) >= _SCHED_KEYS
    assert rule["schedule"]["type"] == expected_schedule_type
    # priority 默认 200
    assert rule["priority"] == 200
    # metadata
    assert "source" in rule["metadata"]


def test_all_5_presets_build_valid_rules():
    # 每个预设都能构建出合法结构（参数齐全）；workday_performance 为 weekly，其余 daily
    for pid in PRESETS:
        rules = build_preset_rules(pid, _params(pid))
        assert rules, f"{pid} 未生成任何规则"
        for r in rules:
            _assert_valid_rule(
                r,
                "model_override",
                "weekly" if pid == "workday_performance" else "daily",
            )


def test_presets_have_required_meta():
    for pid, preset in PRESETS.items():
        assert preset["id"] == pid
        assert preset["name"]
        assert preset["desc"]
        assert preset["kind"] in ("model_override", "group_switch")
        assert isinstance(preset["params"], list) and preset["params"]
        # params 每项字段齐全
        for p in preset["params"]:
            assert {"key", "label", "type", "required", "default"} <= p.keys()


def test_peak_valley_generates_two_complementary_rules():
    rules = build_preset_rules("peak_valley", _params("peak_valley"))
    assert len(rules) == 2
    peak, valley = rules
    # 高峰：premium→cheap，18:00-23:00
    assert peak["source_provider"] == "premium-model"
    assert peak["target_provider"] == "cheap-model"
    assert peak["schedule"]["start"] == "18:00"
    assert peak["schedule"]["end"] == "23:00"
    # 低谷：cheap→premium，反区间跨午夜 23:00-18:00
    assert valley["source_provider"] == "cheap-model"
    assert valley["target_provider"] == "premium-model"
    assert valley["schedule"]["start"] == "23:00"
    assert valley["schedule"]["end"] == "18:00"
    # 两者的时间窗互补（互反）
    assert peak["schedule"]["type"] == "daily"
    assert valley["schedule"]["type"] == "daily"
    assert peak["schedule"]["end"] == valley["schedule"]["start"]
    assert valley["schedule"]["end"] == peak["schedule"]["start"]


def test_night_saving_cross_midnight():
    rules = build_preset_rules("night_saving", _params("night_saving"))
    assert len(rules) == 1
    sch = rules[0]["schedule"]
    # 23:00-08:00，end<start 跨午夜
    assert sch["start"] == "23:00"
    assert sch["end"] == "08:00"
    assert sch["type"] == "daily"


def test_workday_performance_weekdays():
    rules = build_preset_rules("workday_performance", _params("workday_performance"))
    assert len(rules) == 1
    sch = rules[0]["schedule"]
    assert sch["type"] == "weekly"
    assert sch["weekdays"] == [0, 1, 2, 3, 4]  # 工作日 周一~五
    assert sch["start"] == "09:00"
    assert sch["end"] == "18:00"


def test_force_replace_date_schedule():
    rules = build_preset_rules(
        "force_replace",
        _params("force_replace", date="2026-06-20", weekdays=[0, 1]),
    )
    assert len(rules) == 1
    sch = rules[0]["schedule"]
    # date 优先 → type=date
    assert sch["type"] == "date"
    assert sch["date"] == "2026-06-20"
    assert sch["weekdays"] == []


def test_force_replace_weekly_schedule():
    rules = build_preset_rules(
        "force_replace", _params("force_replace", weekdays=[0, 3])
    )
    sch = rules[0]["schedule"]
    assert sch["type"] == "weekly"
    assert sch["weekdays"] == [0, 3]


def test_maintenance_semantics_name_desc():
    preset = PRESETS["maintenance"]
    rules = build_preset_rules("maintenance", _params("maintenance"))
    assert rules[0]["kind"] == "model_override"
    # 维护语义：name/desc 体现维护
    assert "维护" in preset["name"]
    assert "维护" in preset["desc"]


def test_custom_name_applied():
    rules = build_preset_rules(
        "night_saving", _params("night_saving", name="我的夜间省钱")
    )
    assert rules[0]["name"] == "我的夜间省钱"


def test_priority_from_params():
    rules = build_preset_rules("night_saving", _params("night_saving", priority=500))
    assert rules[0]["priority"] == 500


def test_unknown_preset_raises_keyerror():
    with pytest.raises(KeyError):
        build_preset_rules("no_such_preset", {})


def test_missing_group_raises_valueerror():
    p = _params("night_saving")
    p.pop("group_id")
    with pytest.raises(ValueError):
        build_preset_rules("night_saving", p)


def test_missing_provider_raises_valueerror():
    p = _params("force_replace")
    p.pop("target_provider")
    with pytest.raises(ValueError):
        build_preset_rules("force_replace", p)


def test_missing_start_end_raises_valueerror():
    p = _params("force_replace")
    p.pop("start")
    p.pop("end")
    with pytest.raises(ValueError):
        build_preset_rules("force_replace", p)


def test_rules_share_same_id_prefix_and_valid_schedule_type():
    for pid in PRESETS:
        for r in build_preset_rules(pid, _params(pid)):
            assert r["id"].startswith("t_")
            assert r["schedule"]["type"] in ("always", "daily", "weekly", "date")
