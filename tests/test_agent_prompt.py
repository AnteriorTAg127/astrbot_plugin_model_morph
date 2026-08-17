"""agent.py 提示词断言测试（v1.0.2 + v1.0.3，离线，读源码文本而非 import）。

`agent.py` import astrbot 运行时（含 ToolLoopAgentRunner 等），离线 pytest 环境
**不 import 它**，改为用 pathlib 读取其源码文本并对关键串做断言，验证：
- ``CONFIG_AGENT_SYSTEM_PROMPT`` 含 UMO 格式 / 消歧准则 / 防多余调用 / 分级审批段（v1.0.3，G-1）；
- ``tool_defs`` 内各工具描述覆盖关键枚举（strategy / kind / schedule / scope）；
- v1.0.3（G-1）：tool_defs 规则引擎 5 工具 + staged/approve 暂存语义、model_keyword
  三枚举、replace_model 结构。
"""

from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_AGENT_SRC = (_PLUGIN_ROOT / "scheduler" / "agent.py").read_text(encoding="utf-8")


def _find_span(src: str, marker: str) -> str:
    """返回从 marker 首次出现位置起、向后的约 6000 字符源码片段。

    用于把断言限定在 CONFIG_AGENT_SYSTEM_PROMPT 或 tool_defs 附近，避免误命中注释。
    由于两个目标（提示词与工具描述）在文件中位置确定且互不重叠，这里按 marker 定位
    后取「到同级结束」的近似区间即可。
    """
    idx = src.find(marker)
    assert idx != -1, f"源码中未找到 marker: {marker}"
    return src[idx : idx + 6000]


def test_prompt_has_umo_format_section():
    seg = _find_span(_AGENT_SRC, "【会话与 UMO")
    assert "platform_id:message_type:session_id" in seg
    assert "GroupMessage" in seg
    assert "FriendMessage" in seg
    # 三种 message_type 白名单语义至少出现群聊 / 私聊说明。
    assert "群号" in seg
    assert "私聊" in seg or "QQ" in seg
    # scope 三键说明需存在。
    assert "sessions" in seg


def test_prompt_has_disambiguation_rules():
    seg = _find_span(_AGENT_SRC, "【需求分类")
    assert "时间调度规则" in seg
    assert "生命周期" in seg
    # v1.0.3（G-1⑤）：规则引擎写工具已完整提供（create/update/delete/toggle_rule 可完整
    # 读改），旧版「如实说明无写工具」措辞已被移除 —— 断言新语义并锁定旧措辞消失。
    assert "规则引擎" in seg
    assert "create_rule" in seg and "toggle_rule" in seg
    assert "不提供对应的写工具" not in seg
    # 含糊词先查再确认。
    assert "查询" in seg or "list_schedule_rules" in seg or "list_lifecycles" in seg


def test_prompt_prevents_redundant_calls():
    seg = _find_span(_AGENT_SRC, "【需求分类")
    # 防多余调用要点：一句话只触发一次 + 禁止重复 create。
    assert "只触发一次" in seg
    assert "禁止对同一目标重复" in seg


def test_prompt_has_values_in_tool_descriptions():
    # v1.0.3 在 tool_defs 新增 5 个规则引擎工具声明后，固定 6000 字符窗口不再覆盖
    # 靠后的 create_schedule_rule 描述，故改为「tool_defs 开头 → def make_tool 之前」的完整列表区间。
    start = _AGENT_SRC.find("tool_defs:")
    end = _AGENT_SRC.find("def make_tool", start)
    seg = _AGENT_SRC[start:end]
    # strategy 五枚举（含 target 三个：round_robin / weighted / fallback）。
    assert "round_robin" in seg
    assert "weighted" in seg
    assert "fallback" in seg
    # kind 二枚举含 group_switch。
    assert "group_switch" in seg
    # schedule 类型含 weekly。
    assert "weekly" in seg
    # scope 三键。
    assert "groups" in seg
    assert "users" in seg
    assert "sessions" in seg


def test_prompt_preview_ops_enum_full():
    seg = _find_span(_AGENT_SRC, "tool_preview_configuration_change")
    for op in (
        "create_schedule_rule",
        "update_schedule_rule",
        "delete_schedule_rule",
        "create_model_group",
        "update_model_group",
        "delete_model_group",
        "create_lifecycle",
        "update_lifecycle",
        "delete_lifecycle",
    ):
        assert op in seg, f"preview ops 枚举缺少 {op}"


def test_prompt_lifecycle_fields_documented():
    seg = _find_span(_AGENT_SRC, "tool_create_lifecycle")
    for field in (
        "stages",
        "final_group",
        "periodic_group",
        "periodic_interval",
        "calibration_event",
        "calibration_group",
        "calibration_rounds",
    ):
        assert field in seg, f"生命周期 spec 缺少字段 {field}"


def test_prompt_short_and_long_schedule_constraints_documented():
    seg = _find_span(_AGENT_SRC, "tool_create_schedule_rule")
    # 时间约束：HH:MM、跨午夜、weekdays 0-6、date 格式。
    assert "HH:MM" in seg
    assert "跨午夜" in seg
    assert "weekdays" in seg and "周日" in seg
    assert "YYYY-MM-DD" in seg


def test_prompt_scope_global_semantics_documented():
    """G4：scope 结构说明须含「三键全空=全局」语义与 sessions 存完整 UMO 的措辞。"""
    seg = _find_span(_AGENT_SRC, "【会话与 UMO")
    assert "全局" in seg
    assert "完整 UMO" in seg or "原样" in seg or "原样使用" in seg


def test_prompt_schedule_rule_fields_documented():
    """G5：create_schedule_rule 描述须含 source_provider / target_provider / target_group 三字段。"""
    seg = _find_span(_AGENT_SRC, "tool_create_schedule_rule")
    assert "source_provider" in seg
    assert "target_provider" in seg
    assert "target_group" in seg


# ---- v1.0.3 新断言（G-1，PRD §9 验收项）----


def test_prompt_tool_defs_rule_tools_and_staging():
    """G-1①：tool_defs 区间含 5 个规则引擎工具名及其所述 暂存 / approve 语义。"""
    start = _AGENT_SRC.find("tool_defs:")
    end = _AGENT_SRC.find("def make_tool", start)
    seg = _AGENT_SRC[start:end]
    for name in (
        "tool_get_rule",
        "tool_create_rule",
        "tool_update_rule",
        "tool_delete_rule",
        "tool_toggle_rule",
    ):
        assert name in seg, f"tool_defs 缺少规则工具 {name}"
    # 暂存语义措辞：高危写「不会立即生效 / 暂存」，批准方式以工具返回的 approval_hint 为准
    # （v1.0.3 起按来源区分 Web 按钮 / SubAgent 指令，工具描述不再写死 /scheduler approve）。
    assert "暂存" in seg
    assert "approval_hint" in seg


def test_prompt_rule_tool_mentions_model_keyword_and_replace_model():
    """G-1②③：create_rule 描述含 model_keyword 三枚举与 replace_model 结构。"""
    seg = _find_span(_AGENT_SRC, "tool_create_rule")
    assert "model_keyword" in seg
    assert "关键词" in seg
    for mode in ("all", "any", "min_n"):
        assert mode in seg, f"model_keyword 缺少枚举 {mode}"
    assert "replace_model" in seg
    assert "provider_id" in seg
    assert "model" in seg


def test_prompt_has_graded_approval_section():
    """G-1④：CONFIG_AGENT_SYSTEM_PROMPT 含分级审批 / 暂存 / approve / reject 指令提示。"""
    seg = _find_span(_AGENT_SRC, "【需求分类")
    assert "分级审批" in seg or "暂存" in seg
    assert "/scheduler approve" in seg or "approve" in seg
    assert "/scheduler reject" in seg or "reject" in seg


# ---- v1.0.3 场景化提示词（Web 与 SubAgent 批准方式区分）----


def test_prompt_has_source_aware_builder():
    """build_config_system_prompt 存在，且按 source 附加「当前场景」指引。"""
    seg = _find_span(_AGENT_SRC, "def build_config_system_prompt")
    assert "web_agent" in seg
    assert "subagent" in seg
    assert "当前场景" in seg


def test_prompt_web_scene_uses_button_not_command():
    """Web 场景：提示词必须引导「点击页面按钮」，且明确禁止让管理员执行聊天指令。"""
    seg = _find_span(_AGENT_SRC, "当前场景：Web 配置助手")
    assert "没有指令输入框" in seg
    assert "『批准』按钮" in seg
    assert "/scheduler approve" in seg  # 出现在「不要让管理员执行」的禁令语境中
    assert "无法执行" in seg


def test_prompt_subagent_scene_uses_commands():
    """SubAgent 场景：提示词引导在聊天中执行 /scheduler approve|reject|pending 指令。"""
    seg = _find_span(_AGENT_SRC, "当前场景：聊天 SubAgent")
    assert "/scheduler approve" in seg
    assert "/scheduler reject" in seg
    assert "/scheduler pending" in seg
    assert "聊天" in seg
