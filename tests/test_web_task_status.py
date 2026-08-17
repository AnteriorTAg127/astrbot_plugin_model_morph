"""web/api.py Agent 生成任务状态 + SSE 断连续跑测试（模块 W 扩展，v1.0.3）。

dict / 状态级纯逻辑测试（不启动 AstrBot 运行时）：
- 插件以 ``data.plugins.<插件名>`` 包形式被 AstrBot 加载（相对导入），因此本文件把
  AstrBot 主仓库根加入 sys.path 后按同路径导入
  ``data.plugins.astrbot_plugin_model_morph.web.api``（与 test_web_pending.py 同导入方式）；
- ``_handler_agent_task_status`` 为纯 async 函数（无框架装饰器），构造 mock plugin 直接调用，
  解码 JSONResponse 断言；
- SSE 断连续跑核心行为（``_web_agent_task`` + ``_agent_chat_stream_gen``）可离线模拟：
  monkeypatch ``api.run_web_agent_stream`` 为最小异步生成器，手动取消 SSE 生成器（模拟切页 /
  断开），验证后台任务不被取消、done 写回 ChatStore、任务状态被清理。
  该模拟不依赖 AstrBot 运行时（不真正调 Provider / ToolLoopAgentRunner）。

覆盖：
- 端点注册存在（register_all 记录断言）；
- task-status：无任务 → {running:false, cid:null, started_at:null}；
- task-status：有运行任务 → running=true + cid/started_at 正确；
- task-status：任务结束（done）→ running=false 且清理后再次查询为无任务；
- 断连续跑：SSE 断开后后台任务继续跑完并写回 assistant、清理任务状态；
- 正常流（未断开）：meta → delta → done → finish 帧序 + 写回；
- error 帧路径：流中出错不写半截 assistant。

（所有 asyncio 对象均在事件循环内创建，避免 "no running event loop"；每次测试用
独立事件循环，与 test_web_pending.py 一致。）
"""

import asyncio
import json
import sys
import types
from pathlib import Path

# AstrBot 主仓库根加入 sys.path：使 astrbot 包与 data.plugins.<插件名> 包可导入。


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

from data.plugins.astrbot_plugin_model_morph.web import api as api  # noqa: E402

CID = "conv-123"


class _PendingStub:
    """最小 pending 替身：get() 返回预设值（finish 帧用）。"""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class FakeChats:
    """记录 append_message 调用的 ChatStore 替身。"""

    def __init__(self):
        self.appended: list[tuple[str, str, str]] = []

    def append_message(self, cid, role, content):
        self.appended.append((cid, role, content))


class FakeAudit:
    """记录审计条目。"""

    def __init__(self):
        self.entries = []

    def add(self, entry):
        self.entries.append(entry)


class FakePlugin:
    """最小插件替身：只暴露断连续跑路径访问的属性。"""

    def __init__(self, pending=None):
        self.context = types.SimpleNamespace()
        self.tool_ctx = types.SimpleNamespace(pending=_PendingStub(pending))
        self.chats = FakeChats()
        self.audit = FakeAudit()
        self._agent_task = None


def _decode(resp, status=200):
    """解码 JSONResponse，断言 HTTP 状态码后返回 body dict。"""
    assert resp.status_code == status, resp.body.decode("utf-8", "replace")
    return json.loads(resp.body.decode("utf-8"))


def _run(coro):
    """在独立事件循环里跑完协程并返回结果（asyncio.run 负责清理/shutdown）。"""
    return asyncio.run(coro)


def _not_done():
    """事件循环内构造一个未完成的 Future（done()==False，模拟「运行中」）。"""

    async def _make():
        return asyncio.Future()

    return _run(_make())


def _done():
    """事件循环内构造一个已完成的 Future（done()==True，模拟「已结束」）。"""

    async def _make():
        fut = asyncio.Future()
        fut.set_result(None)
        return fut

    return _run(_make())


# ------------------------------------------------------------------ #
# 端点注册
# ------------------------------------------------------------------ #


def test_task_status_endpoint_registered():
    """register_all 注册了 agent/task-status（GET）。"""
    calls: list[tuple] = []

    class FakeCtx:
        def register_web_api(self, route, invoke, methods, desc):  # noqa: ARG002
            calls.append((route, methods))

    api.register_all(types.SimpleNamespace(context=FakeCtx()))
    assert ("/astrbot_plugin_model_morph/agent/task-status", ["GET"]) in calls


# ------------------------------------------------------------------ #
# agent/task-status（纯状态读取）
# ------------------------------------------------------------------ #


def test_task_status_no_task():
    """无任务 → {running:false, cid:null, started_at:null}，不报错。"""
    plugin = FakePlugin()
    data = _decode(_run(api._handler_agent_task_status(plugin)))
    assert data == {"running": False, "cid": None, "started_at": None}


def test_task_status_running_task():
    """有运行中的任务 → running=true + cid/started_at 正确。"""
    plugin = FakePlugin()
    plugin._agent_task = {
        "cid": CID,
        "started_at": "2026-08-20T10:30:00",
        "task": _not_done(),
        "_token": object(),
    }
    data = _decode(_run(api._handler_agent_task_status(plugin)))
    assert data == {"running": True, "cid": CID, "started_at": "2026-08-20T10:30:00"}


def test_task_status_finished_task_reports_not_running():
    """任务已结束（done）但状态尚未清理 → running=false。"""
    plugin = FakePlugin()
    plugin._agent_task = {
        "cid": CID,
        "started_at": "2026-08-20T10:30:00",
        "task": _done(),
        "_token": object(),
    }
    data = _decode(_run(api._handler_agent_task_status(plugin)))
    assert data["running"] is False
    assert data["cid"] == CID


def test_task_status_cleared_after_finish():
    """任务结束并清理 _agent_task 后 → running=false + cid null。"""
    plugin = FakePlugin()
    assert plugin._agent_task is None
    data = _decode(_run(api._handler_agent_task_status(plugin)))
    assert data["running"] is False and data["cid"] is None


# ------------------------------------------------------------------ #
# 断连续跑核心行为（离线模拟，不依赖 AstrBot 运行时）
# ------------------------------------------------------------------ #


async def _collect(frames, gen):
    """把一个异步生成器跑完，收集全部 SSE 帧字符串到 frames。"""
    async for frame in gen:
        frames.append(frame)


def test_detach_continue_run_writes_back_on_disconnect(monkeypatch):
    """SSE 断开后后台任务不被取消，继续跑完并写回 assistant + 清理状态。

    模拟：monkeypatch ``api.run_web_agent_stream`` 为最小异步生成器（delta → done）；
    用驱动任务消费 ``_agent_chat_stream_gen``；消费到 delta 后取消驱动任务（模拟客户端
    断开 → CancelledError 注入生成器的 ``await queue.get()``）；随后等待后台任务
    ``_web_agent_task`` 自然跑完，断言 assistant 已写回、_agent_task 已清理。
    """

    async def fake_stream(context, tc, history, pid):
        yield {"type": "delta", "text": "hello"}
        yield {"type": "done", "reply": "hello there"}

    monkeypatch.setattr(api, "run_web_agent_stream", fake_stream)

    async def _scenario():
        plugin = FakePlugin(pending={"ops": []})
        q: asyncio.Queue = asyncio.Queue()
        token = object()
        task = asyncio.create_task(
            api._web_agent_task(plugin, "pid", CID, [], q, token)
        )
        plugin._agent_task = {
            "cid": CID,
            "started_at": "2026-08-20T10:30:00",
            "task": task,
            "_token": token,
        }

        frames: list[str] = []
        gen = api._agent_chat_stream_gen(plugin, CID, "t", q, task)
        driver = asyncio.ensure_future(_collect(frames, gen))

        # 消费到 delta 帧后取消驱动任务（模拟 SSE 断开）。
        while not any('"type": "delta"' in f for f in frames):
            await asyncio.sleep(0.005)
        driver.cancel()
        try:
            await driver
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        # 后台任务不被取消，自然跑完。
        await asyncio.wait_for(task, timeout=5)

        return plugin, task

    plugin, task = _run(_scenario())
    # 写回 assistant（在后台任务内完成）。
    assert plugin.chats.appended == [(CID, "assistant", "hello there")]
    assert task.done() and not task.cancelled()
    # 任务状态已清理。
    assert plugin._agent_task is None


def test_detach_full_stream_normal_completion(monkeypatch):
    """正常流（客户端未断开）：生成器转发 meta → delta → done → finish，后台写回。"""

    async def fake_stream(context, tc, history, pid):
        yield {"type": "delta", "text": "hi"}
        yield {"type": "done", "reply": "hi there"}

    monkeypatch.setattr(api, "run_web_agent_stream", fake_stream)

    async def _scenario():
        plugin = FakePlugin(pending={"ops": []})
        q: asyncio.Queue = asyncio.Queue()
        token = object()
        task = asyncio.create_task(
            api._web_agent_task(plugin, "pid", CID, [], q, token)
        )
        plugin._agent_task = {
            "cid": CID,
            "started_at": "2026-08-20T10:30:00",
            "task": task,
            "_token": token,
        }
        frames: list[str] = []
        gen = api._agent_chat_stream_gen(plugin, CID, "t", q, task)
        await _collect(frames, gen)
        await asyncio.wait_for(task, timeout=5)
        return plugin, task, frames

    plugin, task, frames = _run(_scenario())
    types_seen = [json.loads(f[6:])["type"] for f in frames]
    assert types_seen[0] == "meta"
    assert "delta" in types_seen
    assert "done" in types_seen
    assert "finish" in types_seen
    assert plugin.chats.appended == [(CID, "assistant", "hi there")]
    assert plugin._agent_task is None


def test_web_agent_task_error_does_not_write_half_reply(monkeypatch):
    """error 帧路径：流中出错不写半截 assistant，任务结束并清理。"""

    async def fake_stream(context, tc, history, pid):
        yield {"type": "delta", "text": "partial"}
        yield {"type": "error", "message": "boom"}

    monkeypatch.setattr(api, "run_web_agent_stream", fake_stream)

    async def _scenario():
        plugin = FakePlugin()
        q: asyncio.Queue = asyncio.Queue()
        token = object()
        task = asyncio.create_task(
            api._web_agent_task(plugin, "pid", CID, [], q, token)
        )
        plugin._agent_task = {
            "cid": CID,
            "started_at": "2026-08-20T10:30:00",
            "task": task,
            "_token": token,
        }
        await asyncio.wait_for(task, timeout=5)
        return plugin

    plugin = _run(_scenario())
    # 出错不写 assistant（避免半截回复落库）。
    assert plugin.chats.appended == []
    assert plugin._agent_task is None
