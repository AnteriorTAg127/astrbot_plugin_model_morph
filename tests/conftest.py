"""pytest 共享 fixture：FakeAdapter、make_store（临时数据目录）、make_engine、meta。

约束：
- 临时数据目录固定建在 ``开发/v0.1/.pytest_tmp`` 下
  （沙箱禁止系统 tempfile / pytest tmp_path）。
- ``make_store`` 每次调用在 .pytest_tmp 下创建唯一子目录并返回新 ``ConfigStore``，
  保证单测内互不影响、且多个 pytest 进程（并行子 Agent）互不冲突。
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# 插件根目录加入 sys.path（使 ``import scheduler`` 可解析）。
# 从本文件位置推导插件根目录（可移植：不硬编码开发者本机路径）。
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# 临时数据根：开发/v0.1/.pytest_tmp（沙箱可写，禁止 tempfile/tmp_path）。
_PYTEST_TMP = _PLUGIN_ROOT / "开发" / "v0.1" / ".pytest_tmp"


class FakeAdapter:
    """模拟 compat.RuntimeAdapter：可切换 provider 集合 / 当前 provider /
    时区 / 时间 / 通话记录。

    额外属性：
    - ``calls``: 记录 ``set_provider(provider_id, umo)`` 调用。
    - ``cid``: ``get_conversation_id`` 的返回值（可用做 conversation_id 变化测试）。
    - ``local``: ``is_local`` 返回值；设为 False 测试第三方 runner 跳过。
    - ``fail_on``: 一个属性名，访问/调用时抛异常（异常兜底测试）。
    - ``now_dt``: ``now()`` 返回的 datetime（可切换）。
    """

    def __init__(
        self,
        provider_ids,
        current=None,
        tz_name="Asia/Shanghai",
        enabled=True,
        debug=False,
    ):
        self._ids = set(provider_ids)
        self._current = current or (sorted(provider_ids)[0] if provider_ids else None)
        self.calls = []
        self.cid = "conv-1"
        self.local = True
        self.enabled = enabled
        self.debug = debug
        self.tz = ZoneInfo(tz_name)
        self.now_dt = datetime(2026, 6, 10, 14, 0, tzinfo=self.tz)
        self.fail_on = None  # 命中的调用点（method 名）抛异常

    def _maybe_fail(self, method):
        if self.fail_on and method in self.fail_on:
            raise RuntimeError(f"FakeAdapter 注入异常 at {method}")

    def provider_ids(self):
        self._maybe_fail("provider_ids")
        return set(self._ids)

    def is_local(self):
        self._maybe_fail("is_local")
        return self.local

    def is_enabled(self):
        self._maybe_fail("is_enabled")
        return self.enabled

    def is_debug(self):
        self._maybe_fail("is_debug")
        return self.debug

    def get_timezone(self):
        self._maybe_fail("get_timezone")
        return self.tz

    def set_settings(self, settings):
        self._settings = settings

    async def get_conversation_id(self, umo):
        self._maybe_fail("get_conversation_id")
        return self.cid

    async def set_provider(self, provider_id, umo):
        self._maybe_fail("set_provider")
        self.calls.append((provider_id, umo))
        self._current = provider_id

    def current_provider_id(self, umo):
        self._maybe_fail("current_provider_id")
        return self._current

    def now(self):
        self._maybe_fail("now")
        return self.now_dt


def make_store():
    """创建 ConfigStore：数据目录为 .pytest_tmp 下每次调用唯一的子目录。

    并行安全：多个 pytest 进程（子 Agent 并行开发）互不清理对方目录。
    """
    import uuid as _uuid

    d = _PYTEST_TMP / _uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    from scheduler.persistence import ConfigStore

    return ConfigStore(d)


def make_engine(store, adapter=None, groups_patch=None, rules_patch=None):
    """组装一个可用的 SchedulerEngine（默认空配置 + FakeAdapter）。"""
    from scheduler.engine import SchedulerEngine
    from scheduler.groups import ModelGroupManager
    from scheduler.lifecycle import LifecycleEngine
    from scheduler.logs import SchedulerLog
    from scheduler.rules import RuleEngine
    from scheduler.state import SessionStateStore

    cfg = store.load()
    if groups_patch is not None:
        cfg["groups"] = groups_patch
    if rules_patch is not None:
        cfg["rules"] = rules_patch
    if groups_patch is not None or rules_patch is not None:
        store.save(cfg)

    states = SessionStateStore()
    groups = ModelGroupManager(store)
    rules = RuleEngine(store)
    lifecycles = LifecycleEngine(store)
    slog = SchedulerLog(retention=100)
    if adapter is None:
        adapter = FakeAdapter(["prov-a", "prov-b", "prov-c"])
    engine = SchedulerEngine(store, states, groups, rules, lifecycles, slog, adapter)
    return engine, adapter, store, states, slog


@pytest.fixture
def make_store_f():
    return make_store


def meta_f(umo="umo:group:1", **overrides):
    base = {
        "umo": umo,
        "platform_id": "p1",
        "platform_name": "aiocqhttp",
        "group_id": "12345",
        "sender_id": "user1",
        "is_group": True,
        "message_type": "group",
        "message_str": "hello",
        "at_bot": False,
    }
    base.update(overrides)
    return base


def run(coro):
    """把 asyncio 协程跑完并返回结果（无 pytest-asyncio 时用）。"""
    return asyncio.run(coro)
