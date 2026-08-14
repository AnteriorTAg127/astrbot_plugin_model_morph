"""agent —— Agent 配置层（模块 T4 的 astrbot 侧薄层）。

本文件接触 AstrBot 运行时（import astrbot 是预期的），不参与离线测试。职责：
- ``CONFIG_AGENT_SYSTEM_PROMPT``：配置 Agent 的中文系统提示词（身份 / 职责 /
  绝不能 / 工作流 / 时间规则语义 / 时区说明 / 失败重试策略）。
- ``build_config_toolset(tc)``：把 `agent_tools` 的工具函数包装成 ``FunctionTool``，
  构造 `astrbot.core.agent.tool.ToolSet`。
- ``ModelMorphConfigAgentTool``：注册为聊天 SubAgent 的入口工具，仅管理员可用，
  收到管理员原话后把 ``query`` 交给 ``tool_loop_agent`` 走「配置 Agent」多步循环。
- ``run_web_agent``：Web 助手入口，用 ``agent_context``（event=None）在无事件场景
  驱动同一套配置 Agent。

结构化工具包装形式与调用链参考主仓库 ``docs/zh/dev/star/guides/ai.md``：
``FunctionTool[AstrAgentContext]`` + ``ContextWrapper[AstrAgentContext]``，
``tool_loop_agent(...)`` 的 ``agent_context`` 参数见 ``astrbot/core/star/context.py``。
"""

from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.message import Message
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from . import agent_tools
from .agent_tools import ToolContext

# 配置 Agent 的中文系统提示词（涵盖 spec §28 全部要点）。
CONFIG_AGENT_SYSTEM_PROMPT = (
    "你是「Model Morph 模型调度配置代理」，负责帮助管理员通过自然语言读取和修改模型的调度配置"
    "（模型组、时间调度规则、全局状态）。你的所有解释和汇报都使用中文。\n\n"
    "【你的身份】你是 AstrBot 插件 Model Morph 的配置助手。你只能通过调用下面提供的结构化工具"
    "来读取或修改配置，除此之外不要编造任何配置内容。\n\n"
    "【你的职责（8 条）】\n"
    "1. 查询并解释当前配置：模型组、Provider、现有规则、生效中的时间调度规则、调度器状态。\n"
    "2. 创建模型组，并支持向组内指定 Provider。\n"
    "3. 更新模型组（改名、换 Provider、调优先/权重、启停成员）。\n"
    "4. 删除模型组（属于高危操作，必须走预览→确认→应用流程）。\n"
    "5. 创建时间调度规则（模型替换 model_override / 整组切换 group_switch 两类）。\n"
    "6. 更新 / 停用 / 启用 / 删除时间调度规则（删除属高危操作）。\n"
    "7. 校验当前配置并汇报错误 / 警告 / 规则冲突。\n"
    "8. 管理 预览 → 应用 → 回滚 流程，确保高风险变更可撤销。\n\n"
    "【你绝不能（6 条）】\n"
    "1. 绝不能编造不存在的模型组或 Provider：动手前必须先用查询工具确认真实 id，再引用它。"
    "2. 绝不能跳过预览：凡涉及删除、单批≥3 项写操作，或工具明确要求 preview 的高危变更，"
    "必须先调用 preview_configuration_change 生成预览，经管理员确认后再 apply。\n"
    "3. 绝不能乱猜时间规则的含义：跨午夜、星期、时区都有严格语义，见下文「时间规则语义」。\n"
    "4. 绝不能直接修改基础配置或删改无关内容，一次只处理管理员要求的变更。\n"
    "5. 绝不能隐瞒失败：某个工具返回失败时，必须如实告知管理员原因。\n"
    "6. 绝不能在没有确凿依据时声称「已生效」：应用成功才算完成。\n\n"
    "【你的工作流】严格按：查询 → 理解 → 规划 → 验证 → 预览 → 执行 → 汇报。\n"
    "先查询现状，理解管理员意图并确定要改的对象（组 / 规则），规划需要的一组操作，"
    "运行时校验参数，对高危或批量变更先 preview 生成预览，确认后再 apply，最后用中文向"
    "管理员解释改了什么、当前状态如何、如何回滚。遵循「先查询再修改」：create / update 前"
    "先用读取工具确认目标存在及当前值。\n\n"
    "【时间规则语义（务必遵守）】\n"
    "- 时间类型：daily（每天）/ weekly（每周，需 weekdays）/ date（指定日期 YYYY-MM-DD）/"
    "always（始终）。\n"
    "- start/end 用 24 小时制 HH:MM；当 end 小于 start 表示跨午夜（如 23:00-08:00 = 夜晚到次日清晨）。\n"
    "- weekdays 用整数 0=周一、1=周二 …… 6=周日；weekly 类型必须提供 weekdays，空列表视为不生效。\n"
    "- 时区：默认使用插件的调度时区（见调度状态里的 tz 字段）；不要在规则里臆造时区。\n"
    "- 模型替换 model_override：作用域内本选 source_provider 的请求改为 target_provider；"
    "整组切换 group_switch：本选 group_id 组的请求改为 target_group。\n\n"
    '【工具失败与重试】某工具返回 {"ok": false, "error": ...} 时，先根据 error 修正参数后'
    "重试，最多重试 2 次；仍失败则停止，并如实向管理员汇报失败原因，绝不假装成功。\n\n"
    "【汇报】全部完成后，用中文向管理员解释：做了什么、改动了哪些模型/规则、当前生效状态、"
    "如有待应用的预览请提示管理员预览并应用。\n\n"
    "【降级生命周期编排（v0.1.6 多阶段降级 + 事件校准）】\n"
    "- 多阶段降级用 create_lifecycle 的 stages 完成：stages=[{group_id, rounds}...]"
    "按累计轮次逐段切换（前 4 轮 → A 组，5-9 轮 → B 组，……），rounds 全部耗尽后走 "
    "final_group（主组）。不要捏造组 id：所有 group_id / provider_id 必须先查询确认存在。\n"
    "- 周期校准：periodic_group 非空且 periodic_interval>0 时，staged 模式下每 N 轮"
    "（round 为 N 的整数倍）固定用 periodic_group 校准一次，其余轮次按 stages/final_group。\n"
    '- 上下文压缩校准：calibration_event 设为 "context_compression"，并同时设置 '
    "calibration_group（压缩后要用的校准组）与 calibration_rounds（校准持续轮数，正整数）。"
    "插件会在检测到上下文压缩时自动把会话切换到校准组并计数，无需管理员干预。\n"
    "- default_lifecycle = 全局启用：用 tool_set_default_lifecycle 把某生命周期设为全局默认，"
    "会话未显式绑定生命周期时即按它降级；传空串可清除默认。这是「全局启用某生命周期 / 降级预设」"
    "的唯一方式。\n"
    "- 时段路由用 temporal model_override 规则：如每天两个高峰时段各写一条 daily 规则"
    "（schedule start/end 分别为 09:00-11:00、14:00-18:00，source 为被替换 Provider，"
    "target 为目标 Provider）。\n"
    "- 示例（前 4 轮 A→5-9 轮 B→之后 C；每 15 轮用 D 校准；压缩后用 E 校准 5 轮；"
    "两时段 V4 路由到 Luna）对应的工具调用步骤清单：\n"
    "  1) 先查询：list_model_groups / list_models / list_lifecycles 确认 A/B/C/D/E 与 "
    "V4、Luna 的真实 id；\n"
    "  2) 建组：逐组 create_model_group 创建 A/B/C/D/E（或确认已存在则跳过）；\n"
    "  3) 建生命周期：create_lifecycle({name, stages:[{A,4},{B,5}], final_group:C, "
    'periodic_group:D, periodic_interval:15, calibration_event:"context_compression", '
    "calibration_group:E, calibration_rounds:5})；\n"
    "  4) 全局启用：set_default_lifecycle(生命周期 id)；\n"
    "  5) 建时段路由：create_schedule_rule 两条 model_override（09:00-11:00 与 14:00-18:00，"
    "source=V4 provider id，target=Luna provider id）；\n"
    "  6) 校验与预览：validate_configuration 确认无错误，再 preview_configuration_change "
    "生成预览，经管理员确认后 apply_configuration_change 应用；\n"
    "  7) 用中文汇报全部创建结果与当前生效状态。\n"
    "工作流严格按：先查询 → 再创建 → 校验 → 预览 → 应用 → 中文汇报；任何一步工具返回失败，"
    "先按 error 修正重试，仍失败则如实汇报。"
)


def build_config_toolset(tc: ToolContext) -> ToolSet:
    """把 agent_tools 的全部工具包装成 `astrbot.core.agent.tool.ToolSet`。

    Args:
        tc: ``ToolContext`` 实例（以闭包注入到每个工具的 call 实现）。

    Returns:
        配置 Agent 可用的 ToolSet。
    """

    # 每个工具独立声明的人性化名称与参数（调用方 agent 据此生成 JSON Schema）。
    tool_defs: list[tuple[str, str, dict, Any]] = [
        ("tool_list_model_groups", "列出全部模型组（id/name/enabled/策略/成员数）", {}),
        (
            "tool_get_model_group",
            "按组 id 或名称查询模型组详情",
            {"name": "组 id 或名称"},
        ),
        (
            "tool_list_models",
            "列出已配置 Provider（id/model/type/enabled/所属组数）",
            {},
        ),
        ("tool_list_rules", "列出现有规则与时间调度规则", {}),
        ("tool_get_active_rules", "列出当前时刻生效的时间调度规则", {}),
        ("tool_get_scheduled_rules", "列出全部时间调度规则", {}),
        ("tool_get_scheduler_status", "获取调度器状态（开关/时区/数量/冲突）", {}),
        ("tool_get_runtime_routing", "只读推演某组当前最终路由", {}),
        (
            "tool_create_model_group",
            "创建模型组",
            {
                "spec": {
                    "type": "object",
                    "description": (
                        '模型组字典：{"name": 组名, "desc": 描述, "enabled": bool, '
                        '"strategy": "priority"|"round_robin"|"weighted"|"random"|'
                        '"fallback", "providers": [{"provider_id": 已配置Provider id, '
                        '"priority": int, "weight": int, "enabled": bool}], '
                        '"fallbacks": [Provider id 列表]}'
                    ),
                }
            },
        ),
        (
            "tool_update_model_group",
            "更新模型组",
            {
                "group_id": "组 id",
                "spec": {
                    "type": "object",
                    "description": "要更新的字段（结构同 create_model_group 的 spec，只写需要改的键）",
                },
            },
        ),
        (
            "tool_delete_model_group",
            "删除模型组（高危，需预览）",
            {"group_id": "组 id"},
        ),
        (
            "tool_create_schedule_rule",
            "创建时间调度规则",
            {
                "spec": {
                    "type": "object",
                    "description": (
                        '时间调度规则字典：{kind: "model_override"|"group_switch", '
                        'group_id: ""（全局）或组 id, source_provider, target_provider, '
                        'target_group, schedule: {type: "always"|"daily"|"weekly"|'
                        '"date", start: "HH:MM", end: "HH:MM", weekdays: [0-6], '
                        'date: "YYYY-MM-DD"}, priority: int}'
                    ),
                }
            },
        ),
        (
            "tool_update_schedule_rule",
            "更新时间调度规则",
            {
                "rule_id": "规则 id",
                "spec": {
                    "type": "object",
                    "description": "要更新的字段（结构同 create_schedule_rule 的 spec）",
                },
            },
        ),
        (
            "tool_delete_schedule_rule",
            "删除时间调度规则（高危，需预览）",
            {"rule_id": "规则 id"},
        ),
        ("tool_enable_schedule_rule", "启用时间调度规则", {"rule_id": "规则 id"}),
        ("tool_disable_schedule_rule", "停用时间调度规则", {"rule_id": "规则 id"}),
        (
            "tool_create_model_override",
            "创建模型替换规则（model_override）",
            {
                "spec": {
                    "type": "object",
                    "description": "规则字典（kind 固定为 model_override；结构见 create_schedule_rule 的 spec）",
                }
            },
        ),
        (
            "tool_update_model_override",
            "更新模型替换规则",
            {
                "rule_id": "规则 id",
                "spec": {
                    "type": "object",
                    "description": "要更新的字段（kind 固定为 model_override）",
                },
            },
        ),
        (
            "tool_delete_model_override",
            "删除模型替换规则（高危，需预览）",
            {"rule_id": "规则 id"},
        ),
        ("tool_validate_configuration", "全量校验当前配置，返回错误/警告/冲突", {}),
        ("tool_reload_scheduler", "使调度缓存失效并返回状态", {}),
        (
            "tool_preview_configuration_change",
            "预览一批配置更改（校验但不写入）",
            {
                "ops": {
                    "type": "array",
                    "description": (
                        '操作列表，每项：{action: "create_schedule_rule"|'
                        '"update_schedule_rule"|"delete_schedule_rule"|"create_model_group"|'
                        '"update_model_group"|"delete_model_group"|"create_lifecycle"|'
                        '"update_lifecycle"|"delete_lifecycle", '
                        "data: 规则/组/生命周期字典, rule_id?, group_id?, lifecycle_id?}"
                    ),
                }
            },
        ),
        ("tool_apply_configuration_change", "应用待执行的配置更改", {}),
        ("tool_rollback_configuration_change", "回滚到应用前的配置", {}),
        # ---- v0.1.6：生命周期（含多阶段降级 / 周期校准 / 全局默认） ----
        (
            "tool_list_lifecycles",
            "列出全部生命周期策略（含 id/name/enabled/模式概要）",
            {},
        ),
        (
            "tool_get_lifecycle",
            "按 id 或名称查询生命周期详情",
            {"name": "生命周期 id 或名称"},
        ),
        (
            "tool_create_lifecycle",
            "创建生命周期（多阶段降级 / 周期校准）",
            {
                "spec": {
                    "type": "object",
                    "description": (
                        "生命周期字典：{name: 名称, enabled: bool, "
                        "stages: [{group_id: 已存在组 id, rounds: 正整数}...] 多阶段降级阶段列表, "
                        "final_group: stages 耗尽后使用的主组 id（可为空串）, "
                        "periodic_group: 周期校准组 id（可为空串）, "
                        "periodic_interval: 正整数, "
                        'calibration_event: ""|"context_compression", '
                        "calibration_group: 校准组 id（calibration_event 非空时必填）, "
                        "calibration_rounds: 正整数（calibration_event 非空时必填）}"
                    ),
                }
            },
        ),
        (
            "tool_update_lifecycle",
            "合并更新生命周期",
            {
                "lifecycle_id": "生命周期 id",
                "spec": {
                    "type": "object",
                    "description": "要更新的字段（结构同 create_lifecycle 的 spec，只写需要改的键）",
                },
            },
        ),
        (
            "tool_delete_lifecycle",
            "删除生命周期（高危，需预览）",
            {"lifecycle_id": "生命周期 id"},
        ),
        (
            "tool_set_default_lifecycle",
            "设置全局默认生命周期（全局启用某生命周期 / 降级预设；传空串清除）",
            {"lifecycle_id": "生命周期 id 或空串（空串=清除全局默认）"},
        ),
    ]

    def make_tool(func_name: str, desc: str, props: dict):
        # 参数类型声明：props 值可为描述字符串（类型默认 string）或
        # {"type": str, "description": str} dict（spec/ops 类复杂参数用 "object"）。
        properties: dict = {}
        for k, v in props.items():
            if isinstance(v, dict):
                properties[k] = {
                    "type": str(v.get("type", "string")),
                    "description": str(v.get("description", "")),
                }
            else:
                properties[k] = {"type": "string", "description": str(v)}
        # 类体内的字段名 name 会遮蔽外层闭包变量，先把工具名计算出来再定义类。
        tool_name = func_name.replace("tool_", "")

        @dataclass
        class _Tool(FunctionTool[AstrAgentContext]):
            name: str = tool_name
            description: str = desc
            parameters: dict = Field(
                default_factory=lambda: {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties.keys()),
                }
            )

            async def call(
                self, context: ContextWrapper[AstrAgentContext], **kwargs
            ) -> ToolExecResult:
                func = getattr(agent_tools, func_name)
                try:
                    data = func(tc, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 兜底
                    return json.dumps(
                        {"ok": False, "error": f"工具执行异常：{exc}"},
                        ensure_ascii=False,
                    )
                return json.dumps(data, ensure_ascii=False)

        return _Tool()

    return ToolSet([make_tool(n, d, p) for n, d, p in tool_defs])


@dataclass
class ModelMorphConfigAgentTool(FunctionTool[AstrAgentContext]):
    """管理员要求修改模型调度配置时的子代理入口工具。

    把管理员原话作为 ``query`` 传给配置 Agent（仅在管理员身份下放行）。

    注意：``FunctionTool`` 是 pydantic dataclass，父类 ``ToolSchema`` 的
    ``name/description/parameters`` 为无默认值的必填字段，因此本类与
    ``build_config_toolset`` 一样采用「``@dataclass`` 子类声明默认字段 +
    ``tc`` 以构造参数注入」的形式（不能定义会调用 ``super().__init__()``
    的自定义 __init__）。
    """

    name: str = "model_scheduler_config"
    description: str = (
        "当管理员要求修改模型调度配置时使用，例如：'晚上换个便宜模型'、'高峰期别用某模型'、"
        "创建/修改/删除时间调度规则或模型组。把管理员的需求原话作为 query 传入。"
        "仅管理员可用，非管理员调用会被拒绝。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "管理员关于模型调度配置的原始需求描述",
                }
            },
            "required": ["query"],
        }
    )

    # 配置上下文（构造时注入；非 schema 字段，不参与工具声明）。
    tc: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        """执行权限检查并把 query 交给配置 Agent 循环。"""
        try:
            tc = self.tc
            if tc is None:  # 防御：未注入上下文时拒绝
                return "配置助手未初始化，请联系管理员检查插件配置"
            event = context.context.event
            # 非管理员拒绝
            if event is not None and not event.is_admin():
                tc.source = "subagent"
                tc.operator = "unknown"
                tc.audit.add(
                    {
                        "time": "",
                        "operator": "unknown",
                        "source": "subagent",
                        "action": "model_scheduler_config",
                        "target": "permission_denied",
                        "before": None,
                        "after": None,
                        "result": "failed",
                        "detail": "非管理员尝试修改模型调度配置",
                    }
                )
                return "权限不足：模型调度配置仅管理员可用"
            # 管理员：装配 tc 与工具，进入配置 Agent 循环
            tc.source = "subagent"
            try:
                tc.operator = event.get_sender_id() if event is not None else "admin"
            except Exception:  # noqa: BLE001 - 兜底
                tc.operator = "admin"
            query = str(kwargs.get("query", ""))
            ctx = context.context.context  # astrbot.core.star.context.Context
            # 配置助手使用指定模型（settings.agent_provider_id）；未指定则跟随当前聊天 Provider。
            chat_provider_id = ""
            cfg_pid = str((tc.settings or {}).get("agent_provider_id") or "")
            if cfg_pid:
                try:
                    cfg_prov = await ctx.provider_manager.get_provider_by_id(cfg_pid)
                    if cfg_prov is not None:
                        chat_provider_id = cfg_prov.meta().id
                except Exception:  # noqa: BLE001 - 指定 Provider 解析失败回退
                    logger.warning(
                        "agent: 指定 Provider %r 解析失败，回退默认聊天 Provider",
                        cfg_pid,
                        exc_info=True,
                    )
            if not chat_provider_id:
                chat_provider_id = await ctx.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
            llm_resp = await ctx.tool_loop_agent(
                event=event,
                chat_provider_id=chat_provider_id,
                prompt=query,
                system_prompt=CONFIG_AGENT_SYSTEM_PROMPT,
                tools=build_config_toolset(tc),
                max_steps=30,
            )
            return llm_resp.completion_text or "（未产生回复）"
        except Exception as exc:  # noqa: BLE001 - 整体兜底，不向 LLM 抛出
            logger.error("agent: model_scheduler_config 执行异常: %s", exc)
            return f"配置助手执行出错：{exc}"


def _make_web_admin_event():
    """构造 Web 配置助手的合成管理员事件。

    ``tool_loop_agent`` 的 ``agent_context`` 要求 ``AstrAgentContext.event`` 为
    ``AstrMessageEvent`` 实例（pydantic 校验，传 None 会直接报错），而 Web 页面
    场景没有真实消息事件。``AstrMessageEvent`` 无抽象方法且构造参数简单，因此
    合成一个 ``role="admin"`` 的空事件（不发送消息、不接触平台）。
    """
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    meta = PlatformMetadata(
        name="webchat",
        description="Model Morph Web 配置助手（合成事件）",
        id="webchat",
    )
    # AstrBotMessage 为普通类，实例字段需手动补齐（AstrMessageEvent.__init__ 会读取）。
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE
    msg.self_id = "model-morph"
    msg.session_id = "model_morph_web_agent"
    msg.message_id = "model_morph_web_agent"
    msg.sender = MessageMember(user_id="admin", nickname="admin")
    msg.message = []
    msg.message_str = ""
    event = AstrMessageEvent(
        message_str="",
        message_obj=msg,
        platform_meta=meta,
        session_id="model_morph_web_agent",
    )
    event.role = "admin"  # Web Plugin Page 仅管理员可达，合成事件按管理员处理
    return event


async def run_web_agent(context, tc: ToolContext, messages, chat_provider_id) -> dict:
    """Web 助手入口：在无消息事件场景下驱动配置 Agent。

    Args:
        context: astrbot.core.star.context.Context 实例。
        tc: ``ToolContext`` 实例（web 场景置 source=web_agent）。
        messages: 对话历史，形如 ``[{"role", "content"}, ...]``。
        chat_provider_id: 使用的聊天 Provider id。

    Returns:
        ``{"reply": 文本, "pending": tc.pending.get()}``；异常时返回 ``{"error": str(e)}``。
    """
    try:
        tc.source = "web_agent"
        tc.operator = "admin"
        # 合成管理员事件（AstrAgentContext.event 不接受 None，见 _make_web_admin_event）。
        event = _make_web_admin_event()
        agent_context = AstrAgentContext(context=context, event=event)
        # 把 message dict 转成 astrbot.core.agent.message.Message（tool_loop_agent 内部会 model_dump）
        contexts = [
            Message(role=m.get("role", "user"), content=m.get("content", ""))
            for m in (messages or [])
            if isinstance(m, dict)
        ]
        llm_resp = await context.tool_loop_agent(
            event=event,
            chat_provider_id=chat_provider_id,
            system_prompt=CONFIG_AGENT_SYSTEM_PROMPT,
            tools=build_config_toolset(tc),
            contexts=contexts,
            max_steps=30,
            agent_context=agent_context,
        )
        return {
            "reply": llm_resp.completion_text or "（未产生回复）",
            "pending": tc.pending.get(),
        }
    except Exception as exc:  # noqa: BLE001 - 异常兜底
        logger.error("agent: run_web_agent 异常: %s", exc)
        return {"error": str(exc)}
