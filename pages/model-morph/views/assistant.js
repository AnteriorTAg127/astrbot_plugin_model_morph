// ==========================================================================
// Model Morph · AI 配置助手（views/assistant.js）— 会话驱动（v0.1.8）
// 会话持久化：侧栏列出历史会话（标题/时间/创建/预览/条数/删除），聊天区渲染当前会话消息。
// - 侧栏：#assistantNewConv（新对话）+ #assistantConvList（会话列表）。
// - 聊天区：assistant/user 气泡，纯文本 + white-space:pre-wrap（不支持 markdown，防 XSS）。
// - pending 非空 → 聊天区下方「待应用更改」预览卡：逐条渲染 action/target/before/after/warnings
//   (before/after 用 <pre> + JSON.stringify(v,null,2) + textContent 赋值)。
// - [应用修改] agent/apply │ [撤销修改] agent/rollback │ 再次发送前若 pending 未决 → confirmDialog。
// 路由（与主 API 注册一致）：
//   GET agent/conversations(可选 ?id) │ POST agent/chat(content|conversation_id+content)
//   POST agent/conversations/delete{id} │ POST agent/apply │ POST agent/rollback │ GET agent/pending
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

// pending 结构：{pending_id, ops(真实操作), snapshot, preview(预览项[{action,target,before,after,warnings}])}
// 渲染预览卡优先用 pending.preview（含 before/after）；无 preview（旧数据）时回退 ops。
function pendingOpsList(p) {
    const src = p && Array.isArray(p.preview) && p.preview.length ? p.preview : (p && p.ops);
    return Array.isArray(src) ? src : [];
}

function renderPending() {
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
        // 未应用：显示 应用/撤销 按钮
        const bar = el("div", "chat-pending-actions");
        const applyBtn = el("button", "btn btn-primary btn-sm", t(`${S}.apply`, "✅ 应用修改"));
        const rollbackBtn = el("button", "btn btn-ghost btn-sm", t(`${S}.rollback`, "↩ 撤销修改"));
        applyBtn.addEventListener("click", () => doApply(applyBtn, rollbackBtn));
        rollbackBtn.addEventListener("click", () => doRollback(applyBtn, rollbackBtn));
        bar.appendChild(applyBtn);
        bar.appendChild(rollbackBtn);
        area.appendChild(bar);
    } else if (appliedShown) {
        // 已应用：提示可撤销
        const hint = el("div", "chat-pending-hint", t(`${S}.applied_hint`, "以下修改已写入配置。你可以在审计日志中查看，或通过对话让助手继续调整。"));
        area.appendChild(hint);
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
        if (p && pendingOpsList(p).length > 0) {
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

    // 存在未决 pending → confirmDialog 提醒
    if (pendingOpsList(pending).length > 0) {
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
            // pending：非空才渲染预览卡
            if (res && res.pending && pendingOpsList(res.pending).length > 0) {
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
    let streamDone = false;     // 防止结束后重复处理
    let toolHintEl = null;

    const endStream = (() => {
        resetStreamThrottle();
        streamDone = true;
        // 清理 tool 提示
        if (toolHintEl) { toolHintEl.remove(); toolHintEl = null; }
        body.scrollTop = body.scrollHeight;
    });

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
        accText += piece;
        clearThinking();
        // 先挂载节流容器（首次 delta 时创建 md 渲染容器）
        if (!streamThrottle.container) startStreamBubble(msg);
        // 只有容器指向当前气泡才累积渲染
        streamThrottle.pendingText = accText;
        scheduleStreamRender();
    };

    const handleEnd = async (data) => {
        endStream();
        clearThinking();
        // done 帧：用 data.reply 全量重渲染一次，保证与持久化一致
        const reply = (data && typeof data.reply === "string") ? data.reply : accText;
        if (!streamThrottle.container) startStreamBubble(msg);
        msg.replaceChildren(renderMarkdown(reply || accText));
        streamThrottle.textCurrent = reply || "";
        // pending
        if (data && data.pending && pendingOpsList(data.pending).length > 0) {
            pending = data.pending;
            appliedPending = null;
        } else {
            pending = null;
        }
        renderPending();
        await loadSidebar();
    };

    const handleError = (errText) => {
        endStream();
        clearThinking();
        const errTxt = errText || t(`${S}.stream_error`, "连接中断，请重试");
        msg.replaceChildren(renderMarkdown(errTxt));
        showToast(errTxt, "error");
    };

    const handlers = {
        onMessage(msgEvt) {
            if (streamDone) return;
            const data = msgEvt && msgEvt.parsed;
            if (!data || typeof data !== "object") return;
            const type = data.type;
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
                const pendingData = data && data.pending;
                if (pendingData && pendingOpsList(pendingData).length > 0) {
                    pending = pendingData;
                    appliedPending = null;
                } else {
                    pending = null;
                }
                renderPending();
                handleEnd(data).then(() => {});
            } else if (type === "finish") {
                if (data && data.conversation_id) currentCid = data.conversation_id;
                if (data && data.pending && pendingOpsList(data.pending).length > 0) {
                    pending = data.pending;
                    appliedPending = null;
                }
                renderPending();
                handleEnd(data).then(() => {});
            } else if (type === "error") {
                handleError(data.message);
            }
        },
        onError(err) {
            // HTTP 非 2xx / 连接中断
            handleError((err && err.message) || t(`${S}.stream_error`, "连接中断，请重试"));
        },
    };

    try {
        await bridge.subscribeSSE("agent/chat/stream", handlers, payload);
    } catch (e) {
        // subscribeSSE reject：回退到非流式 POST（原样可用）
        if (streamDone) return;
        handleError(t(`${S}.fallback_post`, "已切换为普通模式"));
        try {
            msg.remove();
            await postNonStreaming(body, text);
        } catch (e2) {
            // 兜底：不再崩溃
        }
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

async function load() {
    const body = document.getElementById("assistantBody");
    const tzEl = document.getElementById("assistantTz");
    // 聊天骨架（首载）
    body.replaceChildren();
    currentCid = "";
    pending = null;
    appliedPending = null;
    body.appendChild(el("div", "chat-welcome", t(`${S}.welcome`, "你好，我是模型调度配置助手。直接用中文告诉我你想怎么调，例如下方示例需求。")));
    renderChips();
    renderPending();
    renderSidebar();

    // 拉取侧栏会话列表（渲染列表；无会话时保持在「新对话」态）
    await loadSidebar();

    // 读取 runtime 显示当前调度时区
    try {
        const rt = await bridge.apiGet("runtime");
        const tz = rt && rt.timezone ? rt.timezone : "";
        tzEl.textContent = t(`${S}.timezone_label`, "当前调度时区") + "：" + (tz || t(`${S}.na`, "—"));
    } catch (e) {
        tzEl.textContent = t(`${S}.timezone_label`, "当前调度时区") + "：" + t(`${S}.na`, "—");
    }
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
