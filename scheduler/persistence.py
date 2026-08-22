"""persistence —— 配置持久化层（模块 A，纯逻辑，不依赖 astrbot）。

负责把插件的调度配置（草稿 settings / groups / rules / lifecycles / overrides /
temporal_rules）以 JSON 形式原子读写到 ``data/plugin_data/astrbot_plugin_model_morph/config.json``。

设计要点：
- ``DEFAULT_CONFIG`` 是配置默认值的单一事实来源（缺字段时由此补齐）。
- 读取损坏文件时先备份为 ``config.json.bak`` 再用默认配置，保证插件不崩溃。
- 写入走「写 tmp → os.replace」原子替换，避免写一半中断导致文件损坏。
- ``SCHEMA_VERSION = 2``：v1 配置在 ``load()`` / ``import_all()`` 时经
  ``migrate_v1_to_v2`` 自动迁移（补 temporal_rules、agent_confirm），无需人工介入。
- ``revision()`` 在每次 ``save()`` 成功后自增，供 temporal 引擎用作缓存失效键。
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

from .migrate import ensure_entity_ids, migrate_v1_to_v2

logger = logging.getLogger("astrbot_plugin_model_morph")

# 配置 schema 版本；load() 见到 v1 会就地迁移，写入/导入后恒为 2。
SCHEMA_VERSION = 2

# 配置默认值（单一事实来源）。groups/rules/lifecycles 为列表，overrides 为 dict。
DEFAULT_CONFIG: dict = {
    "schema_version": SCHEMA_VERSION,
    "settings": {
        "enabled": True,
        "debug": False,
        "timezone": "auto",
        "base_group": "",
        "log_retention": 500,
        "state_persist": True,
        "agent_confirm": True,  # v2：高危写操作须先 preview 再 apply（0.1.5 Agent 层）
        "audit_retention": 500,  # v2：审计日志内存环形缓冲保留条数
        "default_lifecycle": "",  # v0.1.6：全局默认生命周期 id（空=不启用）
        "agent_provider_id": "",  # v0.1.6：配置助手使用的 Provider id（空=跟随默认聊天 Provider）
    },
    "groups": [],
    "rules": [],
    "lifecycles": [],
    "overrides": {},
    "temporal_rules": [],  # v2：时间段模型强制替换/整组切换规则
}

# 顶层允许的 section（update 方法的合法键）。
SECTIONS = (
    "settings",
    "groups",
    "rules",
    "lifecycles",
    "overrides",
    "temporal_rules",
)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：``override`` 为 dict 时递归合并键，否则整体覆盖；返回新 dict。

    用于把读取到的用户配置与默认配置合并，补齐缺失字段。
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _basic_valid(config: object) -> bool:
    """import_all 前的基本结构校验：为 dict 且含 settings 顶层键。"""
    return isinstance(config, dict) and isinstance(config.get("settings"), dict)


class ConfigStore:
    """配置文件的 JSON 读写与校验封装。"""

    def __init__(self, data_dir: Path):
        """初始化数据目录（不存在则创建）。

        Args:
            data_dir: 插件持久化数据目录，即 ``data/plugin_data/astrbot_plugin_model_morph``。
        """
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self._data_dir / "config.json"
        self._cache: dict | None = None
        self._revision = 0  # 配置版本号：每次 save() 成功 +1（load 不改变）

    def config_path(self) -> Path:
        """配置文件路径（``data_dir/config.json``）。"""
        return self._config_path

    # ---- 读写核心 ----

    def load(self) -> dict:
        """读取配置并与 DEFAULT_CONFIG 深度合并（缺失字段补齐）。

        文件不存在 → 返回默认配置副本；文件损坏（JSON 解析失败 / 结构非法）→
        备份为 ``config.json.bak`` 后用默认配置并告警。
        磁盘配置 ``schema_version == 1`` → **自动迁移**到 v2：在内存中经
        ``migrate_v1_to_v2`` 转换后**立即保存**（无需用户操作），迁移报告写入 ``logger.info``。
        """
        if not self._config_path.exists():
            self._cache = copy.deepcopy(DEFAULT_CONFIG)
            return self._cache
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            if not _basic_valid(raw):
                raise ValueError("config.json 顶层结构非法")
            # 磁盘 v1 → 先迁移再合并保存（自动迁移）。
            if raw.get("schema_version") == 1:
                migrated, notes = migrate_v1_to_v2(raw)
                config = _deep_merge(DEFAULT_CONFIG, migrated)
                config["schema_version"] = SCHEMA_VERSION
                ensure_entity_ids(config)  # 顺带补齐空 id 实体（与正常路径同口径）
                self.save(config)
                logger.info(
                    "persistence.load: 检测到旧版配置(schema v1)，已自动迁移至 v2。变更: %s",
                    "；".join(notes) or "无",
                )
                self._cache = config
                return config
            config = _deep_merge(DEFAULT_CONFIG, raw)
            # 固定 schema_version，防止被覆盖成非法值
            config["schema_version"] = SCHEMA_VERSION
            # 空 id 实体自愈（v1.0.4+）：补齐并落盘一次，之后 id 稳定可删可改。
            n_fixed = ensure_entity_ids(config)
            if n_fixed:
                self.save(config)
                logger.info(
                    "persistence.load: 检测到 %d 个空 id 实体，已自动补齐",
                    n_fixed,
                )
            self._cache = config
            return config
        except Exception as exc:  # noqa: BLE001 - 损坏文件兜底
            logger.warning(
                "persistence.load: 读取配置失败 %r，备份为 .bak 并使用默认", exc
            )
            try:
                backup = self._config_path.with_suffix(
                    self._config_path.suffix + ".bak"
                )
                os.replace(self._config_path, backup)
            except Exception:  # noqa: BLE001 - 备份失败忽略，不阻断
                logger.warning("persistence.load: 备份损坏配置失败", exc_info=True)
            self._cache = copy.deepcopy(DEFAULT_CONFIG)
            return self._cache

    def save(self, config: dict) -> None:
        """原子写入完整配置（写 tmp → os.replace），成功则 ``revision()`` +1。

        Args:
            config: 待持久化的完整配置 dict。

        Raises:
            RuntimeError: 写入失败时抛出，调用方（写操作 API）应转为错误响应。
        """
        payload = copy.deepcopy(config)
        payload["schema_version"] = SCHEMA_VERSION
        tmp = self._config_path.with_suffix(self._config_path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._config_path)
            self._cache = payload
            self._revision += 1
        except Exception as exc:  # noqa: BLE001 - 转为 RuntimeError
            logger.error("persistence.save: 写入配置失败", exc_info=True)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"保存配置失败: {exc}") from exc

    def revision(self) -> int:
        """返回当前配置版本号（初始 0，每次 ``save()`` 成功 +1；load 不改变）。

        用于 temporal 引擎的缓存失效键：配置发生写操作后 revision 变化，缓存自动失效。
        """
        return self._revision

    # ---- 读取访问器 ----

    def get_settings(self) -> dict:
        """返回当前 settings（深度合并默认后的副本）。"""
        return copy.deepcopy(self.load().get("settings", {}))

    def get_groups(self) -> list[dict]:
        """返回当前 groups。"""
        return copy.deepcopy(self.load().get("groups", []))

    def get_rules(self) -> list[dict]:
        """返回当前 rules。"""
        return copy.deepcopy(self.load().get("rules", []))

    def get_lifecycles(self) -> list[dict]:
        """返回当前 lifecycles。"""
        return copy.deepcopy(self.load().get("lifecycles", []))

    def get_overrides(self) -> dict:
        """返回当前 overrides（``umo -> 覆盖配置`` 映射）。"""
        return copy.deepcopy(self.load().get("overrides", {}))

    def get_temporal_rules(self) -> list[dict]:
        """返回当前 temporal 时间调度规则列表（v2，深拷贝）。"""
        return copy.deepcopy(self.load().get("temporal_rules", []))

    def update(self, section: str, value) -> dict:
        """替换指定 section 的值并保存，返回完整新配置。

        Args:
            section: ``settings`` / ``groups`` / ``rules`` / ``lifecycles`` /
                ``overrides`` / ``temporal_rules``。
            value: section 的新值。

        Returns:
            保存后的完整配置 dict。

        Raises:
            RuntimeError: section 非法或保存失败。
        """
        if section not in SECTIONS:
            raise RuntimeError(f"非法配置段: {section}")
        config = self.load()
        config[section] = copy.deepcopy(value)
        self.save(config)
        return config

    def export_all(self) -> dict:
        """导出完整配置（深拷贝，供下载 / 备份用）。"""
        return copy.deepcopy(self.load())

    def import_all(self, config: dict) -> dict:
        """校验后整体替换配置并保存，返回新配置。

        接受 ``schema_version`` ∈ {1, 2}：v1 先经 ``migrate_v1_to_v2`` 转换再保存，
        其他版本报原样错误。保存后的配置 ``schema_version`` 恒为 2。

        Args:
            config: 待导入的完整配置（须含合法 ``schema_version`` 与合法 settings）。

        Returns:
            保存后的完整配置。

        Raises:
            RuntimeError: schema 版本或结构不合法，或保存失败（此时不改动原配置）。
        """
        if not _basic_valid(config):
            raise RuntimeError("导入配置结构非法：缺少 settings 段")
        version = config.get("schema_version")
        if version not in (1, SCHEMA_VERSION):
            raise RuntimeError(
                f"导入配置 schema 版本不兼容: 期望 1 或 {SCHEMA_VERSION}，实际 {version}"
            )
        if version == 1:
            # v1 → 先迁移（补 temporal_rules / agent_confirm 等）再合并保存。
            migrated, _notes = migrate_v1_to_v2(config)
            config = migrated
        merged = _deep_merge(DEFAULT_CONFIG, config)
        merged["schema_version"] = SCHEMA_VERSION
        ensure_entity_ids(merged)  # 导入配置里空 id 实体一并补齐
        self.save(merged)
        return merged
