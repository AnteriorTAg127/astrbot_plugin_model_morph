"""compat —— AstrBot 运行时兼容层（模块 A）。

本模块是唯一接触 astrbot 运行时的纯基础设施模块之一（另一个是 main.py）。
它把 AstrBot 的 Provider / 会话 / 时间获取等能力封装成引擎（SchedulerEngine）可直接
使用的稳定接口：引擎通过注入的 ``RuntimeAdapter`` 访问运行时，从而保持纯逻辑可离线测试。

所有对外函数均为「安全函数」：内部 try/except 兜底，任何异常都返回安全默认值，不向外抛出。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.core.provider.entities import ProviderType

logger = logging.getLogger("astrbot_plugin_model_morph")

# 时区解析失败 / "auto" 且系统时区缺失时的回退时区
_FALLBACK_TZ = "UTC"


def get_provider_info_list(context) -> list[dict]:
    """遍历 AstrBot 已配置的聊天 Provider，返回精简信息列表。

    Args:
        context: 插件 Context（需有 ``get_all_providers()`` 方法）。

    Returns:
        形如 ``[{"id", "model", "type", "enabled"}]`` 的列表；单条读取异常会跳过并告警。
    """
    result: list[dict] = []
    try:
        providers = context.get_all_providers()
    except Exception:  # noqa: BLE001 - 兼容层兜底
        logger.warning(
            "compat.get_provider_info_list: get_all_providers 读取失败", exc_info=True
        )
        return result
    for prov in providers:
        try:
            meta = prov.meta()
            result.append(
                {
                    "id": meta.id or prov.provider_config.get("id", ""),
                    "model": prov.get_model()
                    if hasattr(prov, "get_model")
                    else (meta.model or ""),
                    "type": getattr(meta, "type", "")
                    or prov.provider_config.get("type", ""),
                    "enabled": bool(prov.provider_config.get("enable", True)),
                }
            )
        except Exception:  # noqa: BLE001 - 单条跳过并告警
            logger.warning(
                "compat.get_provider_info_list: 忽略读取异常的 Provider %r",
                prov,
                exc_info=True,
            )
    return result


def is_local_agent_runner(context) -> bool:
    """判断当前是否使用本地 agent runner（local）而非第三方（dify/coze 等）执行。

    Args:
        context: 插件 Context（需有 ``get_config()`` 方法）。

    Returns:
        ``True`` 表示本地 runner，可进行本地 Provider 调度；读取配置异常时默认返回 True。
    """
    try:
        cfg = context.get_config()
        return (
            cfg.get("provider_settings", {}).get("agent_runner_type", "local")
            == "local"
        )
    except Exception:  # noqa: BLE001 - 异常兜底默认 local
        logger.warning(
            "compat.is_local_agent_runner: 读取配置失败，默认视为 local", exc_info=True
        )
        return True


def _parse_zone(name: str) -> ZoneInfo:
    """解析 IANA 时区名，非法值回退 UTC 并告警。"""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 非法时区名兜底
        logger.warning("compat: 无法解析时区名 %r，回退 %r", name, _FALLBACK_TZ)
        return ZoneInfo(_FALLBACK_TZ)


def resolve_timezone(context, settings: dict) -> ZoneInfo:
    """决定调度使用的时区。

    ``settings["timezone"] == "auto"`` 时跟随 AstrBot 全局配置时区（可能为 None → UTC）；
    否则解析显式 IANA 时区名。

    Args:
        context: 插件 Context（需有 ``get_config()``）。
        settings: 插件设置 dict（含 ``timezone``）。

    Returns:
        ``ZoneInfo`` 实例，永不抛出。
    """
    tz_setting = (settings or {}).get("timezone", "auto")
    if tz_setting == "auto":
        try:
            val = context.get_config().get("timezone")
        except Exception:  # noqa: BLE001 - 兜底
            logger.warning(
                "compat.resolve_timezone: 读取全局时区失败，使用 UTC", exc_info=True
            )
            val = None
        if not val:
            return ZoneInfo(_FALLBACK_TZ)
        try:
            return ZoneInfo(val)
        except Exception:  # noqa: BLE001 - 非法全局时区兜底
            logger.warning("compat.resolve_timezone: 全局时区 %r 非法，使用 UTC", val)
            return ZoneInfo(_FALLBACK_TZ)
    return _parse_zone(tz_setting)


async def get_current_conversation_id(context, umo: str) -> str | None:
    """获取指定会话（umo）当前使用的 conversation id。

    Args:
        context: 插件 Context（需有 ``conversation_manager``）。
        umo: 会话唯一标识。

    Returns:
        当前 conversation id，读取异常返回 None。
    """
    try:
        cm = context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        return cid
    except Exception:  # noqa: BLE001 - 兜底
        logger.warning(
            "compat.get_current_conversation_id: 获取会话 id 失败", exc_info=True
        )
        return None


def get_session_meta(event) -> dict:
    """从消息事件提取调度器需要的会话元数据（统一结构，供 engine / web 复用）。

    Args:
        event: AstrMessageEvent 实例。

    Returns:
        包含 ``umo`` / ``platform_id`` / ``platform_name`` / ``group_id`` / ``sender_id`` /
        ``is_group`` / ``message_type`` / ``message_str`` / ``at_bot`` 的 dict；任何字段异常兜底为空值。
    """
    umo = getattr(event, "unified_msg_origin", "") or ""
    try:
        platform_id = event.get_platform_id() or ""
        platform_name = event.get_platform_name() or ""
    except Exception:  # noqa: BLE001
        platform_id = ""
        platform_name = ""
    try:
        group_id = event.get_group_id() or ""
    except Exception:  # noqa: BLE001
        group_id = ""
    try:
        sender_id = event.get_sender_id() or ""
    except Exception:  # noqa: BLE001
        sender_id = ""
    message_str = getattr(event, "message_str", "") or ""
    at_bot = False
    try:
        if hasattr(event, "is_at_or_wake_command"):
            at_bot = bool(event.is_at_or_wake_command())
    except Exception:  # noqa: BLE001 - @ 判断失败视为未 @
        at_bot = False
    is_group = bool(group_id)
    return {
        "umo": umo,
        "platform_id": platform_id,
        "platform_name": platform_name,
        "group_id": group_id,
        "sender_id": sender_id,
        "is_group": is_group,
        "message_type": "group" if is_group else "private",
        "message_str": message_str,
        "at_bot": at_bot,
    }


async def set_session_provider(context, provider_id: str, umo: str) -> None:
    """将会话（umo）的聊天 Provider 切换为 ``provider_id``（按会话隔离持久化）。

    Args:
        context: 插件 Context（需有 ``provider_manager``）。
        provider_id: 目标 Provider id。
        umo: 会话唯一标识。
    """
    try:
        await context.provider_manager.set_provider(
            provider_id, ProviderType.CHAT_COMPLETION, umo=umo
        )
    except Exception:  # noqa: BLE001 - 调度失败不得阻断消息
        logger.error(
            "compat.set_session_provider: 切换会话 %s 到 Provider %s 失败",
            umo,
            provider_id,
            exc_info=True,
        )
        # 不向外抛出，由上层（engine）依据返回值决定记录 error 日志
        raise


def get_current_provider_id(context, umo: str) -> str | None:
    """返回会话（umo）当前实际使用的聊天 Provider id。

    Args:
        context: 插件 Context（需有 ``get_using_provider()``）。
        umo: 会话唯一标识。

    Returns:
        Provider id；未设置 / 异常 / 无 Provider 时返回 None。
    """
    try:
        prov = context.get_using_provider(umo)
        if prov is None:
            return None
        return prov.meta().id
    except Exception:  # noqa: BLE001 - 兜底
        logger.warning(
            "compat.get_current_provider_id: 读取当前 Provider 失败", exc_info=True
        )
        return None


async def get_session_provider_preference(context, umo: str) -> str | None:
    """读取会话级聊天 Provider 偏好（``provider_perf_chat_completion``）。

    该偏好由 ``/provider`` 指令与插件的 ``set_provider`` 写入（umo 作用域的
    shared_preferences 存储）。webchat（web 前端）场景下，前端每条消息携带的
    ``selected_provider`` extra 会覆盖该存储，因此 main.py 在
    ``on_waiting_llm_request`` 中把它回灌到事件 extra，保证手动 /provider 切换生效。

    Args:
        context: 插件 Context（当前实现不使用，保留参数对齐兼容层风格）。
        umo: 会话唯一标识。

    Returns:
        会话级 Provider id；无偏好 / 读取异常返回 None（不抛出）。
    """
    try:
        from astrbot.core import sp

        value = await sp.session_get(umo, "provider_perf_chat_completion", None)
        if isinstance(value, str) and value:
            return value
        return None
    except Exception:  # noqa: BLE001 - 兜底
        logger.warning(
            "compat.get_session_provider_preference: 读取会话 Provider 偏好失败",
            exc_info=True,
        )
        return None


class RuntimeAdapter:
    """调度引擎的运行时适配器（依赖注入：引擎不 import astrbot，可离线测试）。

    由 main.py 组装：``RuntimeAdapter(context, store.get_settings(), native_config=config)``
    注入 SchedulerEngine。``enabled`` / ``debug`` 两个开关由 AstrBot 原生配置面板
    （_conf_schema.json）持有，经 ``native_config``（AstrBotConfig 实时对象）读取，
    每次 resolve 都取最新值——用户在面板修改后立即生效，无需重载插件。
    """

    def __init__(self, context, settings: dict, native_config=None):
        self._context = context
        self._settings = dict(settings or {})
        self._native_config = native_config if isinstance(native_config, dict) else {}

    def is_enabled(self) -> bool:
        """插件总开关（原生配置面板 enabled，实时读取）。"""
        return bool(self._native_config.get("enabled", True))

    def is_debug(self) -> bool:
        """调试模式（原生配置面板 debug，实时读取）。"""
        return bool(self._native_config.get("debug", False))

    def set_settings(self, settings: dict) -> None:
        """同步最新的插件设置（settings 变更后由 main.py 调用）。"""
        self._settings = dict(settings or {})

    def provider_ids(self) -> set[str]:
        """可用聊天 Provider id 集合（enabled=True 过滤后）。"""
        ids: set[str] = set()
        for item in get_provider_info_list(self._context):
            if item.get("enabled"):
                ids.add(item["id"])
        return ids

    def is_local(self) -> bool:
        """判断是否本地 agent runner（可能是第三方，不支持本地调度）。"""
        return is_local_agent_runner(self._context)

    def get_timezone(self) -> ZoneInfo:
        """当前调度时区（按 settings）。"""
        return resolve_timezone(self._context, self._settings)

    async def get_conversation_id(self, umo: str) -> str | None:
        """获取会话当前 conversation id。"""
        return await get_current_conversation_id(self._context, umo)

    async def set_provider(self, provider_id: str, umo: str) -> None:
        """切换会话 Provider（引擎调度结果）。"""
        await set_session_provider(self._context, provider_id, umo)

    def current_provider_id(self, umo: str) -> str | None:
        """会话当前实际使用的 Provider id。"""
        return get_current_provider_id(self._context, umo)

    def provider_model_name(self, provider_id: str) -> str:
        """查询指定 Provider 实例的默认模型名（供引擎名义模型名解析 C7）。

        通过对 ``get_provider_info_list`` 逐条比对 id 取 ``model``；查不到 / 异常返回 ""。
        该方法为可选能力：引擎用 ``getattr`` 兜底，缺省按查不到处理，不阻塞调度。
        """
        try:
            for item in get_provider_info_list(self._context):
                if item.get("id") == provider_id:
                    return str(item.get("model") or "")
        except Exception:  # noqa: BLE001 - 查询失败按查不到处理
            logger.warning(
                "compat.provider_model_name: 查询 Provider %r 模型名失败", provider_id
            )
        return ""

    def now(self) -> datetime:
        """当前时间（调度时区）。"""
        return datetime.now(self.get_timezone())
