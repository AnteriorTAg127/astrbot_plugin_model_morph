"""migrate 模块测试：migrate_v1_to_v2 幂等、旧字段保留、报告内容；
upgrade_config 按 schema_version 分派。"""

from scheduler.migrate import migrate_v1_to_v2, upgrade_config


def _v1_config(custom_settings=None):
    """构造一个典型的 v1 全字段配置（settings 可带自定义键）。"""
    settings = {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "base_group": "g_default",
    }
    if custom_settings:
        settings.update(custom_settings)
    return {
        "schema_version": 1,
        "settings": settings,
        "groups": [{"id": "g1", "name": "默认组"}],
        "rules": [{"id": "r1", "name": "规则1"}],
        "lifecycles": [{"id": "lc1"}],
        "overrides": {"umo:1": {"provider_id": "x"}},
    }


def test_migrate_sets_schema_v2_and_adds_fields():
    cfg, notes = migrate_v1_to_v2(_v1_config())
    assert cfg["schema_version"] == 2
    assert cfg["temporal_rules"] == []
    assert cfg["settings"]["agent_confirm"] is True
    assert "schema_version" in "".join(notes).lower() or any(
        "schema" in n for n in notes
    )


def test_migrate_is_idempotent():
    first, _ = migrate_v1_to_v2(_v1_config())
    second, notes2 = migrate_v1_to_v2(first)
    # 结果一致
    assert second == first
    # 幂等：再次迁移不再产生变更说明
    assert notes2 == []


def test_migrate_preserves_old_sections():
    cfg, _ = migrate_v1_to_v2(_v1_config(custom_settings={"custom_key": "keep"}))
    # groups/rules/lifecycles/overrides 原样保留
    assert cfg["groups"] == [{"id": "g1", "name": "默认组"}]
    assert cfg["rules"] == [{"id": "r1", "name": "规则1"}]
    assert cfg["lifecycles"] == [{"id": "lc1"}]
    assert cfg["overrides"] == {"umo:1": {"provider_id": "x"}}
    # settings 自定义键保留，且不影响默认键
    assert cfg["settings"]["custom_key"] == "keep"
    assert cfg["settings"]["enabled"] is True
    assert cfg["settings"]["timezone"] == "Asia/Shanghai"


def test_migrate_preserves_existing_agent_confirm_false():
    cfg = _v1_config(custom_settings={"agent_confirm": False})
    migrated, _ = migrate_v1_to_v2(cfg)
    # 已有键（哪怕 False）保留，不强制覆盖为 True
    assert migrated["settings"]["agent_confirm"] is False


def test_migrate_report_lists_changes():
    _, notes = migrate_v1_to_v2(_v1_config())
    joined = "\n".join(notes)
    assert "temporal_rules" in joined
    assert "agent_confirm" in joined


def test_migrate_does_not_mutate_input():
    src = _v1_config()
    before = json_now(src)
    migrate_v1_to_v2(src)
    assert json_now(src) == before


def test_upgrade_config_dispatches_v1():
    cfg, notes = upgrade_config({"schema_version": 1, "settings": {}})
    assert cfg["schema_version"] == 2
    assert cfg["temporal_rules"] == []
    assert cfg["settings"]["agent_confirm"] is True
    assert notes


def test_upgrade_config_passes_v2_unchanged():
    v2 = {"schema_version": 2, "settings": {}, "groups": []}
    cfg, notes = upgrade_config(v2)
    assert cfg == v2
    assert cfg["schema_version"] == 2
    assert notes == []


def test_upgrade_config_defaults_missing_version_to_v1():
    cfg, _ = upgrade_config({"settings": {}})
    assert cfg["schema_version"] == 2
    assert cfg["temporal_rules"] == []


def test_upgrade_config_non_dict_returns_v2_default():
    cfg, _ = upgrade_config(None)
    assert cfg["schema_version"] == 2
    assert cfg["temporal_rules"] == []


def json_now(obj):
    """用于对比的 JSON 序列化（深拷贝等价判定）。"""
    import json

    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
