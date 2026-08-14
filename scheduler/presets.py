"""presets —— 时间调度规则预设（模块 T3，纯逻辑，不依赖 astrbot）。

提供 5 个常见的时间调度场景预设，及其到标准 temporal rule（SchedulerRule）
结构的构建函数，供 Agent 配置层 / Web 预设页一键套用。

设计要点：
- ``PRESETS`` 只描述「元信息 + 参数声明」；不硬编码任何模型/Provider/价格，
  具体模型与组、时间窗全部由调用方在 ``params`` 里指定。
- ``build_preset_rules`` 生成标准的 temporal rule dict（字段名与 v0.1.5 约定严格
  一致：id / name / enabled / kind / group_id / source_provider / target_provider /
  target_group / scope / schedule{type,start,end,weekdays,date,timezone} /
  priority / metadata）。id 形如 ``t_`` + uuid 短 hex。
- 未知 preset_id 抛 ``KeyError``；必填参数缺失抛中文 ``ValueError``。
- 本模块不依赖 temporal（其 normalize/validate 由 T1 并行开发），仅构造标准结构。
"""

from __future__ import annotations

import copy
import uuid

# 目标字段名（供轻量校验/文档参考），与 v0.1.5 统一 SchedulerRule 结构一致。
RULE_KEYS = (
    "id",
    "name",
    "enabled",
    "kind",
    "group_id",
    "source_provider",
    "target_provider",
    "target_group",
    "scope",
    "schedule",
    "priority",
    "metadata",
)

# 默认优先级：时间调度规则（与 temporal 常量 PRIORITY_SCHEDULED 一致）。
DEFAULT_PRIORITY = 200


def _as_list(value) -> list:
    """把值规整为列表（None → []，标量 → [标量]）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _to_int(value, default: int) -> int:
    """把参数转 int；非法/缺失回默认。"""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _require_str(params: dict, key: str, label: str) -> None:
    """必填字符串参数校验；缺失/为空抛中文 ValueError。"""
    if not isinstance(params.get(key), str) or not params[key].strip():
        raise ValueError(f"预设参数缺失或为空: {label}（{key}）")


def _timezone(params: dict) -> str:
    """读取可选的 schedule 时区参数。"""
    return str(params.get("timezone") or "")


PRESETS: dict[str, dict] = {
    "peak_valley": {
        "id": "peak_valley",
        "name": "峰谷模型切换",
        "desc": "高峰时段用低成本模型、低谷时段用高性能模型，生成两条互补的时间规则。",
        "enabled": True,
        "kind": "model_override",
        "times": "默认高峰 18:00-23:00 → 低成本；其余（低谷，跨午夜 23:00-18:00）→ 高性能",
        "params": [
            {
                "key": "group_id",
                "label": "目标模型组",
                "type": "group",
                "required": True,
                "default": "",
            },
            {
                "key": "source_provider",
                "label": "高峰前置模型（高性能）",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "target_provider",
                "label": "低成本替代模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "start",
                "label": "高峰开始",
                "type": "time",
                "required": False,
                "default": "18:00",
            },
            {
                "key": "end",
                "label": "高峰结束",
                "type": "time",
                "required": False,
                "default": "23:00",
            },
            {
                "key": "priority",
                "label": "优先级",
                "type": "int",
                "required": False,
                "default": DEFAULT_PRIORITY,
            },
            {
                "key": "name",
                "label": "规则名",
                "type": "string",
                "required": False,
                "default": "",
            },
        ],
    },
    "night_saving": {
        "id": "night_saving",
        "name": "夜间省钱模式",
        "desc": "夜间（23:00-08:00，跨午夜）把主用模型切到低成本模型，节约费用。",
        "enabled": True,
        "kind": "model_override",
        "times": "默认 23:00-08:00（跨午夜）→ 低成本",
        "params": [
            {
                "key": "group_id",
                "label": "目标模型组",
                "type": "group",
                "required": True,
                "default": "",
            },
            {
                "key": "source_provider",
                "label": "白昼主用模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "target_provider",
                "label": "夜间低成本模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "start",
                "label": "夜间开始",
                "type": "time",
                "required": False,
                "default": "23:00",
            },
            {
                "key": "end",
                "label": "夜间结束",
                "type": "time",
                "required": False,
                "default": "08:00",
            },
            {
                "key": "priority",
                "label": "优先级",
                "type": "int",
                "required": False,
                "default": DEFAULT_PRIORITY,
            },
            {
                "key": "name",
                "label": "规则名",
                "type": "string",
                "required": False,
                "default": "",
            },
        ],
    },
    "workday_performance": {
        "id": "workday_performance",
        "name": "工作时间高性能",
        "desc": "工作日（周一~五）09:00-18:00 使用高性能模型，午休/非工作时段回落基础模型。",
        "enabled": True,
        "kind": "model_override",
        "times": "默认工作日(weekdays=[0..4]) 09:00-18:00 → 高性能",
        "params": [
            {
                "key": "group_id",
                "label": "目标模型组",
                "type": "group",
                "required": True,
                "default": "",
            },
            {
                "key": "source_provider",
                "label": "基础模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "target_provider",
                "label": "工作时间高性能模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "start",
                "label": "开始",
                "type": "time",
                "required": False,
                "default": "09:00",
            },
            {
                "key": "end",
                "label": "结束",
                "type": "time",
                "required": False,
                "default": "18:00",
            },
            {
                "key": "priority",
                "label": "优先级",
                "type": "int",
                "required": False,
                "default": DEFAULT_PRIORITY,
            },
            {
                "key": "name",
                "label": "规则名",
                "type": "string",
                "required": False,
                "default": "",
            },
        ],
    },
    "force_replace": {
        "id": "force_replace",
        "name": "指定模型强制替换",
        "desc": "在指定的时间段内，把组内某模型强制替换为目标模型。",
        "enabled": True,
        "kind": "model_override",
        "times": "用户指定 start/end/date/weekdays",
        "params": [
            {
                "key": "group_id",
                "label": "目标模型组",
                "type": "group",
                "required": True,
                "default": "",
            },
            {
                "key": "source_provider",
                "label": "被替换模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "target_provider",
                "label": "替换为目标模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "start",
                "label": "开始",
                "type": "time",
                "required": True,
                "default": "",
            },
            {
                "key": "end",
                "label": "结束",
                "type": "time",
                "required": True,
                "default": "",
            },
            {
                "key": "weekdays",
                "label": "星期(0=周一..6=周日)",
                "type": "list",
                "required": False,
                "default": [],
            },
            {
                "key": "date",
                "label": "指定日期(YYYY-MM-DD)",
                "type": "string",
                "required": False,
                "default": "",
            },
            {
                "key": "priority",
                "label": "优先级",
                "type": "int",
                "required": False,
                "default": DEFAULT_PRIORITY,
            },
            {
                "key": "name",
                "label": "规则名",
                "type": "string",
                "required": False,
                "default": "",
            },
        ],
    },
    "maintenance": {
        "id": "maintenance",
        "name": "临时维护切换",
        "desc": "维护期间，把指定模型临时切换到备用模型，结束后自动恢复。",
        "enabled": True,
        "kind": "model_override",
        "times": "用户指定 start/end/date/weekdays",
        "params": [
            {
                "key": "group_id",
                "label": "目标模型组",
                "type": "group",
                "required": True,
                "default": "",
            },
            {
                "key": "source_provider",
                "label": "被维护模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "target_provider",
                "label": "备用模型",
                "type": "provider",
                "required": True,
                "default": "",
            },
            {
                "key": "start",
                "label": "维护开始",
                "type": "time",
                "required": True,
                "default": "",
            },
            {
                "key": "end",
                "label": "维护结束",
                "type": "time",
                "required": True,
                "default": "",
            },
            {
                "key": "weekdays",
                "label": "星期(0=周一..6=周日)",
                "type": "list",
                "required": False,
                "default": [],
            },
            {
                "key": "date",
                "label": "指定日期(YYYY-MM-DD)",
                "type": "string",
                "required": False,
                "default": "",
            },
            {
                "key": "priority",
                "label": "优先级",
                "type": "int",
                "required": False,
                "default": DEFAULT_PRIORITY,
            },
            {
                "key": "name",
                "label": "规则名",
                "type": "string",
                "required": False,
                "default": "",
            },
        ],
    },
}


def _make_rule(
    preset: dict,
    params: dict,
    schedule: dict,
    source_provider: str,
    target_provider: str,
    name_suffix: str = "",
) -> dict:
    """构造一条标准 temporal rule dict（默认值 200，scope 全空=全局）。

    Args:
        preset: PRESETS 中的元信息（提供 name / kind）。
        params: 调用方参数（含 group_id / priority / name 等）。
        schedule: 完整的 schedule dict。
        source_provider / target_provider: 替换源/目标模型。
        name_suffix: 追加到规则名的后缀（用于区分互补规则）。

    Returns:
        标准 temporal rule dict。
    """
    base_name = str(params.get("name") or preset["name"])
    return {
        "id": "t_" + uuid.uuid4().hex[:8],
        "name": base_name + name_suffix,
        "enabled": True,
        "kind": preset["kind"],
        "group_id": str(params.get("group_id") or ""),
        "source_provider": str(source_provider or ""),
        "target_provider": str(target_provider or ""),
        "target_group": "",
        "scope": {"groups": [], "users": [], "sessions": []},
        "schedule": copy.deepcopy(schedule),
        "priority": _to_int(params.get("priority"), DEFAULT_PRIORITY),
        "metadata": {"created_by": "", "created_at": "", "source": "preset"},
    }


def _schedule_from_params(params: dict) -> dict:
    """按 params 的 date/weekdays/start/end 推断 schedule。

    有 ``date`` → type=date；否则有 ``weekdays`` → type=weekly；否则 type=daily。
    """
    date = str(params.get("date") or "")
    weekdays = [int(d) for d in _as_list(params.get("weekdays")) if str(d).isdigit()]
    tz = _timezone(params)
    if date:
        return {
            "type": "date",
            "start": str(params.get("start") or ""),
            "end": str(params.get("end") or ""),
            "weekdays": [],
            "date": date,
            "timezone": tz,
        }
    if weekdays:
        return {
            "type": "weekly",
            "start": str(params.get("start") or ""),
            "end": str(params.get("end") or ""),
            "weekdays": weekdays,
            "date": "",
            "timezone": tz,
        }
    return {
        "type": "daily",
        "start": str(params.get("start") or ""),
        "end": str(params.get("end") or ""),
        "weekdays": [],
        "date": "",
        "timezone": tz,
    }


def build_preset_rules(preset_id: str, params: dict) -> list[dict]:
    """按预设生成标准 temporal rule dict 列表。

    Args:
        preset_id: PRESETS 中的预设 id（peak_valley / night_saving /
            workday_performance / force_replace / maintenance）。
        params: 参数 dict，至少提供预设声明为 required 的键。
            公共示例：``{"group_id": "g_x", "source_provider": "p_a",
            "target_provider": "p_b", "start": "20:00", "end": "23:00",
            "weekdays": [], "date": "", "priority": 200, "name": ""}``。

    Returns:
        标准 temporal rule dict 列表（peak_valley 生成两条互补规则，其余各一）。

    Raises:
        KeyError: ``preset_id`` 不在 PRESETS 中。
        ValueError(中文): 必填参数缺失或为空。
    """
    if preset_id not in PRESETS:
        raise KeyError(f"未知预设: {preset_id}")
    preset = PRESETS[preset_id]
    p = params if isinstance(params, dict) else {}

    # 公共必填：group_id / source_provider / target_provider
    _require_str(p, "group_id", "目标模型组")
    _require_str(p, "source_provider", "源模型（被替换模型）")
    _require_str(p, "target_provider", "目标模型（替换后模型）")

    if preset_id == "peak_valley":
        peak_start = str(p.get("start") or "18:00")
        peak_end = str(p.get("end") or "23:00")
        tz = _timezone(p)
        peak = _make_rule(
            preset,
            p,
            {
                "type": "daily",
                "start": peak_start,
                "end": peak_end,
                "weekdays": [],
                "date": "",
                "timezone": tz,
            },
            source_provider=p["source_provider"],
            target_provider=p["target_provider"],
            name_suffix="（高峰）",
        )
        # 低谷：与高峰互补，反区间跨午夜（end→start）。
        valley = _make_rule(
            preset,
            p,
            {
                "type": "daily",
                "start": peak_end,
                "end": peak_start,
                "weekdays": [],
                "date": "",
                "timezone": tz,
            },
            source_provider=p["target_provider"],
            target_provider=p["source_provider"],
            name_suffix="（低谷）",
        )
        return [peak, valley]

    if preset_id == "night_saving":
        schedule = {
            "type": "daily",
            "start": str(p.get("start") or "23:00"),
            "end": str(p.get("end") or "08:00"),  # end<start → 跨午夜
            "weekdays": [],
            "date": "",
            "timezone": _timezone(p),
        }
        return [
            _make_rule(
                preset,
                p,
                schedule,
                source_provider=p["source_provider"],
                target_provider=p["target_provider"],
            )
        ]

    if preset_id == "workday_performance":
        schedule = {
            "type": "weekly",
            "start": str(p.get("start") or "09:00"),
            "end": str(p.get("end") or "18:00"),
            "weekdays": [0, 1, 2, 3, 4],  # 工作日 周一~五
            "date": "",
            "timezone": _timezone(p),
        }
        return [
            _make_rule(
                preset,
                p,
                schedule,
                source_provider=p["source_provider"],
                target_provider=p["target_provider"],
            )
        ]

    # force_replace / maintenance：需显式 start/end。
    _require_str(p, "start", "开始时间")
    _require_str(p, "end", "结束时间")
    schedule = _schedule_from_params(p)
    return [
        _make_rule(
            preset,
            p,
            schedule,
            source_provider=p["source_provider"],
            target_provider=p["target_provider"],
        )
    ]
