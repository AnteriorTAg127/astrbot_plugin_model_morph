"""web/api —— Model Morph 的插件 Web API（模块 F）。

把调度器各模块的读写能力暴露为 Dashboard Plugin Page 后端接口，供
``pages/model-morph`` 前端通过 ``window.AstrBotPluginPage`` bridge 调用。

约定：
- 路由统一以 ``/{PLUGIN_NAME}/<route>`` 注册（Plugin Page 机制要求带插件名前缀）。
- handler 用 ``astrbot.api.web`` 返回 ``json_response`` / ``error_response``。
- 所有写操作校验输入，失败返回 ``error_response(msg, 400)``。
- 每个 handler 加 try/except 兜底：未知异常 → ``error_response(str(e), 500)`` 且 ``logger.exception``。
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

# 注意：AstrBot 以包形式加载插件，包内导入必须使用相对导入。
from ..scheduler import agent_tools
from ..scheduler import compat
from ..scheduler.agent import run_web_agent, run_web_agent_stream
from ..scheduler.groups import GROUP_STRATEGIES
from ..scheduler.lifecycle import LIFECYCLE_TEMPLATES
from ..scheduler.presets import PRESETS, build_preset_rules
from ..scheduler.rules import ACTIONS

PLUGIN_NAME = "astrbot_plugin_model_morph"

# SSE 后台任务的结束哨兵：推入事件队列后，``_agent_chat_stream_gen`` 读到即结束转发。
_END = object()


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #


def _register(plugin, route: str, methods: list[str], desc: str, handler):
    """注册一条 Web API 路由。

    AstrBot 会以 ``handler(**path_params)`` 形式调用该 handler；本插件路由均无动态
    路径片段，因此用闭包把 ``plugin`` 预绑定，避免把插件实例当作路径参数误传。
    """

    def invoke(**kwargs):  # noqa: ARG001 - 兼容框架可能传入的路径参数
        return handler(plugin)

    plugin.context.register_web_api(f"/{PLUGIN_NAME}/{route}", invoke, methods, desc)


async def _body(default=None) -> dict:
    """读取 POST JSON 请求体（解析失败返回 default / 空 dict）。"""
    try:
        payload = await request.json(default=default if default is not None else {})
        if isinstance(payload, dict):
            return payload
        return dict(default if default is not None else {})
    except Exception:  # noqa: BLE001 - 读取失败兜底为默认
        return dict(default if default is not None else {})


def _require(obj: dict, key: str, type_=str, default=None):
    """从 dict 取指定类型字段；缺失或类型不符返回 default。"""
    val = obj.get(key, default)
    if val is None or not isinstance(val, type_):
        return default
    return val


def _audit(
    plugin, action: str, target, result="success", detail: str | None = None
) -> None:
    """追加一条 Web API 操作审计（source=manual, operator=admin，成功后调用）。

    仅记录关键字段（before/after 传 None），避免超大对象进入审计缓冲；
    ``detail`` 为可选说明（不记录全文，如会话消息保存提示）。
    """
    try:
        plugin.audit.add(
            {
                "time": datetime.now().isoformat(),
                "operator": "admin",
                "source": "manual",
                "action": action,
                "target": target,
                "before": None,
                "after": None,
                "result": result,
                "detail": detail,
            }
        )
    except Exception:  # noqa: BLE001 - 审计失败不阻断写操作
        logger.warning("web.api: 写审计失败（忽略）", exc_info=True)


# ---------------------------------------------------------------------- #
# 注册入口
# ---------------------------------------------------------------------- #


def register_all(plugin):
    """注册全部 Web API（在 main.py 的 ``__init__`` 中调用）。"""
    # ---- Dashboard / Settings ----
    _register(plugin, "dashboard", ["GET"], "调度器概览", _handler_dashboard)
    _register(plugin, "settings", ["GET"], "读取设置", _handler_settings_get)
    _register(plugin, "settings", ["POST"], "保存设置", _handler_settings_post)

    # ---- 模型组 ----
    _register(plugin, "groups", ["GET"], "模型组列表", _handler_groups_list)
    _register(plugin, "groups/save", ["POST"], "新建/更新模型组", _handler_groups_save)
    _register(plugin, "groups/delete", ["POST"], "删除模型组", _handler_groups_delete)
    _register(
        plugin, "groups/duplicate", ["POST"], "复制模型组", _handler_groups_duplicate
    )

    # ---- 规则 ----
    _register(plugin, "rules", ["GET"], "规则列表", _handler_rules_list)
    _register(plugin, "rules/save", ["POST"], "新建/更新规则", _handler_rules_save)
    _register(plugin, "rules/delete", ["POST"], "删除规则", _handler_rules_delete)
    _register(plugin, "rules/duplicate", ["POST"], "复制规则", _handler_rules_duplicate)

    # ---- 生命周期 ----
    _register(plugin, "lifecycles", ["GET"], "生命周期列表", _handler_lifecycles_list)
    _register(
        plugin,
        "lifecycles/save",
        ["POST"],
        "新建/更新生命周期",
        _handler_lifecycles_save,
    )
    _register(
        plugin,
        "lifecycles/delete",
        ["POST"],
        "删除生命周期",
        _handler_lifecycles_delete,
    )
    _register(
        plugin,
        "lifecycles/duplicate",
        ["POST"],
        "复制生命周期",
        _handler_lifecycles_duplicate,
    )
    _register(
        plugin,
        "lifecycles/templates",
        ["GET"],
        "生命周期模板",
        _handler_lifecycles_templates,
    )

    # ---- 会话 ----
    _register(plugin, "sessions", ["GET"], "活跃会话列表", _handler_sessions_list)
    _register(plugin, "sessions/lock", ["POST"], "锁定会话", _handler_sessions_lock)
    _register(plugin, "sessions/unlock", ["POST"], "解锁会话", _handler_sessions_unlock)
    _register(
        plugin, "sessions/reset", ["POST"], "重置会话调度", _handler_sessions_reset
    )
    _register(
        plugin, "sessions/remove", ["POST"], "移除会话状态", _handler_sessions_remove
    )

    # ---- 日志 / 模拟 / 导入导出 ----
    _register(plugin, "logs", ["GET"], "调度日志", _handler_logs_list)
    _register(plugin, "logs/clear", ["POST"], "清空调度日志", _handler_logs_clear)
    _register(plugin, "simulate", ["POST"], "调度模拟（Dry Run）", _handler_simulate)
    _register(plugin, "export", ["GET"], "导出配置", _handler_export)
    _register(plugin, "import", ["POST"], "导入配置", _handler_import)
    _register(plugin, "providers", ["GET"], "可用 Provider 列表", _handler_providers)
    _register(plugin, "platforms", ["GET"], "已注册平台实例列表", _handler_platforms)

    # ---- v0.1.5：时间调度规则（temporal）----
    _register(plugin, "temporal", ["GET"], "时间调度规则列表", _handler_temporal_list)
    _register(
        plugin,
        "temporal/save",
        ["POST"],
        "新建/更新时间调度规则",
        _handler_temporal_save,
    )
    _register(
        plugin,
        "temporal/delete",
        ["POST"],
        "删除时间调度规则",
        _handler_temporal_delete,
    )
    _register(
        plugin,
        "temporal/toggle",
        ["POST"],
        "启用/停用时间调度规则",
        _handler_temporal_toggle,
    )

    # ---- v0.1.5：校验 / 运行时 / 预设 ----
    _register(plugin, "validate", ["POST"], "全量配置校验", _handler_validate)
    _register(plugin, "runtime", ["GET"], "调度运行时状态", _handler_runtime)
    _register(plugin, "presets", ["GET"], "时间调度预设", _handler_presets_list)
    _register(
        plugin, "presets/apply", ["POST"], "套用时间调度预设", _handler_presets_apply
    )

    # ---- v0.1.5：Agent 配置层 / 审计 ----
    _register(plugin, "agent/pending", ["GET"], "待应用更改", _handler_agent_pending)
    # ---- v1.0.3：分级审批端点（与 /scheduler approve|reject 共用 agent_tools 公开入口 C8）----
    _register(plugin, "agent/approve", ["POST"], "批准暂存变更", _handler_agent_approve)
    _register(plugin, "agent/reject", ["POST"], "拒绝暂存变更", _handler_agent_reject)
    _register(plugin, "agent/chat", ["POST"], "AI 配置助手对话", _handler_agent_chat)
    _register(
        plugin,
        "agent/chat/stream",
        ["GET"],
        "AI 配置助手流式对话（SSE）",
        _handler_agent_chat_stream,
    )
    # ---- v1.0.3：Agent 生成任务状态（前端「进入时校验」断连续跑）----
    _register(
        plugin,
        "agent/task-status",
        ["GET"],
        "Agent 生成任务状态",
        _handler_agent_task_status,
    )
    _register(plugin, "agent/apply", ["POST"], "应用 AI 助手更改", _handler_agent_apply)
    _register(
        plugin,
        "agent/rollback",
        ["POST"],
        "回滚 AI 助手更改",
        _handler_agent_rollback,
    )
    # ---- v0.1.8：会话持久化（列表 / 切换 / 删除）----
    _register(
        plugin,
        "agent/conversations",
        ["GET"],
        "AI 助手会话列表 / 单个会话",
        _handler_agent_conversations,
    )
    _register(
        plugin,
        "agent/conversations/delete",
        ["POST"],
        "删除 AI 助手会话",
        _handler_agent_conversations_delete,
    )
    _register(plugin, "audit", ["GET"], "审计日志", _handler_audit_list)
    _register(plugin, "audit/clear", ["POST"], "清空审计日志", _handler_audit_clear)


# ---------------------------------------------------------------------- #
# 各 handler（注：handler 第一参数为插件对象，约定命名为 plugin）
# ---------------------------------------------------------------------- #


async def _handler_dashboard(plugin):
    """返回调度器概览数据；存在强锁模型会话时附加 ``force_lock`` 摘要（v1.0.3）。

    ``force_lock`` 为 ``{"umo", "provider_id", "model"}``（多会话时附 ``count``），
    前端兼容字符串 / 对象两种形态；无强锁会话时不含该字段。
    """
    try:
        data = dict(plugin.engine.dashboard())
        strong: list[dict] = []
        for st in await plugin.states.all_states():
            if st.lock_provider_id and st.lock_model:
                strong.append(
                    {
                        "umo": st.umo,
                        "provider_id": st.lock_provider_id,
                        "model": st.lock_model,
                    }
                )
        if strong:
            force_lock = dict(strong[0])
            if len(strong) > 1:
                force_lock["count"] = len(strong)
            data["force_lock"] = force_lock
        return json_response(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.dashboard 异常")
        return error_response(str(exc), status_code=500)


async def _handler_settings_get(plugin):
    """返回当前设置（enabled / debug 实时取自 AstrBot 原生配置面板）。"""
    try:
        settings = dict(plugin.store.get_settings())
        settings["enabled"] = plugin.adapter.is_enabled()
        settings["debug"] = plugin.adapter.is_debug()
        return json_response(settings)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.settings GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_settings_post(plugin):
    """校验并保存设置（enabled/debug 除外，二者由原生配置面板持有）并同步到 adapter。"""
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("设置须为对象", status_code=400)
        current = plugin.store.get_settings()

        if "timezone" in payload:
            tz = payload.get("timezone")
            if not isinstance(tz, str):
                return error_response("timezone 须为字符串", status_code=400)
            current["timezone"] = tz
        if "base_group" in payload:
            bg = payload.get("base_group")
            if not isinstance(bg, str):
                return error_response("base_group 须为字符串", status_code=400)
            current["base_group"] = bg
        if "log_retention" in payload:
            retention = payload.get("log_retention")
            if not isinstance(retention, int) or retention < 1:
                return error_response("log_retention 须为 >= 1 的整数", status_code=400)
            current["log_retention"] = retention
        # enabled / debug 由 AstrBot 原生配置面板持有，忽略页面传入值。
        if "state_persist" in payload:
            if not isinstance(payload["state_persist"], bool):
                return error_response("state_persist 须为布尔值", status_code=400)
            current["state_persist"] = payload["state_persist"]

        # v0.1.5：agent_confirm（高危操作须先预览再应用）与审计保留数。
        if "agent_confirm" in payload:
            if not isinstance(payload["agent_confirm"], bool):
                return error_response("agent_confirm 须为布尔值", status_code=400)
            current["agent_confirm"] = payload["agent_confirm"]
        if "audit_retention" in payload:
            retention = payload.get("audit_retention")
            if not isinstance(retention, int) or retention < 1:
                return error_response(
                    "audit_retention 须为 >= 1 的整数", status_code=400
                )
            current["audit_retention"] = retention

        # v0.1.6：default_lifecycle（全局默认生命周期）与 agent_provider_id（配置助手模型）。
        if "default_lifecycle" in payload:
            if not isinstance(payload["default_lifecycle"], str):
                return error_response("default_lifecycle 须为字符串", status_code=400)
            current["default_lifecycle"] = payload["default_lifecycle"]
        if "agent_provider_id" in payload:
            if not isinstance(payload["agent_provider_id"], str):
                return error_response("agent_provider_id 须为字符串", status_code=400)
            current["agent_provider_id"] = payload["agent_provider_id"]

        saved = plugin.store.update("settings", current)
        # 同步设置到运行时适配器（时区随之变更）。
        plugin.adapter.set_settings(current)
        # 同步 Agent 工具上下文的设置与时区，保证开关 / 时区一致性。
        plugin.tool_ctx.settings = current
        plugin.tool_ctx.tz = plugin.adapter.get_timezone()
        return json_response(saved.get("settings", current))
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.settings POST 异常")
        return error_response(str(exc), status_code=500)


# ---- 模型组 ---- #


async def _handler_groups_list(plugin):
    """返回全部模型组列表。"""
    try:
        return json_response(plugin.groups.list_())
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.groups GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_groups_save(plugin):
    """新建或更新模型组（body 含 ``id`` 且存在则更新，否则新建）。"""
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("模型组须为对象", status_code=400)
        raw = copy.deepcopy(payload)
        # 结构校验：providers 必须为列表且每个条目为对象
        if "providers" in raw and not isinstance(raw["providers"], list):
            return error_response("providers 须为列表", status_code=400)
        if "fallbacks" in raw and not isinstance(raw["fallbacks"], list):
            return error_response("fallbacks 须为列表", status_code=400)
        strategy = raw.get("strategy", "priority")
        if strategy not in GROUP_STRATEGIES:
            return error_response(
                f"strategy 非法，可选: {', '.join(GROUP_STRATEGIES)}", status_code=400
            )

        gid = raw.get("id")
        if isinstance(gid, str) and gid.strip() and plugin.groups.get(gid):
            updated = plugin.groups.update_group(gid, raw)
            _audit(plugin, "groups/save", gid)
            return json_response(updated)
        created = plugin.groups.create(raw)
        _audit(plugin, "groups/save", created.get("id"))
        return json_response(created)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.groups/save 异常")
        return error_response(str(exc), status_code=500)


async def _handler_groups_delete(plugin):
    """按 id 删除模型组。"""
    try:
        payload = await _body()
        gid = _require(payload, "id", str, "")
        if not gid:
            return error_response("缺少模型组 id", status_code=400)
        ok = plugin.groups.delete(gid)
        if not ok:
            return error_response(f"模型组 {gid} 不存在", status_code=404)
        _audit(plugin, "groups/delete", gid)
        return json_response({"deleted": True, "id": gid})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.groups/delete 异常")
        return error_response(str(exc), status_code=500)


async def _handler_groups_duplicate(plugin):
    """复制模型组。"""
    try:
        payload = await _body()
        gid = _require(payload, "id", str, "")
        if not gid:
            return error_response("缺少模型组 id", status_code=400)
        cloned = plugin.groups.duplicate(gid)
        if cloned is None:
            return error_response(f"模型组 {gid} 不存在", status_code=404)
        _audit(plugin, "groups/duplicate", gid)
        return json_response(cloned)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.groups/duplicate 异常")
        return error_response(str(exc), status_code=500)


# ---- 规则 ---- #


async def _handler_rules_list(plugin):
    """返回全部规则（按优先级降序）。"""
    try:
        return json_response(plugin.rules.list_())
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.rules GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_rules_save(plugin):
    """新建或更新规则。"""
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("规则须为对象", status_code=400)
        raw = copy.deepcopy(payload)
        when = raw.get("when")
        if when is not None and not isinstance(when, dict):
            return error_response("when 须为对象", status_code=400)
        then = raw.get("then")
        if then is not None and not isinstance(then, dict):
            return error_response("then 须为对象", status_code=400)
        if isinstance(then, dict):
            action = then.get("action", "switch_group")
            if action not in ACTIONS:
                return error_response(
                    f"动作非法，可选: {', '.join(ACTIONS)}", status_code=400
                )

        rid = raw.get("id")
        if isinstance(rid, str) and rid.strip():
            existing = [r for r in plugin.rules.list_() if r.get("id") == rid]
            if existing:
                updated = plugin.rules.update_rule(rid, raw)
                _audit(plugin, "rules/save", rid)
                return json_response(updated)
        created = plugin.rules.create_rule(raw)
        _audit(plugin, "rules/save", created.get("id"))
        return json_response(created)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.rules/save 异常")
        return error_response(str(exc), status_code=500)


async def _handler_rules_delete(plugin):
    """按 id 删除规则。"""
    try:
        payload = await _body()
        rid = _require(payload, "id", str, "")
        if not rid:
            return error_response("缺少规则 id", status_code=400)
        ok = plugin.rules.delete(rid)
        if not ok:
            return error_response(f"规则 {rid} 不存在", status_code=404)
        _audit(plugin, "rules/delete", rid)
        return json_response({"deleted": True, "id": rid})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.rules/delete 异常")
        return error_response(str(exc), status_code=500)


async def _handler_rules_duplicate(plugin):
    """复制规则。"""
    try:
        payload = await _body()
        rid = _require(payload, "id", str, "")
        if not rid:
            return error_response("缺少规则 id", status_code=400)
        cloned = plugin.rules.duplicate(rid)
        if cloned is None:
            return error_response(f"规则 {rid} 不存在", status_code=404)
        _audit(plugin, "rules/duplicate", rid)
        return json_response(cloned)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.rules/duplicate 异常")
        return error_response(str(exc), status_code=500)


# ---- 生命周期 ---- #


async def _handler_lifecycles_list(plugin):
    """返回全部生命周期策略。"""
    try:
        return json_response(plugin.lifecycles.list_())
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.lifecycles GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_lifecycles_save(plugin):
    """新建或更新生命周期策略。"""
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("生命周期须为对象", status_code=400)
        raw = copy.deepcopy(payload)
        for int_key in ("initial_rounds", "periodic_interval"):
            if int_key in raw and not isinstance(raw[int_key], int):
                return error_response(f"{int_key} 须为整数", status_code=400)
        # v0.1.6：stages 若存在须为 list（非法条目交给 normalize_lifecycle 剔除，不逐条校验）。
        if "stages" in raw and not isinstance(raw["stages"], list):
            return error_response("stages 须为列表", status_code=400)
        # v1.0.1：priority 须为整数；scope 若存在须为对象（条目校验交给 normalize）。
        if "priority" in raw and not isinstance(raw["priority"], int):
            return error_response("priority 须为整数", status_code=400)
        if "scope" in raw and not isinstance(raw["scope"], dict):
            return error_response("scope 须为对象", status_code=400)

        lid = raw.get("id")
        if isinstance(lid, str) and lid.strip() and plugin.lifecycles.get(lid):
            updated = plugin.lifecycles.update(lid, raw)
            _audit(plugin, "lifecycles/save", lid)
            return json_response(updated)
        created = plugin.lifecycles.create(raw)
        _audit(plugin, "lifecycles/save", created.get("id"))
        return json_response(created)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.lifecycles/save 异常")
        return error_response(str(exc), status_code=500)


async def _handler_lifecycles_delete(plugin):
    """按 id 删除生命周期策略。"""
    try:
        payload = await _body()
        lid = _require(payload, "id", str, "")
        if not lid:
            return error_response("缺少生命周期 id", status_code=400)
        ok = plugin.lifecycles.delete(lid)
        if not ok:
            return error_response(f"生命周期 {lid} 不存在", status_code=404)
        _audit(plugin, "lifecycles/delete", lid)
        return json_response({"deleted": True, "id": lid})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.lifecycles/delete 异常")
        return error_response(str(exc), status_code=500)


async def _handler_lifecycles_duplicate(plugin):
    """复制生命周期策略。"""
    try:
        payload = await _body()
        lid = _require(payload, "id", str, "")
        if not lid:
            return error_response("缺少生命周期 id", status_code=400)
        cloned = plugin.lifecycles.duplicate(lid)
        if cloned is None:
            return error_response(f"生命周期 {lid} 不存在", status_code=404)
        _audit(plugin, "lifecycles/duplicate", lid)
        return json_response(cloned)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.lifecycles/duplicate 异常")
        return error_response(str(exc), status_code=500)


async def _handler_lifecycles_templates(plugin):
    """返回生命周期预设模板（供前端一键载入后填写组）。"""
    try:
        return json_response(LIFECYCLE_TEMPLATES)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.lifecycles/templates 异常")
        return error_response(str(exc), status_code=500)


# ---- 会话 ---- #


async def _handler_sessions_list(plugin):
    """返回活跃会话的展示数据。

    每个会话条目包含 umo 与调度字段；state 未存平台信息，故展示 umo + 调度指标，
    并通过 groups 解析组名、providers 解析 Provider 名。
    """
    try:
        states = await plugin.states.all_states()
        providers = compat.get_provider_info_list(plugin.context)
        provider_names = {p.get("id"): p.get("model", "") for p in providers}
        rows = []
        for st in states:
            group_name = ""
            if st.current_group_id:
                g = plugin.groups.get(st.current_group_id)
                group_name = g.get("name", "") if g else ""
            # v1.0.1：补齐校准三字段、锁定目标具体值、最近切换时间、组游标与决策轨迹。
            lock_group_name = ""
            if st.lock_group_id:
                lg = plugin.groups.get(st.lock_group_id)
                lock_group_name = (lg.get("name", "") if lg else "") or st.lock_group_id
            # v1.0.3：锁定展示标签（前端优先用 lock_label）：强锁模型 > 锁组 > 未锁。
            lock_label = ""
            if st.lock_provider_id and st.lock_model:
                lock_label = f"模型({st.lock_provider_id} @ {st.lock_model})"
            elif st.lock_group_id:
                lock_label = f"组({lock_group_name})"
            calibration_group_name = ""
            if st.calibration_group_id:
                cg = plugin.groups.get(st.calibration_group_id)
                calibration_group_name = (
                    cg.get("name", "") if cg else ""
                ) or st.calibration_group_id
            rows.append(
                {
                    "umo": st.umo,
                    "platform": "",  # state 未存平台字段，留空（调度字段为准）
                    "current_provider_id": st.current_provider_id,
                    "current_provider_model": provider_names.get(
                        st.current_provider_id, ""
                    ),
                    "current_group_id": st.current_group_id,
                    "current_group_name": group_name,
                    "round": st.round,
                    "stage": st.stage,
                    "lifecycle_id": st.lifecycle_id,
                    "last_rule_id": st.last_rule_id,
                    "lock_group_id": st.lock_group_id,
                    "lock_provider_id": st.lock_provider_id,
                    "lock_model": st.lock_model,
                    "lock_label": lock_label,
                    "lock_group_name": lock_group_name,
                    "lock_provider_model": provider_names.get(st.lock_provider_id, "")
                    or (st.lock_provider_id or ""),
                    "locked": bool(st.lock_group_id or st.lock_provider_id),
                    "pending_reset": st.pending_reset,
                    "calibration_rounds_left": st.calibration_rounds_left,
                    "calibration_group_id": st.calibration_group_id,
                    "calibration_group_name": calibration_group_name,
                    "calibration_reason": st.calibration_reason,
                    "last_switch_at": st.last_switch_at,
                    "group_cursor": copy.deepcopy(st.group_cursor),
                    "last_trace": copy.deepcopy(st.last_trace),
                }
            )
        return json_response(rows)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.sessions GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_sessions_lock(plugin):
    """锁定会话：body 须含 ``umo``；支持三种锁定目标（v1.0.3 扩展强锁模型）。

    - ``provider_id`` + ``model`` → 强锁模型（最高优先级）：写 ``lock_model`` 并清锁组，
      审计 ``sessions/lock(model)``，返回含 ``lock_model`` / ``lock_label``；
    - ``group_id`` → 锁组（旧逻辑，同时清 Provider / 模型锁定）；
    - 仅有 ``provider_id``（无 model）→ 旧版「锁 Provider」（引擎按该 Provider 默认模型
      调度），兼容旧字段组合；
    - 三者皆无 → 400。
    """
    try:
        payload = await _body()
        umo = _require(payload, "umo", str, "")
        if not umo:
            return error_response("缺少 umo", status_code=400)
        group_id = _require(payload, "group_id", str, "")
        provider_id = _require(payload, "provider_id", str, "")
        model = _require(payload, "model", str, "")
        if provider_id and model:
            await plugin.states.update(
                umo,
                lock_group_id=None,
                lock_provider_id=provider_id,
                lock_model=model,
            )
            _audit(plugin, "sessions/lock(model)", umo)
            return json_response(
                {
                    "locked": True,
                    "umo": umo,
                    "lock_model": model,
                    "lock_label": f"模型({provider_id} @ {model})",
                }
            )
        if group_id:
            await plugin.states.update(
                umo, lock_group_id=group_id, lock_provider_id=None, lock_model=None
            )
            _audit(plugin, "sessions/lock", umo)
            return json_response({"locked": True, "umo": umo})
        if provider_id:
            # 旧版「锁 Provider」：兼容无 model 的旧请求，并清掉可能残留的强锁模型。
            await plugin.states.update(
                umo, lock_group_id=None, lock_provider_id=provider_id, lock_model=None
            )
            _audit(plugin, "sessions/lock", umo)
            return json_response({"locked": True, "umo": umo})
        return error_response(
            "须指定 provider_id+model、group_id 或 provider_id 之一", status_code=400
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.sessions/lock 异常")
        return error_response(str(exc), status_code=500)


async def _handler_sessions_unlock(plugin):
    """解锁会话：body 须含 ``umo``；同时清除组 / Provider / 模型锁定。"""
    try:
        payload = await _body()
        umo = _require(payload, "umo", str, "")
        if not umo:
            return error_response("缺少 umo", status_code=400)
        await plugin.states.update(
            umo, lock_group_id=None, lock_provider_id=None, lock_model=None
        )
        _audit(plugin, "sessions/unlock", umo)
        return json_response({"locked": False, "umo": umo})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.sessions/unlock 异常")
        return error_response(str(exc), status_code=500)


async def _handler_sessions_reset(plugin):
    """重置会话调度状态（round/stage 归零，保留锁定）。"""
    try:
        payload = await _body()
        umo = _require(payload, "umo", str, "")
        if not umo:
            return error_response("缺少 umo", status_code=400)
        new_state = await plugin.states.reset(umo, None)
        _audit(plugin, "sessions/reset", umo)
        return json_response(
            {
                "reset": True,
                "umo": umo,
                "round": new_state.round,
                "stage": new_state.stage,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.sessions/reset 异常")
        return error_response(str(exc), status_code=500)


async def _handler_sessions_remove(plugin):
    """移除会话的全部调度状态。"""
    try:
        payload = await _body()
        umo = _require(payload, "umo", str, "")
        if not umo:
            return error_response("缺少 umo", status_code=400)
        existed = await plugin.states.remove(umo)
        _audit(plugin, "sessions/remove", umo)
        return json_response({"removed": existed, "umo": umo})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.sessions/remove 异常")
        return error_response(str(exc), status_code=500)


# ---- 日志 / 模拟 / 导入导出 ---- #


async def _handler_logs_list(plugin):
    """返回调度日志（支持按 umo / level / limit 筛选）。"""
    try:
        limit = request.query.get("limit", 100, type=int) or 100
        umo = request.query.get("umo", "")
        level = request.query.get("level", "")
        entries = plugin.slog.recent(limit=max(1, int(limit)), umo=umo, level=level)
        return json_response(entries)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.logs GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_logs_clear(plugin):
    """清空调度日志（内存环形缓冲；持久化文件在下次写入时覆盖）。"""
    try:
        plugin.slog.clear()
        _audit(plugin, "logs/clear", "logs")
        return json_response({"cleared": True})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.logs/clear 异常")
        return error_response(str(exc), status_code=500)


async def _handler_simulate(plugin):
    """调度模拟（Dry Run）：按给定 payload 推演决策，不触碰真实状态。"""
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("payload 须为对象", status_code=400)
        result = await plugin.engine.simulate(payload)
        return json_response(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.simulate 异常")
        return error_response(str(exc), status_code=500)


async def _handler_export(plugin):
    """导出完整配置（JSON），返回文件名 + 内容供前端下载。"""
    try:
        content = plugin.store.export_all()
        return json_response(
            {"filename": f"{PLUGIN_NAME}-config.json", "content": content}
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.export 异常")
        return error_response(str(exc), status_code=500)


async def _handler_import(plugin):
    """导入配置：body 须含 ``content``（完整配置 dict）。"""
    try:
        payload = await _body()
        content = payload.get("content")
        if not isinstance(content, dict):
            return error_response("content 须为配置对象", status_code=400)
        try:
            new_config = plugin.store.import_all(content)
        except RuntimeError as exc:
            return error_response(str(exc), status_code=400)
        # 导入后同步设置到 adapter 与 Agent 工具上下文（时区 / agent_confirm 保持一致）。
        settings_now = new_config.get("settings", plugin.store.get_settings())
        plugin.adapter.set_settings(settings_now)
        plugin.tool_ctx.settings = settings_now
        plugin.tool_ctx.tz = plugin.adapter.get_timezone()
        _audit(plugin, "import", f"{PLUGIN_NAME}-config")
        return json_response({"imported": True, "config": new_config})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.import 异常")
        return error_response(str(exc), status_code=500)


async def _handler_providers(plugin):
    """返回可用 Provider 列表（供前端下拉选择）。"""
    try:
        return json_response(compat.get_provider_info_list(plugin.context))
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.providers 异常")
        return error_response(str(exc), status_code=500)


async def _handler_platforms(plugin):
    """返回已注册平台实例列表 `[{id, name}]`（供前端构造 UMO 预览与下拉选择）。

    遍历 ``context.platform_manager.get_insts()``，每个实例取 ``inst.meta()`` 的
    ``id`` 与 ``name``；单个实例 meta 解析失败不影响其它实例。
    """
    try:
        rows: list[dict] = []
        for inst in plugin.context.platform_manager.get_insts():
            try:
                meta = inst.meta()
                pid = str(meta.id) if getattr(meta, "id", None) else ""
                pname = str(meta.name) if getattr(meta, "name", None) else ""
                if pid:
                    rows.append({"id": pid, "name": pname})
            except Exception:  # noqa: BLE001 - 单实例 meta 解析失败跳过
                logger.warning("web.platforms: 解析平台实例 meta 失败", exc_info=True)
        return json_response(rows)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.platforms 异常")
        return error_response(str(exc), status_code=500)


# ---------------------------------------------------------------------- #
# v0.1.5：时间调度规则（temporal）
# ---------------------------------------------------------------------- #


async def _handler_temporal_list(plugin):
    """返回全部时间调度规则（按优先级降序）。"""
    try:
        return json_response(plugin.temporal.list_())
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.temporal GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_temporal_save(plugin):
    """新建或更新时间调度规则（body 含 ``id`` 且存在 → 更新，否则新建）。

    写入前做完整校验（provider / 组存在性、时间 / 字段合法性），失败返回 400 且不落库。
    """
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("时间调度规则须为对象", status_code=400)
        raw = copy.deepcopy(payload)
        # 校验（provider 存在性取「全部已配置 Provider」与 groups.list_ 实时值，
        # 与 Agent 工具口径一致：未启用的 Provider 也允许作为替换目标；无 Provider 时放宽）。
        known_providers = {
            p.get("id")
            for p in compat.get_provider_info_list(plugin.context)
            if p.get("id")
        } or None
        known_groups = {g.get("id") for g in plugin.groups.list_()} or None
        result = plugin.temporal.validate(
            raw, known_provider_ids=known_providers, known_group_ids=known_groups
        )
        if not result.get("ok"):
            return error_response("; ".join(result.get("errors", [])), status_code=400)

        if isinstance(raw.get("id"), str) and raw["id"].strip():
            existing = plugin.temporal.get(raw["id"])
            if existing is not None:
                updated = plugin.temporal.update_rule(raw["id"], raw)
                _audit(plugin, "temporal_save", updated.get("id"))
                return json_response(updated)

        created = plugin.temporal.create(raw)
        _audit(plugin, "temporal_save", created.get("id"))
        return json_response(created)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.temporal/save 异常")
        return error_response(str(exc), status_code=500)


async def _handler_temporal_delete(plugin):
    """按 id 删除时间调度规则。"""
    try:
        payload = await _body()
        rid = _require(payload, "id", str, "")
        if not rid:
            return error_response("缺少规则 id", status_code=400)
        ok = plugin.temporal.delete(rid)
        if not ok:
            return error_response(f"时间调度规则 {rid} 不存在", status_code=404)
        _audit(plugin, "temporal_delete", rid)
        return json_response({"deleted": True, "id": rid})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.temporal/delete 异常")
        return error_response(str(exc), status_code=500)


async def _handler_temporal_toggle(plugin):
    """启用 / 停用时间调度规则（body 含 ``id`` 与可选的 ``enabled``）。"""
    try:
        payload = await _body()
        rid = _require(payload, "id", str, "")
        if not rid:
            return error_response("缺少规则 id", status_code=400)
        enabled = payload.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return error_response("enabled 须为布尔值", status_code=400)
        updated = plugin.temporal.toggle(rid, enabled)
        if updated is None:
            return error_response(f"时间调度规则 {rid} 不存在", status_code=404)
        _audit(plugin, "temporal_toggle", rid)
        return json_response(updated)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.temporal/toggle 异常")
        return error_response(str(exc), status_code=500)


# ---------------------------------------------------------------------- #
# v0.1.5：配置校验 / 运行时 / 预设
# ---------------------------------------------------------------------- #


async def _handler_validate(plugin):
    """全量配置校验：单条（body 含 ``rule``）或全部结构校验 + 冲突检测。

    返回 ``{"ok", "errors", "warnings", "conflicts"}``。
    """
    try:
        payload = await _body()
        errors: list[str] = []
        warnings: list[str] = []
        # 与 temporal/save 同口径：全部已配置 Provider（含未启用），无 Provider 时放宽。
        known_providers = {
            p.get("id")
            for p in compat.get_provider_info_list(plugin.context)
            if p.get("id")
        } or None
        known_groups = {g.get("id") for g in plugin.groups.list_()} or None

        if isinstance(payload, dict) and "rule" in payload:
            rule = payload["rule"]
            if isinstance(rule, dict):
                result = plugin.temporal.validate(
                    rule,
                    known_provider_ids=known_providers,
                    known_group_ids=known_groups,
                )
                errors.extend(result.get("errors", []))
                warnings.extend(result.get("warnings", []))
                rules = plugin.temporal.list_()
            else:
                errors.append("rule 须为对象")
                rules = plugin.temporal.list_()
        else:
            # 全量：校验每个已存 temporal 规则，并检测两两冲突。
            rules = plugin.temporal.list_()
            for rule in rules:
                result = plugin.temporal.validate(
                    rule,
                    known_provider_ids=known_providers,
                    known_group_ids=known_groups,
                )
                errors.extend(
                    f"规则 {rule.get('id')}: {e}" for e in (result.get("errors") or [])
                )
                warnings.extend(result.get("warnings") or [])
            # 模型组结构校验（provider 存在性）。
            for g in plugin.store.get_groups():
                for entry in g.get("providers") or []:
                    pid = entry.get("provider_id")
                    if pid and known_providers and pid not in known_providers:
                        errors.append(
                            f"组 {g.get('id')}: Provider {pid} 不存在（已配置："
                            f"{sorted(known_providers)}）"
                        )

        try:
            conflicts = plugin.temporal.find_conflicts(rules)
        except Exception:  # noqa: BLE001 - 冲突检测兜底
            conflicts = []
        return json_response(
            {
                "ok": not errors,
                "errors": list(dict.fromkeys(errors)),
                "warnings": list(dict.fromkeys(warnings)),
                "conflicts": conflicts,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.validate 异常")
        return error_response(str(exc), status_code=500)


async def _handler_runtime(plugin):
    """调度运行时状态（当前时间/时区/开关/base_group/当前生效的 temporal 规则）。"""
    try:
        now = plugin.adapter.now()
        tz = plugin.adapter.get_timezone()
        settings = plugin.store.get_settings()
        # 当前生效规则浅层展示（同引擎 dashboard 的字段，最多 20 条）。
        actives = []
        try:
            for r in plugin.temporal.active_rules(now, tz):
                sched = r.get("schedule") or {}
                actives.append(
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "kind": r.get("kind"),
                        "group_id": r.get("group_id"),
                        "source_provider": r.get("source_provider"),
                        "target_provider": r.get("target_provider"),
                        "target_group": r.get("target_group"),
                        "scope": copy.deepcopy(r.get("scope") or {}),
                        "schedule_type": sched.get("type"),
                        "schedule_start": sched.get("start"),
                        "schedule_end": sched.get("end"),
                        "priority": r.get("priority"),
                    }
                )
                if len(actives) >= 20:
                    break
        except Exception:  # noqa: BLE001 - 生效规则读取失败给空
            logger.warning("web.runtime: 读取生效规则失败", exc_info=True)
        return json_response(
            {
                "now": now.isoformat(),
                "timezone": str(tz),
                "enabled": plugin.adapter.is_enabled(),
                "base_group": settings.get("base_group", ""),
                "active_rules": actives,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.runtime 异常")
        return error_response(str(exc), status_code=500)


async def _handler_presets_list(plugin):
    """返回时间调度规则预设（元信息与参数声明，供前端动态渲染表单）。"""
    try:
        return json_response(PRESETS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.presets GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_presets_apply(plugin):
    """套用预设：body 含 ``id`` 与 ``params`` → 生成规则并逐条创建（审计 source=preset）。"""
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("body 须为对象", status_code=400)
        preset_id = _require(payload, "id", str, "")
        params = payload.get("params")
        if not preset_id:
            return error_response("缺少预设 id", status_code=400)
        if not isinstance(params, dict):
            return error_response("params 须为对象", status_code=400)
        try:
            rules = build_preset_rules(preset_id, params)
        except KeyError:
            return error_response(f"未知预设: {preset_id}", status_code=400)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        created = []
        for rule in rules:
            created.append(plugin.temporal.create(rule))
        # 预设来源审计（operator 仍为 admin）。
        try:
            plugin.audit.add(
                {
                    "time": datetime.now().isoformat(),
                    "operator": "admin",
                    "source": "preset",
                    "action": "preset_apply",
                    "target": preset_id,
                    "before": None,
                    "after": [r.get("id") for r in created],
                    "result": "success",
                }
            )
        except Exception:  # noqa: BLE001 - 审计失败不阻断
            logger.warning("web.presets/apply: 写审计失败（忽略）", exc_info=True)
        return json_response({"applied": True, "rules": created})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.presets/apply 异常")
        return error_response(str(exc), status_code=500)


# ---------------------------------------------------------------------- #
# v0.1.5：Agent 配置层 / 审计
# ---------------------------------------------------------------------- #


async def _handler_agent_pending(plugin):
    """返回当前待应用的更改（v1.0.3：含 summary / staged_at 的扩展条目，无暂存返回 {}）。

    经 ``agent_tools.pending_view``（契约 C8）读取，与 /scheduler pending 指令同源。
    """
    try:
        return json_response(agent_tools.pending_view(plugin.tool_ctx) or {})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/pending 异常")
        return error_response(str(exc), status_code=500)


async def _handler_agent_approve(plugin):
    """批准暂存变更（v1.0.3）：body 可选 ``pending_id``（缺省=批准当前唯一暂存）。

    与指令 ``/scheduler approve`` 共用 ``agent_tools.apply_staged``（契约 C8），
    应用失败（ok:false）返回 400 错误；成功返回 ``{ok, applied, summary}``。
    """
    try:
        payload = await _body()
        pending_id = _require(payload, "pending_id", str, "")
        plugin.tool_ctx.source = "web_agent"
        plugin.tool_ctx.operator = "admin"
        res = agent_tools.apply_staged(plugin.tool_ctx, pending_id)
        if not res.get("ok"):
            return error_response(res.get("error") or "批准失败", status_code=400)
        return json_response(res)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/approve 异常")
        return error_response(str(exc), status_code=500)


async def _handler_agent_reject(plugin):
    """拒绝暂存变更（v1.0.3）：body 可选 ``pending_id``（缺省=拒绝当前唯一暂存）。

    与指令 ``/scheduler reject`` 共用 ``agent_tools.reject_staged``（契约 C8）；
    拒绝前先取 pending 的 summary（拒绝后条目即被清除），保证响应含人性化摘要。
    """
    try:
        payload = await _body()
        pending_id = _require(payload, "pending_id", str, "")
        plugin.tool_ctx.source = "web_agent"
        plugin.tool_ctx.operator = "admin"
        summary = (agent_tools.pending_view(plugin.tool_ctx) or {}).get("summary") or []
        res = agent_tools.reject_staged(plugin.tool_ctx, pending_id)
        if not res.get("ok"):
            return error_response(res.get("error") or "拒绝失败", status_code=400)
        return json_response({**res, "summary": summary})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/reject 异常")
        return error_response(str(exc), status_code=500)


async def _handler_agent_chat(plugin):
    """AI 配置助手对话（v0.1.8 双流程兼容）。

    双分支：
    - ``payload.content`` 为非空 str → 会话持久化流程：可选 ``conversation_id`` 续接
      已有会话，否则自动新建会话；把用户消息写入会话，取最近 60 条交给 Agent 循环，
      返回 ``{**result, conversation_id, title, messages}``；回复成功则写回 assistant 消息。
    - ``payload.messages`` 为 list → 旧流程（无会话持久化，向前端不再使用，保留兼容）。
    - 两者皆非 → 400。
    """
    try:
        payload = await _body()
        if not isinstance(payload, dict):
            return error_response("body 须为对象", status_code=400)
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return await _handle_agent_chat_conversation(
                plugin, payload, content.strip()
            )
        if isinstance(payload.get("messages"), list):
            return await _handle_agent_chat_legacy(plugin, payload)
        return error_response(
            "须提供 content（新流程）或 messages（旧流程）", status_code=400
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/chat 异常")
        return error_response(str(exc), status_code=500)


async def _resolve_agent_provider(plugin) -> str:
    """决定配置助手使用的 Provider id。

    优先 ``settings.agent_provider_id``（后台可指定助手运行模型，见 v0.1.6），
    否则回退当前正在使用的聊天 Provider；无可用 Provider 返回空串。
    """
    try:
        from astrbot.core.provider.entities import ProviderType

        pid = ""
        settings = plugin.store.get_settings()
        cfg_pid = str(settings.get("agent_provider_id") or "")
        if cfg_pid:
            try:
                cfg_prov = await plugin.context.provider_manager.get_provider_by_id(
                    cfg_pid
                )
                if cfg_prov is not None:
                    pid = cfg_prov.meta().id
            except Exception:  # noqa: BLE001 - 指定 Provider 解析失败回退
                logger.warning(
                    "web.agent/chat: 指定 Provider %r 解析失败，回退",
                    cfg_pid,
                    exc_info=True,
                )
        if not pid:
            prov = plugin.context.provider_manager.get_using_provider(
                ProviderType.CHAT_COMPLETION
            )
            if prov is not None:
                pid = prov.meta().id
    except Exception:  # noqa: BLE001
        logger.warning("web.agent/chat: 获取聊天 Provider 失败", exc_info=True)
    return pid


async def _handle_agent_chat_conversation(plugin, payload: dict, content: str):
    """新流程：content + 可选 conversation_id 的会话持久化对话。

    复用 ``_resolve_agent_provider`` 解析 Provider（失败返回 400）；
    追加用户消息 → 取最近 60 条发给 Agent → 回复成功写回 assistant 消息。
    """
    pid = await _resolve_agent_provider(plugin)
    if not pid:
        return error_response("无可用聊天 Provider", status_code=400)
    cid = _require(payload, "conversation_id", str, "")
    conv = plugin.chats.get_conversation(cid) if cid else None
    if conv is None:
        conv = plugin.chats.new_conversation(content)  # 标题取首条用户消息
    plugin.chats.append_message(conv["id"], "user", content)
    conv = plugin.chats.get_conversation(conv["id"])
    history = [{"role": m["role"], "content": m["content"]} for m in conv["messages"]]
    # 只把最近 60 条发给 LLM（防 token 超限）。
    result = await run_web_agent(plugin.context, plugin.tool_ctx, history[-60:], pid)
    if isinstance(result, dict) and result.get("error"):
        return error_response(result["error"], status_code=500)
    if isinstance(result, dict):
        plugin.chats.append_message(conv["id"], "assistant", result.get("reply", ""))
        conv = plugin.chats.get_conversation(conv["id"])
    _audit(plugin, "agent_chat", conv["id"], detail="已保存会话消息")
    return json_response(
        {
            **result,
            "conversation_id": conv["id"],
            "title": conv["title"],
            "messages": conv["messages"],
        }
    )


async def _handle_agent_chat_legacy(plugin, payload: dict):
    """旧流程：messages 一次性历史（无会话持久化，向前端不再使用，保留兼容）。

    逻辑同 v0.1.7 现状：校验 messages 结构、解析 pid、run_web_agent、
    返回 ``{reply, pending}``（不含 conversation_id）。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(m, dict) and isinstance(m.get("content", ""), str) for m in messages
    ):
        return error_response(
            "messages 须为非空消息列表 [{'role','content'}]", status_code=400
        )
    pid = await _resolve_agent_provider(plugin)
    if not pid:
        return error_response("无可用聊天 Provider", status_code=400)
    result = await run_web_agent(plugin.context, plugin.tool_ctx, messages, pid)
    if isinstance(result, dict) and result.get("error"):
        return error_response(result["error"], status_code=500)
    return json_response(result)


async def _handler_agent_chat_stream(plugin):
    """AI 配置助手流式对话（SSE）。

    query 参数：``content``（必填）、``conversation_id``（可选）。会话持久化语义与
    ``POST agent/chat`` 完全一致：cid 存在则续用，否则自动新建；用户消息先写入
    ChatStore，``run_web_agent_stream`` 流式驱动 Agent，``done`` 帧时把完整回复写回
    assistant 消息，``error`` 帧时不写半截 assistant 直接结束。

    返回 ``text/event-stream``，每个事件为 ``data: <json>\\n\\n``（前端 bridge 的
    SSE 解析依赖 ``data:`` 前缀）。事件帧顺序：``meta`` →（``delta``/``tool``）*
    → ``done``（后接 ``finish``）/ ``error``。

    **断连续跑（v1.0.3）**：Agent 生成由独立的 ``asyncio.Task``（``_web_agent_task``）
    在后台消费，SSE 生成器只经队列转发任务产出的事件帧，并记录任务状态
    （``plugin._agent_task``，供 ``GET agent/task-status`` 读取）。当客户端切页 /
    卸载 / 取消导致 SSE 断开时（生成器被取消），后台任务**不被取消**，继续跑完并把
    完整回复写回 ChatStore，根治「切回后回复被吞」。
    """
    try:
        content = request.query.get("content", "") or ""
        content = content.strip()
        if not content:
            return error_response("缺少 content 参数", status_code=400)
        cid = request.query.get("conversation_id", "") or ""
        conv = plugin.chats.get_conversation(cid) if cid else None
        if conv is None:
            conv = plugin.chats.new_conversation(content)  # 标题取首条用户消息
        conv_id = conv["id"]
        plugin.chats.append_message(conv_id, "user", content)
        conv = plugin.chats.get_conversation(conv_id)
        title = conv.get("title", "")
        pid = await _resolve_agent_provider(plugin)
        if not pid:
            return error_response("无可用聊天 Provider", status_code=400)
        history = [
            {"role": m["role"], "content": m["content"]} for m in conv["messages"]
        ]
        # 事件队列：后台任务写入、SSE 生成器读取（解耦发送失败与生成失败）。
        queue: asyncio.Queue = asyncio.Queue()
        started_at = datetime.now().isoformat()
        token = object()  # 唯一 token，任务结束时只清理属于自己的状态
        # 后台生成任务：SSE 断开后仍自然跑完并写回 ChatStore。
        task = asyncio.create_task(
            _web_agent_task(plugin, pid, conv_id, history[-60:], queue, token)
        )
        # 记录任务状态（GET agent/task-status 读取），记录最近一个生成任务。
        plugin._agent_task = {
            "cid": conv_id,
            "started_at": started_at,
            "task": task,
            "_token": token,
        }
        # 只把最近 60 条发给 LLM（防 token 超限），与 POST 流程一致。
        return stream_response(
            _agent_chat_stream_gen(plugin, conv_id, title, queue, task),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/chat/stream 异常")
        return error_response(str(exc), status_code=500)


async def _web_agent_task(
    plugin,
    pid: str,
    cid: str,
    history: list,
    queue: asyncio.Queue,
    token: object,
):
    """后台 Agent 生成任务（SSE 断连续跑的核心）。

    消费 ``run_web_agent_stream`` 产出的事件帧并推入队列（由 SSE 生成器转发给客户端）；
    当收到 ``done`` 帧时，在**本任务内**把完整回复写回 ChatStore 并追发 ``finish`` 帧
    （含 conversation_id 与待应用 pending，与 POST 流程返回的 pending 保持一致）；
    ``error`` 帧时不写半截 assistant，直接结束。

    与 SSE 连接完全解耦：即便客户端中途断开，本任务也会自然跑完、写回 assistant 消息，
    并在结束时清理状态（``plugin._agent_task``，仅当仍指向本任务）。任务内部吞掉一切
    异常并按需产出 error 帧，**绝不向上抛出**，保证任务总能跑到清理分支。
    """
    try:
        async for ev in run_web_agent_stream(
            plugin.context, plugin.tool_ctx, history, pid
        ):
            queue.put_nowait(ev)
            if ev.get("type") == "done":
                # 完整回复已产出，写回 assistant 消息（与 POST 流程一致）。
                try:
                    plugin.chats.append_message(cid, "assistant", ev.get("reply", ""))
                except Exception:  # noqa: BLE001 - 写回失败不阻断任务结束
                    logger.warning(
                        "web.agent/chat/stream: 写回 assistant 消息失败", exc_info=True
                    )
                _audit(plugin, "agent_chat", cid, detail="已保存会话消息（流式）")
                queue.put_nowait(
                    {
                        "type": "finish",
                        "conversation_id": cid,
                        "pending": plugin.tool_ctx.pending.get(),
                    }
                )
            elif ev.get("type") == "error":
                # 出错：不写半截 assistant，直接结束。
                queue.put_nowait(ev)
    except Exception as exc:  # noqa: BLE001 - 任务异常兜底，产出 error 帧结束
        logger.exception("web.agent/chat/stream 后台任务异常")
        try:
            queue.put_nowait({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001 - 队列写入失败静默（下游已不可达）
            pass
    finally:
        # 推入结束哨兵：SSE 生成器读到即结束转发（防止其永久 await queue.get()）。
        queue.put_nowait(_END)
        # 清理任务状态：仅当 plugin._agent_task 仍指向本任务时清空，避免误清新任务。
        current = getattr(plugin, "_agent_task", None)
        if current is not None and current.get("_token") is token:
            plugin._agent_task = None


async def _agent_chat_stream_gen(
    plugin, cid: str, title: str, queue: asyncio.Queue, task: asyncio.Task
):
    """SSE 事件生成器：meta → 队列转发的 stream 事件 → 队列结束哨兵。

    帧契约（与 ``scheduler.agent.run_web_agent_stream`` 产出的事件完全一致）：
    - ``meta``：会话元信息（conversation_id / title），本生成器首先发出；
    - ``delta`` / ``tool``：透传后台任务转发的增量文本与工具提示帧；
    - ``done`` / ``finish``：后台任务在 ``done`` 时写回 assistant 并追发 ``finish``
      （本生成器仅转发，不负责写回——写回在后台任务内，保证断连也不丢）；
    - ``error``：流中出错（透传），直接结束。

    客户端断开（Starlette 取消本生成器 → ``CancelledError`` / ``GeneratorExit``）时，
    **不取消后台任务**，仅结束本转发器，让后台任务继续跑完并写回 ChatStore。
    """
    import json as _json

    def _frame(ev: dict) -> str:
        return f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"

    yield _frame({"type": "meta", "conversation_id": cid, "title": title})
    try:
        while True:
            ev = await queue.get()
            if ev is _END:
                break
            yield _frame(ev)
    except (GeneratorExit, asyncio.CancelledError):
        # SSE 断开：后台任务继续跑完（不取消），本生成器就此结束转发。
        if not task.done():
            logger.info(
                "web.agent/chat/stream: SSE 断开，Agent 生成任务继续后台跑完（cid=%s）",
                cid,
            )


async def _handler_agent_task_status(plugin):
    """Agent 生成任务状态（用于前端「进入时校验」断连续跑）。

    读取 ``plugin._agent_task``（由 ``_handler_agent_chat_stream`` 写入、后台任务
    结束时清理）：
    - ``running=true``：有 Agent 生成任务正在后台续跑（含 SSE 断开后的断连续跑）；
    - ``cid``：任务对应会话 id（前端提示「完成后可在该会话查看」）；无任务 null；
    - ``started_at``：任务开始时间（ISO）；无任务 null。
    无任务 / 异常时均返回 ``{"running": false, "cid": null, "started_at": null}``，
    不报错。
    """
    try:
        current = getattr(plugin, "_agent_task", None)
        if not current:
            return json_response({"running": False, "cid": None, "started_at": None})
        task = current.get("task")
        running = bool(task) and not task.done()
        return json_response(
            {
                "running": running,
                "cid": current.get("cid"),
                "started_at": current.get("started_at"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/task-status 异常")
        return error_response(str(exc), status_code=500)


async def _handler_agent_conversations(plugin):
    """AI 助手会话：query ``id`` 非空 → 单个会话完整 JSON；否则返回会话概要列表。

    会话概要按 ``updated_at`` 降序，含 message_count / last_preview，供侧栏渲染。
    """
    try:
        cid = request.query.get("id", "")
        if cid:
            conv = plugin.chats.get_conversation(cid)
            if conv is None:
                return error_response(f"会话 {cid} 不存在", status_code=404)
            return json_response(conv)
        return json_response(plugin.chats.list_conversations())
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/conversations GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_agent_conversations_delete(plugin):
    """删除 AI 助手会话：body 含 ``id``；不存在返回 404。"""
    try:
        payload = await _body()
        cid = _require(payload, "id", str, "")
        if not cid:
            return error_response("缺少会话 id", status_code=400)
        ok = plugin.chats.delete(cid)
        if not ok:
            return error_response(f"会话 {cid} 不存在", status_code=404)
        _audit(plugin, "chat_delete", cid)
        return json_response({"deleted": True, "id": cid})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/conversations/delete 异常")
        return error_response(str(exc), status_code=500)


async def _handler_agent_apply(plugin):
    """应用待执行的配置更改（source=web_agent, operator=admin）。"""
    try:
        plugin.tool_ctx.source = "web_agent"
        plugin.tool_ctx.operator = "admin"
        result = agent_tools.tool_apply_configuration_change(plugin.tool_ctx)
        return json_response(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/apply 异常")
        return error_response(str(exc), status_code=500)


async def _handler_agent_rollback(plugin):
    """回滚到应用前的配置快照（source=web_agent, operator=admin）。"""
    try:
        plugin.tool_ctx.source = "web_agent"
        plugin.tool_ctx.operator = "admin"
        result = agent_tools.tool_rollback_configuration_change(plugin.tool_ctx)
        return json_response(result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.agent/rollback 异常")
        return error_response(str(exc), status_code=500)


async def _handler_audit_list(plugin):
    """返回审计日志（支持按 source / action / limit 筛选）。"""
    try:
        limit = request.query.get("limit", 100, type=int) or 100
        source = request.query.get("source", "")
        action = request.query.get("action", "")
        entries = plugin.audit.recent(
            limit=max(1, int(limit)), source=source, action=action
        )
        return json_response(entries)
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.audit GET 异常")
        return error_response(str(exc), status_code=500)


async def _handler_audit_clear(plugin):
    """清空审计日志（内存缓冲；持久化文件在下次写入时覆盖）。"""
    try:
        plugin.audit.clear()
        return json_response({"cleared": True})
    except Exception as exc:  # noqa: BLE001
        logger.exception("web.audit/clear 异常")
        return error_response(str(exc), status_code=500)
