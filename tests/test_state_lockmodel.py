"""state 模块 v1.0.3：强锁模型字段 lock_model 测试。

覆盖：默认 None；to_dict→from_dict 往返（含非 None）；旧字典（无 lock_model 键）
容错为 None；reset 后 lock_model 保留、其余状态重置；持久化 snapshot/restore 往返。
"""

from conftest import run
from scheduler.state import SessionState, SessionStateStore


def test_lock_model_default_none():
    """lock_model 字段默认 None，且新建状态的 to_dict 含该键。"""
    st = SessionState(umo="u")
    assert st.lock_model is None
    assert st.to_dict()["lock_model"] is None


def test_lock_model_to_from_dict_roundtrip():
    """to_dict → from_dict 往返一致（含非 None 的强锁模型名）。"""
    st = SessionState(
        umo="u",
        round=3,
        stage="MAIN",
        lock_provider_id="openai",
        lock_model="gpt-5-mini",
    )
    data = st.to_dict()
    assert data["lock_model"] == "gpt-5-mini"
    back = SessionState.from_dict(data, "u")
    assert back.lock_model == "gpt-5-mini"
    assert back.lock_provider_id == "openai"
    # 其余字段不回退
    assert back.round == 3
    assert back.stage == "MAIN"


def test_lock_model_old_dict_defaults_none():
    """旧字典（无 lock_model 键）from_dict 容错为 None，不抛异常。"""
    old = {
        "umo": "u",
        "conversation_id": "c",
        "round": 2,
        "stage": "MAIN",
        "lock_group_id": None,
        "lock_provider_id": "openai",
    }
    st = SessionState.from_dict(old, "u")
    assert st.lock_model is None  # 缺省容错
    assert st.lock_provider_id == "openai"  # 既有字段不受影响
    assert st.round == 2
    # 显示置为 None 的情况等同缺省
    old["lock_model"] = None
    assert SessionState.from_dict(old, "u").lock_model is None


def test_lock_model_preserved_on_reset():
    """reset 后 lock_model 保留，其余状态（round/stage 等）重置。"""
    states = SessionStateStore()

    async def _main():
        await states.update(
            "umo:1", round=7, stage="MAIN", lifecycle_id="lc1", pending_reset=True
        )
        await states.update(
            "umo:1",
            lock_group_id=None,
            lock_provider_id="openai",
            lock_model="gpt-5-mini",
        )
        await states.update("umo:1", last_rule_id="r1")
        res = await states.reset("umo:1", "cid-new")
        return res

    res = run(_main())
    # 强锁模型跨重置保留
    assert res.lock_model == "gpt-5-mini"
    assert res.lock_provider_id == "openai"
    # 其余调度状态重置
    assert res.round == 0
    assert res.stage == "NEW"
    assert res.lifecycle_id is None
    assert res.pending_reset is False
    assert res.conversation_id == "cid-new"
    assert res.last_rule_id is None


def test_lock_model_group_and_model_both_preserved():
    """同时锁定组与模型（历史数据/并发写）reset 后两者都保留。"""
    states = SessionStateStore()

    async def _main():
        await states.update("umo:2", round=5)
        await states.update(
            "umo:2",
            lock_group_id="g_lock",
            lock_provider_id="openai",
            lock_model="gpt-4o",
        )
        res = await states.reset("umo:2", "cid-new")
        return res

    res = run(_main())
    assert res.lock_group_id == "g_lock"
    assert res.lock_provider_id == "openai"
    assert res.lock_model == "gpt-4o"


def test_lock_model_store_update_and_get():
    """通过 SessionStateStore.update 写入 lock_model 并经 get 读回。"""
    states = SessionStateStore()

    async def _main():
        await states.update(
            "umo:u", lock_provider_id="anthropic", lock_model="claude-s"
        )
        return await states.get("umo:u")

    st = run(_main())
    assert st.lock_provider_id == "anthropic"
    assert st.lock_model == "claude-s"


def test_lock_model_snapshot_restore_roundtrip():
    """session snapshot/restore 往返保留 lock_model 字段。"""
    states = SessionStateStore()

    async def _main():
        await states.update(
            "umo:s", round=1, lock_provider_id="openai", lock_model="gpt-5-mini"
        )
        snap = states.snapshot()
        assert snap["umo:s"]["lock_model"] == "gpt-5-mini"
        fresh = SessionStateStore()
        fresh.restore(snap)
        return await fresh.get("umo:s")

    st = run(_main())
    assert st.lock_model == "gpt-5-mini"
    assert st.lock_provider_id == "openai"
    assert st.round == 1


def test_lock_model_old_snapshot_restore_defaults():
    """旧快照（无 lock_model 键）restore 不报错且回退默认 None。"""
    states = SessionStateStore()

    async def _main():
        await states.update("umo:old", round=3, stage="MAIN", lock_provider_id="openai")
        snap = states.snapshot()
        snap["umo:old"].pop("lock_model", None)  # 模拟旧版本快照
        fresh = SessionStateStore()
        fresh.restore(snap)
        return await fresh.get("umo:old")

    st = run(_main())
    assert st.lock_model is None
    assert st.lock_provider_id == "openai"
    assert st.round == 3
    assert st.stage == "MAIN"
