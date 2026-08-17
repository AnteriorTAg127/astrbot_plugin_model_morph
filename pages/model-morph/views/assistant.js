// ==========================================================================
// Model Morph · AI 配置助手（views/assistant.js）— 会话驱动（v0.1.8）
// 会话持久化：侧栏列出历史会话（标题/时间/创建/预览/条数/删除），聊天区渲染当前会话消息。
// - 侧栏：#assistantNewConv（新对话）+ #assistantConvList（会话列表）。
// - 聊天区：assistant/user 气泡，纯文本 + white-space:pre-wrap（不支持 markdown，防 XSS）。
// - pending 非空 → 聊天区下方「待应用更改」审批卡（v1.0.3 人性化）：
//   优先渲染后端生成的 summary[] 人类可读变更列表（每条：类型徽标色 + 文本），附
//   [批准] agent/approve / [拒绝] agent/reject 按钮，成功后刷新；提供「展开查看原始
//   数据」折叠区显示 ops JSON（供高级用户核对）。
// - 兼容旧 pending 结构：无 summary（老 preview 数据）时回退旧 preview 渲染，不报错。
// - 流式 done 帧 pending 取新结构 {pending_id, summary[]}，以 Markdown 列表渲染 + 按钮，
//   不再直接以 JSON 为主视图。
// - 跨页面切换的流式保护：流式对话进行中切到其它页再切回时，load() 依据模块级
//   streamingActive 跳过聊天区重建与状态重置（保留正在渲染的流式气泡）；若气泡仍被
//   其它路径清空（幽灵节点），收尾时检测 msg.isConnected === false 则自愈重建并重拉
//   后端会话，保证回复不丢失（后端 ChatStore 已写回，前端需重新拉取展示）。
// 路由（与主 API 注册一致）：
//   GET agent/conversations(可选 ?id) │ POST agent/chat(content|conversation_id+content)
//   POST agent/conversations/delete{id} │ POST agent/apply │ POST agent/rollback
//   GET agent/pending │ POST agent/approve{pending_id?} │ POST agent/reject{pending_id?}
// 全部动态文本一律 textContent / el()，防 XSS；请求中禁用发送按钮。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";
import { renderMarkdown } from "../markdown.js";

const S = "pages.model-morph.assistant";

// 会话状态
let currentCid = "";      // 当前会话 id；空串表示「新对话」（发送时后端自动创建）
let convList = [];        // 侧栏会话概要 [{id,title,created_at,updated_at,message_count,last_preview}]
let pending = null;       // 最近一次 agent/chat 返回的 pending 结构（未应用）
let appliedPending = null; // 已应用的那份（用于「可撤销」提示）
let busy = false;
// 是否有流式对话进行中。app.js 每次切入 assistant tab 都会调用 load()，
// 该标志用于让 load() 在流式进行中跳过聊天区重建（详见 load()）。
let streamingActive = false;

// 快捷需求 chips（点击填入输入框，不自动发送）
const CHIPS = [
    "assistant.chip1",
    "assistant.chip2",
    "assistant.chip3",
];

// ========== 工具：时间格式化（ISO → 本地 "MM-DD HH:MM"） ==========
function formatTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ========== 文本 bubble（防 XSS） ==========
// role = "user" | "assistant"；opts.markdown=true 时 assistant 消息走内置 Markdown 渲染。
// - user 气泡：保持现状（pre-wrap + textContent 纯文本）；
// - assistant 气泡：输出器渲染 markdown（白名单 + textContent），样式类追加 chat-msg-md。
function bubble(role, text, opts = {}) {
    const row = el("div", `chat-row ${role === "user" ? "chat-row-user" : "chat-row-assistant"}`);
    const msg = el("div", `chat-msg ${role === "user" ? "chat-msg-user" : "chat-msg-assistant"}`);
    if (role === "user") {
        msg.style.whiteSpace = "pre-wrap";
        msg.textContent = text;
    } else {
        msg.classList.add("chat-msg-md");
        // renderMarkdown 返回受控 DOM（textContent + 白名单标签），直接追加。
        msg.appendChild(renderMarkdown(text));
    }
    row.appendChild(msg);
    return row;
}

// ========== pending 预览卡 ==========
// op: { action, target, before, after, warnings }（before/after 可为任意对象）
function pendingOpCard(entry) {
    const card = el("div", "pending-op");
    const head = el("div", "pending-op-head");
    head.appendChild(el("span", "pending-op-action", entry.action || ""));
    head.appendChild(el("span", "pending-op-target mono", entry.target || ""));
    card.appendChild(head);

    if (entry.warnings && entry.warnings.length) {
        const warn = el("div", "pending-op-warn");
        for (const w of entry.warnings) warn.appendChild(el("div", null, "⚠ " + w));
        card.appendChild(warn);
    }

    const grid = el("div", "pending-op-beforeafter");
    const kv = (label, value) => {
        const cell = el("div", "pending-op-cell");
        cell.appendChild(el("div", "pending-op-label", label));
        const pre = document.createElement("pre");
        pre.className = "pending-op-pre mono";
        pre.textContent = value === undefined ? t(`${S}.na`, "—") : JSON.stringify(value, null, 2);
        cell.appendChild(pre);
        return cell;
    };
    grid.appendChild(kv(t(`${S}.before`, "修改前"), entry.before));
    grid.appendChild(kv(t(`${S}.after`, "修改后"), entry.after));
    card.appendChild(grid);
    return card;
}

// pending 结构：{pending_id, ops(真实操作), snapshot, preview(预览项[...]), summary?, staged_at?}
// summary 为后端生成的人类可读变更描述列表（v1.0.3 起新结构）。
function pendingSummaryList(p) {
    const s = p && Array.isArray(p.summary) && p.summary.length ? p.summary : null;
    // summary 元素可能是字符串，也可能是 {text, type} 结构（徽标类型色）；统一归一化。
    if (s) return s;
    return null;
}

// 渲染预览卡优先用 pending.preview（含 before/after）；无 preview（旧数据）时回退 ops。
function pendingOpsList(p) {
    const src = p && Array.isArray(p.preview) && p.preview.length ? p.preview : (p && p.ops);
    return Array.isArray(src) ? src : [];
}

// 判断 pending 是否存在「有内容」的变更（兼容新 summary 与旧 ops/preview 结构）。
function hasPendingContent(p) {
    if (!p) return false;
    if (pendingSummaryList(p)) return true;
    return pendingOpsList(p).length > 0;
}

// 判断应显示「人性化」审批卡（有 summary）还是旧预览卡（无 summary）。
function hasHumanizedPending(p) {
    if (!p) return false;
    return !!pendingSummaryList(p) && pendingSummaryList(p).length > 0;
}

// 展开折叠区（显示原始 ops JSON，供高级用户核对）。textContent 防 XSS。
function buildRawExpand(title, rawObj) {
    const box = el("div", "pending-raw");
    const head = el("button", "pending-raw-toggle", "▸ " + title);
    head.type = "button";
    const pre = el("pre", "pending-op-pre mono");
    const obj = rawObj || [];
    const isEmpty = !(Array.isArray(obj) ? obj.length : obj && typeof obj === "object" && Object.keys(obj).length);
    pre.textContent = isEmpty ? t(`${S}.na`, "—") : JSON.stringify(obj, null, 2);
    box.appendChild(head);
    box.appendChild(pre);
    let open = false;
    head.addEventListener("click", () => {
        open = !open;
        pre.style.display = open ? "block" : "none";
        head.textContent = (open ? "▾ " : "▸ ") + title;
    });
    return box;
}

// 创建批准/拒绝按钮行；onApprove/onReject 异步处理，成功后由调用方刷新。
function buildApproveRejectBar(p, { onDone, pendingId } = {}) {
    const bar = el("div", "chat-pending-actions");
    const approveBtn = el("button", "btn btn-primary btn-sm", t(`${S}.approve`, "✅ 批准"));
    const rejectBtn = el("button", "btn btn-danger btn-sm", t(`${S}.reject`, "🛑 拒绝"));
    approveBtn.type = "button"; rejectBtn.type = "button";
    approveBtn.addEventListener("click", () => doApprove(approveBtn, rejectBtn, { pendingId, onDone }));
    rejectBtn.addEventListener("click", () => doReject(approveBtn, rejectBtn, { pendingId, onDone }));
    bar.appendChild(approveBtn);
    bar.appendChild(rejectBtn);
    return bar;
}

// 人性化审批卡主渲染：summary 列表（徽标类型色 + 文本）+ 批准/拒绝 + 展开原始数据。
function renderHumanizedPending(p, { pendingId } = {}) {
    const area = document.getElementById("assistantPending");
    area.replaceChildren();
    area.style.display = "block";

    area.appendChild(el("div", "pending-title", t(`${S}.pending_approval_title`, "📥 待审批的更改")));
    if (pendingId) area.appendChild(el("div", "pending-id mono", t(`${S}.pending_id_label`, "暂存编号") + "：" + pendingId));
    if (p && p.staged_at) area.appendChild(el("div", "pending-staged-at", t(`${S}.staged_at_label`, "暂存时间") + "：" + p.staged_at));

    // summary 列表（可为字符串或 {text,type} 结构）
    const list = el("div", "pending-summary-list");
    for (const item of pendingSummaryList(p)) {
        const row = el("div", "pending-summary-row");
        if (typeof item === "string") {
            row.appendChild(el("span", "badge muted", t(`${S}.change_badge`, "变更")));
            row.appendChild(el("span", "pending-summary-text", item));
        } else {
            const it = item || {};
            const type = it.type || "";
            const badgeClass = type ? `badge ${summaryBadgeClass(type)}` : "badge muted";
            row.appendChild(el("span", badgeClass, type || t(`${S}.change_badge`, "变更")));
            row.appendChild(el("span", "pending-summary-text", it.text || ""));
        }
        list.appendChild(row);
    }
    area.appendChild(list);

    // 展开查看原始数据（ops JSON）
    const rawObj = Array.isArray(p && p.ops) ? p.ops : (p && p.ops);
    area.appendChild(buildRawExpand(t(`${S}.expand_raw`, "展开查看原始数据"), rawObj));

    // 批准 / 拒绝
    area.appendChild(buildApproveRejectBar(p, { pendingId, onDone: () => { appliedPending = null; pending = null; renderPending(); } }));
}

// summary 条目类型徽标 CSS 类映射（新建/修改/删除 → 颜色差异化）。
function summaryBadgeClass(type) {
    if (type === "新建" || type === "create" || type === "new") return "success";
    if (type === "修改" || type === "update" || type === "modify") return "primary";
    if (type === "删除" || type === "delete" || type === "remove") return "danger";
    if (type === "启用" || type === "enable") return "success";
    if (type === "停用" || type === "disable") return "warning";
    return "primary";
}

// 旧结构（无 summary）预览卡：保持 v0.1.x 原样渲染（preview/apply 老流程兼容）。
function renderLegacyPending() {
    const area = document.getElementById("assistantPending");
    area.replaceChildren();
    const notApplied = pending && pendingOpsList(pending).length > 0;
    const appliedShown = !notApplied && appliedPending;

    if (!notApplied && !appliedShown) {
        area.style.display = "none";
        return;
    }
    area.style.display = "block";

    const title = el("div", "pending-title",
        notApplied ? t(`${S}.pending_title`, "📥 待应用的更改（预览）")
                   : t(`${S}.applied_title`, "✅ 已应用（可撤销）"));
    area.appendChild(title);

    const ops = notApplied ? pendingOpsList(pending) : pendingOpsList(appliedPending);
    for (const op of ops) area.appendChild(pendingOpCard(op));

    if (notApplied) {
        const bar = el("div", "chat-pending-actions");
        const applyBtn = el("button", "btn btn-primary btn-sm", t(`${S}.apply`, "✅ 应用修改"));
        const rollbackBtn = el("button", "btn btn-ghost btn-sm", t(`${S}.rollback`, "↩ 撤销修改"));
        applyBtn.addEventListener("click", () => doApply(applyBtn, rollbackBtn));
        rollbackBtn.addEventListener("click", () => doRollback(applyBtn, rollbackBtn));
        bar.appendChild(applyBtn);
        bar.appendChild(rollbackBtn);
        area.appendChild(bar);
    } else if (appliedShown) {
        const hint = el("div", "chat-pending-hint", t(`${S}.applied_hint`, "以下修改已写入配置。你可以在审计日志中查看，或通过对话让助手继续调整。"));
        area.appendChild(hint);
    }
}

function renderPending() {
    // v1.0.3：已有 summary 的新结构 → 人性化审批卡；否则回退旧预览渲染。
    if (pending && hasHumanizedPending(pending)) {
        renderHumanizedPending(pending, { pendingId: pending.pending_id });
        return;
    }
    renderLegacyPending();
}

async function doApprove(approveBtn, rejectBtn, { pendingId, onDone } = {}) {
    approveBtn.disabled = true; rejectBtn.disabled = true;
    try {
        const body = pendingId ? { pending_id: pendingId } : {};
        const res = await bridge.apiPost("agent/approve", body);
        if (res && typeof res === "object" && res.ok === false) {
            showToast(res.error || t(`${S}.approve_fail`, "批准失败"), "error");
        } else {
            const applied = (res && typeof res === "object" && res.applied != null) ? res.applied : 0;
            showToast(t(`${S}.approved`, "已批准生效") + (Number(applied) > 0 ? `（${applied} 项）` : ""), "success");
        }
    } catch (e) {
        showToast(e.message || t("pages.model-morph.common.error", "请求失败"), "error");
    } finally {
        pending = null;
        appliedPending = null;
        if (typeof onDone === "function") onDone();
        else renderPending();
        approveBtn.disabled = false; rejectBtn.disabled = false;
    }
}

async function doReject(approveBtn, rejectBtn, { pendingId, onDone } = {}) {
    approveBtn.disabled = true; rejectBtn.disabled = true;
    try {
        const body = pendingId ? { pending_id: pendingId } : {};
        const res = await bridge.apiPost("agent/reject", body);
        if (res && typeof res === "object" && res.ok === false) {
            showToast(res.error || t(`${S}.reject_fail`, "拒绝失败"), "error");
        } else {
            showToast(t(`${S}.rejected`, "已拒绝该更改"), "success");
        }
    } catch (e) {
        showToast(e.message || t("pages.model-morph.common.error", "请求失败"), "error");
    } finally {
        pending = null;
        appliedPending = null;
        if (typeof onDone === "function") onDone();
        else renderPending();
        approveBtn.disabled = false; rejectBtn.disabled = false;
    }
}

async function doApply(applyBtn, rollbackBtn) {
    applyBtn.disabled = true; rollbackBtn.disabled = true;
    try {
        const res = await bridge.apiPost("agent/apply", {});
        if (res && typeof res === "object" && res.ok === false) {
            showToast(res.error || t(`${S}.apply_fail`, "应用失败"), "error");
        } else {
            showToast(t(`${S}.applied`, "修改已应用，可撤销"), "success");
        }
    } catch (e) {
        showToast(e.message || t("pages.model-morph.common.error", "请求失败"), "error");
    } finally {
        appliedPending = pending; // 已应用 → 展示为可撤销态
        pending = null;
        renderPending();
        applyBtn.disabled = false; rollbackBtn.disabled = false;
    }
}

async function doRollback(applyBtn, rollbackBtn) {
    applyBtn.disabled = true; rollbackBtn.disabled = true;
    try {
        const res = await bridge.apiPost("agent/rollback", {});
        if (res && typeof res === "object" && res.ok === false) {
            showToast(res.error || t(`${S}.rollback_fail`, "撤销失败"), "error");
        } else {
            showToast(t(`${S}.rolled_back`, "已撤销修改"), "success");
        }
    } catch (e) {
        showToast(e.message || t("pages.model-morph.common.error", "请求失败"), "error");
    } finally {
        pending = null;
        appliedPending = null;
        renderPending();
        applyBtn.disabled = false; rollbackBtn.disabled = false;
    }
}

// ========== 聊天区 ==========
function renderMessages(msgs) {
    const body = document.getElementById("assistantBody");
    body.replaceChildren();
    const list = Array.isArray(msgs) ? msgs : [];
    if (!list.length) {
        body.appendChild(el("div", "chat-welcome", t(`${S}.empty_history`, "当前会话暂无消息，输入配置需求开始对话。")));
        body.scrollTop = 0;
        return;
    }
    for (const m of list) {
        const role = m && m.role === "user" ? "user" : "assistant";
        // 历史消息：assistant 一律走 Markdown 渲染；user 保持纯文本。
        body.appendChild(bubble(role, (m && m.content) || "", { markdown: role !== "user" }));
    }
    body.scrollTop = body.scrollHeight;
}

function renderWelcome() {
    const body = document.getElementById("assistantBody");
    body.replaceChildren();
    body.appendChild(el("div", "chat-welcome", t(`${S}.welcome`, "你好，我是模型调度配置助手。直接用中文告诉我你想怎么调，例如下方示例需求。")));
    renderChips();
    body.scrollTop = 0;
}

// ========== 侧栏 ==========
function renderSidebar() {
    const listEl = document.getElementById("assistantConvList");
    listEl.replaceChildren();
    if (!convList.length) {
        listEl.appendChild(el("div", "conv-empty", t(`${S}.no_conversations`, "暂无会话")));
        return;
    }
    for (const conv of convList) {
        listEl.appendChild(convItem(conv));
    }
}

function onSelectConv(cid) {
    if (cid === currentCid) return;
    selectConversation(cid);
}

function convItem(conv) {
    const item = el("div", "conv-item" + (conv.id === currentCid ? " active" : ""));
    // 点击条目本身 → 切换会话
    item.addEventListener("click", (e) => {
        if (e.target && e.target.closest(".conv-delete")) return; // 删除按钮独占
        onSelectConv(conv.id);
    });

    const title = el("div", "conv-title", conv.title || t(`${S}.no_title`, "未命名会话"));
    item.appendChild(title);

    const meta = el("div", "conv-meta");
    meta.appendChild(el("span", "conv-count",
        `${t(`${S}.message_count`, "消息")}：${conv.message_count || 0}`));
    meta.appendChild(el("span", "conv-time",
        `${t(`${S}.updated_at`, "更新")}：${formatTime(conv.updated_at)}`));
    meta.appendChild(el("span", "conv-count",
        `${t(`${S}.created_at`, "创建")}：${formatTime(conv.created_at)}`));
    meta.appendChild(el("span", "conv-preview",
        `${t(`${S}.last_preview`, "预览")}：${((conv.last_preview || "").slice(0, 40)) || "—"}`));
    item.appendChild(meta);

    const del = el("button", "conv-delete", t(`${S}.delete_conversation`, "删除"));
    del.type = "button";
    del.title = t(`${S}.delete_conversation`, "删除");
    del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteConversation(conv.id);
    });
    item.appendChild(del);
    return item;
}

// ========== 会话操作 ==========
async function selectConversation(cid) {
    if (busy) return;
    try {
        const conv = await bridge.apiGet("agent/conversations", { id: cid });
        if (!conv || conv.id !== cid) {
            showToast(t(`${S}.error`, "请求失败"), "error");
            return;
        }
        currentCid = conv.id;
        renderMessages(conv.messages);
        renderSidebar();
        refreshPending();
    } catch (e) {
        showToast(e.message || t(`${S}.error`, "请求失败"), "error");
    }
}

async function refreshPending() {
    // 切换会话后重新拉取全局 pending 预览（v0.1.7 现状：agent/pending 为全局单份）
    try {
        const p = await bridge.apiGet("agent/pending");
        if (hasPendingContent(p)) {
            pending = p;
            appliedPending = null;
        } else {
            pending = null;
        }
    } catch (e) {
        pending = null;
    }
    renderPending();
}

async function deleteConversation(cid) {
    const ok = await confirmDialog(
        t(`${S}.confirm_delete_conversation`, "确认删除该会话及其全部消息？"),
        { title: t(`${S}.delete_conversation`, "删除会话") },
    );
    if (!ok) return;
    try {
        const res = await bridge.apiPost("agent/conversations/delete", { id: cid });
        if (res && typeof res === "object" && res.ok === false) {
            showToast(res.error || t(`${S}.error`, "请求失败"), "error");
            return;
        }
        showToast(t(`${S}.conversation_deleted`, "会话已删除"), "success");
        if (cid === currentCid) {
            currentCid = "";
            pending = null;
            appliedPending = null;
            renderWelcome();
            renderPending();
        }
        await loadSidebar();
    } catch (e) {
        showToast(e.message || t(`${S}.error`, "请求失败"), "error");
    }
}

function newConversation() {
    if (busy) return; // 流式/请求进行中禁止新建：否则清空正在渲染的流式气泡 → 幽灵节点
    currentCid = "";
    pending = null;
    appliedPending = null;
    const input = document.getElementById("assistantInput");
    if (input) input.value = "";
    renderWelcome();
    renderPending();
    renderSidebar();
}

// ========== 流式渲染辅助（rAF 节流 + replaceChildren 增量重渲染） ==========
// 维护累计文本，用 requestAnimationFrame 把累积文本全量渲染进 assistant 气泡容器，
// 避免每个 delta 帧都做一次完整 DOM 重建而卡顿。
const streamThrottle = {
    container: null,      // assistant 气泡的 .chat-msg-md 容器（当前流）
    pendingText: "",      // 未刷新的累积文本
    textCurrent: "",      // 已渲染文本（用于判断是否有新内容）
    rafId: 0,             // rAF 句柄，>0 表示已排入一帧
};

function scheduleStreamRender() {
    if (streamThrottle.rafId) return; // 已有排定的一帧
    streamThrottle.rafId = requestAnimationFrame(() => {
        streamThrottle.rafId = 0;
        const c = streamThrottle.container;
        if (!c) return;
        // 只有当累积文本与已渲染文本不同才重渲染（全量替换最简、最稳）
        const text = streamThrottle.pendingText;
        if (text !== streamThrottle.textCurrent) {
            c.replaceChildren(renderMarkdown(text));
            streamThrottle.textCurrent = text;
            // 自动滚底（父容器 .chat-body 滚动到最下）
            const body = document.getElementById("assistantBody");
            if (body) body.scrollTop = body.scrollHeight;
        }
    });
}

// 把累计文本渲染进指定容器（供 done/finish 最终一致性渲染）。
function appendStreamBubble(container, text) {
    // container 是 assistant 气泡的 message 节点（.chat-msg）
    if (!container) return;
    const md = container.querySelector(".chat-msg-md");
    if (md) {
        md.replaceChildren(renderMarkdown(text));
    } else {
        // 兼容无 md 容器的气泡
        container.replaceChildren(renderMarkdown(text));
    }
}

// 清空流式节流状态（结束或错误时调用）。
function resetStreamThrottle() {
    if (streamThrottle.rafId) {
        cancelAnimationFrame(streamThrottle.rafId);
        streamThrottle.rafId = 0;
    }
    streamThrottle.container = null;
    streamThrottle.pendingText = "";
    streamThrottle.textCurrent = "";
}

// 挂载一个新的流式 assistant 气泡容器。
function startStreamBubble(container) {
    resetStreamThrottle();
    streamThrottle.container = container;
    streamThrottle.pendingText = "";
}

// ========== 发送 ==========
async function send() {
    const input = document.getElementById("assistantInput");
    const text = input.value.trim();
    if (!text || busy) return;

    // 跨插件页切回后：若后端报告仍有生成任务进行中/未收尾，则拦截发送，
    // 避免并发打断「断流续跑」的写回。接口未实现/失败时静默放行。
    const ts = await checkAgentTaskStatus();
    if (ts.ok && ts.running) {
        showToast(t(`${S}.task_interrupted_toast`, "检测到生成任务进行中或被中断，请稍候再试"), "warning");
        return;
    }

    // 存在未决 pending → confirmDialog 提醒
    if (hasPendingContent(pending)) {
        const ok = await confirmDialog(
            t(`${S}.confirm_pending`, "有未应用的更改，继续对话将保留预览，是否继续？"),
            { title: t(`${S}.pending_title`, "待应用更改"), danger: false },
        );
        if (!ok) return;
    }

    const body = document.getElementById("assistantBody");
    body.appendChild(bubble("user", text));
    input.value = "";
    busy = true;
    document.getElementById("assistantSend").disabled = true;

    // 流式判定：仅当文本不长且后端支持流式时才走 SSE；否则回退非流式 POST。
    const streaming = text.length <= 2000;
    if (!streaming) {
        await postNonStreaming(body, text);
        busy = false;
        document.getElementById("assistantSend").disabled = false;
        body.scrollTop = body.scrollHeight;
        return;
    }
    await postStreaming(body, text);
    busy = false;
    document.getElementById("assistantSend").disabled = false;
    body.scrollTop = body.scrollHeight;
}

// ========== 非流式 POST 路径（保留原实现，长文/不可用流式时兜底） ==========
async function postNonStreaming(body, text) {
    const payload = currentCid
        ? { conversation_id: currentCid, content: text }
        : { content: text };
    const thinking = el("div", "chat-row chat-row-assistant");
    thinking.appendChild(el("div", "chat-msg chat-msg-assistant thinking", t(`${S}.thinking`, "正在思考…")));
    body.appendChild(thinking);
    body.scrollTop = body.scrollHeight;

    try {
        const res = await bridge.apiPost("agent/chat", payload);
        thinking.remove();
        if (res && typeof res === "object" && res.error) {
            const errText = res.error || t(`${S}.error`, "请求失败");
            body.appendChild(bubble("assistant", errText, { markdown: true }));
            showToast(errText, "error");
        } else {
            // 以返回值 messages 为准渲染（服务端已写回 user + assistant 消息）
            const reply = (res && res.reply) || "";
            if (res && res.conversation_id) currentCid = res.conversation_id;
            if (res && Array.isArray(res.messages)) {
                renderMessages(res.messages);
            } else {
                // 兼容：无 messages 时手动补充分支
                body.appendChild(bubble("assistant", reply, { markdown: true }));
            }
            // pending：非空才渲染预览卡（兼容 summary 新结构与旧 ops/preview）
            if (res && res.pending && hasPendingContent(res.pending)) {
                pending = res.pending;
                appliedPending = null;
            } else {
                pending = null;
            }
            renderPending();
            await loadSidebar();
        }
        body.scrollTop = body.scrollHeight;
    } catch (e) {
        thinking.remove();
        const errText = (e && e.message) || t(`${S}.error`, "请求失败");
        body.appendChild(bubble("assistant", errText, { markdown: true }));
        showToast(errText, "error");
        body.scrollTop = body.scrollHeight;
    }
}

// ========== SSE 流式路径（agent/chat/stream） ==========
// 帧分发：meta（会话元信息）→ delta（文本增量）→ tool（工具提示）→ done/finish（收尾）→ error。
// 状态机要点（v1.0.2 修复）：
// - 订阅后**等待流结束**（done/finish/error/断连）才返回，期间 busy 保持、输入框禁用，
//   杜绝并发对话复用模块级 streamThrottle 单例导致的「跨轮文本串扰」；
// - finish 帧放行（即使 done 已置 streamDone），保证 pending 预览卡不丢失；
// - 最终渲染优先使用流式累积文本（覆盖模型在工具调用前输出的解释文本），
//   与后端 done.reply（累积正文）保持一致。
async function postStreaming(body, text) {
    // 1) 创建 assistant 气泡 + 「正在思考…」占位
    const payload = currentCid
        ? { content: text, conversation_id: currentCid }
        : { content: text };

    const row = el("div", "chat-row chat-row-assistant");
    const msg = el("div", "chat-msg chat-msg-assistant chat-msg-md");
    const thinking = el("div", "thinking", t(`${S}.thinking`, "正在思考…"));
    msg.appendChild(thinking);
    row.appendChild(msg);
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;

    // 已累积的流式文本；tool 提示单独一个 span（不影响正文容器）
    let accText = "";
    let streamDone = false;      // 防止结束后重复处理
    let toolHintEl = null;
    // 流结束信号：done / finish / error / 断连 时 resolve，postStreaming 等待它，
    // 保证一轮流式对话期间 busy 保持（防止并发复用 streamThrottle 单例）。
    let settleStream;
    const streamFinished = new Promise((resolve) => { settleStream = resolve; });

    // 无条件绑定当前气泡（重置上一轮可能残留的节流容器）。
    startStreamBubble(msg);
    // 置位流式进行中：此后若用户切页再切回（app.js 每次切入都调 load()），
    // load() 会跳过聊天区重建，保证 delta 继续写入仍在文档中的本气泡。
    streamingActive = true;

    const endStream = () => {
        streamingActive = false;
        resetStreamThrottle();
        streamDone = true;
        // 清理 tool 提示
        if (toolHintEl) { toolHintEl.remove(); toolHintEl = null; }
        body.scrollTop = body.scrollHeight;
        // 注意：不在此处 settle streamFinished —— busy 须保持到收尾渲染/幽灵自愈完成，
        // 由 handleEnd（finally）/ handleError / 订阅失败兜底在完成后放行，防止新 send
        // 打断正在进行的重建（详见 handleEnd 中的自愈注释）。
    };

    const showToolHint = (name) => {
        const label = t(`${S}.tool_using`, "正在使用工具 …");
        if (toolHintEl) toolHintEl.remove();
        // tool_using 模板：中文「正在使用工具 xxx…」；支持 i18n 键带占位
        toolHintEl = el("span", "chat-tool-hint", label + " " + (name || ""));
        msg.appendChild(toolHintEl);
        body.scrollTop = body.scrollHeight;
    };

    const clearThinking = () => {
        thinking.remove();
    };

    const handleDelta = (d) => {
        if (streamDone) return;
        const piece = (d && typeof d.text === "string") ? d.text : "";
        if (!piece) return;
        accText += piece;
        clearThinking();
        // 确保节流容器始终指向当前气泡（防御跨轮残留）。
        if (streamThrottle.container !== msg) startStreamBubble(msg);
        streamThrottle.pendingText = accText;
        scheduleStreamRender();
    };

    // 幽灵节点自愈：流式收尾时气泡已不在文档（流式中被 newConversation/deleteConversation
    // 或其它路径清空聊天区）→ 重建聊天区并重拉后端已写回的会话消息，避免回复在前端
    // 永久丢失。幂等：done 与 finish 双帧都会触发收尾，healed 保证只重建一次。
    let healed = false;
    const healGhostStream = async (finalText, userText) => {
        if (healed) return;
        healed = true;
        const cid = currentCid;
        await load(); // streamingActive 已由 endStream() 置 false → 正常重建骨架（欢迎页+侧栏+时区）
        const bodyEl = document.getElementById("assistantBody");
        if (cid) {
            try {
                const conv = await bridge.apiGet("agent/conversations", { id: cid });
                if (conv && conv.id === cid) {
                    currentCid = conv.id;
                    renderMessages(conv.messages); // 后端 ChatStore 已写回完整回复
                    renderSidebar();
                    refreshPending();
                    return;
                }
            } catch (e) { /* 拉取失败 → 走下方气泡兜底 */ }
        }
        // 兜底（新会话未建 / 会话已删除 / 拉取失败）：user + assistant 两气泡直渲当前视图，
        // 与 send() 的追加渲染一致；完整回复仍可在侧栏会话列表中打开。
        if (userText) bodyEl.appendChild(bubble("user", userText));
        bodyEl.appendChild(bubble("assistant", finalText || t(`${S}.na`, "—"), { markdown: true }));
        bodyEl.scrollTop = bodyEl.scrollHeight;
        refreshPending();
    };

    // 收尾：最终一致性渲染（不动 pending，pending 由 onMessage 的 done/finish 分支管理）。
    // settle 放在 finally：busy 保持到收尾渲染（含幽灵自愈）完成，杜绝收尾期间新 send
    // 打断重建导致再次丢回复。
    const handleEnd = async (data) => {
        try {
            endStream();
            clearThinking();
            // 优先流式累积文本（覆盖工具调用前的解释文本），与后端 done.reply 保持一致。
            const reply = (data && typeof data.reply === "string") ? data.reply : "";
            const finalText = accText || reply;
            if (!msg.isConnected) {
                // 气泡已被移除（切页后 load() 曾清空 / 点击新对话等）→ 自愈重建。
                await healGhostStream(finalText, text);
                return;
            }
            if (finalText) {
                msg.replaceChildren(renderMarkdown(finalText));
                streamThrottle.textCurrent = finalText;
            }
            await loadSidebar();
        } finally {
            settleStream();
        }
    };

    const handleError = (errText) => {
        endStream();
        clearThinking();
        const errTxt = errText || t(`${S}.stream_error`, "连接中断，请重试");
        if (!msg.isConnected) {
            // 幽灵节点：直接以 user + 错误气泡重建到当前 DOM（保持可见反馈，不丢上下文）。
            const bodyEl = document.getElementById("assistantBody");
            if (text) bodyEl.appendChild(bubble("user", text));
            bodyEl.appendChild(bubble("assistant", errTxt, { markdown: true }));
            bodyEl.scrollTop = bodyEl.scrollHeight;
        } else {
            msg.replaceChildren(renderMarkdown(errTxt));
        }
        showToast(errTxt, "error");
        settleStream();
    };

    const handlers = {
        onMessage(msgEvt) {
            const data = msgEvt && msgEvt.parsed;
            if (!data || typeof data !== "object") return;
            const type = data.type;
            // finish 帧放行（即使 done 已置 streamDone），保证 pending 预览卡不丢失。
            if (streamDone && type !== "finish") return;
            if (type === "meta") {
                if (data.conversation_id) currentCid = data.conversation_id;
            } else if (type === "delta") {
                handleDelta(data);
            } else if (type === "tool") {
                if (data && (data.name || data.args)) {
                    showToolHint(data.name || "");
                }
            } else if (type === "done") {
                if (data && data.conversation_id) currentCid = data.conversation_id;
                // v1.0.3：done 帧可能携带 pending（{pending_id, summary[]} 新结构）；
                // 有内容则展示人性化审批卡，否则清空避免残留旧预览。
                if (data && data.pending && hasPendingContent(data.pending)) {
                    pending = data.pending;
                    appliedPending = null;
                } else {
                    pending = null;
                }
                renderPending();
                handleEnd(data).then(() => {});
            } else if (type === "finish") {
                if (data && data.conversation_id) currentCid = data.conversation_id;
                if (data && data.pending && hasPendingContent(data.pending)) {
                    pending = data.pending;
                    appliedPending = null;
                } else {
                    pending = null;
                }
                renderPending();
                handleEnd(data).then(() => {});
            } else if (type === "error") {
                handleError(data.message);
            }
        },
        onError(err) {
            // HTTP 非 2xx / 连接中断。已正常收尾（done/finish）后的传输层错误不再覆盖回复。
            if (streamDone) return;
            handleError((err && err.message) || t(`${S}.stream_error`, "连接中断，请重试"));
        },
    };

    try {
        await bridge.subscribeSSE("agent/chat/stream", handlers, payload);
        // 等待流真正结束（done/finish/error/断连），期间保持 busy。
        await streamFinished;
    } catch (e) {
        // subscribeSSE reject：回退到非流式 POST（原样可用）。
        // 注意：这里只 endStream()（置 streamingActive=false / 清理节流），不调用
        // handleError（它会 settle 提前放行 busy）；回退完成后统一 settle，保证
        // busy 在整个回退期间保持。
        if (streamDone) return;
        endStream();
        showToast(t(`${S}.fallback_post`, "已切换为普通模式"), "error");
        try {
            msg.remove();
            await postNonStreaming(body, text);
        } catch (e2) {
            // 兜底：不再崩溃
        }
        settleStream();
    }
}

// ========== load / bind ==========
function renderChips() {
    const chipBar = document.getElementById("assistantChips");
    chipBar.replaceChildren();
    for (const key of CHIPS) {
        const chip = el("button", "chat-chip", t(`${S}.${key.split(".").pop()}`, key));
        chip.addEventListener("click", () => {
            const input = document.getElementById("assistantInput");
            input.value = chip.textContent;
            input.focus();
        });
        chipBar.appendChild(chip);
    }
}

async function loadSidebar() {
    try {
        const data = await bridge.apiGet("agent/conversations");
        convList = Array.isArray(data) ? data : (data && Array.isArray(data.conversations) ? data.conversations : []);
    } catch (e) {
        convList = [];
    }
    renderSidebar();
}

// 仅刷新「当前调度时区」显示（load() 两条路径复用）。
async function refreshRuntimeTz(tzEl) {
    try {
        const rt = await bridge.apiGet("runtime");
        const tz = rt && rt.timezone ? rt.timezone : "";
        tzEl.textContent = t(`${S}.timezone_label`, "当前调度时区") + "：" + (tz || t(`${S}.na`, "—"));
    } catch (e) {
        tzEl.textContent = t(`${S}.timezone_label`, "当前调度时区") + "：" + t(`${S}.na`, "—");
    }
}

// ========== 跨插件页切回的生成任务状态探测 ==========
// 场景：用户在 Model Morph 里发起流式对话后，切到 AstrBot「其它插件页」再切回。
// 该切换会卸载整个插件页 iframe、abort 正在进行的 SSE（见 PluginPagePage.vue
// onBeforeUnmount → cleanupSSEConnections），前端状态随 iframe 销毁归零，后端生成
// 若无「断流续跑」则不会写回 assistant 消息 → 回复被吞。
// 本前端配合后端（主 Agent 分派）新增的 `GET agent/task-status` 做「进入时校验」，
// 契约返回 { running, cid, started_at }：
//   running=true  → 有生成任务在后台续跑/未收尾 → 提示用户等待，并尽量保留 currentCid
//   running=false / 请求失败 / 接口未实现 → 走原有正常逻辑（静默降级，不报错）
// 返回值统一为 { ok, running, cid }（cid 可空）。
function checkAgentTaskStatus() {
    // 后端接口可能尚未上线（404/405）或网络异常：一律静默返回，绝不阻断 UI。
    return bridge.apiGet("agent/task-status")
        .then((r) => ({ ok: true, running: !!(r && r.running), cid: (r && r.cid) || "" }))
        .catch(() => ({ ok: false, running: false, cid: "" }));
}

// 在聊天区顶部插入「生成任务进行中/被中断」提示条（防止用户误以为无响应而重复发送）。
// 幂等：每次调用先清掉旧的再插。返回元素供 send() 判断/移除。
function renderAgentTaskBar(show) {
    const body = document.getElementById("assistantBody");
    let bar = document.getElementById("assistantTaskBar");
    if (bar) { bar.remove(); bar = null; }
    if (!show || !body) return null;
    bar = el("div", "chat-welcome chat-task-bar", t(`${S}.task_interrupted`, "检测到上一轮 AI 生成被中断或仍在进行，完成后可在会话历史中查看，请稍候。"));
    bar.id = "assistantTaskBar";
    body.insertBefore(bar, body.firstChild);
    return bar;
}

async function load() {
    const body = document.getElementById("assistantBody");
    const tzEl = document.getElementById("assistantTz");

    // 流式对话进行中（如切到其它页再切回，app.js 每次切入都会调用 load()）：
    // 不清空聊天区、不重置会话状态。否则正在渲染的流式气泡会被 replaceChildren()
    // 移出文档，后续 delta 继续写入已脱离文档的幽灵节点，done/finish 也只更新该幽灵
    // 节点 → 前端永久丢失完整回复（后端 ChatStore 已写回，但前端不会重新拉取）。
    // 这里仅刷新侧栏与时区，保留现有 DOM 让流式气泡继续渲染，收尾由 postStreaming
    // 的 handleEnd/handleError（含幽灵自愈）完成。
    if (streamingActive) {
        renderSidebar();
        await loadSidebar();
        await refreshRuntimeTz(tzEl);
        return;
    }

    // 聊天骨架（首载 / 无流式进行）
    body.replaceChildren();
    currentCid = "";
    pending = null;
    appliedPending = null;
    body.appendChild(el("div", "chat-welcome", t(`${S}.welcome`, "你好，我是模型调度配置助手。直接用中文告诉我你想怎么调，例如下方示例需求。")));
    renderChips();
    renderPending();
    renderSidebar();

    // 跨插件页切回：探测是否有「生成进行中/被中断且未写回」的任务，有则顶部提示。
    // 接口未实现/失败时静默跳过（不阻断 UI）。
    const ts = await checkAgentTaskStatus();
    if (ts.ok && ts.running) {
        // 契约：任务在后台续跑。若有 cid 则保留当前会话上下文（供侧栏关联/后续展示），
        // 否则保持「新对话」态；不重置为其它状态。
        if (ts.cid) currentCid = ts.cid;
        renderAgentTaskBar(true);
        showToast(t(`${S}.task_interrupted_toast`, "检测到生成任务进行中或被中断，请稍候再试"), "warning");
    } else {
        renderAgentTaskBar(false);
    }

    // 拉取侧栏会话列表（渲染列表；无会话时保持在「新对话」态）
    await loadSidebar();

    await refreshRuntimeTz(tzEl);
}

function handleEnter(e) {
    // Ctrl/Meta + Enter 发送；纯 Enter 换行
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        send();
    }
}

function bind() {
    document.getElementById("assistantSend").addEventListener("click", send);
    document.getElementById("assistantInput").addEventListener("keydown", handleEnter);
    document.getElementById("assistantNewConv").addEventListener("click", newConversation);
}

export { load, bind };
