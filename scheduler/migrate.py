"""migrate —— 配置 schema 迁移（模块 T3，纯逻辑，不依赖 astrbot）。

负责把旧版（v1）配置就地升级到 v2：
- schema_version 1 → 2；
- 顶层补 ``temporal_rules``（默认 []）；
- ``settings`` 补 ``agent_confirm``（默认 True，高危写操作须先 preview 再 apply）；
- ``groups / rules / lifecycles / overrides`` 及 settings 自定义键**原样保留**（0.1.x 完全兼容）。

设计要点：
- ``migrate_v1_to_v2`` 是**幂等**的：对已是 v2 的配置再次调用不产生任何改动，
  返回的变更说明列表为空；入参永不被修改（深拷贝后处理）。
- ``upgrade_config`` 按 ``schema_version`` 分派（缺失按 v1 处理），是 load/import 的通用入口。
- 不 import astrbot，无第三方依赖，仅标准库（copy）。
"""

from __future__ import annotations

import copy


def migrate_v1_to_v2(config: dict) -> tuple[dict, list[str]]:
    """把 v1 配置就地升级为 v2，返回 ``(新 dict, 变更说明列表)``。

    幂等：重复调用结果一致且不再产生变更说明。补全 ``temporal_rules`` 与
    ``settings.agent_confirm``；``groups / rules / lifecycles / overrides`` 及
    settings 自定义键原样保留。入参不被修改（返回深拷贝）。

    Args:
        config: 待迁移的配置 dict。

    Returns:
        ``(新配置 dict, 变更说明列表 list[str])``；无变化时列表为空。
    """
    cfg = copy.deepcopy(config) if isinstance(config, dict) else {}
    notes: list[str] = []

    # schema_version → 2
    cur_ver = cfg.get("schema_version")
    if cur_ver != 2:
        cfg["schema_version"] = 2
        notes.append(f"schema_version 由 {cur_ver!r} 升级为 {2}")

    # 顶层补 temporal_rules
    if "temporal_rules" not in cfg:
        cfg["temporal_rules"] = []
        notes.append("顶层新增 temporal_rules=[]（v2 时间调度规则）")

    # settings 保证为 dict，并补 agent_confirm
    if not isinstance(cfg.get("settings"), dict):
        if "settings" in cfg:
            notes.append("settings 结构非法，重置为空 dict")
        cfg["settings"] = {}
    if "agent_confirm" not in cfg["settings"]:
        cfg["settings"]["agent_confirm"] = True
        notes.append("settings 新增 agent_confirm=True（高危操作须先预览再应用）")

    return cfg, notes


def upgrade_config(config: dict) -> tuple[dict, list[str]]:
    """按 ``schema_version`` 分派配置升级。

    - ``schema_version == 1`` 或缺省 → ``migrate_v1_to_v2``；
    - ``schema_version == 2`` → 原样返回（不变更）。
    - 结构非法（非 dict）→ 按空 v1 配置处理。

    Args:
        config: 待升级的配置 dict。

    Returns:
        ``(升级后的配置 dict, 变更说明列表 list[str])``。
    """
    if not isinstance(config, dict):
        return migrate_v1_to_v2({})
    if config.get("schema_version") == 2:
        return copy.deepcopy(config), []
    return migrate_v1_to_v2(config)
