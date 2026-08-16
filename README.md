# Model Morph — AstrBot 模型自动调度器

> Model Scheduler / Model Orchestrator for AstrBot：根据时间、会话、用户/群组、消息事件、上下文状态与对话轮数等条件，**自动决定每个会话当前应使用的模型（Provider）组合**，并通过 WebUI 可视化编排。

不再是简单的 `/provider` 切换——而是一个**可视化的、可编排的、按会话独立运行的、可按时间与事件自动触发的模型调度系统**。

## 功能特性

- **模型组（Model Group）**：引用 AstrBot 已配置的 Provider（不保存 API Key），组内支持 `priority` / `round_robin` / `weighted` / `random` / `fallback` 五种调度策略，成员可配置权重、优先级、最大使用次数、冷却时间、模型名覆盖与 fallback 降级。
- **规则系统（Rule Engine）**：`WHEN … AND/OR … THEN …` 式规则，支持 10 种条件（时间范围/跨午夜/星期、日期、群/用户/会话/平台作用域 include+exclude、关键词、命令、@Bot、消息类型、轮数、上下文长度、生命周期事件）与 4 种动作（切换模型组/直选 Provider/应用生命周期/解锁），带优先级与完整决策轨迹。
- **会话生命周期策略（Lifecycle Strategy）**：新会话自动降级——`Strong × 3 → Cheap → 每 5 轮 Medium`，状态机 NEW/INITIAL/MAIN/PERIODIC，`/new`、`/reset` 自动检测。
- **会话隔离**：调度状态按 UMO（会话）完全隔离，多群/多用户并发互不污染；支持会话级手动锁定与恢复自动调度。
- **可解释决策（DecisionTrace）**：每次切换都能回答「为什么是这个模型」——命中规则、被拒绝规则及每个条件的 ✓/✗ 与原因。
- **WebUI（Plugin Page，8 个视图）**：Dashboard / 模型组 / 规则 / 生命周期 / 会话管理 / 调度日志 / **规则模拟器（Dry Run）** / 设置，支持中英双语与深色模式，无外部 CDN。
- **调度日志**：时间/会话/规则/旧模型→新模型/轮数/原因，WebUI 可查。
- **不干预原则**：未配置任何规则与默认模型组时，插件完全不动 AstrBot 原生行为；切换模型**从不**清空对话上下文。

## v1.0.2 新特性

> 当前版本 **v1.0.2**。

- **💬 AI 助手流式输出（SSE）**：配置助手回复逐段增量渲染，不再一次性等待完整回复；期间显示「正在使用工具 xxx…」提示；文本超长或流式不可用时自动回退普通模式。会话持久化语义不变（用户消息先写、完整回复后写、错误不写半截）。
- **📝 Markdown 渲染**：AI 助手气泡（历史 + 流式）支持标题/粗体/斜体/行内代码/围栏代码块/列表/引用/链接/表格/水平线；渲染器内置、无外部 CDN，白名单标签 + textContent 防 XSS；用户消息保持纯文本。
- **🔤 UMO 理解补全**：配置 Agent 现在完全理解 UMO 格式（`platform_id:message_type:session_id`，如 `aiocqhttp:GroupMessage:群号` / `aiocqhttp:FriendMessage:QQ`）——你直接给出 UMO 会原样使用，给出群号/QQ 会自动换算。
- **🧭 规则 vs 生命周期自动消歧**：说「晚上用便宜模型/高峰期别用某模型」→ 时间调度规则；说「前 N 轮用贵的、之后降级/校准」→ 生命周期；含糊的「调度」会先查询现状再向你确认，一句话只执行一次，不再重复创建。
- **👁️ UMO 实时预览**：在时间规则 / 生命周期 / 配置向导的「限定群组」输入区与模拟器中输入群号或 QQ，下方实时显示它对应的 UMO 长什么样（平台可切换）。
- **🔧 工具提示词全面完善**：所有工具的描述补齐取值枚举与参数语义（strategy 五策略、kind 二类型、schedule 四类型、scope 结构、生命周期字段等），模型参数错误率显著下降。

## v1.0.0 — 首个正式版本

> 以下为历史版本说明（v1.0.0 起）。

v1.0.0 在 0.1.x 全部能力之上冻结为正式版，并包含最后一项关键修复：

- **🌐 WebChat 模型切换修复（v0.1.10 并入）**：web 前端每条消息携带的 `selected_provider`（模型下拉选择存 localStorage）会覆盖 `/provider` 指令与插件写入的会话偏好，导致 web 下「插件自动切换」与「手动 /provider」均看似失效。插件现于 `on_waiting_llm_request` 把最终决策回灌到事件 extra，优先级为：**插件调度决策 > `/provider` 会话偏好 > web 前端下拉选择 > AstrBot 默认**；插件禁用（原生面板关闭）时完全不干预。
- 其余机制保持 0.1.x 语义：决策优先级全序（锁定 > 规则 > 校准 > 生命周期 > 默认生命周期 > base_group > temporal 时段替换）、时间规则视图、冲突检测、配置校验、旧配置自动迁移、审计日志、配置助手会话持久化。

## v0.1.5 新特性（历史版本）

- **🤖 Agent 配置助手**：在聊天里直接对模型调度配置说人话（"晚上八点以后用便宜模型"、"DeepSeek 高峰期别用"……），配置 Agent 会通过结构化工具查询 → 规划 → 预览 → 执行 → 汇报；也可在 WebUI 的 **AI 助手** Tab 里用自然语言对话，配合 **预览 → 应用 → 撤销** 流程，高风险变更可随时回滚。
- **⏱️ 时间强制替换**：新增 temporal 调度层，支持**按时间段把某模型替换为另一模型**（`model_override`）或**整组切换**（`group_switch`），运行时生效、时间结束自动恢复，**不改基础配置**；支持跨午夜、星期、指定日期、规则级时区与**优先级体系**（1000/500/200/100/0）。
- **🧩 5 个预设**：峰谷模型切换、夜间省钱、工作日高性能、指定模型强制替换、临时维护切换，一键套用即生成规则。
- **⚠️ 冲突检测**：对同时段/同组但去向不同的规则给出「同级并列 / 低者被遮蔽」提示，避免规则互相打架。
- **📋 审计日志**：每一次配置变更（谁 / 何时 / 来源 / 动作 / 前后 / 结果）都记入 `audit.json`，可在 WebUI **审计** Tab 追溯。
- **🔄 旧配置自动迁移**：旧版配置文件首次加载自动升级到 v2 schema（上线即生效，无需手动操作），`import_all` 同时兼容 v1/v2。

## v0.1.6 新特性（历史版本）

- **🪜 多阶段降级生命周期**：单个生命周期可用 `stages` 声明「前 N 轮 A → 随后 M 轮 B → 之后 C」的复合降级序列，`final_group` 为耗尽后的主组；`normalize_lifecycle` 自动剔除非法 stage。
- **🔄 周期校准**：`periodic_group` + `periodic_interval` 让 staged 模式下每 N 轮固定用校准组跑一轮（优先级高于阶段定位），适合「每 15 个对话升级到 V4 Flash 校准一轮」这类需求。
- **🧯 上下文压缩校准**：插件在 `on_llm_response` 采样 `usage.input`，以相对骤降启发式判定「上下文压缩」发生（AstrBot 本身不暴露压缩事件），随后按生命周期的 `calibration_event="context_compression"` + `calibration_group` + `calibration_rounds` 自动把会话切到校准组并计数 N 轮（新会话前 3 轮只建基线，不误判）。
- **🌐 全局默认生命周期 `default_lifecycle`**：无锁会话直接按全局默认生命周期做降级；Agent 用 `set_default_lifecycle` 一键「全局启用某生命周期 / 降级预设」（空串清除）。
- **🎛️ 配置助手指定模型 `agent_provider_id`**：可在后台指定配置助手（Web/SubAgent）使用的模型 Provider；留空则跟随默认聊天 Provider。
- **⚖️ 决策优先级全序**：`会话锁定 > 命中规则动作 > 校准阶段 > 生命周期(periodic > stages/final > legacy) > default_lifecycle > base_group > 不干预`，最后叠加 temporal 时段替换。

## v0.1.8 新特性（历史版本）

- **💬 AI 配置助手会话持久化**：Web 助手的对话历史现在会保存到插件数据目录的 `agent_chats.json`（最多保留 **50 个会话** / 每会话最多 **200 条消息**，超出自动淘汰最旧；原子写入 + 损坏文件 `.bak` 备份兜底），刷新或重启 AstrBot 后即可直接找回历史会话。
- **🗂️ 会话列表 / 切换 / 删除**：侧栏可查看会话概要（标题 / 时间 / 条数 / 预览），一键切换或删除会话；删除会写入审计日志。
- **🔀 双流程兼容**：`agent/chat` 接口对旧前端保持向后兼容 —— `content` 新流程（自动/续接会话，成功后回写 assistant 消息）与旧 `messages` 一次性历史流程并存。

## v0.1.9 新特性（历史版本）

- **⏱️ 时间规则视图**：新增「时间规则」Tab，查看 / 编辑 / 启停 / 删除由配置向导、AI 配置助手或预设创建的 `model_override`（模型替换）与 `group_switch`（整组切换）时间规则，列表按类型语义化展示时间段（始终 / 每天 / 每周[周几] / 指定日期，跨午夜自动标注）。
- **⚠️ 冲突横幅**：页面顶部实时展示 `priority_tie` / `shadowed` 两类规则冲突警示，避免规则在同一组同时段但去向不同时互相打架。

## v0.1.10 新特性（历史版本）

- **🌐 WebChat 模型切换修复**：web 前端每条消息携带的 `selected_provider` extra 会覆盖 `/provider` 指令与插件写入的 umo 会话偏好，导致 web 场景下切换“无效”。插件现于 `on_waiting_llm_request` 把调度决策（或 `/provider` 会话偏好）回灌到事件 extra：插件决策 > `/provider` 会话偏好 > web 前端下拉选择；插件禁用时不干预。详见 v1.0.0 条目。

## 安装

1. 将本插件目录放入 `AstrBot/data/plugins/astrbot_plugin_model_morph/`（或通过插件市场安装）。
2. 在 AstrBot WebUI「插件」页启用插件。
3. 打开插件详情页的 **Model Morph** Page 开始编排。

无第三方 pip 依赖（仅标准库 + AstrBot 内置）。

## 支持版本

- **AstrBot >= 4.27.0, <5**（开发与验证基准：4.27.2）
- 核心依赖两个官方接口：`@filter.on_waiting_llm_request` 事件钩子与 `ProviderManager.set_provider(provider_id, CHAT_COMPLETION, umo)` 会话隔离机制（与 `/provider` 命令同款，v4.x 均存在）。全部兼容性封装位于 `scheduler/compat.py`。

## 核心概念

| 概念 | 说明 |
|---|---|
| **Provider** | AstrBot 中已配置的模型提供商实例（OpenAI/Claude/DeepSeek/本地模型等）。插件只引用，不管理密钥。 |
| **模型组 Model Group** | 一组 Provider + 组内调度策略 + 降级配置。例如 `Strong = [GPT, Claude, DeepSeek]`（priority 策略）。 |
| **规则 Rule** | `条件（WHEN）→ 动作（THEN）`，带优先级；高优先级命中即生效（Override 语义）。 |
| **生命周期 Lifecycle** | 会话内的阶段式模型序列：INITIAL（初始组 × N 轮）→ MAIN（主组）→ 每 K 轮插入 PERIODIC（周期组）。 |
| **作用域 Scope** | 规则可通过群/用户/会话/平台条件限定生效范围（支持 include/exclude）。 |
| **继承** | 会话未命中任何规则时使用 `base_group`（全局基础配置）；`base_group` 为空则完全不干预（继承 AstrBot 原生行为）。 |
| **锁定 Lock** | 会话级手动覆盖：锁定后自动规则不再覆盖，直到解锁。 |
| **DecisionTrace** | 每次调度决策的可解释轨迹（命中/拒绝/原因/耗时）。 |

## 模型组策略

| 策略 | 行为 |
|---|---|
| `priority` | 按成员优先级取第一个可用 |
| `round_robin` | 会话内按顺序轮换 |
| `weighted` | 按权重随机 |
| `random` | 等权随机 |
| `fallback` | 仅用第一个可用成员，全部不可用时降级到 `fallbacks[]` |

成员级约束：`max_uses`（最大使用次数）、`cooldown_seconds`（冷却）、`enabled`、`model_override`（同一 Provider 实例内换模型名，可选）、`allow_auto_fallback`（组级降级开关）。

## 规则条件与动作

**条件类型**：`time_range`（HH:MM 范围，支持跨午夜与星期过滤）、`date_weekday`（工作日/周末/具体星期/具体日期）、`scope`（群/用户/会话/平台，include+exclude）、`keyword`（包含/前缀）、`command`（命令边界前缀匹配）、`at_bot`、`message_type`（group/private）、`round_gte`（轮数 ≥）、`context_length_gte`（上下文估算 ≥）、`lifecycle_event`（new/reset）。组合方式：`AND` 或 `OR`（v0.1 单层组合）。

**动作类型**：`switch_group`（切换模型组）、`switch_provider`（直选 Provider）、`apply_lifecycle`（绑定生命周期策略）、`unlock`（解除会话锁定）。

## WebUI 使用说明

插件详情页 → 打开 **Model Morph** Page（8 个核心视图 + v0.1.5 新增 4 个 tools 分区 Tab）：

1. **Dashboard**：调度器开关状态、当前时区、Provider/组/规则/会话计数、最近切换与最近错误；v0.1.5 新增「当前生效时段规则」卡片。
2. **Model Groups**：新建/编辑/复制/启停模型组；编辑面板含策略下拉、成员表格（Provider 下拉、权重、冷却、最大次数）、fallbacks 多选。
3. **Rules**：条件行编辑器（类型下拉 + 参数 + AND/OR + THEN 动作 + 优先级），每条规则可一键「模拟」。
4. **Lifecycles**：初始组/初始轮数/主组/周期组/周期间隔编辑，内置 Balanced / Quality / Cost Saving / New Conversation 四套模板一键载入。
5. **Sessions**：活跃会话列表（UMO/当前 Provider/组/轮数/阶段/命中规则/锁定状态），支持 Lock / Unlock / Reset / Remove。
6. **Logs**：调度日志表格，按会话/级别筛选、清空。
7. **Simulator**：输入时间/群/用户/会话/轮数/事件/消息 → 返回命中规则、拒绝规则及失败条件、最终 Provider 与原因（Dry Run，不影响真实状态）。
8. **Settings**：时区（auto=跟随 AstrBot）/ 默认模型组 / **全局默认生命周期（default_lifecycle）** / **配置助手模型（agent_provider_id）** / 日志保留数 / Agent 确认开关 / 审计保留数 / 会话状态持久化 + 配置导入导出（JSON）。「启用调度器」与「调试模式」两个总开关在 **AstrBot 原生插件配置面板**中修改，此处只读展示。

**v0.1.5 新增 Tab（tools 分区）**：
- **AI 助手**：自然语言对话配置模型调度（流式输出 + Markdown 渲染，预览/应用/撤销），底部提供快捷需求 chips；对话可持久化、侧栏切换历史会话。
- **配置向导**：六步可视化创建时间调度规则，无需手写 JSON。
- **预设**：5 个一键套用的时间调度预设。
- **审计日志**：配置变更审计追溯（按来源筛选、清空）。

### 时间调度规则（temporal）
- 查看 / 新建 / 编辑 / 删除「时间段模型替换」与「整组切换」规则（Rules 页之上新增的独立规则集合）。
- 每条规则包含 `kind`（model_override / group_switch）、目标组、源/目标 Provider（或目标组）、`schedule`（daily / weekly / date / always，支持跨午夜、星期、时区）、`priority`。
- 保存前自动校验（时间格式、Provider / 组存在性、自引用、替换环、冲突提示）。

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/scheduler` / `/scheduler status` | 所有用户 | 当前会话调度状态（Provider/组/轮数/阶段/锁定/最近规则） |
| `/scheduler model` | 所有用户 | 当前 Provider 与模型名 |
| `/scheduler lock <组id或组名>` | 管理员 | 锁定本会话到指定模型组 |
| `/scheduler unlock` | 管理员 | 解锁，恢复自动调度 |
| `/scheduler reset` | 管理员 | 重置本会话调度状态（轮数归零，不动对话上下文） |

## 配置说明

- AstrBot 原生配置面板（`_conf_schema.json`）：仅 `enabled`（总开关）与 `debug`（调试日志）两项。
- 其余全部配置在 WebUI Settings/各组页面中维护，持久化到 **`data/plugin_data/astrbot_plugin_model_morph/`**：
  - `config.json`：settings / groups / rules / lifecycles / temporal_rules（原子写入，schema 版本化）
  - `logs.json`：调度日志持久化
  - `audit.json`：审计日志持久化
  - `state.json`：会话运行时状态快照（每 300 秒 + 插件卸载时）
- 时区：`timezone: auto` 跟随 AstrBot 全局配置时区；也可显式填写 IANA 时区名（如 `Asia/Shanghai`）。WebUI Dashboard 显示当前检测到的时区。
- `agent_confirm`（默认开）：高危配置变更（删除模型组/规则、批量写操作）须先预览再应用；关闭后直接执行（仍记审计）。
- `default_lifecycle`（默认空）：全局默认生命周期 id，无锁会话据此做多阶段降级；可用 Agent 工具 `set_default_lifecycle` 或 Settings 页设置，空串清除。
- `agent_provider_id`（默认空）：配置助手（Web/SubAgent）使用的 Provider id；空则跟随默认聊天 Provider。
- Debug 模式开启后，AstrBot 日志输出完整决策轨迹（规则评估、条件结果、状态变化）。

## 完整示例

### 示例 A：按时间切换

需求：08:00-18:00 用 Normal；18:00-23:00 用 Strong；23:00-08:00 用 Cheap（跨午夜）。

1. Model Groups 页创建 `Normal`、`Strong`、`Cheap` 三个组（各自配置 Provider 成员）。
2. Rules 页创建三条规则，`WHEN 时间范围 = 08:00-18:00 → THEN 切换模型组 = Normal`（其余两条同理；23:00-08:00 即跨午夜范围）。
3. 生效：任意会话消息进入 LLM 前，调度器按当前时间命中规则并切换该会话 Provider，上下文不变。

### 示例 B：新对话自动降级（生命周期）

需求：`/new` 或 `/reset` 后，Strong × 3 轮 → Cheap → 每 5 轮插入一次 Medium。

1. Model Groups 页创建 `Strong`、`Cheap`、`Medium` 三组。
2. Lifecycles 页新建策略：Initial Group=Strong、Initial Rounds=3、Main Group=Cheap、Periodic Group=Medium、Periodic Interval=5。
3. Rules 页创建规则：`WHEN 生命周期事件 = reset（或 new）→ THEN 应用生命周期 = <该策略>`。
4. 生效：会话执行 `/new` 后，调度器检测到重置事件 → 轮数归零、阶段 NEW。随后第 1-3 条消息用 Strong，第 4-7 条用 Cheap，第 8 条用 Medium，第 9-12 条用 Cheap，第 13 条用 Medium……**全程对话上下文保持不清空**。另一个群同时 `/new` 时拥有完全独立的轮数与状态。

### 示例 C：不同群不同模型 + 会话锁定

需求：开发群用 Strong、娱乐群用 Cheap、其他群用 Normal；某个会话临时锁定 Strong。

1. Rules 页创建三条规则，`WHEN 作用域 群 = <群号> → THEN 切换模型组 = Strong/Cheap`；Settings 页把 `base_group` 设为 `Normal`（兜底）。
2. 生效：各群自动使用对应组；未命中的群继承 `base_group`（Normal）。
3. Sessions 页找到目标会话 → `Lock` → 选择 `Strong`：该会话从此忽略规则；`Unlock` 恢复自动调度。验证优先级：锁定 > 规则 > base_group。

### 示例 D：完整降级预设（多阶段轮次降级 + 周期校准 + 压缩校准 + 时段路由）

需求：前 4 个对话用 gemini3.7flash → 5 个对话用 deepseekv4flash → 之后用 ling3.0flash；
每完成 15 个对话升级到 V4 Flash 校准一轮；每次检测到上下文压缩用 gemini3.7flash 校准
5 轮；每天 09:00-11:00 与 14:00-18:00 把 V4 Flash 路由到 gpt5.6luna；并全局启用该预设。

1. Model Groups 页创建 `flashA`（gemini3.7flash）、`flashB`（deepseekv4flash）、
   `flashC`（ling3.0flash）、`calD`（V4 Flash）、`calE`（gemini3.7flash）五组，各自放入对应 Provider。
2. Lifecycles 页在「多阶段降级」区新建策略：`stages = [{flashA, 4}, {flashB, 5}]`、
   `final_group = flashC`、`periodic_group = calD`、`periodic_interval = 15`、
   `calibration_event = context_compression`、`calibration_group = calE`、`calibration_rounds = 5`。
3. Settings 页把 **全局默认生命周期** 设为该策略（等效 Agent 调用 `set_default_lifecycle`）。
4. Temporal 规则页新建两条 `model_override`：daily 09:00-11:00 与 daily 14:00-18:00，
   `source = deepseekv4flash 的 Provider`、`target = gpt5.6luna 的 Provider`。
5. 生效：前 4 轮用 flashA，第 5-9 轮用 flashB，之后用 flashC；第 15/30/… 轮用 calD 校准；
   每次检测到上下文压缩后连续 5 轮用 calE；两时段内的 V4 Flash 会被时段规则替换为 gpt5.6luna；
   **上下文全程不清空**。
6. 直接在聊天里对配置助手说「前 4 轮用 gemini3.7flash，第 5-9 轮用 deepseekv4flash，
   之后常用 ling3.0flash，每 15 轮用 V4 Flash 校准，压缩后 gemini3.7flash 校准 5 轮，
   每天 9-11 点与 14-18 点把 V4 路由到 gpt5.6luna，并设为全局默认」，配置助手也会按
   相同的优先/查询/预览/应用 流程为你编排（见「Agent 配置助手」）。

## 故障排查

| 现象 | 排查项 |
|---|---|
| 调度器完全不切换 | AstrBot 插件配置面板中「总开关」是否开启；是否使用了第三方 Agent Runner（dify/coze/deerflow 等，本地调度自动跳过）；规则是否被 `enabled`/优先级影响 |
| 某条规则不命中 | 打开 Rules 页该规则的「模拟」按钮，或 Simulator 页填入相同输入查看失败条件与原因；检查 scope 的 exclude 是否误伤 |
| 模型组选择不符合预期 | 检查成员 `enabled`、`max_uses`、`cooldown_seconds` 与 Provider 是否仍存在于 AstrBot；组策略是否正确 |
| 想看决策细节 | Settings 开启 `debug`，AstrBot 日志将输出完整 DecisionTrace |
| 数据位置 | `data/plugin_data/astrbot_plugin_model_morph/`（config/logs/audit/state 四个 JSON） |

## 已知限制

1. **同会话并发双消息的极窄竞态**：`on_waiting_llm_request` 在框架会话锁之外执行，两条消息几乎同时到达同一会话时，切换归属可能错位一轮（实践中平台按序投递 + 框架会话锁使窗口极小）。调度状态本身由 per-UMO 锁保证一致。
2. **第三方 Agent Runner**（dify/coze/deerflow/dashscope）的 LLM 请求在外部产品中执行，插件自动跳过本地调度。
3. `model_override`（同一 Provider 实例内换模型名）依赖对应 provider adapter 支持请求级 `model` 参数（openai/anthropic/gemini 系列支持）；跨实例切换无此限制。
4. 条件组合 v0.1 为单层 AND/OR；嵌套布尔表达式、cron 表达式、统计图表为后续版本规划。
5. 插件重启后运行时状态按快照恢复（conversation_id 校验），无法恢复时该会话按新会话处理。

## 开发

```bash
# 离线测试（无需运行 AstrBot；在插件根目录执行）
python -m pytest tests -q -p no:cacheprovider
```

> 测试临时数据自动落在 `开发/v0.1/.pytest_tmp/`（`tests/conftest.py` 内可移植路径），不依赖系统 tempfile。

- 代码结构：`main.py`（Star 入口/钩子/命令）→ `scheduler/`（compat 兼容层 / persistence 持久化 / state 会话状态 / groups 模型组 / rules 规则 / lifecycle 生命周期 / temporal 时间调度 / audit 审计 / agents agent 配置层 / presets 预设 / engine 调度引擎 / logs 日志）→ `web/api.py`（Web API 路由，v0.1.5 新增 temporal/validate/runtime/presets/agent/audit）→ `pages/model-morph/`（前端 SPA）。
- 设计文档与调研报告见 `开发/`（不提交 git）。
- 运行时接入点：`@filter.on_waiting_llm_request`（Provider 解析前调用 `ProviderManager.set_provider(umo=...)`，与 `/provider` 同款会话隔离机制）；`/new` `/reset` 检测基于 `_clean_group_context_session` extra 标志 + conversation_id 变化双保险。
