# Changelog

## [1.0.0] - 2026-06 — 首个正式版本

> 说明：本次 1.0.0 为**首个正式发布版本**（此前历史误标的 1.0.0/1.0.1/1.0.2 实为 0.1.2/0.1.3/0.1.4 的错标，见下文「版本号勘误说明」）。0.1.5 ~ 0.1.10 为开发迭代序列，本版将其全部能力冻结为正式版。

### 正式版能力总览（来自 0.1.x 迭代）

- 模型自动调度：按时间 / 会话 / 用户 / 群组 / 消息事件 / 上下文状态 / 轮数自动决定每个会话使用的模型（Provider），切换不清空对话上下文、未配置时不干预 AstrBot 原生行为。
- 模型组：5 种组内策略（priority / round_robin / weighted / random / fallback）、成员级权重/优先级/max_uses/冷却/模型名覆盖、组级 fallbacks。
- 规则引擎：10 种条件（时间含跨午夜、星期/日期、作用域 include+exclude、关键词、命令、@Bot、消息类型、轮数、上下文长度、生命周期事件）、AND/OR、优先级、4 种动作、可解释决策轨迹。
- 会话生命周期：NEW/INITIAL/MAIN/PERIODIC 状态机；v0.1.6 起支持多阶段降级 `stages`+`final_group`、每 N 轮周期校准、上下文压缩触发校准（usage 骤降启发式）。
- temporal 时间强制调度：`model_override` 时段模型替换与 `group_switch` 整组切换，运行时生效、时间结束自动恢复；优先级体系 1000/500/200/100/0；冲突检测（priority_tie / shadowed）；前端「时间规则」视图。
- Agent 配置层：聊天 SubAgent（仅管理员）+ Web AI 配置助手（预览→应用→回滚），30 个结构化工具；5 个预设；配置校验；审计日志；AI 助手会话后端持久化（50 会话 / 200 条）。
- Web 前端模型切换修复：WebChat 下插件决策与 `/provider` 会话偏好回灌 `selected_provider` extra，优先级为 插件决策 > /provider 会话偏好 > 前端下拉选择（插件禁用时不干预）。
- WebUI：16 个视图（总览/模型组/规则/时间规则/生命周期/会话/日志/审计/模拟器/设置/AI 助手/配置向导/预设），中英双语、深色模式、无 CDN。
- 兼容与工程：旧配置自动迁移（schema v1→v2）；`/scheduler` 指令组；331 个离线测试用例；ruff 全绿。

## [0.1.10] - 2026-06

### Fixed

- 修复「web 前端（WebChat）下 /provider 指令与插件自动切换模型均无效」：根因为 AstrBot `_select_provider` 优先采用前端每条消息携带的 `selected_provider` extra（web 模型下拉选择存于 localStorage，恒非空），导致 umo 会话存储（`provider_perf_chat_completion`，/provider 与插件 `set_provider` 的写入目标）永远不被读取。插件现在于 `on_waiting_llm_request` 中把调度决策（或 /provider 写入的会话偏好）回灌到 `selected_provider` extra：插件有决策时决策优先；插件无决策且存在会话偏好时，偏好覆盖前端下拉；插件禁用（原生面板关闭）时不干预。`compat.py` 新增 `get_session_provider_preference()`（经 `astrbot.core.sp.session_get` 读取会话偏好，异常兜底返回 None）。

## [0.1.9] - 2026-06

### Added

- **时间调度规则视图（「时间规则」页）**：此前由配置向导 / AI 配置助手 / 预设创建的 temporal 时间规则（`model_override` 模型替换 / `group_switch` 整组切换）只能存储、无处查看编辑，本次新增前端「时间规则」Tab 完整呈现——列表（名称 / 类型 / 作用组 / 替换 / 时间段 / 优先级 / 启用 / 操作）+ 新建/编辑面板（kind、作用组、源/目标 Provider 或目标组、调度类型 always/daily/weekly/date、时间段、星期复选、指定日期、时区、优先级、规则名），支持查看、编辑、启停（`temporal/toggle`）与删除（`temporal/delete`），写操作后自动刷新列表与冲突。
- **冲突横幅**：页面顶部按 `validate` 返回的 `conflicts` 渲染警示横幅，逐条展示「规则A ⇄ 规则B：同组同时段但去向不同」，并区分 `priority_tie`（同级并列，低优先级将被高优先级遮蔽）/ `shadowed`（被遮蔽）两种 note，黄色警示样式复用现有 badge/warning 体系。
- 时间段在列表按 type 语义化展示：`always`=始终、`daily`=每天、`weekly`=每周[周几]、`date`=date start-end，跨午夜（`end<start`）自动标注「跨午夜」。

## [0.1.8] - 2026-06

### Added

- **AI 配置助手会话后端持久化**：Web 助手的对话历史持久化到插件数据目录的 `agent_chats.json`（最多保留 50 个会话 / 每会话最多 200 条消息，超出淘汰最旧；原子写 tmp→os.replace；读取损坏文件自动备份 `.bak` 并用空列表兜底），刷新或重启后可直接找回历史会话。
- **会话列表 / 切换 / 删除 API**：新增 `agent/conversations`（GET，query `id` 为空返回概要列表、非空返回单会话完整 JSON）与 `agent/conversations/delete`（POST，删除会话并记审计 `chat_delete`）。
- **`agent/chat` 双流程兼容**：`content` 非空 → 会话持久化流程（可选 `conversation_id` 续接，自动新建会话标题取首条，取最近 60 条给 LLM，成功后写回 assistant 消息，审计 `agent_chat`）；`messages` 为列表 → 保留 v0.1.7 旧流程（无持久化，前端不再使用）；两者皆非返回 400。`agent_provider_id` 优先解析 Provider、否则回退默认聊天 Provider 的逻辑不变。

## [0.1.7] - 2026-06

### Fixed

- 修复「AI 助手创建的模型组在模型组页不可见」：前端 tab 懒加载缓存导致切回列表页显示过期数据。现在每次切入 tab 都重新拉取数据并刷新参考下拉（app.js）；模型组页新增「↻ 刷新」按钮；`groups` 接口拉取失败不再静默渲染空列表（改为 toast 提示 + 错误空态）。数据本身一直正确落盘（config.json），无需迁移。

## [0.1.6] - 2026-06

### Added（0.1.6 交付清单）

- **多阶段降级生命周期**：`lifecycle` 新增 `stages: [{group_id, rounds}...]` 与 `final_group`，支持「前 N 轮 A → 随后 M 轮 B → 之后 C」的复合降级编排；`normalize_lifecycle` 自动剔除非法 stage 条目。
- **每 N 轮周期校准**：staged 模式下 `periodic_group` + `periodic_interval` 每 N 轮固定插入校准组一次（优先级高于阶段定位，用于周期校准）。
- **上下文压缩触发校准**：`main.py` 在 `on_llm_response` 采样 `resp.usage.input`，以「相对上一轮骤降」（`prev>=2000 且 cur<prev*0.6`）启发式判定压缩发生（AstrBot 不暴露压缩事件），命中后按生命周期 `calibration_event="context_compression"` + `calibration_group` + `calibration_rounds` 自动写入会话校准状态。
- **全局默认生命周期 `default_lifecycle`**：`settings` 新增该项；无锁无规则会话首轮即按该生命周期选组；Agent 工具 `set_default_lifecycle` 提供「全局启用某生命周期 / 降级预设」方式（空串清除）。
- **配置助手模型指定 `agent_provider_id`**：`settings` 新增该项；Web `agent/chat` 与 `ModelMorphConfigAgentTool.call` 在指定非空时用 `provider_manager.get_provider_by_id` 解析，否则回退默认聊天 Provider。
- **决策优先级全序**：`会话锁定 > 命中规则动作 > 校准阶段 > 生命周期(periodic > stages/final > legacy) > default_lifecycle > base_group > 不干预`，最后叠加 temporal 层（周期校准所选 provider 同样可被时段规则替换）。
- 生命周期 Agent 工具：`list_lifecycles` / `get_lifecycle` / `create_lifecycle` / `update_lifecycle` / `delete_lifecycle` / `set_default_lifecycle`；`build_config_toolset` 的 `make_tool` 支持 `{"type", "description"}` 参数类型声明，`spec`/`ops` 类参数用 `"object"`。
- `ToolContext` 新增可选 `lifecycles` 注入；`tool_get_scheduler_status` 增加 `lifecycle_count` / `default_lifecycle`；`validate` / 生命周期写工具做 multi-stage 与校准结构校验。


## 版本号勘误说明

> 特别说明：仓库此前把已发布版本误记为 **1.0.0 / 1.0.1 / 1.0.2**，实为 **0.1.2 / 0.1.3 / 0.1.4**
> 的错标（内容本身不变，仅版本号序列有误）。自本版起**改用 0.1.x 序列**，并把历史条目版本号
> 一并勘误为 0.1.2 / 0.1.3 / 0.1.4，与 `metadata.yaml` 的 `v0.1.5` 对齐。

## [0.1.5] - 2026-06

### Added（0.1.5 交付清单）

- **① SubAgent 配置入口**：新增聊天 SubAgent 工具 `model_scheduler_config`，仅管理员可用；收到管理员原话后由「模型调度配置代理」通过结构化工具读写调度配置。
- **② Web AI 配置助手**：新增 `assistant` 视图与 `agent/chat` / `agent/apply` / `agent/rollback` / `agent/pending` 接口，用自然语言驱动配置变更，并支持预览 / 应用 / 撤销。
- **③ 简单配置向导**：新增 `wizard` 视图（六步导向：目标类型 → 模型组 → 原模型 → 替代模型/目标组 → 时间设置 → 确认），免手写 JSON 即可创建时间调度规则。
- **④ 5 个预设**：`peak_valley`（峰谷切换）、`night_saving`（夜间省钱）、`workday_performance`（工作日高性能）、`force_replace`（指定模型强制替换）、`maintenance`（临时维护切换），一键套用并自动创建规则。
- **⑤ 时间段模型强制替换 + 模型组切换（temporal 层）**：支持 `model_override`（某时间段 A→B）与 `group_switch`（整组切换），运行时生效、时间结束自动恢复，不改基础配置。
- **⑥ 规则优先级体系**：引入 1000（emergency）/ 500（manual）/ 200（scheduled）/ 100（group）/ 0（default）常量，多规则同时命中时高优先级确定性生效。
- **⑦ 冲突检测**：`validate` / 状态 / Web 前端对同 kind / 同组 / 同时段但去向不同的规则给出 `priority_tie`（同级并列）与 `shadowed`（低者被遮蔽）提示。
- **⑧ 审计日志**：新增 `audit.json` 持久化与 Web 审计页，记录管理员 / Agent 的每次配置变更（谁 / 何时 / 来源 / 动作 / 前后 / 结果）。
- **⑨ 配置 Preview / Apply / Rollback**：高风险或批量变更先预览生成待应用快照，确认后应用，失败可一键回滚到前一配置快照。
- **⑩ 配置校验**：temporal 规则（时间 / 字段 / 时区 / 优先级 / 替换环 / 冲突）与模型组（Provider 存在性）全量校验。
- **⑪ 旧配置自动迁移**：磁盘配置 schema v1 首次加载自动升级为 v2（补 `temporal_rules=[]`、`settings.agent_confirm=True`），`import_all` 兼容 v1/v2。
- **⑫ 兼容保留**：现有 0.1.x 的 groups / rules / lifecycles / sessions / 调度引擎 / WebUI 功能与测试全部保留。

## [0.1.4] - 2026-06

### Fixed

- 修复「原生配置面板开启调试模式/总开关后前端仍显示禁用」：`enabled`/`debug` 改由 AstrBot 原生配置面板（`_conf_schema.json`）作为唯一来源，经 `RuntimeAdapter` 实时读取（每次调度取最新值）；移除旧的一次性 `ui_managed` 合并逻辑。Settings 页对这两个开关只读展示并提示去原生面板修改。

### Changed

- WebUI 按「群聊记录存储」插件的 dashboard 设计系统整体重写（三段式滑动高光导航、Arco 风格双主题设计令牌、标准卡片/表格/弹窗/Toast 组件、防 XSS 渲染规范），8 个视图功能与后端 API 契约不变，中英双语键集（249 项）一致，无外部 CDN。

## [0.1.3] - 2026-06

### Fixed

- 修复 AstrBot 运行时插件加载失败（`ModuleNotFoundError: No module named 'scheduler'`）：AstrBot 以包形式加载插件（`data.plugins.<插件名>`），插件目录不在 `sys.path` 上，`main.py` 与 `web/api.py` 的包内导入全部改为相对导入；已按加载器真实路径验证导入成功。
- 修复 `_conf_schema.json` 数组格式导致插件加载失败（`AttributeError: 'list' object has no attribute 'items'`）：AstrBot v4.27 要求配置 Schema 顶层为 JSON 对象（每个配置键为 key），已改为对象格式并经真实 `AstrBotConfig` 解析验证。

## [0.1.2] - 2026-06

### Added

- 模型自动调度器初版：按时间/会话/用户/群组/消息事件/上下文状态/轮数自动决定每个会话使用的模型（Provider）。
- 模型组：引用 AstrBot 已配置 Provider，5 种组内策略（priority/round_robin/weighted/random/fallback），成员级 max_uses/cooldown/权重/优先级/模型名覆盖，组级自动降级 fallbacks。
- 规则引擎：10 种条件（时间范围含跨午夜、星期/日期、群/用户/会话/平台作用域 include+exclude、关键词、命令、@Bot、消息类型、轮数、上下文长度、生命周期事件）、AND/OR 组合、优先级、4 种动作（switch_group/switch_provider/apply_lifecycle/unlock）、可解释决策轨迹（DecisionTrace）。
- 会话生命周期策略：NEW/INITIAL/MAIN/PERIODIC 状态机（Strong×N → Main → 每 K 轮 Periodic），内置 4 套模板；`/new`、`/reset` 自动检测（extra 标志 + conversation_id 双保险）。
- 会话隔离与会话级控制：per-UMO 状态与锁；WebUI 与命令支持锁定/解锁/重置调度状态。
- 调度日志：切换/重置/锁定/外部 Provider 变更/错误五类日志，环形缓冲 + 持久化。
- WebUI（Plugin Page，8 视图，中英双语，深色模式，无 CDN）：Dashboard、模型组、规则、生命周期、会话管理、调度日志、规则模拟器（Dry Run）、设置（含配置导入导出）。
- `/scheduler` 指令组：status / model / lock / unlock / reset（变更类限管理员）。
- 离线测试套件 109 用例（规则/模型组/生命周期/会话隔离/并发/引擎全流程/持久化/日志），pytest 全绿；ruff check/format 全绿。
- 核心机制：`@filter.on_waiting_llm_request` 内调用 `ProviderManager.set_provider(provider_id, CHAT_COMPLETION, umo)`（`/provider` 同款会话隔离机制），切换只影响下一次 LLM 请求的 Provider，不清空对话上下文；未配置时不干预 AstrBot 原生行为。
