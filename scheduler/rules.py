"""rules —— 规则引擎（模块 D，纯逻辑，不依赖 astrbot）。

负责规则的 CRUD 规范化，以及给定 ``RuleContext`` 时逐规则评估条件，
产出可解释的命中/拒绝轨迹（供 WebUI 与调度引擎展示）。

设计要点：
- 纯内存计算，时间比较只用传入的 ``ctx.now``（含时区），不读取系统当前时间。
- 条件类型丰富（time_range / date_weekday / scope / keyword / command /
  at_bot / message_type / round_gte / context_length_gte / lifecycle_event），
  每种返回 ``ConditionResult``，reason 含实际值对比，便于追溯。
- ``evaluate`` 返回结构化结果：matched_rule / rejected 列表 / results。
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .persistence import ConfigStore

logger = logging.getLogger("astrbot_plugin_model_morph")

# 规则动作的合法取值（供前端下拉与 contract 校验）。
ACTIONS = ("switch_group", "switch_provider", "apply_lifecycle", "unlock")

# 条件类型集合（用于校验 / 快速分派）。
_COND_TYPES = (
    "time_range",
    "date_weekday",
    "scope",
    "keyword",
    "command",
    "at_bot",
    "message_type",
    "round_gte",
    "context_length_gte",
    "lifecycle_event",
)

# 时间 "HH:MM" 正则。
_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


@dataclass
class RuleContext:
    """一次规则评估的输入快照（由引擎构造，含时区与会话元数据）。

    ``now`` 必须携带正确时区；``tz`` 用于星期/日期判断解析。
    """

    now: datetime  # 含时区的当前时间
    tz: ZoneInfo  # 当前时区
    umo: str  # 会话唯一标识（unified_msg_origin）
    platform_id: str = ""
    platform_name: str = ""
    group_id: str = ""
    sender_id: str = ""
    is_group: bool = False
    message_type: str = "group"  # group | private
    message_str: str = ""
    at_bot: bool = False
    round: int = 0
    context_length: int = 0
    lifecycle_event: str = ""  # "" | "new" | "reset"


@dataclass
class ConditionResult:
    """单个条件的求值结果：是否命中 + 人类可读原因（含实际值对比）。"""

    condition: dict
    matched: bool
    reason: str

    def to_dict(self) -> dict:
        """转为可 JSON 序列化 dict（供 WebUI / 调度 trace 使用）。"""
        return {
            "condition": copy.deepcopy(self.condition),
            "matched": self.matched,
            "reason": self.reason,
        }


def _none_to(cond, key, default) -> object:
    """读取条件字段，非法 None 返回默认值。"""
    value = cond.get(key)
    return default if value is None else value


def _as_list(value) -> list:
    """把值规整为列表（None → []，标量 → [标量]）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _default_scope() -> dict:
    """scope 条件/规则作用域默认结构（全部 include/exclude 列表为空）。"""
    return {
        "groups": [],
        "users": [],
        "sessions": [],
        "platforms": [],
        "exclude_groups": [],
        "exclude_users": [],
    }


def normalize_rule(raw: dict) -> dict:
    """补齐规则缺失字段并返回新 dict（不修改入参）。

    默认：id ``r_`` + uuid hex[:8]；enabled True；priority 0；
    scope 各 include/exclude 列表为 []；when ``{op:'and', conditions:[]}``；
    then ``{action:'switch_group', group_id:''}``。
    """
    raw = raw or {}
    rule = copy.deepcopy(raw)

    rule.setdefault("id", "r_" + uuid.uuid4().hex[:8])
    rule.setdefault("name", "")
    rule.setdefault("enabled", True)
    rule.setdefault("priority", 0)

    # 作用域：合并默认，保证 6 个键都存在且为列表
    scope = copy.deepcopy(_default_scope())
    raw_scope = rule.get("scope")
    if isinstance(raw_scope, dict):
        for key in scope:
            if key in raw_scope:
                scope[key] = _as_list(raw_scope.get(key))
    rule["scope"] = scope

    # when：op 与 conditions
    raw_when = rule.get("when")
    if isinstance(raw_when, dict):
        when = copy.deepcopy(raw_when)
        when.setdefault("op", "and")
        when.setdefault("conditions", [])
    else:
        when = {"op": "and", "conditions": []}
    rule["when"] = when

    # then：action 与 group_id 默认
    raw_then = rule.get("then")
    if isinstance(raw_then, dict):
        then = copy.deepcopy(raw_then)
        then.setdefault("action", "switch_group")
        then.setdefault("group_id", "")
    else:
        then = {"action": "switch_group", "group_id": ""}
    rule["then"] = then

    return rule


def _parse_hhmm(value) -> tuple[int, int] | None:
    """解析 "HH:MM" → (hour, minute)；非法返回 None。"""
    if not isinstance(value, str):
        return None
    match = _HHMM_RE.match(value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _eval_time_range(cond: dict, ctx: RuleContext) -> ConditionResult:
    """time_range：HH:MM 区间（支持跨午夜），可选 weekdays 过滤。"""
    now = ctx.now
    cur_min = now.hour * 60 + now.minute
    cur_txt = f"{now.hour:02d}:{now.minute:02d}"

    weekdays = _as_list(cond.get("weekdays"))
    if weekdays:
        if now.weekday() not in weekdays:
            return ConditionResult(
                cond,
                False,
                f"time_range ✗ 星期 {now.weekday()} (0=周一) 不在 {weekdays}，当前 {cur_txt}",
            )

    start_hm = _parse_hhmm(_none_to(cond, "start", ""))
    end_hm = _parse_hhmm(_none_to(cond, "end", ""))
    if start_hm is None or end_hm is None:
        return ConditionResult(
            cond,
            False,
            f"time_range ✗ 无效起止时间 start={cond.get('start')!r} end={cond.get('end')!r}",
        )

    start_min = start_hm[0] * 60 + start_hm[1]
    end_min = end_hm[0] * 60 + end_hm[1]
    if end_min < start_min:  # 跨午夜
        matched = cur_min >= start_min or cur_min <= end_min
    else:
        matched = start_min <= cur_min <= end_min

    range_txt = f"{start_hm[0]:02d}:{start_hm[1]:02d}-{end_hm[0]:02d}:{end_hm[1]:02d}"
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond,
        matched,
        f"time_range {mark} {range_txt} 当前 {cur_txt}",
    )


def _eval_date_weekday(cond: dict, ctx: RuleContext) -> ConditionResult:
    """date_weekday：具体日期 / 工作日/周末模式 / 星期列表，三者择一。"""
    now = ctx.now
    today_str = now.strftime("%Y-%m-%d")
    weekday = now.weekday()

    if cond.get("date"):
        date = str(cond["date"]).strip()
        matched = today_str == date
        return ConditionResult(
            cond,
            matched,
            f"date_weekday {'✓' if matched else '✗'} 期望 {date} 当前 {today_str}",
        )

    mode = cond.get("mode")
    if mode in ("workday", "weekend"):
        matched = (weekday < 5) if mode == "workday" else (weekday >= 5)
        label = "工作日(周一~五)" if mode == "workday" else "周末(周六~日)"
        return ConditionResult(
            cond,
            matched,
            f"date_weekday {'✓' if matched else '✗'} {label} "
            f"当前 {today_str} 星期{weekday}",
        )

    days = _as_list(cond.get("days"))
    if days:
        matched = weekday in days
        return ConditionResult(
            cond,
            matched,
            f"date_weekday {'✓' if matched else '✗'} 期望星期 {days} (0=周一) 当前 {today_str} 星期{weekday}",
        )

    return ConditionResult(cond, False, "date_weekday ✗ 未配置 date/mode/days 任一条件")


def _matches_included(scope_cond: dict, ctx: RuleContext) -> bool:
    """至少一个 include 列表命中当前上下文即返回 True（任一列表非空且命中）。"""
    groups = _as_list(scope_cond.get("groups"))
    users = _as_list(scope_cond.get("users"))
    sessions = _as_list(scope_cond.get("sessions"))
    platforms = _as_list(scope_cond.get("platforms"))
    if groups and ctx.group_id in groups:
        return True
    if users and ctx.sender_id in users:
        return True
    if sessions and ctx.umo in sessions:
        return True
    if platforms and (ctx.platform_id in platforms or ctx.platform_name in platforms):
        return True
    return False


def _is_excluded(scope_cond: dict, ctx: RuleContext) -> bool:
    """命中任一 exclude 列表即排除。"""
    if _as_list(scope_cond.get("exclude_groups")) and ctx.group_id in _as_list(
        scope_cond.get("exclude_groups")
    ):
        return True
    if _as_list(scope_cond.get("exclude_users")) and ctx.sender_id in _as_list(
        scope_cond.get("exclude_users")
    ):
        return True
    return False


def _eval_scope(scope_cond: dict, ctx: RuleContext) -> ConditionResult:
    """scope 条件：exclude 命中直接 False；全 include 空=不限制；否则需任一 include 命中。"""
    if _is_excluded(scope_cond, ctx):
        return ConditionResult(
            scope_cond,
            False,
            f"scope ✗ 命中排除：group={ctx.group_id!r} sender={ctx.sender_id!r}",
        )

    include_keys = ("groups", "users", "sessions", "platforms")
    includes_empty = not any(_as_list(scope_cond.get(k)) for k in include_keys)
    if includes_empty:
        return ConditionResult(scope_cond, True, "scope ✓ include 全部为空（不限制）")

    hit = _matches_included(scope_cond, ctx)
    mark = "✓" if hit else "✗"
    return ConditionResult(
        scope_cond,
        hit,
        f"scope {mark} group={ctx.group_id!r} sender={ctx.sender_id!r} umo={ctx.umo!r} "
        f"platform={ctx.platform_id!r}/{ctx.platform_name!r}",
    )


def _eval_keyword(cond: dict, ctx: RuleContext) -> ConditionResult:
    """keyword：关键字匹配 message_str（大小写不敏感），mode=contains|prefix。"""
    message = ctx.message_str.lower()
    keywords = [str(k).lower() for k in _as_list(cond.get("keywords"))]
    mode = cond.get("mode", "contains")
    if not keywords:
        return ConditionResult(cond, False, "keyword ✗ 无关键词")
    if mode == "prefix":
        matched = any(message.startswith(k) for k in keywords)
    else:  # contains
        matched = any(k in message for k in keywords)
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond, matched, f"keyword {mark} 模式={mode} 消息={ctx.message_str!r}"
    )


def _eval_command(cond: dict, ctx: RuleContext) -> ConditionResult:
    """command：message_str 去空白后单词边界前缀匹配任一命令。

    命中规则：``message == cmd``，或 ``message.startswith(cmd)`` 且余下部分
    ``rest[0] == " "``（即命令后接空白边界）。``/tool``、``/tool run`` 均命中
    ``/tool``；``/tools``、``/toolx`` 不命中（避免前缀误伤）。
    """
    message = ctx.message_str.strip().lower()
    commands = [
        str(c).strip().lower() for c in _as_list(cond.get("commands")) if str(c).strip()
    ]
    if not commands:
        return ConditionResult(cond, False, "command ✗ 无命令")
    matched = False
    for cmd in commands:
        if message == cmd:
            matched = True
            break
        if message.startswith(cmd):
            rest = message[len(cmd) :]
            if rest[:1] == " ":
                matched = True
                break
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond, matched, f"command {mark} 命令={commands} 消息={ctx.message_str!r}"
    )


def _eval_at_bot(cond: dict, ctx: RuleContext) -> ConditionResult:
    """at_bot：是否 @ 机器人。"""
    expect = bool(cond.get("value"))
    matched = ctx.at_bot == expect
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond, matched, f"at_bot {mark} 期望={expect} 实际={ctx.at_bot}"
    )


def _eval_message_type(cond: dict, ctx: RuleContext) -> ConditionResult:
    """message_type：group / private。"""
    expect = str(cond.get("value", "")).lower()
    matched = ctx.message_type.lower() == expect
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond, matched, f"message_type {mark} 期望={expect} 实际={ctx.message_type}"
    )


def _eval_round_gte(cond: dict, ctx: RuleContext) -> ConditionResult:
    """round_gte：会话轮数 >= value。"""
    threshold = cond.get("value", 0)
    matched = ctx.round >= threshold
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond, matched, f"round_gte {mark} 期望≥{threshold} 实际={ctx.round}"
    )


def _eval_context_length_gte(cond: dict, ctx: RuleContext) -> ConditionResult:
    """context_length_gte：上下文长度 >= value。"""
    threshold = cond.get("value", 0)
    matched = ctx.context_length >= threshold
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond,
        matched,
        f"context_length_gte {mark} 期望≥{threshold} 实际={ctx.context_length}",
    )


def _eval_lifecycle_event(cond: dict, ctx: RuleContext) -> ConditionResult:
    """lifecycle_event：lifecycle 事件（new / reset）匹配。"""
    expect = str(cond.get("event", "")).lower()
    matched = ctx.lifecycle_event == expect
    mark = "✓" if matched else "✗"
    return ConditionResult(
        cond,
        matched,
        f"lifecycle_event {mark} 期望={expect} 实际={ctx.lifecycle_event!r}",
    )


def _eval_condition(cond: dict, ctx: RuleContext) -> ConditionResult:
    """按条件类型分派求值；未知类型返回 matched=False 并标注 unknown。"""
    ctype = cond.get("type")
    if ctype == "time_range":
        return _eval_time_range(cond, ctx)
    if ctype == "date_weekday":
        return _eval_date_weekday(cond, ctx)
    if ctype == "scope":
        return _eval_scope(cond, ctx)
    if ctype == "keyword":
        return _eval_keyword(cond, ctx)
    if ctype == "command":
        return _eval_command(cond, ctx)
    if ctype == "at_bot":
        return _eval_at_bot(cond, ctx)
    if ctype == "message_type":
        return _eval_message_type(cond, ctx)
    if ctype == "round_gte":
        return _eval_round_gte(cond, ctx)
    if ctype == "context_length_gte":
        return _eval_context_length_gte(cond, ctx)
    if ctype == "lifecycle_event":
        return _eval_lifecycle_event(cond, ctx)
    return ConditionResult(cond, False, f"unknown 条件类型 {ctype!r}（未实现）")


def _eval_when(when: dict, ctx: RuleContext) -> list[ConditionResult]:
    """评估 when.conditions 并按 op（and/or）聚合，返回每个条件的求值结果。"""
    conditions = _as_list(when.get("conditions"))
    results: list[ConditionResult] = []
    # 空条件列表 → 无 to 列表子件，直接视为命中
    if not conditions:
        results.append(
            ConditionResult(
                {"type": "_empty"}, True, "when.conditions 为空（视为命中）"
            )
        )
        return results

    op = when.get("op", "and")
    for c in conditions:
        if isinstance(c, dict):
            results.append(_eval_condition(c, ctx))
        else:
            results.append(ConditionResult(c, False, f"条件格式非法: {c!r}"))

    if op == "or":
        # or：任一命中即整体命中
        matched = any(r.matched for r in results)
        overall = ConditionResult(
            {"type": "_or"}, matched, f"OR 聚合 {'✓' if matched else '✗'}"
        )
        results.append(overall)
    else:
        # and：全部命中（空条件列表已在上方短路）
        matched = all(r.matched for r in results)
        overall = ConditionResult(
            {"type": "_and"}, matched, f"AND 聚合 {'✓' if matched else '✗'}"
        )
        results.append(overall)

    return results


class RuleEngine:
    """规则引擎：规则 CRUD（经 ConfigStore 持久化）+ 条件评估。"""

    ActionNames = ACTIONS

    def __init__(self, store: ConfigStore):
        """Args:
        store: 配置存储（模块 A 的 ``ConfigStore``，或测试用替身）。
        """
        self._store = store

    # ---- CRUD ----

    def list_(self, only_enabled: bool = False) -> list[dict]:
        """返回全部规则，按 priority 降序（同优先级保持创建顺序）。"""
        rules = self._store.get_rules()
        ordered = sorted(rules, key=lambda r: r.get("priority", 0), reverse=True)
        if only_enabled:
            ordered = [r for r in ordered if r.get("enabled", True)]
        return [normalize_rule(r) for r in ordered]

    def create_rule(self, raw: dict) -> dict:
        """新建规则：normalize 后追加并保存，返回新规则。"""
        rule = normalize_rule(raw)
        rules = self._store.get_rules()
        rules.append(rule)
        self._store.update("rules", rules)
        return copy.deepcopy(rule)

    def update_rule(self, rule_id: str, raw: dict) -> dict | None:
        """按 id 合并更新规则；不存在返回 None。"""
        rules = self._store.get_rules()
        idx = next((i for i, r in enumerate(rules) if r.get("id") == rule_id), None)
        if idx is None:
            return None
        merged = copy.deepcopy(rules[idx])
        merged.update(copy.deepcopy(raw))
        rules[idx] = normalize_rule(merged)
        self._store.update("rules", rules)
        return copy.deepcopy(rules[idx])

    def delete(self, rule_id: str) -> bool:
        """删除规则；存在并删除成功返回 True。"""
        rules = self._store.get_rules()
        kept = [r for r in rules if r.get("id") != rule_id]
        if len(kept) == len(rules):
            return False
        self._store.update("rules", kept)
        return True

    def duplicate(self, rule_id: str) -> dict | None:
        """复制规则（深拷贝、新 id、name 加 \"(copy)\"）；源不存在返回 None。"""
        rules = self._store.get_rules()
        src = next((r for r in rules if r.get("id") == rule_id), None)
        if src is None:
            return None
        new_rule = copy.deepcopy(src)
        new_rule["id"] = "r_" + uuid.uuid4().hex[:8]
        new_rule["name"] = str(src.get("name", "")) + "(copy)"
        rules.append(new_rule)
        self._store.update("rules", rules)
        return copy.deepcopy(new_rule)

    # ---- 求值 ----

    def evaluate(self, ctx: RuleContext) -> dict:
        """评估全部启用的规则，返回结构化结果。

        Returns:
            ``{"matched_rule": dict|None, "rejected": [{"rule":..., "results":[...]}...],
            "results": [...]}``。
            - 命中规则取 priority 最高者（降序评估，首条命中即停）。
            - 未命中任何规则时，``results`` 为最高优先级被评估规则的条件结果（或空列表）。
            - scope 未通过或被排除的规则进 ``rejected`` 且不评估 when。
        """
        rules = self.list_(only_enabled=False)
        enabled = [r for r in rules if r.get("enabled", True)]

        matched_rule: dict | None = None
        matched_results: list[dict] = []
        rejected: list[dict] = []
        first_evaluated_results: list[dict] = []

        for rule in enabled:
            scope_fail = self._scope_fail_result(rule, ctx)
            if scope_fail is not None:
                rejected.append(
                    {"rule": copy.deepcopy(rule), "results": [scope_fail.to_dict()]}
                )
                if not first_evaluated_results:
                    first_evaluated_results = [scope_fail.to_dict()]
                continue

            when_results = _eval_when(rule.get("when", {}), ctx)
            # 聚合是否命中（看最后的 _and/_or 聚合项）
            overall_matched = when_results[-1].matched if when_results else False
            result_dicts = [r.to_dict() for r in when_results]

            if not first_evaluated_results:
                first_evaluated_results = list(result_dicts)

            if overall_matched:
                matched_rule = copy.deepcopy(rule)
                matched_results = result_dicts
                break

            # when 未命中：记录为 rejected（带条件结果）
            rejected.append({"rule": copy.deepcopy(rule), "results": result_dicts})

        if matched_rule is not None:
            results = matched_results
        else:
            results = first_evaluated_results

        return {
            "matched_rule": matched_rule,
            "rejected": rejected,
            "results": results,
        }

    def _scope_fail_result(
        self, rule: dict, ctx: RuleContext
    ) -> ConditionResult | None:
        """校验规则级 scope；未通过或被排除返回一条失败 ConditionResult，否则 None。"""
        scope = rule.get("scope", {})
        groups = _as_list(scope.get("groups"))
        users = _as_list(scope.get("users"))
        sessions = _as_list(scope.get("sessions"))
        platforms = _as_list(scope.get("platforms"))
        exclude_groups = _as_list(scope.get("exclude_groups"))
        exclude_users = _as_list(scope.get("exclude_users"))

        # exclude 优先：命中任一直接拒绝
        if (exclude_groups and ctx.group_id in exclude_groups) or (
            exclude_users and ctx.sender_id in exclude_users
        ):
            return ConditionResult(
                {"type": "scope", "rule_level": True},
                False,
                f"rule scope ✗ 命中排除：group={ctx.group_id!r} sender={ctx.sender_id!r}",
            )

        includes_empty = not (groups or users or sessions or platforms)
        if includes_empty:
            return None  # 全空=不限制

        hit = _matches_included(
            {
                "groups": groups,
                "users": users,
                "sessions": sessions,
                "platforms": platforms,
            },
            ctx,
        )
        if not hit:
            return ConditionResult(
                {"type": "scope", "rule_level": True},
                False,
                f"rule scope ✗ 未命中 include：group={ctx.group_id!r} sender={ctx.sender_id!r} "
                f"umo={ctx.umo!r} platform={ctx.platform_id!r}/{ctx.platform_name!r}",
            )
        return None

    def simulate(self, ctx: RuleContext) -> dict:
        """evaluate 的语义包装（便于 Simulator 调用语义区分）。"""
        return self.evaluate(ctx)
