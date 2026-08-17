// ==========================================================================
// Model Morph · 总览视图（views/dashboard.js）
// 状态卡（enabled/debug/时区/Provider数/组数/规则数/生命周期数/会话数
//   + 时段规则数/校准中会话/全局默认生命周期）+ 最近切换 + 最近错误。
// 时段规则卡片包含 scope 摘要（限定:组/用户/会话）。
// 全部动态文本走 textContent / el()，防 XSS；异步竞态用递增序号丢弃过期响应。
// ==========================================================================
import { bridge, t, el, showToast } from "../common.js";

let renderSeq = 0;

// 状态卡配置：icon 类 / i18n 键 / 值提取
const STAT_META = [
    { cls: "stat-blue", icon: "🟢", key: "pages.model-morph.dashboard.enabled", get: (d) => badgeTxt(d.enabled, "pages.model-morph.common.enabled", "pages.model-morph.common.disabled") },
    { cls: "stat-purple", icon: "🐞", key: "pages.model-morph.dashboard.debug", get: (d) => badgeTxt(d.debug, "pages.model-morph.common.enabled", "pages.model-morph.common.disabled") },
    { cls: "stat-orange", icon: "🕐", key: "pages.model-morph.dashboard.timezone", get: (d) => d.timezone || t("pages.model-morph.common.none", "无") },
    { cls: "stat-green", icon: "🧩", key: "pages.model-morph.dashboard.provider_count", get: (d) => d.provider_count },
    { cls: "stat-blue", icon: "👥", key: "pages.model-morph.dashboard.group_count", get: (d) => d.group_count },
    { cls: "stat-purple", icon: "📐", key: "pages.model-morph.dashboard.rule_count", get: (d) => d.rule_count },
    { cls: "stat-green", icon: "🔁", key: "pages.model-morph.dashboard.lifecycle_count", get: (d) => d.lifecycle_count },
    { cls: "stat-purple", icon: "⏱️", key: "pages.model-morph.dashboard.temporal_rule_count", get: (d) => d.temporal_rule_count },
    { cls: "stat-orange", icon: "🧭", key: "pages.model-morph.dashboard.calibration_sessions", get: (d) => d.calibration_sessions },
    { cls: "stat-green", icon: "🔄", key: "pages.model-morph.dashboard.default_lifecycle", get: (d) => d.default_lifecycle_name || t("pages.model-morph.dashboard.no_default_lifecycle", "未设置") },
    { cls: "stat-orange", icon: "📡", key: "pages.model-morph.dashboard.session_count", get: (d) => d.session_count },
];

function badgeTxt(on, onKey, offKey) {
    return on ? t(onKey, "启用") : t(offKey, "禁用");
}

function buildStats(d) {
    const grid = document.getElementById("dashStats");
    grid.replaceChildren();
    for (const m of STAT_META) {
        const card = el("div", `stat-card ${m.cls}`);
        const icon = el("div", "stat-icon", m.icon);
        const body = el("div", "stat-body");
        body.appendChild(el("div", "stat-value", String(m.get(d) ?? "—")));
        body.appendChild(el("div", "stat-label", t(m.key, m.key.split(".").pop())));
        card.appendChild(icon);
        card.appendChild(body);
        grid.appendChild(card);
    }
}

// 最近切换表：表头 + 行（el() 构建）
function buildSwitchTable(switches) {
    const box = document.getElementById("dashSwitches");
    box.replaceChildren();
    const wrap = el("div", "table-wrap");
    const table = el("table", "table");
    const thead = el("thead");
    const tr = el("tr");
    for (const k of ["dashboard.time", "dashboard.umo", "dashboard.old_model", "dashboard.new_model", "dashboard.rule", "dashboard.round"]) {
        tr.appendChild(el("th", null, t(`pages.model-morph.${k}`, k.split(".")[1])));
    }
    thead.appendChild(tr);
    table.appendChild(thead);
    const tbody = el("tbody");
    const rows = switches || [];
    if (!rows.length) {
        const row = el("tr");
        const td = el("td", null, t("pages.model-morph.dashboard.no_switches", "暂无切换记录"));
        td.colSpan = 6;
        row.appendChild(td);
        tbody.appendChild(row);
    } else {
        for (const e of rows) {
            const row = el("tr");
            row.appendChild(el("td", "mono", e.time || ""));
            row.appendChild(el("td", "mono", e.umo || ""));
            row.appendChild(el("td", "mono", e.old || "—"));
            row.appendChild(el("td", "mono", e.new || "—"));
            row.appendChild(el("td", null, e.rule || "—"));
            row.appendChild(el("td", null, String(e.round ?? "")));
            tbody.appendChild(row);
        }
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);
}

// 最近错误表
function buildErrorTable(errors) {
    const box = document.getElementById("dashErrors");
    box.replaceChildren();
    const wrap = el("div", "table-wrap");
    const table = el("table", "table");
    const thead = el("thead");
    const tr = el("tr");
    for (const k of ["dashboard.time", "dashboard.umo", "dashboard.reason"]) {
        tr.appendChild(el("th", null, t(`pages.model-morph.${k}`, k.split(".")[1])));
    }
    thead.appendChild(tr);
    table.appendChild(thead);
    const tbody = el("tbody");
    const rows = errors || [];
    if (!rows.length) {
        const row = el("tr");
        const td = el("td", null, t("pages.model-morph.dashboard.no_errors", "暂无错误记录"));
        td.colSpan = 3;
        row.appendChild(td);
        tbody.appendChild(row);
    } else {
        for (const e of rows) {
            const row = el("tr");
            row.appendChild(el("td", "mono", e.time || ""));
            row.appendChild(el("td", "mono", e.umo || ""));
            row.appendChild(el("td", null, e.reason || ""));
            tbody.appendChild(row);
        }
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);
}

// 当前生效时段规则卡片：读 dashboard API 的 active_temporal_rules。// 后端引擎 dashboard() 输出扁平字段：{id,name,kind,group_id,source_provider,target_provider,
//       target_group,schedule_type,start,end,priority}（schedule 已展开为 *_start/*_end），
// 故此处读取 schedule_type / schedule_start / schedule_end。最多 20 条。
function buildTemporalRules(rules) {
    const box = document.getElementById("dashTemporal");
    box.replaceChildren();
    const list = Array.isArray(rules) ? rules : [];
    if (!list.length) {
        box.appendChild(el("p", "hint", t("pages.model-morph.dashboard.active_temporal_empty", "当前无生效时段规则")));
        return;
    }
    const wrap = el("div", "temporal-active-list");
    for (const r of list) {
        const item = el("div", "temporal-active-item");
        const left = el("div", "temporal-active-info");
        left.appendChild(el("span", "temporal-active-name", r.name || r.id || ""));
        left.appendChild(el("span", "temporal-active-kind badge muted", 
            r.kind === "group_switch"
                ? t("pages.model-morph.dashboard.kind_group_switch", "整组切换")
                : t("pages.model-morph.dashboard.kind_model_override", "模型替换")));
        item.appendChild(left);

        let dur = "";
        if (r.kind === "group_switch") {
            dur = `${r.group_id || ""} → ${r.target_group || ""}`;
        } else {
            dur = `${r.source_provider || ""} → ${r.target_provider || ""}`;
        }
        const timeTxt = [r.schedule_type, r.schedule_start, r.schedule_end].filter(Boolean).join(" ") || "";
        // scope 摘要：对象三键存在非空 → " · 限定:组x/用户y/会话z"（x/y/z 为非空长度，全空不加）
        let scopeTxt = "";
        if (r.scope && typeof r.scope === "object") {
            const cnt = (k) => {
                const v = r.scope[k];
                if (v == null) return 0;
                if (typeof v === "number") return v > 0 ? v : 0;
                if (Array.isArray(v)) return v.length;
                if (typeof v === "string") return v.trim() ? 1 : 0;
                return 0;
            };
            const g = cnt("groups") || cnt("group_ids") || 0;
            const u = cnt("users") || cnt("user_ids") || 0;
            const z = cnt("sessions") || cnt("session_ids") || 0;
            if (g || u || z) {
                const parts = [];
                if (g) parts.push("组" + g);
                if (u) parts.push("用户" + u);
                if (z) parts.push("会话" + z);
                scopeTxt = " · 限定:" + parts.join("/");
            }
        }
        const fullMeta = [dur, timeTxt, scopeTxt].filter(Boolean).join(" · ");
        item.appendChild(el("span", "temporal-active-time mono", fullMeta));
        item.appendChild(el("span", "temporal-active-prio mono", t("pages.model-morph.dashboard.priority_short", "P") + " " + (r.priority ?? "")));
        wrap.appendChild(item);
    }
    box.appendChild(wrap);
}

// 当前生效强锁摘要行（v1.0.3）。
// 无强锁 → 隐藏；有强锁 → 显示「当前生效强锁：umo → provider @ model」。
// force_lock 兼容两种形态：
//   - 字符串 "umo → provider @ model"（后端已格式化）
//   - 对象 {umo, provider_id, model}
function buildForceLock(forceLock) {
    const box = document.getElementById("dashForceLock");
    if (!box) return;
    if (!forceLock) { box.style.display = "none"; return; }
    let text = "";
    if (typeof forceLock === "string") {
        text = forceLock;
    } else if (forceLock && typeof forceLock === "object") {
        const umo = forceLock.umo || "";
        const p = forceLock.provider_id || "";
        const m = forceLock.model || "";
        const pair = (p && m) ? `${p} @ ${m}` : (p || m || "—");
        text = (umo ? `${umo} → ${pair}` : pair);
    } else {
        text = "—";
    }
    box.style.display = "flex";
    const icon = el("span", "force-lock-icon", "🔒");
    const body = el("span", "force-lock-body",
        t("pages.model-morph.dashboard.force_lock_label", "当前生效强锁") + "：" + text);
    box.replaceChildren();
    box.appendChild(icon);
    box.appendChild(body);
}

// 加载总览并渲染
async function load() {
    const seq = ++renderSeq;
    const statsBox = document.getElementById("dashStats");
    statsBox.replaceChildren(el("div", "loading", t("pages.model-morph.common.loading", "加载中…")));
    try {
        const d = await bridge.apiGet("dashboard");
        if (seq !== renderSeq) return; // 丢弃过期响应
        buildStats(d);
        buildForceLock(d.force_lock);
        buildTemporalRules(d.active_temporal_rules);
        buildSwitchTable(d.recent_switches);
        buildErrorTable(d.recent_errors);
    } catch (e) {
        if (seq !== renderSeq) return;
        statsBox.replaceChildren(el("div", "empty-state",
            t("pages.model-morph.common.error", "请求失败") + ": " + (e.message || "未知错误")));
        showToast((e.message || "未知错误"), "error");
    }
}

function bind() {
    document.getElementById("dashRefresh").addEventListener("click", load);
}

export { load, bind };
