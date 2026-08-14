"""audit 模块测试：追加 / 环形截断 / 筛选（source、action）/ 持久化往返 /
损坏文件兜底 / clear / load_entries / 非 dict 忽略。

数据目录一律走 conftest.make_store()（每次唯一子目录），禁止系统临时目录。
"""

from conftest import make_store
from scheduler.audit import AUDIT_SOURCES, AuditLog


def _entry(**kw):
    """构造一条测试审计条目，可覆盖默认字段。"""
    base = {
        "time": "2026-06-10T14:00:00",
        "operator": "admin",
        "source": "manual",
        "action": "update_rule",
        "target": "g1",
        "before": {"id": "g1"},
        "after": {"id": "g1", "name": "new"},
        "result": "success",
        "detail": "测试条目",
    }
    base.update(kw)
    return base


def test_audit_sources_constant():
    # AUDIT_SOURCES 为规定的六种合法来源
    assert AUDIT_SOURCES == (
        "web_agent",
        "subagent",
        "wizard",
        "preset",
        "manual",
        "system",
    )


def test_add_and_to_list():
    log = AuditLog()
    log.add(_entry())
    log.add(_entry(action="delete_rule"))
    items = log.to_list()
    assert len(items) == 2
    # to_list 按最旧在前
    assert items[0]["action"] == "update_rule"
    assert items[1]["action"] == "delete_rule"


def test_add_fills_default_fields():
    log = AuditLog()
    log.add({"time": "t1", "action": "create_rule"})
    items = log.to_list()
    assert len(items) == 1
    item = items[0]
    # 缺省字段被补齐
    for key in (
        "time",
        "operator",
        "source",
        "action",
        "target",
        "before",
        "after",
        "result",
        "detail",
    ):
        assert key in item
    assert item["operator"] == ""
    assert item["source"] == ""
    assert item["target"] == ""
    assert item["before"] is None
    assert item["after"] is None
    assert item["result"] == ""
    assert item["detail"] == ""


def test_add_ignores_non_dict():
    log = AuditLog()
    log.add("not a dict")
    log.add([1, 2])
    log.add(None)
    assert len(log.to_list()) == 0


def test_add_deep_copies_entry():
    log = AuditLog()
    before = {"id": "g1", "tags": ["a", "b"]}
    entry = _entry(before=before)
    log.add(entry)
    # 修改外部对象不影响已入库条目
    before["tags"].append("c")
    entry["action"] = "hacked"
    stored = log.to_list()[0]
    assert stored["action"] == "update_rule"
    assert stored["before"]["tags"] == ["a", "b"]


def test_ring_buffer_truncation():
    log = AuditLog(retention=3)
    for i in range(10):
        log.add(_entry(action=f"a{i}"))
    assert len(log.to_list()) == 3
    # 只剩最后 3 条（最旧在前）
    assert [e["action"] for e in log.to_list()] == ["a7", "a8", "a9"]


def test_retention_min_at_least_one():
    log = AuditLog(retention=0)
    log.add(_entry())
    assert len(log.to_list()) == 1


def test_recent_newest_first():
    log = AuditLog()
    for i in range(5):
        log.add(_entry(action=f"a{i}"))
    recent = log.recent()
    # 最新在前
    assert recent[0]["action"] == "a4"
    assert recent[-1]["action"] == "a0"


def test_recent_limit():
    log = AuditLog()
    for i in range(10):
        log.add(_entry(action=f"a{i}"))
    recent = log.recent(limit=3)
    assert len(recent) == 3
    assert recent[0]["action"] == "a9"
    assert recent[-1]["action"] == "a7"


def test_recent_filter_by_source():
    log = AuditLog()
    log.add(_entry(source="manual"))
    log.add(_entry(source="subagent"))
    log.add(_entry(source="manual"))
    filtered = log.recent(source="subagent")
    assert len(filtered) == 1
    assert filtered[0]["source"] == "subagent"


def test_recent_filter_by_action():
    log = AuditLog()
    log.add(_entry(action="create_rule"))
    log.add(_entry(action="delete_rule"))
    log.add(_entry(action="create_rule"))
    filtered = log.recent(action="delete_rule")
    assert len(filtered) == 1
    assert filtered[0]["action"] == "delete_rule"


def test_recent_filter_by_source_and_action():
    log = AuditLog()
    log.add(_entry(source="manual", action="update"))
    log.add(_entry(source="subagent", action="update"))
    log.add(_entry(source="manual", action="delete"))
    filtered = log.recent(source="manual", action="update")
    assert len(filtered) == 1
    assert filtered[0]["action"] == "update"
    assert filtered[0]["source"] == "manual"


def test_clear():
    log = AuditLog()
    log.add(_entry())
    log.add(_entry())
    log.clear()
    assert len(log.to_list()) == 0
    assert log.recent() == []


def test_load_entries_appends():
    log = AuditLog(retention=2)
    log.add(_entry(action="first"))
    log.load_entries([_entry(action="second"), _entry(action="third")])
    # 追加后环形截断为 2 条（最旧丢弃）
    items = log.to_list()
    assert len(items) == 2
    assert [e["action"] for e in items] == ["second", "third"]


def test_load_entries_ignores_non_list_and_bad_items():
    log = AuditLog()
    log.load_entries("not a list")  # 非列表，忽略
    assert len(log.to_list()) == 0
    log.load_entries([_entry(), "bad", 42])  # 只载入合法 dict
    assert len(log.to_list()) == 1


def test_save_load_roundtrip():
    store = make_store()
    path = store.config_path().parent / "audit.json"
    log = AuditLog()
    log.add(_entry(action="create_rule", source="subagent"))
    log.add(_entry(action="delete_rule", source="manual"))
    log.save_to(path)
    assert path.exists()

    # 新缓冲从 disk 恢复
    restored = AuditLog()
    count = restored.load_from(path)
    assert count == 2
    assert len(restored.to_list()) == 2
    assert restored.to_list()[0]["action"] == "create_rule"
    assert restored.to_list()[1]["action"] == "delete_rule"
    # 字段完整
    assert restored.to_list()[0]["source"] == "subagent"
    assert restored.to_list()[0]["result"] == "success"


def test_load_from_missing_file_returns_zero():
    store = make_store()
    path = store.config_path().parent / "audit_missing.json"
    log = AuditLog()
    log.add(_entry())
    result = log.load_from(path)  # 文件不存在，不抛异常
    assert result == 0
    assert len(log.to_list()) == 1  # 原有条目不受影响


def test_load_from_corrupt_file_returns_zero():
    store = make_store()
    path = store.config_path().parent / "audit.json"
    # 写入非 JSON 文本模拟损坏文件
    path.write_text("this is not valid json {", encoding="utf-8")
    log = AuditLog()
    result = log.load_from(path)  # JSON 解析失败 → 0，不抛
    assert result == 0
    assert len(log.to_list()) == 0


def test_load_from_non_list_top_level_returns_zero():
    store = make_store()
    path = store.config_path().parent / "audit.json"
    path.write_text('{"foo": 1}', encoding="utf-8")  # 顶层非列表
    log = AuditLog()
    assert log.load_from(path) == 0


def test_roundtrip_then_append_preserves_order():
    store = make_store()
    path = store.config_path().parent / "audit.json"
    log = AuditLog()
    log.add(_entry(action="a0"))
    log.add(_entry(action="a1"))
    log.save_to(path)

    restored = AuditLog()
    restored.load_from(path)
    # 恢复后再追加，最新在后
    restored.add(_entry(action="a2"))
    actions = [e["action"] for e in restored.to_list()]
    assert actions == ["a0", "a1", "a2"]
    # recent 最新在前
    assert restored.recent()[0]["action"] == "a2"
