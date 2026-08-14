// ==========================================================================
// Model Morph · 时间调度规则视图（views/temporal.js）
// 列表（名称/类型/作用组/限定群组/替换/时间段/优先级/来源/启用/操作，共 10 列）
//   + 冲突横幅 + 新建/编辑面板。
// scope 二段式语义：scope={groups,users,sessions}，三键全空=全局规则；
// 限定命中的规则优先于全局规则生效。
// 数据来源：
//   - GET  temporal          → 全部时间调度规则（plugin.temporal.list_()，按优先级降序）
//   - POST validate (body {}) → 全量校验 + 冲突检测，返回 {ok,errors,warnings,conflicts}
//     其中 conflicts 来自 temporal.find_conflicts，每项
//     {a: 规则A id, b: 规则B id, group_id, source_provider, kind, note}
//     note ∈ {"priority_tie","shadowed"}。
//   - POST temporal/save / temporal/delete / temporal/toggle → 写操作后刷新列表+冲突。
// 提交的 rule 字段名严格对齐 normalize_temporal_rule（与 wizard.js 一致）：
//   {name, enabled, kind, group_id, source_provider, target_provider, target_group,
//    scope:{groups:[],users:[],sessions:[]}, schedule:{type,start,end,weekdays,date,timezone},
//    priority, metadata:{created_by:"manual", created_at:<ISO>, source:"manual"}}
// 全部动态文本 textContent / el() 防 XSS；删除走 confirmDialog()。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";
import { refData, buildProviderSelect, buildGroupSelect } from "./shared.js";

const S = "pages.model-morph.temporal";
const SCHEDULE_TYPES = ["always", "daily", "weekly", "date"];
const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];
const TZ_VALUES = ["Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore", "Asia/Kolkata", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "UTC", "GMT"];

let seq = 0;          // 异步竞态序号
let draft = null;     // 编辑中的规则草稿
let conflicts = [];   // 最近一次拉取的冲突列表
let rulesStore = [];  // 最近一次拉取的规则列表（供冲突名称解析）

// ========== 显示名解析（refData id → name，未解析则回退 id） ==========
function groupName(id) {
    if (!id) return "";
    const g = refData.groupOptions.find((x) => x.id === id);
    return g ? g.name : id;
}

function providerName(id) {
    if (!id) return "";
    const p = refData.providers.find((x) => x.id === id);
    return p ? (p.model || p.id) : id;
}

function kindLabel(kind) {
    return t(`${S}.${kind === "group_switch" ? "kind_group_switch" : "kind_model_override"}`, kind);
}

function stateBadge(on) {
    return on ? el("span", "badge success", t("pages.model-morph.common.enabled", "启用"))
              : el("span", "badge muted", t("pages.model-morph.common.disabled", "禁用"));
}

// 时间段展示：按 schedule.type 语义化。
function scheduleText(r) {
    const sch = r.schedule || {};
    const type = sch.type || "daily";
    const start = sch.start || "";
    const end = sch.end || "";
    const cross = !!start && !!end && end < start;
    const crossNote = cross ? " (" + t(`${S}.cross_midnight`, "跨午夜") + ")" : "";
    let text = "";
    if (type === "always") {
        text = t(`${S}.type_always`, "始终");
    } else {
        const range = (start ? start : "??:??") + " - " + (end ? end : "??:??");
        if (type === "date") {
            text = (sch.date || "????-??-??") + " " + range + crossNote;
        } else if (type === "weekly") {
            const days = (sch.weekdays || []).map((d) => String(d)).join(",");
            text = t(`${S}.type_weekly_range`, "每周") + (days ? " [" + days + "]" : " [?]") + " " + range + crossNote;
        } else {
            // daily
            const days = (sch.weekdays || []).length
                ? " (" + (sch.weekdays || []).map((d) => String(d)).join(",") + ")"
                : "";
            text = t(`${S}.type_daily_range`, "每天") + days + " " + range + crossNote;
        }
    }
    if (sch.timezone) text += " · " + sch.timezone;
    return text;
}

function scheduleCell(r) {
    const cell = el("td");
    cell.appendChild(el("span", null, scheduleText(r)));
    return cell;
}

// 替换列：model_override → 「源 → 目标」；group_switch → 「组 → 目标组」。
function replaceText(r) {
    if (r.kind === "group_switch") {
        return (groupName(r.group_id) || "—") + " → " + groupName(r.target_group);
    }
    return (providerName(r.source_provider) || "—") + " → " + providerName(r.target_provider);
}

// 限定群组列：scope={groups,users,sessions}，三键全空=全局规则；否则「组:x 用户:y 会话:z」。
function scopeText(r) {
    const s = (r.scope && typeof r.scope === "object") ? r.scope : {};
    const groups = Array.isArray(s.groups) ? s.groups : [];
    const users = Array.isArray(s.users) ? s.users : [];
    const sessions = Array.isArray(s.sessions) ? s.sessions : [];
    if (!groups.length && !users.length && !sessions.length) {
        return t(`${S}.scope_global`, "全局");
    }
    return "组:" + groups.length + " 用户:" + users.length + " 会话:" + sessions.length;
}

// 来源列：metadata.source → 向导 / 预设 / 手动 / 其他。
function sourceLabel(r) {
    const src = (r.metadata && r.metadata.source) || "";
    if (src === "wizard") return t(`${S}.source_wizard`, "向导");
    if (src === "preset") return t(`${S}.source_preset`, "预设");
    if (src === "manual") return t(`${S}.source_manual`, "手动");
    return "—";
}

// ========== 列表 ==========
function ruleActionsRow(r) {
    const box = el("div", "cell-actions");
    const edit = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.common.edit", "编辑"));
    edit.type = "button"; edit.addEventListener("click", () => openEditor(JSON.parse(JSON.stringify(r))));
    const togg = el("button", "btn btn-ghost btn-sm",
        r.enabled ? t("pages.model-morph.common.disabled", "停用") : t("pages.model-morph.common.enabled", "启用"));
    togg.type = "button"; togg.addEventListener("click", () => toggleRule(r));
    const del = el("button", "btn btn-danger btn-sm", t("pages.model-morph.common.delete", "删除"));
    del.type = "button"; del.addEventListener("click", () => removeRule(r));
    box.append(edit, togg, del);
    return box;
}

function paintList(rules) {
    const body = document.getElementById("temporalListBody");
    const empty = document.getElementById("temporalListEmpty");
    body.replaceChildren();
    if (!rules.length) { empty.style.display = ""; return; }
    empty.style.display = "none";
    for (const r of rules) {
        const row = el("tr");
        const nameCell = el("td");
        nameCell.appendChild(el("strong", "cell-name", r.name || r.id));
        nameCell.appendChild(el("div", "hint mono", r.id));
        row.appendChild(nameCell);
        row.appendChild(el("td")).appendChild(
            el("span", "badge " + (r.kind === "group_switch" ? "primary" : "warning"), kindLabel(r.kind))
        );
        row.appendChild(el("td", null, r.group_id ? groupName(r.group_id) : t(`${S}.global_scope`, "全局")));
        const scopeTd = el("td");
        scopeTd.appendChild(el("span", "scope-chip", scopeText(r)));
        row.appendChild(scopeTd);
        row.appendChild(el("td", null, replaceText(r)));
        row.appendChild(scheduleCell(r));
        row.appendChild(el("td", null, String(r.priority ?? 200)));
        row.appendChild(el("td", null, sourceLabel(r)));
        const enTd = el("td");
        enTd.appendChild(stateBadge(r.enabled));
        row.appendChild(enTd);
        row.appendChild(el("td")).appendChild(ruleActionsRow(r));
        body.appendChild(row);
    }
}

// 冲突横幅：按 note 分「同级并列 / 被遮蔽」两类展示。
function paintConflicts() {
    const box = document.getElementById("temporalConflicts");
    box.replaceChildren();
    if (!conflicts || !conflicts.length) {
        box.style.display = "none";
        return;
    }
    box.style.display = "";
    box.appendChild(el("div", "conflict-title", t(`${S}.conflicts`, "规则冲突（同组同时段但去向不同）")));
    const rulesById = {};
    rulesStore.forEach((r) => { rulesById[r.id] = r; });
    for (const c of conflicts) {
        const nameA = (rulesById[c.a] && (rulesById[c.a].name || rulesById[c.a].id)) || c.a;
        const nameB = (rulesById[c.b] && (rulesById[c.b].name || rulesById[c.b].id)) || c.b;
        const noteKey = c.note === "priority_tie" ? "conflict_tie" : "conflict_shadowed";
        const item = el("div", "conflict-item");
        const arrow = el("span", "conflict-arrow", nameA + " ⇄ " + nameB);
        const note = el("span", "conflict-note",
            noteKey === "conflict_tie"
                ? t(`${S}.conflict_tie`, "同级并列，低优先级将被高优先级遮蔽")
                : t(`${S}.conflict_shadowed`, "低优先级将被高优先级遮蔽"));
        item.append(arrow, note);
        box.appendChild(item);
    }
}

async function load() {
    const mySeq = ++seq;
    const tbody = document.getElementById("temporalListBody");
    const loadRow = el("tr");
    const loadTd = el("td", "loading", t("pages.model-morph.common.loading", "加载中…"));
    loadTd.colSpan = 10;
    loadRow.appendChild(loadTd);
    tbody.replaceChildren(loadRow);
    try {
        let rules = [];
        let validate = null;
        try {
            const res = await Promise.all([
                bridge.apiGet("temporal"),
                bridge.apiPost("validate", {}).catch(() => null),
            ]);
            rules = Array.isArray(res[0]) ? res[0] : [];
            validate = res[1];
        } catch (e) {
            rules = await bridge.apiGet("temporal").catch(() => []);
        }
        if (mySeq !== seq) return;
        rulesStore = rules;
        conflicts = (validate && Array.isArray(validate.conflicts)) ? validate.conflicts : [];
        paintList(rules);
        paintConflicts();
    } catch (e) {
        if (mySeq !== seq) return;
        showToast(e.message || "未知错误", "error");
    }
}

// ========== 编辑面板 ==========
function freshSchedule() {
    return { type: "daily", start: "09:00", end: "18:00", weekdays: [], date: "", timezone: "" };
}

function openEditor(r) {
    if (!r.schedule) r.schedule = freshSchedule();
    if (!r.scope || typeof r.scope !== "object") r.scope = { groups: [], users: [], sessions: [] };
    if (!Array.isArray(r.scope.groups)) r.scope.groups = [];
    if (!Array.isArray(r.scope.users)) r.scope.users = [];
    if (!Array.isArray(r.scope.sessions)) r.scope.sessions = [];
    draft = r;
    renderEditor();
}

function lb(label, control) {
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(control);
    return f;
}

function checkSwitch(label, checked, onChange) {
    const lbl = el("label", "all-mode-switch");
    const inp = document.createElement("input");
    inp.type = "checkbox"; inp.checked = !!checked;
    inp.addEventListener("change", () => onChange(inp.checked));
    lbl.appendChild(inp);
    const track = el("span", "switch-track"); track.appendChild(el("span", "switch-thumb"));
    lbl.appendChild(track);
    lbl.appendChild(el("span", null, label));
    return { el: lbl, inp };
}

// kind / type 切换时联动显隐相关字段。
function renderEditor() {
    const box = document.getElementById("temporalEditor");
    box.replaceChildren();
    const card = el("div", "section-card");
    card.appendChild(el("div", "section-title",
        draft.id ? t(`${S}.title`, "时间调度规则") + " · " + (draft.name || draft.id)
                 : t(`${S}.new`, "新建时间调度规则")));

    const kind = draft.kind === "group_switch" ? "group_switch" : "model_override";
    const sType = draft.schedule.type || "daily";

    // 基本信息
    const grid = el("div", "form-grid");

    const nameInp = document.createElement("input");
    nameInp.type = "text"; nameInp.className = "input";
    nameInp.value = draft.name || "";
    nameInp.addEventListener("input", () => { draft.name = nameInp.value; });
    grid.appendChild(lb(t("pages.model-morph.common.name", "名称"), nameInp));

    const enCtrl = checkSwitch(t("pages.model-morph.common.enabled", "启用"), draft.enabled !== false, (v) => { draft.enabled = v; });
    const enF = el("div", "form-field");
    enF.appendChild(el("label", null, t("pages.model-morph.common.enabled", "启用")));
    enF.appendChild(enCtrl.el);
    grid.appendChild(enF);

    const kindSel = document.createElement("select");
    kindSel.className = "input input-select";
    for (const k of ["model_override", "group_switch"]) {
        const opt = el("option", null, kindLabel(k));
        opt.value = k;
        if (kind === k) opt.selected = true;
        kindSel.appendChild(opt);
    }
    kindSel.addEventListener("change", () => {
        draft.kind = kindSel.value;
        if (draft.kind === "model_override") { draft.target_group = ""; }
        else { draft.source_provider = ""; draft.target_provider = ""; }
        renderEditor();
    });
    grid.appendChild(lb(t(`${S}.kind`, "类型"), kindSel));

    // 作用组（首项「全局（任意组）」空值）
    const gidSel = document.createElement("select");
    gidSel.className = "input input-select";
    const gNone = el("option", null, t(`${S}.global_scope`, "全局（任意组）"));
    gNone.value = "";
    if (!draft.group_id) gNone.selected = true;
    gidSel.appendChild(gNone);
    for (const g of refData.groupOptions) {
        const opt = el("option", null, g.name);
        opt.value = g.id;
        if (draft.group_id === g.id) opt.selected = true;
        gidSel.appendChild(opt);
    }
    gidSel.addEventListener("change", () => { draft.group_id = gidSel.value; });
    grid.appendChild(lb(t(`${S}.group`, "作用组"), gidSel));

    const priInp = document.createElement("input");
    priInp.type = "number"; priInp.min = "0"; priInp.step = "1"; priInp.className = "input input-sm";
    priInp.value = String(draft.priority !== undefined && draft.priority !== null ? draft.priority : 200);
    priInp.addEventListener("change", () => {
        const n = Number(priInp.value);
        draft.priority = Number.isNaN(n) ? 200 : n;
    });
    grid.appendChild(lb(t(`${S}.priority`, "优先级"), priInp));
    card.appendChild(grid);

    // 替换设置（model_override / group_switch 联动）
    card.appendChild(el("div", "editor-heading", t(`${S}.replace`, "替换设置")));
    const repGrid = el("div", "form-grid");
    if (kind === "model_override") {
        const srcSel = buildProviderSelect(draft.source_provider);
        srcSel.addEventListener("change", () => { draft.source_provider = srcSel.value; });
        repGrid.appendChild(lb(t(`${S}.source`, "源模型"), srcSel));
        const tgtSel = buildProviderSelect(draft.target_provider);
        tgtSel.addEventListener("change", () => { draft.target_provider = tgtSel.value; });
        repGrid.appendChild(lb(t(`${S}.target`, "目标模型"), tgtSel));
    } else {
        const srcW = el("div", "form-field");
        srcW.appendChild(el("label", null, t(`${S}.source`, "源组")));
        srcW.appendChild(el("div", "hint", groupName(draft.group_id) || t(`${S}.global_scope`, "全局")));
        repGrid.appendChild(srcW);
        const tgtSel = buildGroupSelect(draft.target_group);
        tgtSel.addEventListener("change", () => { draft.target_group = tgtSel.value; });
        repGrid.appendChild(lb(t(`${S}.target_group`, "目标组"), tgtSel));
    }
    card.appendChild(repGrid);

    // 时间安排
    card.appendChild(el("div", "editor-heading", t(`${S}.schedule`, "时间安排")));
    const sGrid = el("div", "form-grid");

    const typeSel = document.createElement("select");
    typeSel.className = "input input-select";
    for (const ty of SCHEDULE_TYPES) {
        const opt = el("option", null, t(`${S}.type_${ty}`, ty));
        opt.value = ty;
        if (sType === ty) opt.selected = true;
        typeSel.appendChild(opt);
    }
    typeSel.addEventListener("change", () => {
        draft.schedule.type = typeSel.value;
        if (draft.schedule.type === "always") { draft.schedule.start = ""; draft.schedule.end = ""; }
        renderEditor();
    });
    sGrid.appendChild(lb(t(`${S}.type`, "类型"), typeSel));

    // start / end（daily/weekly/date 显示）
    const showTime = sType !== "always";
    if (showTime) {
        const startInp = document.createElement("input");
        startInp.type = "time"; startInp.className = "input input-sm";
        if (draft.schedule.start) startInp.value = draft.schedule.start;
        startInp.addEventListener("input", () => { draft.schedule.start = startInp.value; });
        sGrid.appendChild(lb(t(`${S}.start`, "开始"), startInp));

        const endInp = document.createElement("input");
        endInp.type = "time"; endInp.className = "input input-sm";
        if (draft.schedule.end) endInp.value = draft.schedule.end;
        endInp.addEventListener("input", () => { draft.schedule.end = endInp.value; });
        sGrid.appendChild(lb(t(`${S}.end`, "结束"), endInp));
    }

    // weekdays（weekly/daily 显示；daily 留空=每天）
    if (sType === "daily" || sType === "weekly") {
        const wd = el("div", "form-field temporal-weekdays");
        wd.appendChild(el("label", null, t(`${S}.weekdays`, "星期（0=周一 .. 6=周日）")));
        const bar = el("div", "wizard-weekday-row");
        for (const d of WEEKDAYS) {
            const lbl = el("label", "wizard-weekday");
            const cb = document.createElement("input");
            cb.type = "checkbox"; cb.value = String(d);
            if (draft.schedule.weekdays.includes(d)) cb.checked = true;
            cb.addEventListener("change", () => {
                if (cb.checked && !draft.schedule.weekdays.includes(d)) draft.schedule.weekdays.push(d);
                else if (!cb.checked) draft.schedule.weekdays = draft.schedule.weekdays.filter((x) => x !== d);
            });
            lbl.appendChild(cb);
            lbl.appendChild(el("span", null, String(d)));
            bar.appendChild(lbl);
        }
        wd.appendChild(bar);
        sGrid.appendChild(wd);
    }

    // date（date 类型）
    if (sType === "date") {
        const dateInp = document.createElement("input");
        dateInp.type = "date"; dateInp.className = "input input-sm";
        if (draft.schedule.date) dateInp.value = draft.schedule.date;
        dateInp.addEventListener("input", () => { draft.schedule.date = dateInp.value; });
        sGrid.appendChild(lb(t(`${S}.date`, "指定日期"), dateInp));
    }

    // 时区（空 = 跟随插件时区 + 常用 IANA）
    const tzSel = document.createElement("select");
    tzSel.className = "input input-select";
    const tzNone = el("option", null, t(`${S}.timezone_default`, "跟随插件调度时区"));
    tzNone.value = "";
    if (!draft.schedule.timezone) tzNone.selected = true;
    tzSel.appendChild(tzNone);
    for (const z of TZ_VALUES) {
        const opt = el("option", null, z);
        opt.value = z;
        if (draft.schedule.timezone === z) opt.selected = true;
        tzSel.appendChild(opt);
    }
    tzSel.addEventListener("change", () => { draft.schedule.timezone = tzSel.value; });
    sGrid.appendChild(lb(t(`${S}.timezone`, "时区"), tzSel));

    card.appendChild(sGrid);

    // 限定群组（scope：三键全空=全局规则；限定命中的规则优先于全局规则生效）
    const scopeHead = el("div", "editor-heading", t(`${S}.scope`, "限定群组"));
    card.appendChild(scopeHead);
    card.appendChild(el("div", "hint", t(`${S}.scope_hint`, "留空=全局规则；限定命中的规则优先于全局规则生效")));
    const scopeGrid = el("div", "form-grid");
    for (const key of ["groups", "users", "sessions"]) {
        const input = document.createElement("input");
        input.type = "text"; input.className = "input";
        input.value = (draft.scope[key] || []).join(",");
        input.addEventListener("input", () => {
            draft.scope[key] = input.value.split(",").map((x) => x.trim()).filter(Boolean);
        });
        const label = key === "groups" ? t(`${S}.scope_groups`, "限定群组（逗号分隔，留空=全局）")
            : key === "users" ? t(`${S}.scope_users`, "限定用户（逗号分隔，留空=全局）")
            : t(`${S}.scope_sessions`, "限定会话 UMO（逗号分隔，留空=全局）");
        scopeGrid.appendChild(lb(label, input));
    }
    card.appendChild(scopeGrid);

    // 动作
    const actionsBar = el("div", "toolbar");
    const save = el("button", "btn btn-primary", draft.id ? t("pages.model-morph.common.save", "保存") : t(`${S}.save_new`, "创建规则"));
    save.type = "button"; save.addEventListener("click", () => saveRule());
    const cancel = el("button", "btn btn-ghost", t("pages.model-morph.common.cancel", "取消"));
    cancel.type = "button"; cancel.addEventListener("click", closeEditor);
    actionsBar.append(save, cancel);
    card.appendChild(actionsBar);

    box.appendChild(card);
}

// ========== 提交 ==========
function buildPayload() {
    const kind = draft.kind === "group_switch" ? "group_switch" : "model_override";
    const payload = {
        name: (draft.name || "").trim(),
        enabled: draft.enabled !== false,
        kind,
        group_id: draft.group_id || "",
        source_provider: kind === "model_override" ? (draft.source_provider || "") : "",
        target_provider: kind === "model_override" ? (draft.target_provider || "") : "",
        target_group: kind === "group_switch" ? (draft.target_group || "") : "",
        scope: {
            groups: ((draft.scope && draft.scope.groups) || []).slice(),
            users: ((draft.scope && draft.scope.users) || []).slice(),
            sessions: ((draft.scope && draft.scope.sessions) || []).slice(),
        },
        schedule: {
            type: draft.schedule.type || "daily",
            start: draft.schedule.type === "always" ? "" : (draft.schedule.start || ""),
            end: draft.schedule.type === "always" ? "" : (draft.schedule.end || ""),
            weekdays: (draft.schedule.weekdays || []).slice(),
            date: draft.schedule.type === "date" ? (draft.schedule.date || "") : "",
            timezone: draft.schedule.timezone || "",
        },
        priority: Number.isFinite(Number(draft.priority)) ? Number(draft.priority) : 200,
        metadata: {
            created_by: "manual",
            created_at: new Date().toISOString(),
            source: "manual",
        },
    };
    if (draft.id) payload.id = draft.id;
    return payload;
}

// 前端基础校验：规则名必填；字段完整性交给后端 validate。
async function saveRule() {
    const name = (draft.name || "").trim();
    if (!name) {
        showToast(t(`${S}.need_name`, "请填写规则名称"), "error");
        return;
    }
    const payload = buildPayload();
    try {
        await bridge.apiPost("temporal/save", payload);
        showToast(t(`${S}.save_success`, "保存成功"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function toggleRule(r) {
    try {
        const next = !r.enabled;
        await bridge.apiPost("temporal/toggle", { id: r.id, enabled: next });
        showToast(next ? t(`${S}.enabled_toast`, "已启用") : t(`${S}.disabled_toast`, "已停用"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function removeRule(r) {
    const ok = await confirmDialog(
        t(`${S}.delete_confirm`, "删除时间调度规则") + "「" + (r.name || r.id) + "」？" + t("pages.model-morph.common.confirm_delete", "此操作不可撤销。"),
        { title: t(`${S}.delete_confirm`, "删除时间调度规则"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("temporal/delete", { id: r.id });
        showToast(t("pages.model-morph.common.delete", "已删除"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

function closeEditor() {
    draft = null;
    document.getElementById("temporalEditor").replaceChildren();
}

function bind() {
    document.getElementById("temporalNew").addEventListener("click", () => {
        openEditor({ id: "", name: "", enabled: true, kind: "model_override", group_id: "", source_provider: "", target_provider: "", target_group: "", scope: { groups: [], users: [], sessions: [] }, schedule: freshSchedule(), priority: 200 });
    });
    document.getElementById("temporalRefresh").addEventListener("click", () => {
        load().catch((e) => showToast(e.message || "未知错误", "error"));
    });
}

export { load, bind };
