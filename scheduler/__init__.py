"""scheduler 包 —— Model Morph 的调度逻辑模块集合。

本包导出插件名常量与各模块的公共类。__init__.py 本身不包含任何业务逻辑，
仅做常量声明与 re-export，避免模块间循环 import。
"""

from .persistence import DEFAULT_CONFIG, ConfigStore

# ---- v0.1.5 纯逻辑 re-export（temporal / audit / presets / migrate / agent_tools 均不依赖 astrbot）----
from .migrate import migrate_v1_to_v2, upgrade_config
from .presets import PRESETS, build_preset_rules
from .audit import AUDIT_SOURCES, AuditLog
from .temporal import (  # noqa: F401
    PRIORITY_DEFAULT,
    PRIORITY_EMERGENCY,
    PRIORITY_GROUP,
    PRIORITY_MANUAL,
    PRIORITY_SCHEDULED,
    TemporalEngine,
)
from .agent_tools import ToolContext

PLUGIN_NAME = "astrbot_plugin_model_morph"

# compat 依赖 astrbot 运行时，仅在可用时 re-export；
# 纯逻辑模块（persistence 等）在离线测试环境中仍可独立 import，不会因此失败。
try:  # pragma: no cover - 覆盖 astrbot 运行时存在的场景
    from .compat import (  # noqa: F401
        RuntimeAdapter,
        get_current_conversation_id,
        get_current_provider_id,
        get_provider_info_list,
        get_session_meta,
        is_local_agent_runner,
        resolve_timezone,
        set_session_provider,
    )

    __all__ = [
        "PLUGIN_NAME",
        "DEFAULT_CONFIG",
        "ConfigStore",
        "PRIORITY_EMERGENCY",
        "PRIORITY_MANUAL",
        "PRIORITY_SCHEDULED",
        "PRIORITY_GROUP",
        "PRIORITY_DEFAULT",
        "TemporalEngine",
        "AuditLog",
        "AUDIT_SOURCES",
        "ToolContext",
        "PRESETS",
        "build_preset_rules",
        "migrate_v1_to_v2",
        "upgrade_config",
        "RuntimeAdapter",
        "get_provider_info_list",
        "is_local_agent_runner",
        "resolve_timezone",
        "get_current_conversation_id",
        "get_session_meta",
        "set_session_provider",
        "get_current_provider_id",
    ]
except ImportError:  # pragma: no cover - 离线测试环境无 astrbot，仅导出纯逻辑部分
    __all__ = [
        "PLUGIN_NAME",
        "DEFAULT_CONFIG",
        "ConfigStore",
        "PRIORITY_EMERGENCY",
        "PRIORITY_MANUAL",
        "PRIORITY_SCHEDULED",
        "PRIORITY_GROUP",
        "PRIORITY_DEFAULT",
        "TemporalEngine",
        "AuditLog",
        "AUDIT_SOURCES",
        "ToolContext",
        "PRESETS",
        "build_preset_rules",
        "migrate_v1_to_v2",
        "upgrade_config",
    ]
