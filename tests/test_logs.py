"""logs 模块测试：环形截断、recent 筛选、clear、load_entries。"""

from scheduler.logs import SchedulerLog


def _entry(i, umo="u", level="info"):
    return {"time": f"t{i}", "umo": umo, "type": "switch", "level": level, "round": i}


def test_ring_truncation():
    log = SchedulerLog(retention=3)
    for i in range(5):
        log.add(_entry(i))
    assert len(log.to_list()) == 3
    assert log.to_list()[-1]["time"] == "t4"
    assert log.to_list()[0]["time"] == "t2"


def test_recent_newest_first():
    log = SchedulerLog(retention=10)
    for i in range(4):
        log.add(_entry(i))
    recent = log.recent()
    assert recent[0]["time"] == "t3"
    assert [r["time"] for r in recent] == ["t3", "t2", "t1", "t0"]


def test_recent_filter_by_umo_and_level():
    log = SchedulerLog(retention=10)
    log.add(_entry(1, umo="a", level="info"))
    log.add(_entry(2, umo="b", level="error"))
    log.add(_entry(3, umo="a", level="error"))
    assert [e["round"] for e in log.recent(umo="a")] == [3, 1]
    assert [e["round"] for e in log.recent(level="error")] == [3, 2]
    assert [e["round"] for e in log.recent(umo="a", level="error")] == [3]


def test_recent_limit():
    log = SchedulerLog(retention=10)
    for i in range(5):
        log.add(_entry(i))
    limited = log.recent(limit=2)
    assert len(limited) == 2
    assert limited[0]["time"] == "t4"


def test_clear():
    log = SchedulerLog(retention=5)
    log.add(_entry(1))
    log.clear()
    assert log.to_list() == []
    assert log.recent() == []


def test_load_entries():
    log = SchedulerLog(retention=10)
    log.load_entries([_entry(1), _entry(2, umo="x")])
    assert len(log.to_list()) == 2
    # 追加到尾部再可筛
    assert log.recent(umo="x")[0]["round"] == 2


def test_add_non_dict_ignored():
    log = SchedulerLog(retention=5)
    log.add(None)
    log.add("str")
    log.add(123)
    log.add(_entry(1))
    assert len(log.to_list()) == 1
