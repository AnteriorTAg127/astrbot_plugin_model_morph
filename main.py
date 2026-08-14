"""Model Morph 插件入口（模块 F）。

本文件是插件的 Star 入口：组装各调度模块（state / groups / rules / lifecycles /
logs / engine），注册事件钩子与 ``/scheduler`` 指令组，并在插件的生命周期内启动
状态持久化后台任务。

职责划分：
- 纯调度逻辑（规则评估、模型组选择、生命周期推演、状态存储）都在 ``scheduler`` 包内；
- main.py 只做「胶水」：把 AstrBot 运行时（Context / AstrMessageEvent / Provider）对接
  到 engine 上，并兜底所有钩子异常，保证插件异常不阻断消息流。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 注意：AstrBot 以包形式加载插件（data.plugins.astrbot_plugin_model_morph），
# 插件目录不在 sys.path 上，因此包内导入必须使用相对导入。
from .scheduler import compat
from .scheduler.agent import ModelMorphConfigAgentTool
from .scheduler.agent_tools import ToolContext
from .scheduler.audit import AuditLog
from .scheduler.chat_store import ChatStore
from .scheduler.engine import SchedulerEngine
from .scheduler.groups import ModelGroupManager
from .scheduler.lifecycle import LifecycleEngine
from .scheduler.lifecycle import should_trigger_compression as _should_compression
from .scheduler.logs import SchedulerLog
from .scheduler.persistence import ConfigStore
from .scheduler.rules import RuleEngine
from .scheduler.state import SessionStateStore
from .scheduler.temporal import TemporalEngine

# 状态持久化间隔（秒）。
_STATE_PERSIST_INTERVAL = 300
# 事件钩子中兜底捕获的 extra 键前缀（脚本外部设置 provider 覆盖时使用）。
_MM_MODEL_OVERRIDE = "_mm_model_override"
# 检测 new/reset 的 extra 标志（AstrBot 内置命令设置）。
_CLEAN_GROUP_CONTEXT_SESSION = "_clean_group_context_session"

try:  # pragma: no cover - AstrBot 配置类型，离线不影响
    from astrbot.api import AstrBotConfig
except Exception:  # noqa: BLE001
    AstrBotConfig = dict  # type: ignore[assignment, misc]


@register(
    "astrbot_plugin_model_morph",
    "ModelMorph",
    "模型自动调度器：按时间/会话/规则自动切换 LLM Provider",
    "1.0.0",
)
class ModelMorph(Star):
    """模型自动调度器：按会话（UMO）隔离地自动切换聊天 Provider。

    Provider 切换时机在 ``@filter.on_waiting_llm_request`` 钩子内（Provider 解析之前），
    通过 ``provider_manager.set_provider(umo=...)`` 实现，与 ``/provider`` 同款机制。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        """组装各调度组件并注册 Web API。

        Args:
            context: 插件上下文（Provider / 会话 / 配置访问）。
            config: 插件 _conf_schema 配置（enabled / debug 两项，由 AstrBot 原生
                配置面板持有，经 adapter 实时读取；不合并进 store，避免「只合并一次」
                导致面板后续修改失效）。
        """
        super().__init__(context)
        self._context = context
        # 数据目录：data/plugin_data/astrbot_plugin_model_morph
        data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_model_morph"
        self.data_dir = data_dir

        # ---- 组件组装 ----
        self.store = ConfigStore(data_dir)
        self.states = SessionStateStore()
        self.groups = ModelGroupManager(self.store)
        self.rules = RuleEngine(self.store)
        self.lifecycles = LifecycleEngine(self.store)
        settings = self.store.get_settings()

        # enabled / debug 由 AstrBot 原生配置面板（_conf_schema.json）持有，
        # 经 adapter 实时读取（config 为 AstrBotConfig 活对象）；store 中的同名键
        # 为历史遗留，不再参与调度判断。
        retention = int(settings.get("log_retention", 500) or 500)
        self.slog = SchedulerLog(retention=retention)
        self.adapter = compat.RuntimeAdapter(
            context, self.store.get_settings(), native_config=config
        )

        # ---- v0.1.5 时间强制调度层 + 审计 + Agent 配置层 ----
        # temporal：时间段模型替换 / 整组切换（运行时叠加层）。
        self.temporal = TemporalEngine(self.store, adapter=self.adapter)
        # 审计日志：记录管理员 / Agent 的每一次配置变更。
        self.audit = AuditLog(
            retention=int(settings.get("audit_retention", 500) or 500)
        )
        # Agent 结构化工具上下文（聊天 SubAgent / Web 助手共用手动来源）。
        self.tool_ctx = ToolContext(
            store=self.store,
            groups=self.groups,
            rules=self.rules,
            temporal=self.temporal,
            audit=self.audit,
            lifecycles=self.lifecycles,
            provider_infos=compat.get_provider_info_list(context),
            tz=self.adapter.get_timezone(),
            settings=self.store.get_settings(),
            data_dir=data_dir,
            source="manual",
            operator="admin",
        )
        # AI 配置助手会话存储（v0.1.8）：持久化 Web 助手对话历史，支持列表/切换/删除。
        self.chats = ChatStore(data_dir)

        self.engine = SchedulerEngine(
            store=self.store,
            states=self.states,
            groups=self.groups,
            rules=self.rules,
            lifecycles=self.lifecycles,
            slog=self.slog,
            adapter=self.adapter,
            logger=logger,
            temporal=self.temporal,
        )

        # 状态持久化后台任务（initialize 中启动）。
        self._persist_task: asyncio.Task | None = None
        self._terminated = False

        # 上下文压缩检测基线：umo -> 上一轮实际 LLM 输入 token 数。
        # 仅供 on_llm_response 的 usage.input 骤降启发式使用；这是运行时内存状态，
        # 不做持久化（重启后前几轮仅重建基线，round<=3 不算压缩，见 on_llm_response）。
        self._last_input_tokens: dict[str, int] = {}

        # 注册 Web API。
        from .web import api as web_api

        web_api.register_all(self)

        # 注册配置 SubAgent 入口工具（仅管理员可用）；注册失败不阻断插件加载。
        try:
            self.context.add_llm_tools(ModelMorphConfigAgentTool(tc=self.tool_ctx))
        except Exception:  # noqa: BLE001 - 配置子代理注册失败不影响插件加载
            logger.warning("ModelMorph.__init__: 注册配置 SubAgent 失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 初始化 / 终止
    # ------------------------------------------------------------------ #

    async def initialize(self):
        """插件加载完成后的异步初始化。

        流程：加载配置与日志 → 恢复状态快照（若 state_persist）→ 注册 Provider 变更
        回调 → 启动状态持久化后台任务。
        """
        try:
            settings = self.store.get_settings()
            state_persist = bool(settings.get("state_persist", True))

            # 恢复日志持久化条目。
            try:
                logs_path = self.data_dir / "logs.json"
                if logs_path.exists():
                    raw = json.loads(logs_path.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        self.slog.load_entries(raw)
            except Exception:  # noqa: BLE001 - 日志恢复失败不阻断
                logger.warning("ModelMorph.initialize: 恢复日志失败", exc_info=True)

            # 恢复审计日志（audit.json）。
            try:
                audit_path = self.data_dir / "audit.json"
                if audit_path.exists():
                    raw = json.loads(audit_path.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        self.audit.load_entries(raw)
            except Exception:  # noqa: BLE001 - 审计恢复失败不阻断
                logger.warning("ModelMorph.initialize: 恢复审计日志失败", exc_info=True)

            # 恢复状态快照（若启用持久化）。
            if state_persist:
                try:
                    snap_path = self.data_dir / "state.json"
                    if snap_path.exists():
                        snap = json.loads(snap_path.read_text(encoding="utf-8"))
                        if isinstance(snap, dict):
                            self.states.restore(snap)
                except Exception:  # noqa: BLE001 - 状态恢复失败按新会话处理
                    logger.warning(
                        "ModelMorph.initialize: 恢复状态快照失败", exc_info=True
                    )

            # 监听外部 /provider 切换，记录到调度日志。
            try:
                self._context.provider_manager.register_provider_change_hook(
                    self._on_provider_changed
                )
            except Exception:  # noqa: BLE001 - 回调注册失败不阻断
                logger.warning(
                    "ModelMorph.initialize: 注册 provider 变更回调失败", exc_info=True
                )

            # 启动状态持久化后台任务。
            if state_persist:
                self._persist_task = asyncio.create_task(self._persist_loop())
        except Exception:  # noqa: BLE001 - 整体兜底
            logger.exception("ModelMorph.initialize 异常")

    async def terminate(self):
        """插件卸载 / 停用：取消后台任务并做最后一次持久化。"""
        self._terminated = True
        if self._persist_task is not None:
            self._persist_task.cancel()
            try:
                await self._persist_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 取消后 await 竞态兜底
                pass
            self._persist_task = None
        # 写状态快照与日志。
        try:
            self._persist_now()
        except Exception:  # noqa: BLE001 - 终止时的持久化失败不抛出
            logger.warning("ModelMorph.terminate: 最终持久化失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # 后台持久化
    # ------------------------------------------------------------------ #

    async def _persist_loop(self):
        """每 300 秒写一次 state.json；异常吞掉并记日志，保证循环不退出。"""
        while not self._terminated:
            try:
                await asyncio.sleep(_STATE_PERSIST_INTERVAL)
                self._persist_now()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 - 持久化异常不得退出循环
                logger.exception("ModelMorph._persist_loop: 状态持久化异常")

    def _persist_now(self):
        """把运行时状态与日志原子写入插件数据目录（state.json / logs.json / audit.json）。"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            snap = self.states.snapshot()
            (self.data_dir / "state.json").write_text(
                json.dumps(snap, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (self.data_dir / "logs.json").write_text(
                json.dumps(self.slog.to_list(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 审计日志单独持久化（原子写 tmp → os.replace）。
            try:
                self.audit.save_to(self.data_dir / "audit.json")
            except Exception:  # noqa: BLE001 - 审计写入失败不阻断整体持久化
                logger.warning(
                    "ModelMorph._persist_now: 保存审计日志失败", exc_info=True
                )
        except Exception:  # noqa: BLE001 - 持久化失败不得抛出
            logger.warning("ModelMorph._persist_now: 持久化失败", exc_info=True)

    # ------------------------------------------------------------------ #
    # Provider 变更回调 / 事件钩子
    # ------------------------------------------------------------------ #

    def _on_provider_changed(self, provider_id: str, provider_type: str, umo: str):
        """ProviderManager 变更回调（含外部 /provider 切换），记录到调度日志。"""
        try:
            self.slog.add(
                {
                    "time": datetime.now().isoformat(),
                    "umo": umo,
                    "type": "provider_change",
                    "new": provider_id,
                    "reason": "外部 Provider 变更（provider_manager 回调）",
                }
            )
        except Exception:  # noqa: BLE001 - 日志失败忽略
            logger.warning("ModelMorph._on_provider_changed: 记录失败", exc_info=True)

    @filter.on_waiting_llm_request()
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """主调度点：在 Provider 解析之前计算本会话应使用的 Provider 并切换。

        ``engine.resolve`` 内部会完成 set_provider（adapter 注入），决策结果产出于
        DecisionTrace，不在此钩子内 yield 发消息。
        """
        try:
            # 若上一条消息触发了 new/reset（extra 标志），在此消费：置为待重置。
            if event.get_extra(_CLEAN_GROUP_CONTEXT_SESSION):
                await self.states.mark_pending_reset(event.unified_msg_origin)
            meta = compat.get_session_meta(event)
            trace = await self.engine.resolve(meta)

            # v0.1.10：webchat（web 前端）每条消息都会携带非空 selected_provider
            # （前端模型下拉的选择存 localStorage，随消息发出），AstrBot 的
            # _select_provider 会优先采用该 extra，导致 umo 会话存储（/provider 指令
            # 与插件 set_provider 写入的 provider_perf_chat_completion）永远不被读取，
            # 表现为「插件自动切换与 /provider 手动切换都无效」。这里把最终决策（或
            # 会话偏好）回灌到 selected_provider，让本消息真正使用期望的 Provider。
            decided = getattr(trace, "final_provider_id", None)
            if decided:
                # 插件有决策：覆盖前端下拉选择（引擎已校验 Provider 可用性）。
                event.set_extra("selected_provider", decided)
            elif not trace.skipped_reason:
                # 插件无决策且调度器正常运行：把 /provider 或早前插件切换写入的
                # 会话偏好回灌，使 webchat 下手动 /provider 切换同样生效。
                pref = await compat.get_session_provider_preference(
                    self._context, event.unified_msg_origin
                )
                if pref and pref in self.adapter.provider_ids():
                    event.set_extra("selected_provider", pref)

            # 若引擎决策携带 model_override（P1，暂不在引擎内设置），存到 event extra
            # 供 on_llm_request 读取；默认无。
            if decided is None:
                event.set_extra(_MM_MODEL_OVERRIDE, None)
        except Exception:  # noqa: BLE001 - 调度异常不得阻断消息
            logger.exception("ModelMorph.on_waiting_llm_request 异常")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """LLM 请求前：若事件带 model_override，则改写本次请求的模型名。

        引擎默认不产生 model override（同一 Provider 实例内换模型列为 P1 可选能力），
        此逻辑仅在外部显式设置了 ``_mm_model_override`` 时生效。
        """
        try:
            override = event.get_extra(_MM_MODEL_OVERRIDE)
            if override:
                req.model = override
        except Exception:  # noqa: BLE001 - 失败不影响请求
            logger.exception("ModelMorph.on_llm_request 异常")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """LLM 响应后：记录 error 日志，并做「上下文压缩」启发式检测。

        压缩检测说明（启发式）：AstrBot 不暴露上下文压缩事件，无法直接得知本次请求的
        上下文被压缩。插件以 ``resp.usage.input``（本轮实际输入 token 数）相对上一轮
        骤降近似判定：``should_trigger_compression(prev, cur)`` 命中即当作发生压缩，
        进而按当前生命周期绑定的 ``calibration_event == "context_compression"`` 校准
        配置，为会话写入 ``calibration_*`` 校准状态。全部逻辑包 try/except，失败仅
        告警、绝不阻断消息流。
        """
        try:
            if getattr(resp, "role", "") == "err":
                detail = str(getattr(resp, "completion_text", ""))[:200]
                self.slog.add(
                    {
                        "time": datetime.now().isoformat(),
                        "umo": event.unified_msg_origin,
                        "type": "error",
                        "level": "error",
                        "detail": detail,
                        "reason": "LLM 响应返回错误",
                    }
                )
            await self._detect_context_compression(event, resp)
        except Exception:  # noqa: BLE001 - 日志失败忽略
            logger.exception("ModelMorph.on_llm_response 异常")

    async def _detect_context_compression(self, event: AstrMessageEvent, resp):
        """采样 usage.input 并据骤降启发式触发上下文压缩校准（可被测试调用）。"""
        try:
            usage = getattr(resp, "usage", None)
            cur = int(usage.input) if usage is not None else None
            if cur is None:
                return
            umo = event.unified_msg_origin
            prev = self._last_input_tokens.get(umo)
            state = await self.states.get(umo)
            # 新会话前几轮（round<=3）只更新基线，不算压缩：reset 后上一轮 token 与新
            # 会话首轮无可比性，骤降阈值对新会话首轮会误报。
            if prev is not None and state.round > 3 and _should_compression(prev, cur):
                lc = (
                    self.lifecycles.get(state.lifecycle_id)
                    if state.lifecycle_id
                    else None
                )
                cfg = self.lifecycles.calibration_config(lc) if lc else None
                if cfg and cfg.get("event") == "context_compression":
                    await self.states.update(
                        umo,
                        calibration_rounds_left=int(cfg["rounds"]),
                        calibration_group_id=cfg["group_id"],
                        calibration_reason="context_compression",
                    )
                    self.slog.add(
                        {
                            "time": datetime.now().isoformat(),
                            "umo": umo,
                            "type": "calibration",
                            "detail": (
                                f"上下文压缩触发校准（{cfg['rounds']} 轮，组 "
                                f"{cfg['group_id']}），输入 token {prev}→{cur}"
                            ),
                            "reason": "context_compression",
                        }
                    )
            # 更新基线供下一轮比较（实例内存状态，terminate 不持久化）。
            self._last_input_tokens[umo] = cur
        except Exception:  # noqa: BLE001 - 压缩检测失败仅告警，不阻断
            logger.warning("ModelMorph: 上下文压缩检测失败", exc_info=True)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """装饰结果前：检测 new/reset 标志，标记会话待重置。"""
        try:
            if event.get_extra(_CLEAN_GROUP_CONTEXT_SESSION):
                await self.states.mark_pending_reset(event.unified_msg_origin)
        except Exception:  # noqa: BLE001 - 兜底
            logger.exception("ModelMorph.on_decorating_result 异常")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """消息发送后：new/reset 标志的兜底检测。"""
        try:
            if event.get_extra(_CLEAN_GROUP_CONTEXT_SESSION):
                await self.states.mark_pending_reset(event.unified_msg_origin)
        except Exception:  # noqa: BLE001 - 兜底
            logger.exception("ModelMorph.after_message_sent 异常")

    # ------------------------------------------------------------------ #
    # 指令组 /scheduler
    # ------------------------------------------------------------------ #

    @filter.command_group("scheduler")
    async def scheduler(self, event: AstrMessageEvent):
        """模型调度器指令组：查看 / 锁定会话调度状态。"""
        # 根指令（仅输入 /scheduler）时框架会提示子指令树；这里作为兜底展示状态摘要。
        yield event.plain_result(await self._status_text(event))

    @scheduler.command("status")
    async def scheduler_status(self, event: AstrMessageEvent):
        """查看当前会话的调度状态摘要。"""
        yield event.plain_result(await self._status_text(event))

    @scheduler.command("model")
    async def scheduler_model(self, event: AstrMessageEvent):
        """查看当前会话使用的 Provider 与模型名。"""
        yield event.plain_result(await self._model_text(event))

    @scheduler.command("lock")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def scheduler_lock(self, event: AstrMessageEvent, group: str):
        """锁定本会话到指定模型组（可用组 id 或组名）。管理员指令。

        Args:
            group: 模型组 id 或名称。
        """
        umo = event.unified_msg_origin
        target = self._find_group(group)
        if target is None:
            yield event.plain_result(f"未找到模型组: {group}")
            return
        await self.states.update(umo, lock_group_id=target["id"], lock_provider_id=None)
        self.slog.add(
            {
                "time": datetime.now().isoformat(),
                "umo": umo,
                "type": "lock",
                "group": target["id"],
                "reason": f"指令锁定到组 {target['name'] or target['id']}",
            }
        )
        yield event.plain_result(
            f"已锁定本会话到模型组: {target['name'] or target['id']}"
        )

    @scheduler.command("unlock")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def scheduler_unlock(self, event: AstrMessageEvent):
        """解除本会话锁定，恢复自动调度。管理员指令。"""
        umo = event.unified_msg_origin
        await self.states.update(umo, lock_group_id=None, lock_provider_id=None)
        self.slog.add(
            {
                "time": datetime.now().isoformat(),
                "umo": umo,
                "type": "unlock",
                "reason": "指令解除会话锁定",
            }
        )
        yield event.plain_result("已解锁本会话，恢复自动调度。")

    @scheduler.command("reset")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def scheduler_reset(self, event: AstrMessageEvent):
        """重置本会话的调度状态（轮数归零 / 阶段回 NEW），不动 Conversation。管理员指令。"""
        umo = event.unified_msg_origin
        await self.states.reset(umo, event.unified_msg_origin)
        self.slog.add(
            {
                "time": datetime.now().isoformat(),
                "umo": umo,
                "type": "reset",
                "reason": "指令手动重置调度状态",
            }
        )
        yield event.plain_result("已重置本会话调度状态（round=0）。")

    # ------------------------------------------------------------------ #
    # 状态文本辅助
    # ------------------------------------------------------------------ #

    def _find_group(self, key: str) -> dict | None:
        """按组 id 或组名查找模型组；找不到返回 None。"""
        key = (key or "").strip()
        groups = self.groups.list_()
        for g in groups:
            if g.get("id") == key or g.get("name") == key:
                return g
        return None

    async def _status_text(self, event: AstrMessageEvent) -> str:
        """组装 /scheduler status 的文本回复。"""
        umo = event.unified_msg_origin
        try:
            state = await self.states.get(umo)
        except Exception:  # noqa: BLE001 - 状态读取失败兜底
            state = None
        cur_provider = self.adapter.current_provider_id(umo)
        lines = ["【Model Morph 调度状态】"]
        lines.append(f"- 会话(UMO): {umo}")
        if state:
            lines.append(
                f"- 当前 Provider: {cur_provider or state.current_provider_id or '(使用 AstrBot 默认)'}"
            )
            lines.append(f"- 当前模型组: {state.current_group_id or '(未设置)'}")
            gname = ""
            if state.current_group_id:
                g = self.groups.get(state.current_group_id)
                gname = g.get("name") if g else ""
            if gname:
                lines[-1] += f" ({gname})"
            lines.append(f"- 轮数(round): {state.round}")
            lines.append(f"- 生命周期阶段: {state.stage}")
            lines.append(f"- 生命周期策略: {state.lifecycle_id or '(未绑定)'}")
            lock = state.lock_group_id or state.lock_provider_id
            lines.append(f"- 锁定: {'是' if lock else '否'}")
            lines.append(f"- 最近命中规则: {state.last_rule_id or '(无)'}")
        else:
            lines.append("- 当前 Provider: (读取状态失败)")
        return "\n".join(lines)

    async def _model_text(self, event: AstrMessageEvent) -> str:
        """组装 /scheduler model 的文本回复（Provider + 模型名）。"""
        umo = event.unified_msg_origin
        cur_provider = self.adapter.current_provider_id(umo)
        model_name = ""
        try:
            if cur_provider:
                for item in compat.get_provider_info_list(self._context):
                    if item.get("id") == cur_provider:
                        model_name = item.get("model", "")
                        break
        except Exception:  # noqa: BLE001 - 模型名读取失败留空
            logger.exception("ModelMorph._model_text 读取 Provider 信息失败")
        if cur_provider:
            return f"当前 Provider: {cur_provider}\n当前模型: {model_name or '(未知)'}"
        return "当前 Provider: (使用 AstrBot 默认)\n当前模型: (未知)"
