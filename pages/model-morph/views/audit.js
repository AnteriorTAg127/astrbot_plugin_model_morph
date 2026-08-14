// ==========================================================================
// Model Morph · 审计日志视图（views/audit.js）
// apiGet("audit", {source, action, limit:200}) 表格：时间/操作者/来源/动作/目标/结果/详情。
// 来源下拉 + 动作输入(auditAction) 筛选 + [刷新] + [清空](confirmDialog → apiPost("audit/clear"))。
// 详情单元格：有 before/after 时追加「展开」按钮 → openAuditDetail 模态。
// 异步竞态用递增序号丢弃过期响应。全部动态文本 textContent / el()，防 XSS。
// 路由：GET audit(查询 source/limit) │ POST audit/clear（与模块 T6 注册一致）。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";

const S = "pages.model-morph.audit";

let cache = [];
let seq = 0;

// result → 徽章样式/文案
function resultBadge(r) {
    const map = {
        success: ["badge success", "audit.result_success"],
        failed: ["badge danger", "audit.result_failed"],
        preview: ["badge primary", "audit.result_preview"],
        rollback: ["badge warning", "audit.result_rollback"],
    };
    const [cls, key] = map[r] || ["badge muted", "audit.result_other"];
    return el("span", cls, t(`pages.model-morph.${key}`, r || ""));
}

function summarizeDetail(e) {
    // before/after 等关键字段的简短摘要（避免超大对象重绘）
    if (e && (e.before !== undefined || e.after !== undefined)) {
        const b = e.before !== undefined ? JSON.stringify(e.before) : "—";
        const a = e.after !== undefined ? JSON.stringify(e.after) : "—";
        return `before: ${b.length > 60 ? b.slice(0, 60) + "…" : b} → after: ${a.length > 60 ? a.slice(0, 60) + "…" : a}`;
    }
    if (e && e.detail) return e.detail;
    return "";
}

function paint() {
    const body = document.getElementById("auditBody");
    const empty = document.getElementById("auditEmpty");
    body.replaceChildren();
    if (!cache.length) { empty.style.display = ""; return; }
    empty.style.display = "none";
    for (const e of cache) {
        const row = el("tr");
        row.appendChild(el("td", "mono", e.time || ""));
        row.appendChild(el("td", null, e.operator || ""));
        row.appendChild(el("td", null, e.source || ""));
        row.appendChild(el("td", "mono", e.action || ""));
        row.appendChild(el("td", "mono", e.target || ""));
        const resTd = el("td"); resTd.appendChild(resultBadge(e.result)); row.appendChild(resTd);
        const detailTd = el("td");
        detailTd.appendChild(el("span", null, summarizeDetail(e) || "—"));
        if (e.before !== undefined || e.after !== undefined) {
            const expand = el("button", "btn btn-ghost btn-sm", t(`${S}.expand`, "展开"));
            expand.type = "button";
            expand.addEventListener("click", () => openAuditDetail(e));
            detailTd.appendChild(expand);
        }
        row.appendChild(detailTd);
        body.appendChild(row);
    }
}

async function load() {
    const mySeq = ++seq;
    const source = document.getElementById("auditSource").value;
    const actionEl = document.getElementById("auditAction");
    const action = actionEl ? actionEl.value : "";
    const body = document.getElementById("auditBody");
    const lr = el("tr"); const td = el("td", "loading", t("pages.model-morph.common.loading", "加载中…"));
    td.colSpan = 7; lr.appendChild(td);
    body.replaceChildren(lr);
    const params = { limit: 200 };
    if (source) params.source = source;
    if (action) params.action = action;
    try {
        const data = await bridge.apiGet("audit", params);
        if (mySeq !== seq) return;
        // 兼容：后端可能返回 {entries:[...]} 或裸数组
        cache = Array.isArray(data) ? data : (data && Array.isArray(data.entries) ? data.entries : []);
        paint();
    } catch (e) {
        if (mySeq !== seq) return;
        showToast(e.message || "未知错误", "error");
    }
}

async function clearAudit() {
    const ok = await confirmDialog(
        t(`${S}.confirm_clear`, "确认清空全部审计日志？"),
        { title: t(`${S}.clear`, "清空审计"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("audit/clear", {});
        showToast(t(`${S}.cleared`, "已清空审计"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

function openAuditDetail(e) {
    const mask = el("div", "modal-mask");
    const card = el("div", "modal-card");
    card.appendChild(el("div", "modal-title", (e.action || "") + " · " + (e.target || "")));

    const kv = (label, value) => {
        const cell = el("div", "modal-block");
        cell.appendChild(el("div", "modal-label", label));
        const pre = document.createElement("pre");
        pre.className = "pending-op-pre mono";
        pre.textContent = value === undefined ? "—" : JSON.stringify(value, null, 2);
        cell.appendChild(pre);
        return cell;
    };
    card.appendChild(kv(t(`${S}.before`, "修改前"), e.before));
    card.appendChild(kv(t(`${S}.after`, "修改后"), e.after));

    const close = el("button", "btn btn-ghost", t(`${S}.close`, "关闭"));
    close.type = "button";
    close.addEventListener("click", () => mask.remove());
    card.appendChild(close);
    mask.appendChild(card);
    document.body.appendChild(mask);
}

function bind() {
    document.getElementById("auditRefresh").addEventListener("click", load);
    document.getElementById("auditClear").addEventListener("click", clearAudit);
    document.getElementById("auditSource").addEventListener("change", load);
    const actionEl = document.getElementById("auditAction");
    if (actionEl) actionEl.addEventListener("input", load);
}

export { load, bind };
