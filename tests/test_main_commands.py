"""main.py 指令测试（v1.0.3：lockmodel / approve / reject / pending / 状态文本）。

覆盖（模块 C 完成标准）：
- lockmodel：provider 存在 / 不存在、model 空、states.update 参数断言（lock_group_id
  被清空、lock_provider_id + lock_model 成对）、slog 写入、unlock 后 lock_model 清除；
- approve / reject：无 pending（agent_tools 返回 ok:false）→ 错误文本；有 pending →
  成功文本含 summary / 已放弃；无参 / 带 pending_id 两种调用；异常兜底；
- pending：无 / 有两条分支的文本（id + 时间 + summary + 审批提示）；
- _status_text 三种锁定状态（模型 / 组 / 否）文本断言；_model_text 强锁展示；
- 权限装饰器存在（源码字符串断言）；@register 版本号 1.0.3（star_map 断言）。

导入方式：插件以 ``data.plugins.<插件名>`` 包形式被 AstrBot 加载（相对导入），因此本
文件把 AstrBot 主仓库根加入 sys.path 后按同路径导入插件包，模拟真实加载；对
``agent_tools`` 的 mock 一律打在 ``plugin_main.agent_tools``（main.py 实际持有的模块对象）。
"""

import asyncio
import inspect
import sys
import types
from pathlib import Path
from unittest import mock

# AstrBot 主仓库根加入 sys.path：使 astrbot 包与 data.plugins.<插件名> 包可导入。
# 向上逐级查找含 astrbot 包（astrbot/ 目录）的仓库根，避免硬编码本机路径。


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

import data.plugins.astrbot_plugin_model_morph.main as plugin_main  # noqa: E402
from astrbot.core.star.star import star_map  # noqa: E402
from data.plugins.astrbot_plugin_model_morph.scheduler.logs import (  # noqa: E402
    SchedulerLog,
)
from data.plugins.astrbot_plugin_model_morph.scheduler.state import (  # noqa: E402
    SessionStateStore,
)

UMO = "umo:webchat:tc_1"


class FakeAdapter:
    """最小 ``RuntimeAdapter`` 替身：provider_ids / current_provider_id 两个只读方法。"""

    def __init__(self, provider_ids=("openai", "deepseek"), current="deepseek"):
        self._ids = set(provider_ids)
        self._current = current

    def provider_ids(self):
        return set(self._ids)

    def current_provider_id(self, umo):
        return self._current


class FakeGroups:
    """最小模型组管理替身：list_ / get 两个方法（_status_text / _find_group 用到）。"""

    def __init__(self, groups=None):
        self._groups = list(groups or [])

    def list_(self):
        return list(self._groups)

    def get(self, group_id):
        for g in self._groups:
            if g.get("id") == group_id:
                return g
        return None


def make_plugin(groups=None, current="deepseek"):
    """构造最小 ModelMorph 实例（绕过重量 __init__），仅装配被测方法依赖的属性。"""
    plugin = object.__new__(plugin_main.ModelMorph)
    plugin.states = SessionStateStore()
    plugin.slog = SchedulerLog(retention=100)
    plugin.groups = FakeGroups(groups)
    plugin.adapter = FakeAdapter(current=current)
    plugin._context = types.SimpleNamespace()
    # 审批指令仅把 tool_ctx 透传给被 patch 的 agent_tools 函数，SimpleNamespace 足够。
    plugin.tool_ctx = types.SimpleNamespace()
    return plugin


def make_event(umo=UMO):
    """构造最小事件替身：unified_msg_origin + plain_result(text)->text。"""
    event = types.SimpleNamespace(unified_msg_origin=umo)
    event.plain_result = lambda text: text
    return event


def collect(gen_factory):
    """在单个事件循环内跑完 handler 异步生成器，返回全部 yield 结果列表。

    Args:
        gen_factory: 返回 handler 生成器的零参 callable（生成器在循环内创建）。
    """
    return asyncio.run(_drain(gen_factory()))


async def _drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


# ------------------------------------------------------------------ #
# lockmodel
# ------------------------------------------------------------------ #


def test_lockmodel_provider_not_found():
    """provider 不存在 → 返回「未找到 Provider: xxx」，不改状态。"""
    plugin = make_plugin()
    out = collect(lambda: plugin.scheduler_lockmodel(make_event(), "kimi", "kimi-k2"))
    assert out == ["未找到 Provider: kimi"]
    state = asyncio.run(plugin.states.get(UMO))
    assert state.lock_model is None
    assert state.lock_provider_id is None


def test_lockmodel_empty_model_shows_usage():
    """model 为空（strip 后）→ 提示用法，含「模型名不含空格」。"""
    plugin = make_plugin()
    out = collect(lambda: plugin.scheduler_lockmodel(make_event(), "openai", "   "))
    assert len(out) == 1
    assert "用法：/scheduler lockmodel" in out[0]
    assert "模型名不含空格" in out[0]
    state = asyncio.run(plugin.states.get(UMO))
    assert state.lock_model is None


def test_lockmodel_success_sets_state_and_slog():
    """成功：states.update 写入 lock_provider_id+lock_model 并清空 lock_group_id；slog 记录。"""

    async def _main():
        await plugin.states.update(UMO, lock_group_id="g1")
        out = []
        async for item in plugin.scheduler_lockmodel(
            make_event(), "openai", "gpt-5-mini"
        ):
            out.append(item)
        return out

    plugin = make_plugin()
    out = asyncio.run(_main())
    assert len(out) == 1
    assert "已强制锁定本会话到模型: openai @ gpt-5-mini" in out[0]
    assert "优先级最高" in out[0]
    assert "规则/关键词/temporal 均不覆盖" in out[0]

    state = asyncio.run(plugin.states.get(UMO))
    assert state.lock_model == "gpt-5-mini"
    assert state.lock_provider_id == "openai"
    assert state.lock_group_id is None  # 强锁模型取代锁组

    latest = plugin.slog.recent(limit=1)
    assert latest and latest[0]["type"] == "lock_model"
    assert latest[0]["provider"] == "openai"
    assert latest[0]["model"] == "gpt-5-mini"
    assert latest[0]["umo"] == UMO
    assert latest[0]["reason"] == "指令强锁模型"


def test_unlock_clears_lock_model():
    """unlock 同时清除 lock_group_id / lock_provider_id / lock_model。"""

    async def _main():
        await plugin.states.update(
            UMO,
            lock_group_id="g1",
            lock_provider_id="openai",
            lock_model="gpt-5-mini",
        )
        out = []
        async for item in plugin.scheduler_unlock(make_event()):
            out.append(item)
        return out

    plugin = make_plugin()
    out = asyncio.run(_main())
    assert out == ["已解锁本会话，恢复自动调度。"]
    state = asyncio.run(plugin.states.get(UMO))
    assert state.lock_group_id is None
    assert state.lock_provider_id is None
    assert state.lock_model is None
    latest = plugin.slog.recent(limit=1)
    assert latest and latest[0]["type"] == "unlock"


# ------------------------------------------------------------------ #
# approve / reject / pending
# ------------------------------------------------------------------ #


def test_approve_no_pending_error():
    """无 pending（apply_staged 返回 ok:false）→ 明确错误文本。"""
    plugin = make_plugin()
    with mock.patch.object(
        plugin_main.agent_tools,
        "apply_staged",
        return_value={"ok": False, "error": "无待批准的暂存更改"},
    ) as m:
        out = collect(lambda: plugin.scheduler_approve(make_event(), ""))
    assert out == ["批准失败：无待批准的暂存更改"]
    m.assert_called_once_with(plugin.tool_ctx, "")


def test_approve_success_with_summary():
    """有 pending → 「已应用 N 项变更：」+ summary 逐行（- 前缀）。"""
    plugin = make_plugin()
    with mock.patch.object(
        plugin_main.agent_tools,
        "apply_staged",
        return_value={
            "ok": True,
            "applied": 2,
            "summary": ["删除条件规则「高峰期」（不可恢复）", "修改规则「夜间」"],
        },
    ):
        out = collect(lambda: plugin.scheduler_approve(make_event(), "p_ab12"))
    assert out == [
        "已应用 2 项变更：\n- 删除条件规则「高峰期」（不可恢复）\n- 修改规则「夜间」"
    ]


def test_approve_passes_pending_id():
    """带 pending_id 时原样传给 apply_staged。"""
    plugin = make_plugin()
    with mock.patch.object(
        plugin_main.agent_tools,
        "apply_staged",
        return_value={"ok": True, "applied": 1, "summary": ["x"]},
    ) as m:
        out = collect(lambda: plugin.scheduler_approve(make_event(), "p_ab12"))
    assert out == ["已应用 1 项变更：\n- x"]
    m.assert_called_once_with(plugin.tool_ctx, "p_ab12")


def test_approve_exception_guarded():
    """apply_staged 抛异常 → 指令兜底返回「批准异常：…」，不崩溃。"""
    plugin = make_plugin()
    with mock.patch.object(
        plugin_main.agent_tools, "apply_staged", side_effect=RuntimeError("boom")
    ):
        out = collect(lambda: plugin.scheduler_approve(make_event(), ""))
    assert len(out) == 1
    assert "批准异常" in out[0]
    assert "boom" in out[0]


def test_reject_success_with_id():
    """reject 成功（带 p id）→ 「已放弃暂存的变更：p_xxx」。"""
    plugin = make_plugin()
    with mock.patch.object(
        plugin_main.agent_tools,
        "reject_staged",
        return_value={"ok": True, "discarded": True},
    ) as m:
        out = collect(lambda: plugin.scheduler_reject(make_event(), "p_ab12"))
    assert out == ["已放弃暂存的变更：p_ab12"]
    m.assert_called_once_with(plugin.tool_ctx, "p_ab12")


def test_reject_success_without_id():
    """reject 成功（无参=当前唯一暂存）→ 「已放弃暂存的变更：（当前暂存）」。"""
    plugin = make_plugin()
    with mock.patch.object(
        plugin_main.agent_tools,
        "reject_staged",
        return_value={"ok": True, "discarded": True},
    ) as m:
        out = collect(lambda: plugin.scheduler_reject(make_event(), ""))
    assert out == ["已放弃暂存的变更：（当前暂存）"]
    m.assert_called_once_with(plugin.tool_ctx, "")


def test_reject_no_pending_error():
    """无 pending → 明确错误文本。"""
    plugin = make_plugin()
    with mock.patch.object(
        plugin_main.agent_tools,
        "reject_staged",
        return_value={"ok": False, "error": "无待放弃的暂存更改"},
    ):
        out = collect(lambda: plugin.scheduler_reject(make_event(), "p_x"))
    assert out == ["放弃失败：无待放弃的暂存更改"]


def test_pending_none():
    """无 pending → 「当前没有待审批的暂存变更。」"""
    plugin = make_plugin()
    with mock.patch.object(plugin_main.agent_tools, "pending_view", return_value=None):
        out = collect(lambda: plugin.scheduler_pending(make_event()))
    assert out == ["当前没有待审批的暂存变更。"]


def test_pending_has_entry():
    """有 pending → id + 时间 + summary 逐行 + 审批指令提示。"""
    plugin = make_plugin()
    view = {
        "pending_id": "p_ab12",
        "staged_at": "2026-08-01T12:00:00+08:00",
        "summary": ["删除条件规则「高峰期」（不可恢复）", "新建规则「夜间」"],
    }
    with mock.patch.object(plugin_main.agent_tools, "pending_view", return_value=view):
        out = collect(lambda: plugin.scheduler_pending(make_event()))
    assert len(out) == 1
    text = out[0]
    assert text.startswith("以下变更等待管理员审批：")
    assert "[p_ab12] 时间：2026-08-01T12:00:00+08:00" in text
    assert "- 删除条件规则「高峰期」（不可恢复）" in text
    assert "- 新建规则「夜间」" in text
    assert (
        "执行 /scheduler approve p_ab12 批准，或 /scheduler reject p_ab12 放弃" in text
    )


# ------------------------------------------------------------------ #
# _status_text / _model_text
# ------------------------------------------------------------------ #


def test_status_text_unlocked():
    """未锁定 → 「- 锁定: 否」。"""
    plugin = make_plugin()
    text = asyncio.run(plugin._status_text(make_event()))
    assert "【Model Morph 调度状态】" in text
    assert "锁定: 否" in text


def test_status_text_locked_group_with_name():
    """锁组且组存在 → 「- 锁定: 组(组名)」。"""
    plugin = make_plugin(groups=[{"id": "g_night", "name": "夜间省钱"}])

    async def _main():
        await plugin.states.update(UMO, lock_group_id="g_night")
        return await plugin._status_text(make_event())

    text = asyncio.run(_main())
    assert "- 锁定: 组(夜间省钱)" in text


def test_status_text_locked_group_without_name():
    """锁组但组不存在 → 「- 锁定: 组(组id)」兜底。"""
    plugin = make_plugin(groups=[])

    async def _main():
        await plugin.states.update(UMO, lock_group_id="g_orphan")
        return await plugin._status_text(make_event())

    text = asyncio.run(_main())
    assert "- 锁定: 组(g_orphan)" in text


def test_status_text_locked_model():
    """强锁模型 → 「- 锁定: 模型(provider @ model)」。"""
    plugin = make_plugin()

    async def _main():
        await plugin.states.update(
            UMO, lock_provider_id="openai", lock_model="gpt-5-mini"
        )
        return await plugin._status_text(make_event())

    text = asyncio.run(_main())
    assert "- 锁定: 模型(openai @ gpt-5-mini)" in text


def test_model_text_locked_model():
    """强锁时 /scheduler model 显示「当前模型(锁定): provider @ model」。"""
    plugin = make_plugin()

    async def _main():
        await plugin.states.update(
            UMO, lock_provider_id="openai", lock_model="gpt-5-mini"
        )
        return await plugin._model_text(make_event())

    text = asyncio.run(_main())
    assert text == "当前 Provider: openai\n当前模型(锁定): openai @ gpt-5-mini"


def test_model_text_unlocked_keeps_behavior():
    """未锁时维持原展示（Provider + 模型名），不出现「锁定」。"""
    plugin = make_plugin(current="deepseek")
    with mock.patch.object(
        plugin_main.compat,
        "get_provider_info_list",
        return_value=[
            {
                "id": "deepseek",
                "model": "deepseek-chat",
                "type": "chat_completion",
                "enabled": True,
            }
        ],
    ):
        text = asyncio.run(plugin._model_text(make_event()))
    assert "当前 Provider: deepseek" in text
    assert "当前模型: deepseek-chat" in text
    assert "锁定" not in text


# ------------------------------------------------------------------ #
# 权限装饰器 / 版本号
# ------------------------------------------------------------------ #


def test_new_commands_have_admin_permission():
    """四个新指令方法均带 permission_type(ADMIN) 装饰器（源码字符串断言）。"""
    src = inspect.getsource(plugin_main.ModelMorph)
    for cmd in ("lockmodel", "approve", "reject", "pending"):
        block = (
            f'    @scheduler.command("{cmd}")\n'
            f"    @filter.permission_type(filter.PermissionType.ADMIN)\n"
            f"    async def scheduler_{cmd}("
        )
        assert block in src, f"scheduler_{cmd} 缺少权限装饰器或装饰器顺序不对"


def test_register_version_1_0_3():
    """@register 版本号更新为 1.0.3（star_map 元数据断言）。"""
    meta = star_map.get(plugin_main.ModelMorph.__module__)
    assert meta is not None
    assert meta.version == "1.0.3"
