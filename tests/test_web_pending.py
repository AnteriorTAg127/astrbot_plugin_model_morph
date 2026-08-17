"""web/api.py 审批端点 / pending 扩展 / 会话锁模型 / dashboard 强锁摘要测试（模块 W，v1.0.3）。

dict 级纯逻辑测试（不启动 AstrBot 运行时）：
- 插件以 ``data.plugins.<插件名>`` 包形式被 AstrBot 加载（相对导入），因此本文件把
  AstrBot 主仓库根加入 sys.path 后按同路径导入 ``data.plugins.astrbot_plugin_model_morph.web.api``
  （与 test_main_commands.py 同一导入方式）；
- handler 均为纯 async 函数（无框架装饰器，见 web/api.py 的 ``_register`` 闭包），构造
  mock plugin 直接调用，asyncio.run 驱动；
- POST body 通过 ``astrbot.api.web.bind_request_context`` 绑定假请求对象；无 body 时
  不绑定（``_body`` 兜底为 {}，等价于「无 body → 默认当前唯一」）；
- ``json_response`` / ``error_response`` 用 AstrBot 真实实现，解码 JSONResponse 断言。

覆盖（task §6 / 分工.md 模块 W 完成标准）：
- agent/approve：ok:true → 200 且返回 {ok, applied, summary}；ok:false → error；
  无 body → 默认「当前唯一」；带 pending_id / id 不匹配；
- agent/reject：同 approve（含 summary 回显）；
- agent/pending：pending_view 返回 None → {}；有暂存 → 含 summary / staged_at；
- sessions/lock：provider+model 分支 states.update 参数断言；group_id 分支兼容；
  都无 → 400；
- sessions/unlock 清除 lock_model 断言；
- sessions 列表包含 lock_model / lock_label；
- dashboard force_lock 存在 / 缺失 / 多会话两态。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

# AstrBot 主仓库根加入 sys.path：使 astrbot 包与 data.plugins.<插件名> 包可导入。
# （conftest 已注入插件根；此处再注入主仓库根，供包式加载。）


def _find_astrbot_root():
    d = Path(__file__).resolve().parent
    while d.parent != d:
        if (d / "astrbot").is_dir():
            return d
        d = d.parent
    return None


_AB_ROOT = _find_astrbot_root()
assert _AB_ROOT is not None, "未找到 AstrBot 主仓库根（含 astrbot 包的上层目录）"
if str(_AB_ROOT) not in sys.path:
    sys.path.insert(0, str(_AB_ROOT))

from astrbot.api.web import bind_request_context  # noqa: E402

from data.plugins.astrbot_plugin_model_morph.web import api as api  # noqa: E402
from data.plugins.astrbot_plugin_model_morph.scheduler.agent_tools import (  # noqa: E402
    tool_delete_model_group,
)
from data.plugins.astrbot_plugin_model_morph.scheduler.state import (  # noqa: E402
    SessionStateStore,
)
from test_agent_tools import _make_tc, _seed_group  # noqa: E402

UMO = "p1:group:1"


class FakeEngine:
    """最小 engine 替身：dashboard() 返回可预测数据（供 force_lock 测试）。"""

    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {}

    def dashboard(self):
        return dict(self._payload)


class FakeReq:
    """假请求对象：json() 返回预设 body（供 bind_request_context 绑定）。"""

    def __init__(self, payload):
        self._payload = payload

    async def json(self, default=None):
        return self._payload if self._payload is not None else default


class FakePlugin:
    """最小插件替身：只暴露被测 handler 访问的属性。

    真实 ToolContext（复用 test_agent_tools 的 _make_tc，含 pending / audit / store /
    groups / rules），states 为真实 SessionStateStore（用 restore 同步预置）。
    """

    def __init__(self, tc, states=None, engine=None):
        self.tool_ctx = tc
        self.states = states if states is not None else SessionStateStore()
        self.engine = engine
        self.context = types.SimpleNamespace()
        self.audit = tc.audit
        self.groups = tc.groups
        self.store = tc.store


def _seed(states, umo, **fields):
    """向 SessionStateStore 同步预置一条状态（restore 直写，不碰 asyncio 锁）。"""
    snap = states.snapshot()
    data = {"umo": umo}
    data.update(fields)
    snap[umo] = data
    states.restore(snap)


def _call(handler, plugin, payload=None):
    """以给定 POST body 调用 handler；payload=None 表示不绑请求（_body 兜底 {}）。

    每个测试只创建一个事件循环（SessionStateStore 的 asyncio.Lock 绑定首个使用循环）。
    """

    async def _run():
        if payload is None:
            return await handler(plugin)
        with bind_request_context(FakeReq(payload)):
            return await handler(plugin)

    return asyncio.run(_run())


def _decode(resp, status=200):
    """解码 JSONResponse，断言 HTTP 状态码后返回 body dict。"""
    assert resp.status_code == status, resp.body.decode("utf-8", "replace")
    return json.loads(resp.body.decode("utf-8"))


# ------------------------------------------------------------------ #
# 端点注册
# ------------------------------------------------------------------ #


def test_approve_reject_endpoints_registered():
    """register_all 注册了 agent/approve 与 agent/reject（POST）。"""
    calls: list[tuple] = []

    class FakeCtx:
        def register_web_api(self, route, invoke, methods, desc):  # noqa: ARG002 - 记录即可
            calls.append((route, methods))

    api.register_all(types.SimpleNamespace(context=FakeCtx()))
    assert ("/astrbot_plugin_model_morph/agent/approve", ["POST"]) in calls
    assert ("/astrbot_plugin_model_morph/agent/reject", ["POST"]) in calls


# ------------------------------------------------------------------ #
# agent/approve
# ------------------------------------------------------------------ #


def _stage_delete_group(tc):
    """预置一个暂存：删除默认组，返回 staged 返回 dict（含 pending_id / summary）。"""
    _seed_group(tc)
    return tool_delete_model_group(tc, group_id="default-chat")


def test_agent_approve_no_body_applies_current_unique():
    """无 body（pending_id 缺省）→ 应用当前唯一暂存，返回 {ok, applied, summary}。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    staged = _stage_delete_group(tc)
    assert staged["status"] == "staged"
    plugin = FakePlugin(tc)
    resp = _call(api._handler_agent_approve, plugin)
    data = _decode(resp)
    assert data["ok"] is True
    assert data["applied"] == 1
    assert data["summary"] == staged["summary"]
    # 真实落库：组已删、暂存已清
    assert tc.groups.get("default-chat") is None
    assert tc.pending.get() is None
    assert any(
        e["action"] == "approve" and e["target"] == staged["pending_id"]
        for e in tc.audit.recent()
    )


def test_agent_approve_with_pending_id():
    """带 pending_id → 应用该暂存。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    staged = _stage_delete_group(tc)
    plugin = FakePlugin(tc)
    resp = _call(
        api._handler_agent_approve, plugin, {"pending_id": staged["pending_id"]}
    )
    data = _decode(resp)
    assert data["ok"] is True and data["applied"] == 1


def test_agent_approve_error_no_pending():
    """无暂存 → apply_staged ok:false → error_response（400）。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    plugin = FakePlugin(tc)
    resp = _call(api._handler_agent_approve, plugin)
    data = _decode(resp, status=400)
    assert data["status"] == "error"
    assert "无待批准" in data["message"]


def test_agent_approve_id_mismatch_error():
    """pending_id 与当前暂存不匹配 → 400 错误。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    _stage_delete_group(tc)
    plugin = FakePlugin(tc)
    resp = _call(api._handler_agent_approve, plugin, {"pending_id": "p_wrong"})
    data = _decode(resp, status=400)
    assert data["status"] == "error"
    assert "不匹配" in data["message"]


# ------------------------------------------------------------------ #
# agent/reject
# ------------------------------------------------------------------ #


def test_agent_reject_discards_with_summary():
    """拒绝暂存：返回 {ok, discarded, summary}，组未被删除、审计 reject 写入。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    staged = _stage_delete_group(tc)
    plugin = FakePlugin(tc)
    resp = _call(
        api._handler_agent_reject, plugin, {"pending_id": staged["pending_id"]}
    )
    data = _decode(resp)
    assert data["ok"] is True
    assert data["discarded"] is True
    assert data["summary"] == staged["summary"]
    assert tc.groups.get("default-chat") is not None  # 未删除
    assert tc.pending.get() is None
    assert any(
        e["action"] == "reject" and e["target"] == staged["pending_id"]
        for e in tc.audit.recent()
    )


def test_agent_reject_no_body_discards_current():
    """无 body → 拒绝当前唯一暂存。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    staged = _stage_delete_group(tc)
    plugin = FakePlugin(tc)
    resp = _call(api._handler_agent_reject, plugin)
    data = _decode(resp)
    assert data["ok"] is True and data["discarded"] is True
    assert data["summary"] == staged["summary"]


def test_agent_reject_error_no_pending():
    """无暂存 → reject_staged ok:false → 400。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    plugin = FakePlugin(tc)
    resp = _call(api._handler_agent_reject, plugin)
    data = _decode(resp, status=400)
    assert data["status"] == "error"
    assert "无待放弃" in data["message"]


# ------------------------------------------------------------------ #
# agent/pending
# ------------------------------------------------------------------ #


def test_agent_pending_empty_returns_dict():
    """无暂存（pending_view 返回 None）→ 返回 {}。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    plugin = FakePlugin(tc)
    data = _decode(_call(api._handler_agent_pending, plugin))
    assert data == {}


def test_agent_pending_extended_entry():
    """有暂存 → 返回含 pending_id / summary / staged_at 的扩展条目。"""
    tc, _, _ = _make_tc(agent_confirm=True)
    staged = _stage_delete_group(tc)
    plugin = FakePlugin(tc)
    data = _decode(_call(api._handler_agent_pending, plugin))
    assert data["pending_id"] == staged["pending_id"]
    assert data["summary"] == staged["summary"]
    assert data["staged_at"]
    assert data["ops"][0]["action"] == "delete_model_group"


# ------------------------------------------------------------------ #
# sessions/lock / unlock
# ------------------------------------------------------------------ #


def test_sessions_lock_provider_model_branch():
    """provider_id + model → 强锁模型：states.update 参数断言 + lock_label。"""
    tc, _, _ = _make_tc()
    states = SessionStateStore()
    _seed(states, UMO, lock_group_id="g_old")  # 已有锁组 → 强锁应清掉
    plugin = FakePlugin(tc, states=states)
    resp = _call(
        api._handler_sessions_lock,
        plugin,
        {"umo": UMO, "provider_id": "openai", "model": "gpt-5-mini"},
    )
    data = _decode(resp)
    assert data["locked"] is True
    assert data["lock_model"] == "gpt-5-mini"
    assert data["lock_label"] == "模型(openai @ gpt-5-mini)"
    st = states.snapshot()[UMO]
    assert st["lock_provider_id"] == "openai"
    assert st["lock_model"] == "gpt-5-mini"
    assert st["lock_group_id"] is None  # 强锁清锁组
    assert any(
        e["action"] == "sessions/lock(model)" and e["target"] == UMO
        for e in tc.audit.recent()
    )


def test_sessions_lock_group_branch_clears_model_lock():
    """group_id → 锁组（旧逻辑）：清 Provider / 模型锁定。"""
    tc, _, _ = _make_tc()
    states = SessionStateStore()
    _seed(states, UMO, lock_provider_id="openai", lock_model="gpt-5-mini")
    plugin = FakePlugin(tc, states=states)
    resp = _call(api._handler_sessions_lock, plugin, {"umo": UMO, "group_id": "g1"})
    data = _decode(resp)
    assert data == {"locked": True, "umo": UMO}
    st = states.snapshot()[UMO]
    assert st["lock_group_id"] == "g1"
    assert st["lock_provider_id"] is None
    assert st["lock_model"] is None


def test_sessions_lock_provider_only_legacy():
    """仅 provider_id（无 model）→ 旧版「锁 Provider」（兼容旧字段），不清历史模型锁。"""
    tc, _, _ = _make_tc()
    states = SessionStateStore()
    _seed(states, UMO, lock_model="gpt-5-mini")
    plugin = FakePlugin(tc, states=states)
    resp = _call(
        api._handler_sessions_lock, plugin, {"umo": UMO, "provider_id": "deepseek"}
    )
    data = _decode(resp)
    assert data["locked"] is True
    st = states.snapshot()[UMO]
    assert st["lock_provider_id"] == "deepseek"
    assert st["lock_model"] is None  # 旧锁被清、不再残留强锁
    assert st["lock_group_id"] is None


def test_sessions_lock_missing_target_400():
    """无 provider+model / group_id / provider_id → 400。"""
    tc, _, _ = _make_tc()
    plugin = FakePlugin(tc)
    resp = _call(api._handler_sessions_lock, plugin, {"umo": UMO})
    data = _decode(resp, status=400)
    assert data["status"] == "error"


def test_sessions_unlock_clears_lock_model():
    """unlock 同时清除 lock_group_id / lock_provider_id / lock_model。"""
    tc, _, _ = _make_tc()
    states = SessionStateStore()
    _seed(
        states,
        UMO,
        lock_group_id="g1",
        lock_provider_id="openai",
        lock_model="gpt-5-mini",
    )
    plugin = FakePlugin(tc, states=states)
    resp = _call(api._handler_sessions_unlock, plugin, {"umo": UMO})
    data = _decode(resp)
    assert data == {"locked": False, "umo": UMO}
    st = states.snapshot()[UMO]
    assert st["lock_group_id"] is None
    assert st["lock_provider_id"] is None
    assert st["lock_model"] is None


# ------------------------------------------------------------------ #
# sessions 列表
# ------------------------------------------------------------------ #


def test_sessions_list_lock_fields(monkeypatch):
    """列表每条含 lock_model 与 lock_label：模型 / 组 / 空串三态。"""
    tc, _, _ = _make_tc()
    _seed_group(tc, group_id="g_group", name="默认组")
    states = SessionStateStore()
    _seed(states, "p1:session:a", lock_provider_id="openai", lock_model="gpt-5-mini")
    _seed(states, "p1:session:b", lock_group_id="g_group")
    _seed(states, "p1:session:c")
    monkeypatch.setattr(
        api.compat,
        "get_provider_info_list",
        lambda ctx: [
            {
                "id": "openai",
                "model": "gpt-4o",
                "type": "chat_completion",
                "enabled": True,
            }
        ],
    )
    plugin = FakePlugin(tc, states=states)
    rows = _decode(_call(api._handler_sessions_list, plugin))
    by_umo = {r["umo"]: r for r in rows}
    assert by_umo["p1:session:a"]["lock_model"] == "gpt-5-mini"
    assert by_umo["p1:session:a"]["lock_label"] == "模型(openai @ gpt-5-mini)"
    assert by_umo["p1:session:b"]["lock_model"] is None
    assert by_umo["p1:session:b"]["lock_label"] == "组(默认组)"
    assert by_umo["p1:session:c"]["lock_label"] == ""


# ------------------------------------------------------------------ #
# dashboard force_lock
# ------------------------------------------------------------------ #


def test_dashboard_force_lock_present_absent_and_multi():
    """force_lock：单强锁对象 / 无强锁不含字段 / 多强锁首条 + count。"""
    tc, _, _ = _make_tc()

    # 单强锁 → 对象
    states1 = SessionStateStore()
    _seed(states1, "p1:session:x", lock_provider_id="openai", lock_model="gpt-5-mini")
    plugin1 = FakePlugin(tc, states=states1, engine=FakeEngine({"session_count": 1}))
    data1 = _decode(_call(api._handler_dashboard, plugin1))
    assert data1["force_lock"] == {
        "umo": "p1:session:x",
        "provider_id": "openai",
        "model": "gpt-5-mini",
    }
    assert data1["session_count"] == 1

    # 无强锁 → 不含 force_lock 字段
    states2 = SessionStateStore()
    _seed(states2, "p1:session:y", lock_provider_id="openai")  # 仅有 provider，非强锁
    _seed(states2, "p1:session:z")
    plugin2 = FakePlugin(tc, states=states2, engine=FakeEngine({"session_count": 2}))
    data2 = _decode(_call(api._handler_dashboard, plugin2))
    assert "force_lock" not in data2

    # 多强锁 → 首条 + count
    states3 = SessionStateStore()
    _seed(states3, "p1:session:a", lock_provider_id="openai", lock_model="gpt-5-mini")
    _seed(states3, "p1:session:b", lock_provider_id="qwen", lock_model="qwen-max")
    plugin3 = FakePlugin(tc, states=states3, engine=FakeEngine({}))
    data3 = _decode(_call(api._handler_dashboard, plugin3))
    assert data3["force_lock"]["umo"] == "p1:session:a"
    assert data3["force_lock"]["provider_id"] == "openai"
    assert data3["force_lock"]["model"] == "gpt-5-mini"
    assert data3["force_lock"]["count"] == 2
