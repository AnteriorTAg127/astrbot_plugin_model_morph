"""chat_store 模块测试：新建/追加/标题截断/列表/深拷贝/上限/非法 role/持久化/损坏兜底/时钟注入。

数据目录一律用 conftest ``make_store`` 的唯一子目录（.pytest_tmp 下），并据此拿到
``agent_chats.json`` 路径（``store.config_path()``）。
"""

import json

from scheduler.chat_store import ChatStore

from conftest import make_store


def _fixed_clock(iso="2026-06-10T12:00:00"):
    """返回固定时间时钟工厂。"""
    return lambda: iso


def _data_dir():
    """基于 conftest make_store 创建唯一数据目录，返回其路径（ConfigStore 的父目录）。"""
    return make_store().config_path().parent


def test_new_conversation_basic():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("  你好，帮我配置模型  ")
    assert conv["id"].startswith("c_")
    assert conv["title"] == "你好，帮我配置模型"
    assert conv["messages"] == []
    assert conv["created_at"] == "2026-06-10T12:00:00"
    assert conv["updated_at"] == "2026-06-10T12:00:00"


def test_title_truncated_to_30():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    long_text = (
        "这是超过三十个字符的标题内容，用于验证标题会被正确地截断到 30 个字符的上限以内"
    )
    conv = store.new_conversation(long_text)
    assert len(conv["title"]) == 30
    assert conv["title"] == long_text[:30]


def test_empty_title_fallback():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("   \n  ")
    assert conv["title"] == "新对话"


def test_append_message_user_and_assistant():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("配置需求")
    cid = conv["id"]
    store.append_message(cid, "user", "你好")
    updated = store.append_message(cid, "assistant", "已生成")
    assert updated["messages"] == [
        {"role": "user", "content": "你好", "time": "2026-06-10T12:00:00"},
        {"role": "assistant", "content": "已生成", "time": "2026-06-10T12:00:00"},
    ]


def test_append_invalid_role_rejected():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("需求")
    pos = store.append_message(conv["id"], "system", "x")
    assert pos is None
    # 非法 role 不应写入任何消息
    assert len(store.get_conversation(conv["id"])["messages"]) == 0


def test_append_missing_conversation_returns_none():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    assert store.append_message("c_nonexistent", "user", "hi") is None


def test_list_sorted_by_updated_at_desc_and_summary():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock("2026-06-10T10:00:00"))
    c1 = store.new_conversation("第一条会话")
    store.append_message(c1["id"], "user", "这条会话的内容用于预览")
    # 时间推进后新会话应排在前面（updated_at 更大）
    store._now_fn = _fixed_clock("2026-06-11T10:00:00")
    c2 = store.new_conversation("第二条会话")
    store.append_message(c2["id"], "user", "另一条内容")

    summary = store.list_conversations()
    assert [s["id"] for s in summary] == [c2["id"], c1["id"]]
    assert summary[0]["title"] == "第二条会话"
    assert summary[0]["message_count"] == 1
    assert summary[1]["last_preview"] == "这条会话的内容用于预览"


def test_last_preview_truncated_to_60():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("会话")
    long_content = "长" * 100
    store.append_message(conv["id"], "user", long_content)
    summary = store.list_conversations()
    assert len(summary[0]["last_preview"]) == 60


def test_get_conversation_deep_copy_isolation():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("深拷贝测试")
    store.append_message(conv["id"], "user", "内容")
    got = store.get_conversation(conv["id"])
    got["title"] = "被篡改的标题"
    got["messages"].append({"role": "user", "content": "注入", "time": "t"})
    got["messages"][0]["content"] = "被篡改的内容"
    # 外部改动不影响内部状态
    inner = store.get_conversation(conv["id"])
    assert inner["title"] == "深拷贝测试"
    assert len(inner["messages"]) == 1
    assert inner["messages"][0]["content"] == "内容"


def test_get_missing_conversation_returns_none():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    assert store.get_conversation("c_nope") is None


def test_delete():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("待删除")
    assert store.delete(conv["id"]) is True
    assert store.get_conversation(conv["id"]) is None
    # 删除不存在的会话返回 False
    assert store.delete("c_nonexistent") is False


def test_max_conversations_evicts_oldest():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock("2026-06-10T00:00:00"))
    # 时钟每次推进，保证 created_at 可区分
    holder = {"i": 0}

    def clock():
        holder["i"] += 1
        return f"2026-06-10T00:00:{holder['i'] % 60:02d}"

    store = ChatStore(_data_dir(), now_fn=clock)
    created = [
        store.new_conversation(f"会话{i}")["id"]
        for i in range(store.MAX_CONVERSATIONS + 5)
    ]
    # 仍保留上限数量
    assert len(store.list_conversations()) == store.MAX_CONVERSATIONS
    # 淘汰的是最早创建的（最旧 5 个被删除）
    assert store.get_conversation(created[0]) is None
    assert store.get_conversation(created[5]) is not None
    assert store.get_conversation(created[-1]) is not None


def test_max_messages_drops_oldest():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock())
    conv = store.new_conversation("大量消息")
    for i in range(store.MAX_MESSAGES + 10):
        store.append_message(conv["id"], "user", f"消息{i}")
    stored = store.get_conversation(conv["id"])
    assert len(stored["messages"]) == store.MAX_MESSAGES
    # 丢弃的是最旧：第一条（消息0）不在，最后保留的是消息209
    assert stored["messages"][0]["content"] == "消息10"
    assert stored["messages"][-1]["content"] == f"消息{store.MAX_MESSAGES + 9}"


def test_persistence_roundtrip():
    data_dir = _data_dir()

    def clock_seq():
        state = {"n": 0}

        def fn():
            state["n"] += 1
            return f"2026-06-10T10:00:{state['n'] % 60:02d}"

        return fn

    fn = clock_seq()
    store = ChatStore(data_dir, now_fn=fn)
    conv = store.new_conversation("持久化会话")
    store.append_message(conv["id"], "user", "你好")
    store.append_message(conv["id"], "assistant", "回复")
    store.save()

    # 新实例从同一文件 load_from 恢复
    store2 = ChatStore(data_dir, now_fn=fn)
    restored = store2.get_conversation(conv["id"])
    assert restored is not None
    assert restored["title"] == "持久化会话"
    assert [m["content"] for m in restored["messages"]] == ["你好", "回复"]
    # load_from 返回恢复的会话数
    assert store2.load_from(store2.config_path()) == 1


def test_corrupt_file_backed_up_and_loads_zero():
    data_dir = _data_dir()
    path = data_dir / "agent_chats.json"
    # 写入非法 JSON
    path.write_text("{ this is not valid json", encoding="utf-8")
    store = ChatStore(data_dir, now_fn=_fixed_clock())
    # 恢复 0 且不抛异常
    assert store.load_from(path) == 0
    assert store.list_conversations() == []
    # 生成 .bak 备份，且原文件已被替换为空档（后续 save 能写）
    assert path.with_suffix(".json.bak").exists()


def test_corrupt_structure_backed_up():
    data_dir = _data_dir()
    path = data_dir / "agent_chats.json"
    path.write_text(json.dumps({"conversations": "nope"}), encoding="utf-8")
    store = ChatStore(data_dir, now_fn=_fixed_clock())
    assert store.list_conversations() == []
    assert path.with_suffix(".json.bak").exists()


def test_missing_file_initializes_empty():
    data_dir = _data_dir()
    store = ChatStore(data_dir, now_fn=_fixed_clock())
    assert store.list_conversations() == []


def test_now_fn_controls_timestamps():
    store = ChatStore(_data_dir(), now_fn=_fixed_clock("2099-01-01T00:00:00"))
    conv = store.new_conversation("时钟断言")
    assert conv["created_at"] == "2099-01-01T00:00:00"
    assert conv["updated_at"] == "2099-01-01T00:00:00"
    store.append_message(conv["id"], "user", "消息")
    stored = store.get_conversation(conv["id"])
    assert stored["updated_at"] == "2099-01-01T00:00:00"
    assert stored["messages"][0]["time"] == "2099-01-01T00:00:00"
