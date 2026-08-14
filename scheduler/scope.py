"""scope —— 限定群组（scope）共享工具（模块 S，纯逻辑，不依赖 astrbot）。

v1.0.1 起，时间规则（temporal）与生命周期策略（lifecycle）共用同一套「限定群组」语义：

- 结构：``scope = {"groups": [...], "users": [...], "sessions": [...]}``，三键全空 = 全局规则；
- 匹配：``meta.group_id ∈ groups`` 或 ``meta.sender_id ∈ users`` 或 ``meta.umo ∈ sessions``
  任一命中即「限定命中」；
- 二段式优先级：限定命中的规则**先于**全局（scope 全空）规则生效；同段内按 priority
  降序。即「不填 = 全局规则按优先级使用；填了且命中 = 优先于一切全局规则」。
"""

from __future__ import annotations

# scope 作用域键（顺序固定，供 JSON 输出保持稳定结构）。
SCOPE_KEYS = ("groups", "users", "sessions")


def _as_list(value) -> list:
    """把值规整为列表（None → []，标量 → [标量]）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def default_scope() -> dict:
    """scope 默认结构（全部列表为空 = 不限制 = 全局规则）。"""
    return {"groups": [], "users": [], "sessions": []}


def normalize_scope(raw) -> dict:
    """把任意输入规范化为 scope 结构（三个键都规整为列表，非法输入回默认）。"""
    scope = default_scope()
    if isinstance(raw, dict):
        for key in SCOPE_KEYS:
            if key in raw:
                scope[key] = _as_list(raw.get(key))
    return scope


def scope_is_empty(scope) -> bool:
    """scope 三键全空 → 全局规则，返回 True。"""
    if not isinstance(scope, dict):
        return True
    return not any(_as_list(scope.get(key)) for key in SCOPE_KEYS)


def scope_match(scope, meta: dict | None) -> bool:
    """scope 过滤：meta 命中任一列表即生效，全空 = 不限制（全局）。

    Args:
        scope: 规范化 scope dict。
        meta: 上下文 dict（``group_id`` / ``sender_id`` / ``umo`` 等）。

    Returns:
        是否在作用域内（scope 全空恒 True）。
    """
    meta = meta or {}
    groups = _as_list((scope or {}).get("groups"))
    users = _as_list((scope or {}).get("users"))
    sessions = _as_list((scope or {}).get("sessions"))
    if not (groups or users or sessions):
        return True
    if groups and meta.get("group_id") in groups:
        return True
    if users and meta.get("sender_id") in users:
        return True
    if sessions and meta.get("umo") in sessions:
        return True
    return False


def parse_scope_text(groups_text: str, users_text: str, sessions_text: str) -> dict:
    """把「逗号分隔」文本解析为 scope 结构（trim / 去空项，供 Web API 使用）。"""
    def _split(text) -> list:
        return [p.strip() for p in str(text or "").split(",") if p.strip()]

    return {
        "groups": _split(groups_text),
        "users": _split(users_text),
        "sessions": _split(sessions_text),
    }
