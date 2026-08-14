"""persistence 模块测试（schema v2）：DEFAULT_CONFIG 深度合并、save/load 往返、
损坏回退、import_all 校验（v1 自动迁移 / v2 / 非法版本）、update 分区、
v1 磁盘配置 load 自动迁移、revision 单调递增。"""

import json

import pytest
from conftest import make_store
from scheduler.persistence import SCHEMA_VERSION


def _write(store, content: str):
    store.config_path().write_text(content, encoding="utf-8")


def test_default_config_loaded_when_file_missing():
    store = make_store()
    cfg = store.load()
    # 顶层结构与默认一致（v2 含 temporal_rules）
    assert cfg["schema_version"] == SCHEMA_VERSION
    for k in (
        "settings",
        "groups",
        "rules",
        "lifecycles",
        "overrides",
        "temporal_rules",
    ):
        assert k in cfg
    assert cfg["settings"]["enabled"] is True
    assert cfg["settings"]["log_retention"] == 500
    assert cfg["settings"]["agent_confirm"] is True
    # v0.1.6 新增：两个 settings 默认项存在且为空串
    assert cfg["settings"]["default_lifecycle"] == ""
    assert cfg["settings"]["agent_provider_id"] == ""
    assert cfg["temporal_rules"] == []


def test_save_load_roundtrip():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [
        {"id": "g1", "name": "Strong", "providers": [{"provider_id": "x"}]}
    ]
    store.save(cfg)
    reloaded = store.load()
    assert reloaded["groups"][0]["id"] == "g1"
    assert reloaded["schema_version"] == SCHEMA_VERSION
    # ConfigStore 只做顶层深度合并；组内成员字段补齐由 groups.normalize_group 负责
    assert reloaded["groups"][0]["providers"][0]["provider_id"] == "x"


def test_deep_merge_fills_missing_settings_keys():
    store = make_store()
    partial = {
        "schema_version": 1,
        "settings": {"enabled": False},
        "groups": [{"id": "g1"}],
    }
    _write(store, json.dumps(partial))
    cfg = store.load()
    # 缺失的 settings 键被默认补齐
    assert cfg["settings"]["enabled"] is False  # 保留用户值
    assert cfg["settings"]["base_group"] == ""
    assert cfg["settings"]["timezone"] == "auto"
    assert cfg["settings"]["state_persist"] is True
    # v0.1.6：缺失的 new keys 也被默认补齐
    assert cfg["settings"]["default_lifecycle"] == ""
    assert cfg["settings"]["agent_provider_id"] == ""
    # v1 配置 load 自动迁移：补 temporal_rules / agent_confirm
    assert cfg["schema_version"] == 2
    assert cfg["temporal_rules"] == []
    assert cfg["settings"]["agent_confirm"] is True
    # 旧字段保留
    assert cfg["groups"][0]["id"] == "g1"


def test_v1_disk_config_auto_migrated_on_load():
    store = make_store()
    v1 = {
        "schema_version": 1,
        "settings": {"enabled": True, "custom_key": "keep-me"},
        "groups": [{"id": "g_old", "name": "Legacy"}],
        "rules": [{"id": "r_old"}],
        "lifecycles": [],
        "overrides": {"umo:1": {"note": "x"}},
    }
    _write(store, json.dumps(v1))
    cfg = store.load()
    # 迁移到 v2 且立即持久化
    assert cfg["schema_version"] == 2
    assert cfg["temporal_rules"] == []
    assert cfg["settings"]["agent_confirm"] is True
    # 旧字段原样保留
    assert cfg["groups"][0]["id"] == "g_old"
    assert cfg["rules"][0]["id"] == "r_old"
    assert cfg["overrides"]["umo:1"]["note"] == "x"
    assert cfg["settings"]["custom_key"] == "keep-me"
    # 已写回磁盘
    on_disk = json.loads(store.config_path().read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 2
    assert on_disk["temporal_rules"] == []


def test_corrupt_json_backs_up_and_returns_default():
    store = make_store()
    _write(store, "{ this is not valid json ")
    cfg = store.load()
    assert cfg["schema_version"] == SCHEMA_VERSION
    assert cfg["groups"] == []
    # 原损坏文件被备份为 .bak
    assert store.config_path().with_suffix(".json.bak").exists()


def test_invalid_top_level_structure_backs_up():
    store = make_store()
    _write(store, json.dumps({"foo": 1}))  # 缺 settings → 结构非法
    cfg = store.load()
    assert cfg["settings"]["enabled"] is True
    assert store.config_path().with_suffix(".json.bak").exists()


def test_import_all_rejects_unknown_version():
    store = make_store()
    with pytest.raises(RuntimeError):
        store.import_all({"schema_version": 3, "settings": {}})
    # 原配置未被动
    assert store.load()["schema_version"] == SCHEMA_VERSION


def test_import_all_rejects_missing_settings():
    store = make_store()
    with pytest.raises(RuntimeError):
        store.import_all({"schema_version": 1})
    with pytest.raises(RuntimeError):
        store.import_all({"schema_version": 1, "settings": "not-dict"})


def test_import_all_accepts_v2():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [{"id": "g_imp", "name": "Imported"}]
    new_cfg = store.import_all(cfg)
    assert new_cfg["groups"][0]["id"] == "g_imp"
    assert new_cfg["schema_version"] == 2
    # 已持久化
    assert store.load()["groups"][0]["id"] == "g_imp"


def test_v2_config_without_new_settings_keys_fills_defaults_on_load():
    """v2 配置不含 v0.1.6 两个新键时，load 经深合并自动补齐（无需迁移）。"""
    store = make_store()
    # 模拟一个「旧 v2」配置：无 default_lifecycle / agent_provider_id 两个键
    v2_old = {
        "schema_version": 2,
        "settings": {"enabled": False, "base_group": "g_x"},
        "groups": [],
        "rules": [],
        "lifecycles": [],
        "overrides": {},
        "temporal_rules": [],
    }
    _write(store, json.dumps(v2_old))
    cfg = store.load()
    # schema 仍为 2，缺失的两个新键被默认补齐为空串
    assert cfg["schema_version"] == 2
    assert cfg["settings"]["enabled"] is False  # 保留用户值
    assert cfg["settings"]["base_group"] == "g_x"
    assert cfg["settings"]["default_lifecycle"] == ""
    assert cfg["settings"]["agent_provider_id"] == ""


def test_import_all_v2_without_new_settings_keys_fills_defaults():
    """import_all 导入不含 v0.1.6 新键的 v2 配置后，两键同样被补齐。"""
    store = make_store()
    v2_old = {
        "schema_version": 2,
        "settings": {"enabled": True},
        "groups": [],
        "rules": [],
        "lifecycles": [],
        "overrides": {},
        "temporal_rules": [],
    }
    new_cfg = store.import_all(v2_old)
    assert new_cfg["schema_version"] == 2
    assert new_cfg["settings"]["default_lifecycle"] == ""
    assert new_cfg["settings"]["agent_provider_id"] == ""
    # 已持久化，再次 load 仍补齐
    on_disk = json.loads(store.config_path().read_text(encoding="utf-8"))
    assert on_disk["settings"]["default_lifecycle"] == ""
    assert on_disk["settings"]["agent_provider_id"] == ""


def test_default_lifecycle_and_agent_provider_id_persisted_roundtrip():
    """设置两个新键后 save/load 往返保持。"""
    store = make_store()
    cfg = store.load()
    cfg["settings"]["default_lifecycle"] = "lc_staged"
    cfg["settings"]["agent_provider_id"] = "p_assistant"
    store.save(cfg)
    reloaded = store.load()
    assert reloaded["settings"]["default_lifecycle"] == "lc_staged"
    assert reloaded["settings"]["agent_provider_id"] == "p_assistant"


def test_import_all_accepts_v1_and_migrates():
    store = make_store()
    v1 = {
        "schema_version": 1,
        "settings": {"enabled": False, "custom": "keep"},
        "groups": [{"id": "g_v1"}],
    }
    new_cfg = store.import_all(v1)
    # v1 先迁移再保存：schema 恒为 2，补 temporal_rules / agent_confirm，旧字段保留
    assert new_cfg["schema_version"] == 2
    assert new_cfg["temporal_rules"] == []
    assert new_cfg["settings"]["agent_confirm"] is True
    assert new_cfg["groups"][0]["id"] == "g_v1"
    assert new_cfg["settings"]["custom"] == "keep"
    # 已持久化为 v2
    on_disk = json.loads(store.config_path().read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 2


def test_update_rejects_bad_section():
    store = make_store()
    with pytest.raises(RuntimeError):
        store.update("bogus", [])


def test_update_valid_section():
    store = make_store()
    result = store.update("groups", [{"id": "gx", "name": "X"}])
    assert result["groups"][0]["id"] == "gx"
    assert store.load()["groups"][0]["id"] == "gx"


def test_update_temporal_rules_allowed():
    store = make_store()
    rules = [{"id": "t_123", "name": "Rule"}]
    result = store.update("temporal_rules", rules)
    assert result["temporal_rules"][0]["id"] == "t_123"
    assert store.get_temporal_rules()[0]["id"] == "t_123"


def test_get_settings_default():
    store = make_store()
    s = store.get_settings()
    assert s["log_retention"] == 500
    assert s["base_group"] == ""
    assert s["agent_confirm"] is True
    # v0.1.6 新增：两个 settings 默认项
    assert s["default_lifecycle"] == ""
    assert s["agent_provider_id"] == ""


def test_get_temporal_rules_default():
    store = make_store()
    assert store.get_temporal_rules() == []


def test_export_all_is_deep_copy():
    store = make_store()
    cfg = store.load()
    cfg["groups"] = [{"id": "g1"}]
    store.save(cfg)
    exported = store.export_all()
    exported["groups"].append({"id": "extra"})
    # 原配置不应因修改导出结果而改变
    assert len(store.load()["groups"]) == 1


def test_save_sets_schema_version():
    store = make_store()
    store.save(
        {
            "settings": {},
            "groups": [],
            "rules": [],
            "lifecycles": [],
            "overrides": {},
            "temporal_rules": [],
        }
    )
    assert store.load()["schema_version"] == SCHEMA_VERSION


def test_revision_monotonic_increases():
    store = make_store()
    # 初始 0，load 不改变
    assert store.revision() == 0
    store.load()
    assert store.revision() == 0
    # 每次 save() 成功 +1
    cfg = store.load()
    store.save(cfg)
    assert store.revision() == 1
    store.save(cfg)
    assert store.revision() == 2
    store.update("groups", [{"id": "gx"}])
    assert store.revision() == 3
