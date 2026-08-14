"""state 模块测试：A/B umo 隔离、reset 语义、并发、mark_pending_reset、
snapshot/restore 往返。"""

import asyncio

from conftest import run
from scheduler.state import SessionState, SessionStateStore


def test_ab_isolation():
    states = SessionStateStore()

    async def _main():
        await states.update("umo:a", round=3, stage="MAIN")
        await states.update("umo:b", round=1)
        sa = await states.get("umo:a")
        sb = await states.get("umo:b")
        return sa, sb

    sa, sb = run(_main())
    assert sa.round == 3 and sa.stage == "MAIN"
    assert sb.round == 1 and sb.stage == "NEW"


def test_reset_semantics_preserves_lock():
    states = SessionStateStore()

    async def _main():
        await states.update("umo:1", round=7, stage="MAIN", lifecycle_id="lc1")
        await states.update("umo:1", lock_group_id="g_lock", lock_provider_id="p_lock")
        await states.update("umo:1", pending_reset=True)
        # 写游标
        st = await states.get("umo:1")
        st.group_cursor["g1"] = {
            "rr": 2,
            "uses": {"p": 3},
            "cooldown_until": {"p": 100.0},
        }
        await states.update("umo:1", group_cursor=st.group_cursor, last_rule_id="r1")
        await states.update("umo:1", last_trace={"matched": True})
        res = await states.reset("umo:1", "cid-new")
        return res

    res = run(_main())
    assert res.round == 0
    assert res.stage == "NEW"
    assert res.lifecycle_id is None
    assert res.pending_reset is False
    assert res.group_cursor == {}
    assert res.last_rule_id is None
    assert res.last_trace is None
    assert res.conversation_id == "cid-new"
    # 锁保留
    assert res.lock_group_id == "g_lock"
    assert res.lock_provider_id == "p_lock"


def test_mark_pending_reset():
    states = SessionStateStore()

    async def _main():
        await states.update("umo:r", round=5)
        await states.mark_pending_reset("umo:r")
        return await states.get("umo:r")

    st = run(_main())
    assert st.pending_reset is True
    assert st.round == 5  # 仅标记，不改 round


def test_snapshot_restore_roundtrip():
    states = SessionStateStore()

    async def _main():
        await states.update("umo:a", round=2, stage="MAIN", current_group_id="g1")
        await states.update("umo:b", round=0, stage="NEW")
        snap = states.snapshot()
        fresh = SessionStateStore()
        fresh.restore(snap)
        return fresh

    fresh = run(_main())
    all_st = run(fresh.all_states())
    by_umo = {s.umo: s for s in all_st}
    assert by_umo["umo:a"].round == 2
    assert by_umo["umo:a"].stage == "MAIN"
    assert by_umo["umo:b"].round == 0


def test_concurrent_updates_isolation():
    states = SessionStateStore()

    async def _main():
        async def write_a(i):
            await states.update("umo:a", round=i + 1)

        async def write_b(i):
            await states.update("umo:b", round=i)

        await asyncio.gather(
            *[write_a(i) for i in range(50)], *[write_b(i) for i in range(50)]
        )
        sa = await states.get("umo:a")
        sb = await states.get("umo:b")
        return sa, sb

    sa, sb = run(_main())
    # 每个 umo 的 50 次写入全部落到对应 umo（无串扰、无丢失），最终为其自身上限
    assert sa.round == 50
    assert sb.round == 49


def test_concurrent_single_umo_no_lost_writes():
    states = SessionStateStore()

    async def _main():
        async def write(i):
            await states.update("umo:c", round=i + 1)

        await asyncio.gather(*[write(i) for i in range(100)])
        return (await states.get("umo:c")).round

    final = run(_main())
    assert final == 100


def test_remove():
    states = SessionStateStore()

    async def _main():
        await states.update("umo:d", round=1)
        existed = await states.remove("umo:d")
        after = await states.get("umo:d")
        return existed, after

    existed, after = run(_main())
    assert existed is True
    assert after.round == 0  # 移除后重新 get 得到新建默认状态
    # 不存在的 umo
    assert run(states.remove("umo:missing")) is False


def test_to_from_dict_roundtrip():
    st = SessionState(
        umo="u",
        round=4,
        stage="PERIODIC",
        current_group_id="g1",
        current_provider_id="p1",
        lock_group_id="lg",
        group_cursor={"g1": {"rr": 1, "uses": {"p": 2}, "cooldown_until": {"p": 99.0}}},
        last_trace={"matched": True},
    )
    back = SessionState.from_dict(st.to_dict(), "u")
    assert back.umo == "u"
    assert back.round == 4
    assert back.stage == "PERIODIC"
    assert back.group_cursor["g1"]["rr"] == 1
    assert back.last_trace == {"matched": True}
    # 容忍缺字段
    sparse = SessionState.from_dict({"round": 2}, "u2")
    assert sparse.stage == "NEW"
    assert sparse.group_cursor == {}


# ---- v0.1.6：会话校准状态三字段 ----


def test_calibration_fields_roundtrip():
    """校准三字段 to_dict → from_dict 往返一致。"""
    st = SessionState(
        umo="u",
        calibration_rounds_left=5,
        calibration_group_id="g_cal",
        calibration_reason="context_compression",
    )
    data = st.to_dict()
    assert data["calibration_rounds_left"] == 5
    assert data["calibration_group_id"] == "g_cal"
    assert data["calibration_reason"] == "context_compression"
    back = SessionState.from_dict(data, "u")
    assert back.calibration_rounds_left == 5
    assert back.calibration_group_id == "g_cal"
    assert back.calibration_reason == "context_compression"


def test_calibration_fields_defaults():
    """未显式提供校准字段时取默认值（0 / None / ""）。"""
    st = SessionState(umo="u")
    assert st.calibration_rounds_left == 0
    assert st.calibration_group_id is None
    assert st.calibration_reason == ""


def test_calibration_old_snapshot_restore_defaults():
    """旧快照（无校准三字段）restore 成功且取默认值，不因缺键报错。"""
    states = SessionStateStore()

    async def _main():
        # 无校准字段的旧快照
        await states.update("umo:old", round=3, stage="MAIN")
        snap = states.snapshot()
        snap["umo:old"].pop("calibration_rounds_left", None)
        snap["umo:old"].pop("calibration_group_id", None)
        snap["umo:old"].pop("calibration_reason", None)
        fresh = SessionStateStore()
        fresh.restore(snap)
        return await fresh.get("umo:old")

    st = run(_main())
    # 旧字段语义不变
    assert st.round == 3
    assert st.stage == "MAIN"
    # 校准字段回到默认
    assert st.calibration_rounds_left == 0
    assert st.calibration_group_id is None
    assert st.calibration_reason == ""


def test_calibration_update_writes_and_reads_back():
    """update 写入校准三字段并通过 get 读回。"""
    states = SessionStateStore()

    async def _main():
        await states.update(
            "umo:u",
            calibration_rounds_left=4,
            calibration_group_id="g_cal",
            calibration_reason="context_compression",
        )
        return await states.get("umo:u")

    st = run(_main())
    assert st.calibration_rounds_left == 4
    assert st.calibration_group_id == "g_cal"
    assert st.calibration_reason == "context_compression"


def test_calibration_cleared_on_reset():
    """reset 清除校准三字段，同时保留手动锁定（现有锁语义不变）。"""
    states = SessionStateStore()

    async def _main():
        await states.update(
            "umo:u", round=6, lifecycle_id="lc1", lock_group_id="g_lock"
        )
        await states.update(
            "umo:u",
            calibration_rounds_left=5,
            calibration_group_id="g_cal",
            calibration_reason="context_compression",
        )
        res = await states.reset("umo:u", "cid-new")
        return res

    res = run(_main())
    assert res.calibration_rounds_left == 0
    assert res.calibration_group_id is None
    assert res.calibration_reason == ""
    # reset 既有语义不动：round/stage 归零，锁保留
    assert res.round == 0
    assert res.stage == "NEW"
    assert res.lock_group_id == "g_lock"
