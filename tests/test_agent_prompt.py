"""agent.py 提示词断言测试（v1.0.2，离线，读源码文本而非 import）。

`agent.py` import astrbot 运行时（含 ToolLoopAgentRunner 等），离线 pytest 环境
**不 import 它**，改为用 pathlib 读取其源码文本并对关键串做断言，验证：
- ``CONFIG_AGENT_SYSTEM_PROMPT`` 含 UMO 格式 / 消歧准则 / 防多余调用；
- ``tool_defs`` 内各工具描述覆盖关键枚举（strategy / kind / schedule / scope）。
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
    # 规则引擎（when/then）如实说明无写工具。
    assert "when/then" in seg or "规则引擎" in seg
    # 含糊词先查再确认。
    assert "查询" in seg or "list_schedule_rules" in seg or "list_lifecycles" in seg


def test_prompt_prevents_redundant_calls():
    seg = _find_span(_AGENT_SRC, "【需求分类")
    # 防多余调用要点：一句话只触发一次 + 禁止重复 create。
    assert "只触发一次" in seg
    assert "禁止对同一目标重复" in seg


def test_prompt_has_values_in_tool_descriptions():
    seg = _find_span(_AGENT_SRC, "tool_defs")
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
