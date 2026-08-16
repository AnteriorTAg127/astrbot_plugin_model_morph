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
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Json, Plain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_system_tmp_path
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
    "【会话与 UMO（务必遵守）】\n"
    "AstrBot 用 UMO（统一消息来源）唯一标识一个会话。UMO 官方格式为："
    "``platform_id:message_type:session_id``。\n"
    "- platform_id：平台适配器实例唯一标识（如 aiocqhttp / webchat / telegram），同一类型"
    "可多实例；\n"
    "- message_type：消息类型，取值 ``GroupMessage``（群聊）、``FriendMessage``（私聊）、"
    "``OtherMessage``（其他）三者之一；\n"
    "- session_id：来源 ID——aiocqhttp 群消息为群号、私聊为发送者 QQ 号。\n"
    "处理规则：\n"
    "- 用户给出**完整 UMO**（如 aiocqhttp:GroupMessage:123456）时，原样用在 ``scope.sessions``，"
    "不做改写；\n"
    "- 用户给出**群号**（一串数字且语境是群）时，换算为 ``{platform_id}:GroupMessage:{群号}``；\n"
    "- 用户给出 **QQ 号**（一串数字且语境是私聊/好友）时，换算为 "
    "``{platform_id}:FriendMessage:{QQ}``；\n"
    '- ``scope`` 结构为 ``{"groups": [群号...], "users": [QQ...], "sessions": [完整 UMO...]}``：'
    "groups 存群号、users 存 QQ 号、sessions 存完整 UMO（含平台类型信息）。三个键都为空表示"
    "全局作用域。\n"
    "- 换算时 platform_id 沿用用户上下文给出的平台（默认 aiocqhttp）；无法确定时向管理员确认"
    "平台后再换算。示例：群 123456 → aiocqhttp:GroupMessage:123456；QQ 987654 → "
    "aiocqhttp:FriendMessage:987654。\n\n"
    "【需求分类（重要，先判断再选工具）】接到管理员关于调度的需求时，先判断它属于下面哪一类，"
    "再调用对应工具，避免调错工具：\n"
    "a) 时间调度规则（temporal）：按时间段 / 星期 / 日期路由模型或整组。触发词如「几点到几点」"
    "「每天 / 每周」「高峰时段」「晚上用便宜模型」。→ 用 create_schedule_rule / "
    "update_schedule_rule（kind=model_override=按小时替换本组内某 Provider "
    "kind=group_switch=本组请求走另一组）。\n"
    "b) 生命周期（lifecycle）：按会话轮数多阶段降级 / 周期校准 / 上下文压缩校准。触发词如"
    "「前 N 轮用 X，之后用 Y / 降级 / 校准」。→ 用 create_lifecycle / update_lifecycle。\n"
    "c) 规则引擎规则（when/then 条件规则）：本版 Agent 工具集**不提供**对应的写工具。若用户"
    "明确要求「条件规则 / 事件规则」，如实说明可在 WebUI 规则页配置，不要尝试用其他工具硬造。\n"
    "d) 含糊词「调度」：先用查询工具（list_schedule_rules / list_lifecycles）了解现有配置；仍"
    "无法确定管理员意图时，向管理员确认后再执行，不要擅自猜测创建。\n"
    "e) 防止多余调用：一句需求**只触发一次**写流程；禁止对同一目标重复 create；任何 create / "
    "update 之前必须先用查询工具确认目标存在及当前值（先查询再修改）。\n\n"
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
        (
            "tool_list_model_groups",
            "查询全部模型组。返回值每个条目含：id（组唯一标识）、name（组名）、enabled（是否启用）、"
            "strategy（组选择策略）、provider_count（成员 Provider 数）。用于动手前确认组 id / 现状。",
            {},
        ),
        (
            "tool_get_model_group",
            "按组 id 或名称查询单个模型组详情（含完整 strategy / providers / fallbacks 配置）。"
            "未精确命中时返回 candidates（模糊匹配建议）。",
            {"name": "组 id 或名称"},
        ),
        (
            "tool_list_models",
            "列出已配置的所有聊天 Provider。返回值条目含：id（Provider 唯一标识）、model（模型名）、"
            "type（服务类型）、enabled（是否启用）、group_count（被多少个组引用）。用于确认 Provider 真实 id。",
            {},
        ),
        (
            "tool_list_rules",
            "查询现有规则与时间调度规则。返回值含：rules（when/then 条件规则，本版无写工具，仅读）、"
            "temporal_rules（时间调度规则）。用于「含糊调度词」时先查看现状。",
            {},
        ),
        (
            "tool_get_active_rules",
            "列出当前时刻正在生效的时间调度规则（按时间匹配）。用于了解此刻哪些规则在起作用。",
            {},
        ),
        (
            "tool_get_scheduled_rules",
            "列出全部时间调度规则（含未启用的）。返回值条目含 kind / schedule / scope / priority / enabled。",
            {},
        ),
        (
            "tool_get_scheduler_status",
            "获取调度器全局状态。返回值字段含义：enabled（开关）、tz（调度时区）、group_count（模型组数）、"
            "rule_count（条件规则数）、temporal_count（时间调度规则数）、provider_count（已配置 Provider 数）、"
            "base_group（基础默认组）、lifecycle_count（生命周期数）、default_lifecycle（全局默认生命周期 id）、"
            "agent_confirm（是否要求高危预览确认）、conflicts（规则冲突数）。",
            {},
        ),
        (
            "tool_get_runtime_routing",
            "只读推演：某模型组在当前时间 / 会话条件下的最终路由。返回值字段含义：group_id（目标组）、"
            "provider（最终 Provider id）、temporal_matched_id（命中的时间调度规则 id，未命中为空）、"
            "replacement_chain（替换链，A→B 逐级）、reason（推演说明）。不改动任何配置。",
            {},
        ),
        (
            "tool_create_model_group",
            "创建模型组。spec 字段说明："
            "name（组名，必填）、desc（描述）、enabled（是否启用，默认 true）、"
            "strategy（组内选 Provider 策略，五枚举：priority=按 priority 取最高可用 / "
            "round_robin=轮流 / weighted=按 weight 权重随机 / random=等权随机 / fallback=优先第一个"
            "失败则降级到下一个）、"
            "providers（成员列表，条目为 {provider_id: 已配置 Provider id, priority: 调用优先级"
            "（数字越小越优先）, weight: 权重（weighted 策略用）, enabled: 是否启用成员}）、"
            "fallbacks（降级 Provider id 列表，主链全部失败后依次兜底）。所有 provider_id 必须先查询确认。",
            {
                "spec": {
                    "type": "object",
                    "description": (
                        '模型组字典：{"name": 组名, "desc": 描述, "enabled": bool, '
                        '"strategy": "priority"|"round_robin"|"weighted"|"random"|"fallback", '
                        '"providers": [{"provider_id": 已配置Provider id, "priority": int, '
                        '"weight": int, "enabled": bool}], "fallbacks": [Provider id 列表]}'
                    ),
                }
            },
        ),
        (
            "tool_update_model_group",
            "更新模型组（合并式：只写需要改的键）。group_id 为要更新的组 id；spec 结构同 "
            "create_model_group。strategy 五枚举与 providers 条目含义见 create_model_group。",
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
            "删除模型组（高危，若 agent_confirm 开启需先 preview 再 apply）。group_id 为目标组 id。",
            {"group_id": "组 id"},
        ),
        (
            "tool_create_schedule_rule",
            "创建时间调度规则。spec："
            "kind 二枚举：model_override=把作用域内本组选用的 source_provider 替换为 target_provider；"
            "group_switch=作用域内本组（group_id）的请求改走 target_group 组。"
            "group_id：规则作用的组 id，为空串表示全局规则。source_provider/target_provider（kind=model_override 用）、"
            "target_group（kind=group_switch 用）须先用查询确认存在。"
            "schedule 四类型：always=始终 / daily=每天（需 start/end 为 24h 制 HH:MM，end<start 表示跨午夜）/"
            "weekly=每周（需 weekdays 0=周一..6=周日，必填）/ date=指定日期（需 date 为 YYYY-MM-DD）。"
            "priority：int，越高越先匹配。scope 结构 {\"groups\": [群号...], \"users\": [QQ...], "
            "\"sessions\": [完整UMO...]}，三键全空=全局。",
            {
                "spec": {
                    "type": "object",
                    "description": (
                        '时间调度规则字典：{kind: "model_override"|"group_switch", group_id: ""（全局）或组 id, '
                        'source_provider, target_provider, target_group, schedule: {type: "always"|"daily"|'
                        '"weekly"|"date", start: "HH:MM", end: "HH:MM", weekdays: [0-6]（weekly 必填）, '
                        'date: "YYYY-MM-DD"}, priority: int, scope: {"groups": [], "users": [], "sessions": []}}'
                    ),
                }
            },
        ),
        (
            "tool_update_schedule_rule",
            "更新时间调度规则（合并式）。rule_id 为目标规则 id；spec 结构同 create_schedule_rule，"
            "kind 二枚举与 schedule 四类型约束见 create_schedule_rule。",
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
            "删除时间调度规则（高危，若 agent_confirm 开启需先 preview 再 apply）。rule_id 为目标规则 id。",
            {"rule_id": "规则 id"},
        ),
        (
            "tool_enable_schedule_rule",
            "启用一条时间调度规则。rule_id 为目标规则 id；启用后立即参与匹配。",
            {"rule_id": "规则 id"},
        ),
        (
            "tool_disable_schedule_rule",
            "停用一条时间调度规则。rule_id 为目标规则 id；停用后不再命中。",
            {"rule_id": "规则 id"},
        ),
        (
            "tool_create_model_override",
            "创建模型替换规则（便捷封装，kind 固定为 model_override）。spec：把作用域内本组选用的 "
            "source_provider 替换为 target_provider（schedule / scope / priority 约束同 create_schedule_rule）。",
            {
                "spec": {
                    "type": "object",
                    "description": "规则字典（kind 固定为 model_override；结构见 create_schedule_rule 的 spec）",
                }
            },
        ),
        (
            "tool_update_model_override",
            "更新模型替换规则（合并式，kind 固定为 model_override）。rule_id 为目标规则 id。",
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
            "删除模型替换规则（高危，若 agent_confirm 开启需先 preview 再 apply）。rule_id 为目标规则 id。",
            {"rule_id": "规则 id"},
        ),
        (
            "tool_validate_configuration",
            "全量校验当前配置：返回 errors（错误）/ warnings（警告）/ conflicts（规则冲突）。任一错误=配置不合法，"
            "改动前先跑一次可确认配置是否健康。",
            {},
        ),
        (
            "tool_reload_scheduler",
            "使调度缓存失效并返回最新状态。用于改动后强制刷新运行时缓存。",
            {},
        ),
        (
            "tool_preview_configuration_change",
            "预览一批配置更改（校验但不写入）。ops 为操作列表，每项的 action 枚举（完整）："
            "create_schedule_rule / update_schedule_rule / delete_schedule_rule / "
            "create_model_group / update_model_group / delete_model_group / "
            "create_lifecycle / update_lifecycle / delete_lifecycle；字段：data（对应规则/组/生命周期字典）、"
            "rule_id（更新/删除规则时用）、group_id（更新/删除组时用）、lifecycle_id（更新/删除生命周期时用）。"
            "预览通过后须 apply 才真正写入。",
            {
                "ops": {
                    "type": "array",
                    "description": (
                        '操作列表，每项：{action: "create_schedule_rule"|"update_schedule_rule"|'
                        '"delete_schedule_rule"|"create_model_group"|"update_model_group"|'
                        '"delete_model_group"|"create_lifecycle"|"update_lifecycle"|"delete_lifecycle", '
                        "data: 规则/组/生命周期字典, rule_id?, group_id?, lifecycle_id?}"
                    ),
                }
            },
        ),
        (
            "tool_apply_configuration_change",
            "应用待执行的配置更改（把 preview 暂存的更改真正写入库）。preview 之后、确认无误时调用。",
            {},
        ),
        (
            "tool_rollback_configuration_change",
            "回滚到应用前的配置快照。用于撤销刚 apply 的更改。",
            {},
        ),
        # ---- v0.1.6：生命周期（含多阶段降级 / 周期校准 / 全局默认） ----
        (
            "tool_list_lifecycles",
            "查询全部生命周期策略。返回值每项含：id（生命周期 id）、name、enabled、模式概要"
            "（stages 段数量 / final_group / 是否周期校准）。用于确认现状与生命周期 id。",
            {},
        ),
        (
            "tool_get_lifecycle",
            "按 id 或名称查询单个生命周期详情（含完整 stages / final_group / periodic / calibration 字段）。"
            "未精确命中时返回 candidates。",
            {"name": "生命周期 id 或名称"},
        ),
        (
            "tool_create_lifecycle",
            "创建生命周期：按会话轮数多阶段降级 / 周期校准 / 上下文压缩校准。spec 字段："
            "name（名称）、enabled（是否启用）、stages=[{group_id: 已存在组 id, rounds: 正整数}]（按累计轮次逐段切换），"
            "final_group（stages 全部耗尽后使用的主组 id，可为空串）、periodic_group（周期校准组 id，可为空串）+ "
            "periodic_interval（正整数，第 N 的整数倍轮固定用 periodic_group 校准一次）、"
            'calibration_event（"" 或 "context_compression"）+ calibration_group + calibration_rounds（校准持续轮数）。'
            "scope / priority 约束同时间调度规则。所有 group_id 须先查询确认存在。",
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
                        "calibration_rounds: 正整数（calibration_event 非空时必填）, "
                        'scope: {"groups": [], "users": [], "sessions": []}}'
                    ),
                }
            },
        ),
        (
            "tool_update_lifecycle",
            "合并更新生命周期。lifecycle_id 为目标生命周期 id；spec 结构同 create_lifecycle，只写需要改的键。",
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
            "删除生命周期（高危，若 agent_confirm 开启需先 preview 再 apply）。lifecycle_id 为目标生命周期 id。",
            {"lifecycle_id": "生命周期 id"},
        ),
        (
            "tool_set_default_lifecycle",
            "设置全局默认生命周期（全局启用某生命周期 / 降级预设）。lifecycle_id 传空串=清除全局默认。",
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
        "创建/修改/删除时间调度规则或模型组、按会话轮数降级。把管理员的需求原话作为 query 传入。"
        "模型会先判断需求属于时间调度规则（temporal）、生命周期（lifecycle）还是查询，再调用"
        "对应工具，避免调错/重复调用。涉及群号/QQ 时按 UMO 格式换算"
        "（platform_id:GroupMessage:群号 / platform_id:FriendMessage:QQ）。"
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


async def run_web_agent_stream(context, tc: ToolContext, messages, chat_provider_id):
    """SSE 配置 Agent 流式生成器：产出统一的中文可序列化 dict 事件流。

    镜像 ``Context.tool_loop_agent`` 的装配流程（``ToolLoopAgentRunner`` + streaming=True），
    逐个消费 ``step_until_done(max_steps=30)`` 产出的 ``AgentResponse`` 帧并转成事件 dict。
    每个事件 dict 由调用方（web/api.py）负责序列化为 SSE ``data: <json>\\n\\n``。

    事件帧类型与字段（与 ``GET agent/chat/stream`` 的契约完全一致）：
    - ``{"type": "meta", ...}``：由调用方注入（本生成器不产出）。
    - ``{"type": "delta", "text": str}``：``streaming_delta`` 帧中正文 Plain 文本的*增量*。
      多个 ``streaming_delta`` 帧可能重复携带**累计**文本（Provider 流式 chunk 常用累计法），
      故按 deerflow runner 的 ``prev_text_for_streaming`` 模式：若新完整文本以已发文本为前缀，
      只下发 ``新文本[len(已发):]`` 增量；否则整段下发。``reasoning``（chain type=="reasoning"）
      帧的思考文本跳过、不与正文混流，避免思考过程污染回复。
    - ``{"type": "tool", "name": str, "args": ...}``：工具调用提示帧。chain type=="tool_call"
      时从 ``Json`` 组件取 ``name`` / ``args`` 下发，供前端显示「正在使用工具 xxx」；若取不到
      跳过该帧，不影响 delta / done / error。
    - ``{"type": "done", "reply": str}``：结束帧，含最终完整回复
      （``runner.get_final_llm_resp().completion_text``）。
    - ``{"type": "error", "message": str}``：异常兜底帧。任何异常（含 reset / step 失败）都
      产出一帧 error 后结束，**绝不向调用方抛出**。

    Args:
        context: astrbot.core.star.context.Context 实例。
        tc: ``ToolContext`` 实例（web 场景置 source=web_agent）。
        messages: 对话历史，形如 ``[{"role", "content"}, ...]``。
        chat_provider_id: 使用的聊天 Provider id。

    Yields:
        事件 dict（可 JSON 序列化）。
    """
    try:
        tc.source = "web_agent"
        tc.operator = "admin"
        # 解析 Provider（与 tool_loop_agent / POST 流程一致；无该 Provider 视为整体失败）。
        provider = await context.provider_manager.get_provider_by_id(chat_provider_id)
        if provider is None:
            yield {"type": "error", "message": "聊天 Provider 不可用"}
            return

        # 构造请求与运行上下文（镜像 Context.tool_loop_agent 的装配）。
        request = ProviderRequest(
            prompt=None,
            image_urls=[],
            audio_urls=[],
            func_tool=build_config_toolset(tc),
            contexts=[
                Message(role=m.get("role", "user"), content=m.get("content", "")).model_dump()
                for m in (messages or [])
                if isinstance(m, dict)
            ],
            system_prompt=CONFIG_AGENT_SYSTEM_PROMPT,
        )
        event = _make_web_admin_event()
        agent_context = AstrAgentContext(context=context, event=event)
        agent_runner = ToolLoopAgentRunner()
        tool_executor = FunctionToolExecutor()

        # 镜像 tool_loop_agent：func_tool 含 astrbot_file_read_tool 时才设置溢出目录与读工具。
        other_kwargs: dict = {}
        if request.func_tool and request.func_tool.get_tool("astrbot_file_read_tool"):
            other_kwargs.setdefault("tool_result_overflow_dir", get_astrbot_system_tmp_path())
            other_kwargs.setdefault(
                "read_tool", request.func_tool.get_tool("astrbot_file_read_tool")
            )
        await agent_runner.reset(
            provider=provider,
            request=request,
            run_context=AgentContextWrapper(
                context=agent_context, tool_call_timeout=120
            ),
            tool_executor=tool_executor,
            agent_hooks=BaseAgentRunHooks[AstrAgentContext](),
            streaming=True,
            **other_kwargs,
        )

        # 逐帧消费，转成统一事件流。
        prev_text = ""  # 已发正文累计文本（增量计算基准）
        async for resp in agent_runner.step_until_done(max_steps=30):
            rtype = getattr(resp, "type", "")
            data = getattr(resp, "data", None) or {}
            chain = data.get("chain") if isinstance(data, dict) else None
            if rtype == "streaming_delta":
                # 跳过 reasoning（思考过程），仅处理正文 Plain 增量。
                if chain is not None and chain.type == "reasoning":
                    continue
                full_text = _extract_plain_text(chain)
                if not full_text:
                    continue
                if full_text.startswith(prev_text):
                    delta = full_text[len(prev_text) :]
                else:
                    delta = full_text if full_text != prev_text else ""
                prev_text = full_text
                if delta:
                    yield {"type": "delta", "text": delta}
            elif rtype == "tool_call" or (
                chain is not None and chain.type == "tool_call"
            ):
                tool_info = _extract_tool_call(chain)
                if tool_info:
                    yield {
                        "type": "tool",
                        "name": tool_info.get("name", ""),
                        "args": tool_info.get("args", {}),
                    }
                # 取不到 tool 信息时跳过该帧（不影响后续）。

        # 结束：用 final llm resp 收尾。
        llm_resp = agent_runner.get_final_llm_resp()
        reply = (
            (llm_resp.completion_text if llm_resp is not None else "")
            or "（未产生回复）"
        )
        yield {"type": "done", "reply": reply}
    except Exception as exc:  # noqa: BLE001 - 流式处理异常兜底，产出 error 帧结束
        logger.error("agent: run_web_agent_stream 异常: %s", exc)
        try:
            yield {"type": "error", "message": str(exc)}
        except Exception:  # noqa: BLE001 - 极端情况下游已断开，静默结束
            logger.warning("agent: run_web_agent_stream 产出 error 帧失败（下游已断开）")


def _extract_plain_text(chain) -> str:
    """从 MessageChain 提取 Plain 组件的拼接文本；chain 为空或非链返回空串。"""
    try:
        if chain is None:
            return ""
        parts = []
        for comp in chain.chain or []:
            if isinstance(comp, Plain) and getattr(comp, "text", None):
                parts.append(str(comp.text))
        return "".join(parts)
    except Exception:  # noqa: BLE001 - 组件解析失败视为无文本
        return ""


def _extract_tool_call(chain) -> dict | None:
    """从 tool_call 类型的 MessageChain 提取 {name, args}；取不到返回 None。"""
    try:
        if chain is None:
            return None
        for comp in chain.chain or []:
            if isinstance(comp, Json) and isinstance(comp.data, dict):
                data = comp.data
                name = data.get("name")
                if name:
                    args = data.get("args") or {}
                    return {"name": str(name), "args": args}
        return None
    except Exception:  # noqa: BLE001 - 解析失败不产出 tool 帧
        return None
