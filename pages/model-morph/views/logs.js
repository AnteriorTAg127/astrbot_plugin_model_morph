// ==========================================================================
// Model Morph · 日志视图（views/logs.js）
// 表格：时间 / umo / 类型 / 规则 / 旧→新 / 轮数 / 原因 + umo/level 筛选 + 清空。
// logs GET 走 query 参数（umo / level / limit）。异步竞态用递增序号丢弃过期响应。
// 全部动态文本走 textContent / el()，防 XSS；清空为破坏性操作 → confirmDialog()。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";

let cache = [];
let seq = 0;
let debounceTimer = null;

const TYPE_LABELS = { switch: "switch", reset: "reset", error: "error", lock: "lock", unlock: "unlock" };

function typeLabel(e) {
    return TYPE_LABELS[e.type] || e.type || "—";
}

function paint() {
    const body = document.getElementById("logsBody");
    const empty = document.getElementById("logsEmpty");
    body.replaceChildren();
    if (!cache.length) { empty.style.display = ""; return; }
    empty.style.display = "none";
    for (const e of cache) {
        const row = el("tr");
        row.appendChild(el("td", "mono", e.time || ""));
        row.appendChild(el("td", "mono", e.umo || ""));
        const typeTd = el("td");
        typeTd.appendChild(e.level === "error"
            ? el("span", "badge danger", "error")
            : el("span", "badge primary", typeLabel(e)));
        row.appendChild(typeTd);
        row.appendChild(el("td", null, e.rule || "—"));
        row.appendChild(el("td", "mono", (e.old || "—") + " → " + (e.new || "—")));
        row.appendChild(el("td", null, String(e.round ?? "")));
        row.appendChild(el("td", null, e.reason || ""));
        body.appendChild(row);
    }
}

async function load() {
    const mySeq = ++seq;
    const umo = document.getElementById("logsUmo").value.trim();
    const level = document.getElementById("logsLevel").value;
    const params = {};
    if (umo) params.umo = umo;
    if (level) params.level = level;
    const body = document.getElementById("logsBody");
    const lr = el("tr"); const td = el("td", "loading", t("pages.model-morph.common.loading", "加载中…"));
    td.colSpan = 7; lr.appendChild(td);
    body.replaceChildren(lr);
    try {
        const data = await bridge.apiGet("logs", params);
        if (mySeq !== seq) return;
        cache = Array.isArray(data) ? data : [];
        paint();
    } catch (e) {
        if (mySeq !== seq) return;
        showToast(e.message || "未知错误", "error");
    }
}

async function clearLogs() {
    const ok = await confirmDialog(
        t("pages.model-morph.logs.confirm_clear", "确认清空全部调度日志？"),
        { title: t("pages.model-morph.logs.clear", "清空日志"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("logs/clear", {});
        showToast(t("pages.model-morph.logs.clear", "已清空"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

function debounce(fn, ms) {
    return () => { clearTimeout(debounceTimer); debounceTimer = setTimeout(fn, ms); };
}

function bind() {
    document.getElementById("logsRefresh").addEventListener("click", load);
    document.getElementById("logsClear").addEventListener("click", clearLogs);
    document.getElementById("logsUmo").addEventListener("input", debounce(load, 250));
    document.getElementById("logsLevel").addEventListener("change", load);
}

export { load, bind };
