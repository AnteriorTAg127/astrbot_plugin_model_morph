"""temporal —— 时间强制调度层（模块 T1，纯逻辑，不依赖 astrbot）。

负责按时间段把模型组内某 Provider 替换为另一 Provider（model_override）或
整组切换（group_switch），运行时生效、时间结束自动恢复（不改基础配置）。

设计要点：
- 优先级常量：emergency(1000) / manual(500) / scheduled(200) / group(100) / default(0)。
  同一 (group, source_provider) 同时命中多条时，priority 高者生效（确定性）；
  同优先级按规则创建顺序（列表顺序）先者生效。
- 时间判断语义与 rules.py 的 ``_eval_time_range`` 一致：HH:MM、``end<start`` 跨午夜、
  weekdays 0=周一；``schedule.timezone`` 非空时先把时间换算到该时区再比较。
- 持久化依赖 T3 的 ``persistence v2`` 契约：``store.get_temporal_rules()`` /
  ``store.update("temporal_rules", ...)`` / ``store.revision()``。为兼容尚未升级的旧 store
  （无上述方法），读取用 ``getattr`` 兜底读 ``load()`` 的 ``temporal_rules`` 段、
  写入用 ``update`` 失败后回退 ``load/save``，写操作成功后显式 ``invalidate()`` 缓存。
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("astrbot_plugin_model_morph")

# 优先级常量（契约规定值）。
PRIORITY_EMERGENCY = 1000
PRIORITY_MANUAL = 500
PRIORITY_SCHEDULED = 200
PRIORITY_GROUP = 100
PRIORITY_DEFAULT = 0

# 规则 kind / 调度类型 合法取值。
KINDS = ("model_override", "group_switch")
SCHEDULE_TYPES = ("always", "daily", "weekly", "date")

# 一周七天的分钟总数。
_DAY_MINUTES = 24 * 60

# ``HH:MM`` 正则（与 rules.py ``_HHMM_RE`` 一致）。
_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

# scope 作用域键。
_SCOPE_KEYS = ("groups", "users", "sessions")


def _as_list(value) -> list:
    """把值规整为列表（None → []，标量 → [标量]）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _default_scope() -> dict:
    """scope 默认结构（全部列表为空 = 不限制）。"""
    return {"groups": [], "users": [], "sessions": []}


def _default_schedule() -> dict:
    """schedule 默认结构。"""
    return {
        "type": "daily",
        "start": "",
        "end": "",
        "weekdays": [],
        "date": "",
        "timezone": "",
    }


def _default_metadata() -> dict:
    """metadata 默认结构。"""
    return {"created_by": "", "created_at": "", "source": ""}


def normalize_temporal_rule(raw: dict) -> dict:
    """规范化一条 temporal 规则，补齐默认字段并返回深拷贝（不修改入参）。

    默认：id ``t_`` + uuid hex[:8]；name ""；enabled True；kind 非法时回退
    ``"model_override"``；group_id/source_provider/target_provider/target_group 均 ""；
    scope/schedule/metadata 补齐结构；priority 转 int（非法回 200）。

    Args:
        raw: 原始规则 dict。

    Returns:
        规范化后的规则深拷贝。
    """
    raw = raw or {}
    rule = copy.deepcopy(raw)

    # id / 基础字段
    rule.setdefault("id", "t_" + uuid.uuid4().hex[:8])
    rule.setdefault("name", "")
    rule.setdefault("enabled", True)
    kind = rule.get("kind")
    if kind not in KINDS:
        logger.warning("temporal: 规则 kind %r 非法，回退 model_override", kind)
        kind = "model_override"
    rule["kind"] = kind
    rule.setdefault("group_id", "")
    rule.setdefault("source_provider", "")
    rule.setdefault("target_provider", "")
    rule.setdefault("target_group", "")

    # scope：合并默认，三个键都规整为列表
    scope = copy.deepcopy(_default_scope())
    raw_scope = rule.get("scope")
    if isinstance(raw_scope, dict):
        for key in _SCOPE_KEYS:
            if key in raw_scope:
                scope[key] = _as_list(raw_scope.get(key))
    rule["scope"] = scope

    # schedule：补齐默认结构
    schedule = copy.deepcopy(_default_schedule())
    raw_schedule = rule.get("schedule")
    if isinstance(raw_schedule, dict):
        for key in _default_schedule():
            if key in raw_schedule:
                schedule[key] = copy.deepcopy(raw_schedule[key])
    schedule["weekdays"] = _as_list(schedule.get("weekdays"))
    for field in ("start", "end", "date", "timezone"):
        if not isinstance(schedule.get(field), str):
            schedule[field] = ""
    s_type = schedule.get("type")
    if s_type not in SCHEDULE_TYPES:
        logger.warning("temporal: 调度类型 %r 非法，回退 daily", s_type)
        schedule["type"] = "daily"
    rule["schedule"] = schedule

    # priority：数值字段非法回默认 200
    try:
        rule["priority"] = int(rule.get("priority", PRIORITY_SCHEDULED))
    except (TypeError, ValueError):
        rule["priority"] = PRIORITY_SCHEDULED

    # metadata
    metadata = copy.deepcopy(_default_metadata())
    raw_metadata = rule.get("metadata")
    if isinstance(raw_metadata, dict):
        metadata.update(copy.deepcopy(raw_metadata))
    rule["metadata"] = metadata

    return rule


def parse_hhmm(value) -> tuple[int, int] | None:
    """解析 ``"HH:MM"`` → ``(hour, minute)``；非法返回 ``None``。

    要求 hour<=23、minute<=59。
    """
    if not isinstance(value, str):
        return None
    match = _HHMM_RE.match(value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _minute_of_day(dt: datetime) -> int:
    """返回一天内的分钟数 ``hour*60+minute``。"""
    return dt.hour * 60 + dt.minute


def _resolve_rule_tz(schedule: dict, default_tz: ZoneInfo) -> tuple[ZoneInfo, str]:
    """解析规则时区：``schedule.timezone`` 非空且合法时用之，否则回退 ``default_tz``。

    返回 ``(tz, 标注字符串)``，非法时区名在标注中体现。
    """
    tz_name = str(schedule.get("timezone", "") or "").strip()
    if not tz_name:
        return default_tz, ""
    try:
        return ZoneInfo(tz_name), tz_name
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "temporal: 规则时区 %r 非法，回退默认时区 %s",
            tz_name,
            default_tz,
        )
        return default_tz, f"时区 {tz_name!r} 非法已回退默认"


def rule_active(rule: dict, dt: datetime, default_tz: ZoneInfo) -> tuple[bool, str]:
    """判断规则在 ``dt``（须带时区）时刻是否生效，返回 ``(是否生效, 人类可读原因)``。

    时间窗语义与 rules.py ``_eval_time_range`` 一致：
    - ``type=always`` → true；
    - ``daily`` → start/end 分钟比较（``end<start`` 跨午夜），weekdays 非空需命中；
    - ``weekly`` → 与 daily 相同但 weekdays 必填（空列表视为不生效，reason 说明）；
    - ``date`` → ``dt.strftime("%Y-%m-%d") == schedule.date`` 且满足 start/end 时间窗；
    - ``schedule.timezone`` 非空时先把 dt 换算到该时区再比较，非法时区名回退
      ``default_tz`` 并在 reason 中标注。

    Args:
        rule: 规范化 temporal 规则。
        dt: 带时区的判断时刻。
        default_tz: 插件调度时区（规则未指定时区时的兜底）。

    Returns:
        ``(active: bool, reason: str)``。
    """
    schedule = rule.get("schedule") or {}
    s_type = schedule.get("type")
    if s_type not in SCHEDULE_TYPES:
        return (False, f"无效调度类型 {s_type!r}")

    # 换算到规则自己的时区（若指定）。
    tz, note = _resolve_rule_tz(schedule, default_tz)
    try:
        local_dt = dt.astimezone(tz)
    except Exception:  # noqa: BLE001 - dt 缺失时区等兜底
        return (False, f"无法换算时区 {tz}")

    cur_min = _minute_of_day(local_dt)
    date_str = local_dt.strftime("%Y-%m-%d")
    weekday = local_dt.weekday()

    if s_type == "always":
        reason = f"type=always 始终生效（{date_str} {local_dt.strftime('%H:%M')}）"
        return (True, f"{reason}{('；' + note) if note else ''}")

    start_hm = parse_hhmm(schedule.get("start"))
    end_hm = parse_hhmm(schedule.get("end"))
    if start_hm is None or end_hm is None:
        return (
            False,
            f"无效起止时间 start={schedule.get('start')!r} end={schedule.get('end')!r}",
        )
    start_min = start_hm[0] * 60 + start_hm[1]
    end_min = end_hm[0] * 60 + end_hm[1]
    if end_min < start_min:  # 跨午夜
        in_window = cur_min >= start_min or cur_min <= end_min
    else:
        in_window = start_min <= cur_min <= end_min

    if s_type == "weekly":
        weekdays = _as_list(schedule.get("weekdays"))
        if not weekdays:
            return (
                False,
                f"type=weekly 但 weekdays 为空列表，视为不生效（{date_str}）"
                f"{('；' + note) if note else ''}",
            )
        wd_hit = weekday in weekdays
        active = wd_hit and in_window
        return (
            active,
            f"weekly 星期{'命中' if wd_hit else '不命中'}(weekday={weekday} ∈ {weekdays}) "
            f"时间窗{'命中' if in_window else '不命中'}（{date_str} "
            f"{local_dt.strftime('%H:%M')}）{('；' + note) if note else ''}",
        )

    if s_type == "date":
        match_date = str(schedule.get("date", "") or "")
        date_hit = date_str == match_date
        active = date_hit and in_window
        return (
            active,
            f"date 期望 {match_date} {'命中' if date_hit else '不命中'}(当前 {date_str}) "
            f"时间窗{'命中' if in_window else '不命中'}（{local_dt.strftime('%H:%M')}）"
            f"{('；' + note) if note else ''}",
        )

    # daily：weekdays 可叠加过滤（为空不过滤）。
    weekdays = _as_list(schedule.get("weekdays"))
    wd_hit = True if not weekdays else weekday in weekdays
    active = wd_hit and in_window
    return (
        active,
        f"daily 星期{'命中' if wd_hit else '不命中'}(weekday={weekday}) "
        f"时间窗{'命中' if in_window else '不命中'}（{start_min // 60:02d}:"
        f"{start_min % 60:02d}-{end_min // 60:02d}:{end_min % 60:02d}，当前 "
        f"{local_dt.strftime('%H:%M')}）{('；' + note) if note else ''}",
    )


def _covered_intervals(schedule: dict) -> list[tuple[int, int]]:
    """把 start/end 分钟区间表达为一天内的覆盖闭区间列表（处理跨午夜）。

    返回形如 ``[(a, b), ...]`` 的闭区间（a<=b，值域 [0, 1440)）。
    """
    start_hm = parse_hhmm(schedule.get("start"))
    end_hm = parse_hhmm(schedule.get("end"))
    if start_hm is None or end_hm is None:
        return []
    start_min = start_hm[0] * 60 + start_hm[1]
    end_min = end_hm[0] * 60 + end_hm[1]
    if end_min < start_min:  # 跨午夜
        return [(start_min, _DAY_MINUTES - 1), (0, end_min)]
    return [(start_min, end_min)] if start_min <= end_min else []


def _weekday_set(schedule: dict) -> set[int] | None:
    """返回调度覆盖的星期集合；``None`` 表示「无星期限制（视作全部 7 天）」。"""
    s_type = schedule.get("type")
    if s_type == "daily":
        days = _as_list(schedule.get("weekdays"))
        return set(days) if days else None
    if s_type == "weekly":
        days = _as_list(schedule.get("weekdays"))
        return set(days) if days else set()
    if s_type == "always":
        return None
    # date：单日，无法表达星期集合，交给调用方保守判定
    return None


def overlaps(a: dict, b: dict) -> bool:
    """粗判两条规则的时间窗是否重叠。

    按「星期集合 × 分钟区间」判断：
    - always 与一切重叠；
    - 两者同为 date 且日期不同 → 不重叠；
    - 两者同为日循环（daily/weekly）→ 星期集合相交且分钟区间相交才重叠；
    - 其余（date 与日循环混用、未知类型）无法精确判定 → 保守返回 True。

    Args:
        a, b: 规范化 temporal 规则。

    Returns:
        是否重叠。
    """
    sa = a.get("schedule") or {}
    sb = b.get("schedule") or {}
    ta, tb = sa.get("type"), sb.get("type")

    if ta == "always" or tb == "always":
        return True

    if ta == "date" and tb == "date":
        if str(sa.get("date", "")) != str(sb.get("date", "")):
            return False
        return _intervals_overlap(_covered_intervals(sa), _covered_intervals(sb))

    if ta in ("daily", "weekly") and tb in ("daily", "weekly"):
        wa, wb = _weekday_set(sa), _weekday_set(sb)
        if wa is not None and wb is not None and not (wa & wb):
            return False
        return _intervals_overlap(_covered_intervals(sa), _covered_intervals(sb))

    # 混用 / 未知类型：保守视为重叠。
    return True


def _intervals_overlap(ia: list[tuple[int, int]], ib: list[tuple[int, int]]) -> bool:
    """两个「覆盖闭区间列表」是否有交集。"""
    if not ia or not ib:
        return False
    for s1, e1 in ia:
        for s2, e2 in ib:
            if min(e1, e2) >= max(s1, s2):
                return True
    return False


def _rule_source(rule: dict) -> str:
    """规则冲突对拍的源：model_override 为 source_provider，group_switch 为 group_id。"""
    if rule.get("kind") == "group_switch":
        return str(rule.get("group_id", "") or "")
    return str(rule.get("source_provider", "") or "")


def _rule_target(rule: dict) -> str:
    """规则的去向：model_override 为 target_provider，group_switch 为 target_group。"""
    if rule.get("kind") == "group_switch":
        return str(rule.get("target_group", "") or "")
    return str(rule.get("target_provider", "") or "")


def find_conflicts(rules: list[dict]) -> list[dict]:
    """检测现有规则两两冲突，返回冲突描述列表。

    判定条件：同 kind、同 group_id、同 source（group_switch 时 source 取 group_id）、
    时间窗重叠且去向（target）不同。每个冲突项形如
    ``{"a": id, "b": id, "group_id", "source_provider", "kind", "note"}``：
    - 相同 priority → ``note = "priority_tie"``；
    - 不同 priority → 低者被遮蔽 → ``note = "shadowed"``（非错误）。

    Args:
        rules: 规范化 temporal 规则列表。

    Returns:
        冲突 dict 列表（升序两两组合，无重复/镜像）。
    """
    conflicts: list[dict] = []
    count = len(rules)
    for i in range(count):
        for j in range(i + 1, count):
            a, b = rules[i], rules[j]
            if a.get("kind") != b.get("kind"):
                continue
            if a.get("group_id") != b.get("group_id"):
                continue
            if _rule_source(a) != _rule_source(b):
                continue
            if not overlaps(a, b):
                continue
            if _rule_target(a) == _rule_target(b):
                continue
            a_id = a.get("id")
            b_id = b.get("id")
            if (a.get("priority", 0) or 0) == (b.get("priority", 0) or 0):
                note = "priority_tie"
            else:
                note = "shadowed"
            conflicts.append(
                {
                    "a": a_id,
                    "b": b_id,
                    "group_id": a.get("group_id", ""),
                    "source_provider": a.get("source_provider", ""),
                    "kind": a.get("kind"),
                    "note": note,
                }
            )
    return conflicts


def _scope_match(rule: dict, meta: dict | None) -> bool:
    """scope 过滤：meta 命中任一列表即生效，全空=不限制。

    Args:
        rule: 规范化规则。
        meta: 上下文 dict（group_id / sender_id / umo 等）。

    Returns:
        是否在作用域内。
    """
    meta = meta or {}
    scope = rule.get("scope") or {}
    groups = _as_list(scope.get("groups"))
    users = _as_list(scope.get("users"))
    sessions = _as_list(scope.get("sessions"))
    if not (groups or users or sessions):
        return True
    if groups and meta.get("group_id") in groups:
        return True
    if users and meta.get("sender_id") in users:
        return True
    if sessions and meta.get("umo") in sessions:
        return True
    return False


class TemporalEngine:
    """时间强制调度引擎：temporal 规则的 CRUD / 校验 / 生效判断。"""

    def __init__(self, store, adapter=None):
        """Args:
        store: 配置存储（T3 ``persistence.v2`` 契约或测试替身）。
        adapter: 可选，提供 ``get_timezone() -> ZoneInfo``；缺省用 UTC 兜底。
        """
        self._store = store
        self._adapter = adapter
        self._cache_key: tuple | None = None
        self._cache_rules: list[dict] | None = None

    # ---- 存储访问（兼容持久化 v1/v2）----

    def _revision(self) -> int:
        """读取存储修订号（兼容旧 store：无 revision 方法视为恒 0）。"""
        rev = getattr(self._store, "revision", None)
        if callable(rev):
            try:
                return int(rev() or 0)
            except Exception:  # noqa: BLE001
                return 0
        return 0

    def _read_rules(self) -> list[dict]:
        """读取全部 temporal 规则（兼容持久化 v1/v2）。"""
        getter = getattr(self._store, "get_temporal_rules", None)
        if callable(getter):
            try:
                return copy.deepcopy(getter())
            except Exception:  # noqa: BLE001 - 兜底走 load
                pass
        try:
            return copy.deepcopy(self._store.load().get("temporal_rules", []))
        except Exception:  # noqa: BLE001 - 兜底返回空
            return []

    def _write_rules(self, rules: list[dict]) -> None:
        """写入全部 temporal 规则（优先 update；旧 store 无该段时回退 load/save）。"""
        try:
            self._store.update("temporal_rules", copy.deepcopy(rules))
            return
        except Exception:  # noqa: BLE001 - 旧 store 不支持 temporal_rules 段
            pass
        config = self._store.load()
        config["temporal_rules"] = copy.deepcopy(rules)
        self._store.save(config)

    def get_timezone(self) -> ZoneInfo:
        """返回插件调度时区（adapter 提供；无 adapter 时用 UTC 兜底）。"""
        if self._adapter is not None:
            try:
                tz = self._adapter.get_timezone()
                if tz is not None:
                    return tz
            except Exception:  # noqa: BLE001
                logger.warning("temporal: 读取 adapter 时区失败", exc_info=True)
        return ZoneInfo("UTC")

    # ---- 缓存 ----

    def invalidate(self) -> None:
        """清空 active_rules 缓存（写操作成功后调用）。"""
        self._cache_key = None
        self._cache_rules = None

    def _cache_key_for(self, now: datetime, tz_name: str) -> tuple:
        """构造缓存键：``(绝对分钟时间戳, tz 名, revision)``。

        时间部分用 ``now.timestamp()`` 的分钟粒度（绝对时刻，与 ``now`` 自带时区无关），
        保证同一墙钟时刻但时区不同（不同绝对时刻）不会误命中同一缓存。
        """
        try:
            minute_ts = int(now.timestamp() // 60)
        except (OSError, OverflowError, ValueError):  # noqa: BLE001 - naive 等兜底
            minute_ts = (now.year, now.month, now.day, now.hour, now.minute)
        return (minute_ts, tz_name, self._revision())

    # ---- CRUD ----

    def list_(self, only_enabled: bool = False) -> list[dict]:
        """返回全部规则，按 priority 降序（同优先级保持存储顺序）。"""
        rules = [normalize_temporal_rule(r) for r in self._read_rules()]
        ordered = sorted(rules, key=lambda r: (r.get("priority", 0) or 0), reverse=True)
        if only_enabled:
            ordered = [r for r in ordered if r.get("enabled", True)]
        return ordered

    def get(self, rule_id: str) -> dict | None:
        """按 id 查规则（深拷贝）；不存在返回 ``None``。"""
        for rule in self._read_rules():
            if rule.get("id") == rule_id:
                return normalize_temporal_rule(rule)
        return None

    def create(self, raw: dict) -> dict:
        """新建规则：normalize → validate（ok 才写）→ 保存 → 失效缓存。

        Args:
            raw: 原始规则 dict。

        Returns:
            新建并保存后的规则。

        Raises:
            ValueError: 校验失败，消息为中文错误汇总。
        """
        rule = normalize_temporal_rule(raw)
        result = self.validate(rule)
        if not result.get("ok"):
            raise ValueError("；".join(result.get("errors", [])) or "规则校验失败")
        rules = self._read_rules()
        rules.append(rule)
        self._write_rules(rules)
        self.invalidate()
        return copy.deepcopy(rule)

    def update_rule(self, rule_id: str, raw: dict) -> dict | None:
        """按 id 合并更新规则；规则不存在返回 ``None``。

        先 normalize 合并值做整体校验，ok 才写库并失效缓存。
        """
        rules = self._read_rules()
        idx = next((i for i, r in enumerate(rules) if r.get("id") == rule_id), None)
        if idx is None:
            return None
        merged = copy.deepcopy(rules[idx])
        merged.update(copy.deepcopy(raw))
        normalized = normalize_temporal_rule(merged)
        result = self.validate(normalized)
        if not result.get("ok"):
            raise ValueError("；".join(result.get("errors", [])) or "规则校验失败")
        rules[idx] = normalized
        self._write_rules(rules)
        self.invalidate()
        return copy.deepcopy(normalized)

    def delete(self, rule_id: str) -> bool:
        """删除规则；存在并删除成功返回 ``True``（删除后失效缓存）。"""
        rules = self._read_rules()
        kept = [r for r in rules if r.get("id") != rule_id]
        if len(kept) == len(rules):
            return False
        self._write_rules(kept)
        self.invalidate()
        return True

    def toggle(self, rule_id: str, enabled: bool | None = None) -> dict | None:
        """切换规则启用状态；``enabled`` 为 None 时取反。规则不存在返回 ``None``。"""
        rules = self._read_rules()
        idx = next((i for i, r in enumerate(rules) if r.get("id") == rule_id), None)
        if idx is None:
            return None
        new_enabled = (
            bool(enabled)
            if enabled is not None
            else not bool(rules[idx].get("enabled", True))
        )
        rules[idx]["enabled"] = new_enabled
        self._write_rules(rules)
        self.invalidate()
        return copy.deepcopy(normalize_temporal_rule(rules[idx]))

    # ---- 校验 ----

    def validate(
        self, raw: dict | None, known_provider_ids=None, known_group_ids=None
    ) -> dict:
        """校验一条（或待写）temporal 规则，返回 ``{"ok", "errors", "warnings"}``。

        校验项：kind 合法；model_override/group_switch 必备字段非空且 source!=target；
        HH:MM 合法；type 合法且字段齐（weekly 需 weekdays、date 需 date 格式 YYYY-MM-DD）；
        timezone 用 ``zoneinfo.ZoneInfo`` 校验（非法→error）；priority 为 int；
        提供 ``known_provider_ids`` 时 source/target 必须存在；提供 ``known_group_ids``
        时 group/target_group 必须存在；自引用（source==target）error；与现有规则构成
        的替换链含环 → error；与现有规则冲突 → warnings（附 note）。

        Args:
            raw: 候选规则（已规范化或原始 dict）；None/空 dict → ok=False。
            known_provider_ids: 可选已知 Provider id 集合（None 表示不校验存在性）。
            known_group_ids: 可选已知组 id 集合（None 表示不校验存在性）。

        Returns:
            校验结果 dict。
        """
        if not raw:
            return {"ok": False, "errors": ["规则为空"], "warnings": []}
        # kind / priority 的合法性需在 normalize 强制回默认**之前**检查（原值）。
        raw_kind = raw.get("kind")
        raw_priority = raw.get("priority")
        rule = normalize_temporal_rule(raw)
        errors: list[str] = []
        warnings: list[str] = []

        kind = rule["kind"]
        if raw_kind not in KINDS:
            errors.append(f"未知 kind: {raw_kind!r}")
        elif kind not in KINDS:
            errors.append(f"未知 kind: {kind!r}")

        # model_override 必备
        if kind == "model_override":
            src = str(rule.get("source_provider", "") or "").strip()
            tgt = str(rule.get("target_provider", "") or "").strip()
            if not src:
                errors.append("model_override 缺少 source_provider")
            if not tgt:
                errors.append("model_override 缺少 target_provider")
            if src and tgt and src == tgt:
                errors.append(
                    "model_override 自引用：source_provider 不能等于 target_provider"
                )
            if known_provider_ids is not None:
                if src and src not in known_provider_ids:
                    errors.append(f"source_provider 不存在: {src!r}")
                if tgt and tgt not in known_provider_ids:
                    errors.append(f"target_provider 不存在: {tgt!r}")
        elif kind == "group_switch":
            gid = str(rule.get("group_id", "") or "").strip()
            tgid = str(rule.get("target_group", "") or "").strip()
            if not gid:
                errors.append("group_switch 缺少 group_id")
            if not tgid:
                errors.append("group_switch 缺少 target_group")
            if gid and tgid and gid == tgid:
                errors.append("group_switch 自引用：group_id 不能等于 target_group")
            if known_group_ids is not None:
                if gid and gid not in known_group_ids:
                    errors.append(f"group_id 不存在: {gid!r}")
                if tgid and tgid not in known_group_ids:
                    errors.append(f"target_group 不存在: {tgid!r}")

        # 时间 / 类型字段
        schedule = rule.get("schedule") or {}
        s_type = schedule.get("type")
        if s_type not in SCHEDULE_TYPES:
            errors.append(f"未知调度类型: {s_type!r}")
        else:
            if s_type in ("daily", "weekly", "date"):
                if parse_hhmm(schedule.get("start")) is None:
                    errors.append(f"无效 start 时间: {schedule.get('start')!r}")
                if parse_hhmm(schedule.get("end")) is None:
                    errors.append(f"无效 end 时间: {schedule.get('end')!r}")
            if s_type == "weekly":
                weekdays = _as_list(schedule.get("weekdays"))
                if not weekdays:
                    errors.append("weekly 类型需提供非空 weekdays")
            if s_type == "date":
                date_val = str(schedule.get("date", "") or "").strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_val):
                    errors.append(f"无效 date（需 YYYY-MM-DD）: {date_val!r}")

        # 时区校验
        tz_name = str(schedule.get("timezone", "") or "").strip()
        if tz_name:
            try:
                ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                errors.append(f"非法时区: {tz_name!r}")

        # priority 为 int（在 normalize 强制转 int 前检查原值，避免暴露 -coerce 掩盖非法）。
        if raw_priority is not None and not isinstance(raw_priority, int):
            errors.append(f"priority 必须为整数: {raw_priority!r}")
        elif not isinstance(rule.get("priority"), int):
            errors.append(f"priority 必须为整数: {rule.get('priority')!r}")

        # 替换环检测（现有规则 + 本规则）
        all_rules = [r for r in self._read_rules() if (r.get("id") != rule["id"])]
        all_rules.append(rule)
        if self._has_chain_cycle(all_rules):
            errors.append("与现有规则构成替换环（A→B→…→A），请检查替换链")

        # 与现有规则冲突 → warnings（附 note）
        others = [r for r in self._read_rules() if (r.get("id") != rule["id"])]
        for conflict in find_conflicts(others + [rule]):
            a_id, b_id = conflict["a"], conflict["b"]
            if rule["id"] in (a_id, b_id):
                note = conflict.get("note", "")
                other_id = b_id if a_id == rule["id"] else a_id
                warnings.append(
                    f"规则 {other_id!r} 与本规则冲突（{note}）："
                    f"kind={conflict['kind']} 同类/同组/同时段但去向不同"
                )

        return {"ok": not errors, "errors": errors, "warnings": warnings}

    def _has_chain_cycle(self, rules: list[dict]) -> bool:
        """按 model_override 的 source→target 建规则图，判断是否存在替换环。

        r1 后接 r2 的条件：r1.target == r2.source 且两规则 group 作用域兼容（group_id
        相同或其一为空）；对规则图做 DFS 探环，返回是否存在有向环。
        """
        rules = [
            r
            for r in rules
            if r.get("kind") == "model_override"
            and r.get("source_provider")
            and r.get("target_provider")
        ]
        n = len(rules)
        if n < 2:
            return False

        def _group_compat(g1: str, g2: str) -> bool:
            return (not g1) or (not g2) or (g1 == g2)

        # 邻接表：规则 i 之后可衔接的规则 j 下标。
        adj: dict[int, list[int]] = {}
        for i in range(n):
            adj.setdefault(i, [])
            for j in range(n):
                if i == j:
                    continue
                if rules[i].get("target_provider") == rules[j].get(
                    "source_provider"
                ) and _group_compat(
                    rules[i].get("group_id", ""), rules[j].get("group_id", "")
                ):
                    adj[i].append(j)

        state = [0] * n  # 0 未访问 / 1 在栈 / 2 已完成

        def _dfs(i: int) -> bool:
            if state[i] == 1:
                return True
            if state[i] == 2:
                return False
            state[i] = 1
            for j in adj.get(i, []):
                if _dfs(j):
                    return True
            state[i] = 2
            return False

        for i in range(n):
            if _dfs(i):
                return True
        return False

    # ---- 生效计算 ----

    def _build_active(self, now: datetime, tz: ZoneInfo) -> list[dict]:
        """计算当前**时间生效**的规则列表（仅按时间窗过滤），按 priority 降序。

        注意：scope 过滤由 resolve_model / resolve_group 分别在调用层完成，不入缓存，
        保证缓存键（分钟粒度时间戳, tz, revision）与 meta 无关、可安全复用。
        """
        active: list[dict] = []
        for raw in self._read_rules():
            if not raw.get("enabled", True):
                continue
            rule = normalize_temporal_rule(raw)
            hit, _reason = rule_active(rule, now, tz)
            if hit:
                active.append(rule)
        active.sort(key=lambda r: (r.get("priority", 0) or 0), reverse=True)
        return active

    def active_rules(
        self, now: datetime, tz: ZoneInfo, meta: dict | None = None
    ) -> list[dict]:
        """返回当前**时间生效**的规则列表（按 priority 降序）。

        含分钟粒度缓存：key =（绝对分钟时间戳, tz 名, store.revision()）。同分钟同
        revision 直接返回缓存的同一对象；写操作使 revision 变化或显式 invalidate 后失效。

        Args:
            now: 带时区的判断时刻。
            tz: 插件调度时区。
            meta: 保留参数（API 兼容）。scope 过滤由 resolve_model / resolve_group 完成，
                本方法仅按时间窗返回生效规则，故缓存与 meta 无关。

        Returns:
            时间生效规则列表。
        """
        if now is None or getattr(now, "tzinfo", None) is None:
            raise ValueError("active_rules 要求 now 携带时区")
        key = self._cache_key_for(now, str(tz))
        if self._cache_key == key and self._cache_rules is not None:
            return self._cache_rules
        result = self._build_active(now, tz)
        self._cache_key = key
        self._cache_rules = result
        return result

    def resolve_model(
        self, group_id: str, provider_id: str, now, tz, meta: dict
    ) -> tuple:
        """对最终 Provider 执行 temporal 模型替换（链式，防环）。

        取 active_rules 中 kind=model_override、组匹配、source==当前 provider、scope 命中
        的规则，按 priority 降序取第一条应用；命中后以前者为起点继续链式替换，visited
        集合防环——环时停止并在原因中标注「检测到替换环」。

        Args:
            group_id: 当前所在组（"" 表示全局/无组）。
            provider_id: 当前 Provider id。
            now: 带时区的判断时刻。
            tz: 插件调度时区。
            meta: 上下文 dict（供 scope 过滤）。

        Returns:
            ``(最终 provider_id, 命中规则|None, 替换链 [orig, t1, ...], 原因)``。
        """
        actives = self.active_rules(now, tz, meta)
        current = provider_id
        chain = [provider_id]
        matched_rule: dict | None = None
        visited: set[str] = set()
        reason_parts: list[str] = []
        guard = 0
        max_steps = len(actives) + 4

        while guard < max_steps:
            guard += 1
            candidate: dict | None = None
            for rule in actives:
                if rule.get("kind") != "model_override":
                    continue
                gid = rule.get("group_id", "")
                if gid and gid != group_id:
                    continue
                if rule.get("source_provider") != current:
                    continue
                if not _scope_match(rule, meta):
                    continue
                candidate = rule
                break
            if candidate is None:
                break
            rid = candidate.get("id")
            if rid in visited:
                reason_parts.append(f"检测到替换环（规则 {rid!r} 已被应用，停止替换）")
                break
            visited.add(rid)
            tgt = candidate.get("target_provider")
            if matched_rule is None:
                matched_rule = candidate
            reason_parts.append(
                f"{candidate.get('source_provider')}→{tgt}（规则 {rid!r}）"
            )
            current = tgt
            chain.append(tgt)

        if not chain[1:] and not matched_rule:
            return (provider_id, None, [], "无命中的 model_override 替换规则")
        reason = "；".join(reason_parts) or "无命中"
        return (current, matched_rule, chain, reason)

    def resolve_group(
        self, group_id: str, now, tz, meta: dict
    ) -> tuple[str, dict | None]:
        """对最终组执行 temporal 整组切换（group_switch）。

        取 active_rules 中 kind=group_switch、组匹配、scope 命中的最高优先级规则，
        命中则返回新组 id 并带上命中规则，否则返回原组 id。

        Args:
            group_id: 当前组 id（"" 表示全局/无组）。
            now: 带时区的判断时刻。
            tz: 插件调度时区。
            meta: 上下文 dict（供 scope 过滤）。

        Returns:
            ``(新 group_id 或原值, 命中规则|None)``。
        """
        actives = self.active_rules(now, tz, meta)
        for rule in actives:
            if rule.get("kind") != "group_switch":
                continue
            gid = rule.get("group_id", "")
            if gid and gid != group_id:
                continue
            if not _scope_match(rule, meta):
                continue
            return (rule.get("target_group") or group_id, rule)
        return (group_id, None)
