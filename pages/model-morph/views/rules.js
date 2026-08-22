// ==========================================================================
// Model Morph · 规则视图（views/rules.js）
// 列表（启停 + 优先级 + 编辑/复制/删除/模拟）+ 新建/编辑面板：
//   - WHEN 条件行编辑器（类型下拉 + 参数表单 + 删行）
//   - AND/OR 切换
//   - scope 六列表单
//   - THEN 动作（switch_group / switch_provider / apply_lifecycle / unlock / replace_model）+ 参数
// 每条规则「模拟」按钮 → 跳到模拟器并预填。
// 全部动态文本走 textContent / el()，防 XSS；删除走 confirmDialog()。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";
import { refData, buildProviderSelect, buildGroupSelect, buildLifecycleSelect } from "./shared.js";

const COND_TYPES = ["time_range", "date_weekday", "scope", "keyword", "command", "at_bot", "message_type", "round_gte", "context_length_gte", "lifecycle_event", "model_keyword"];
const ACTIONS = ["switch_group", "switch_provider", "apply_lifecycle", "unlock", "replace_model"];

let seq = 0;
let draft = null;

function freshCondition() {
    return { type: "time_range", start: "09:00", end: "18:00", weekdays: [] };
}

function freshScope() {
    return { groups: [], users: [], sessions: [], platforms: [], exclude_groups: [], exclude_users: [] };
}

function condLabel(type) {
    return t(`pages.model-morph.rules.cond_${type}`, type);
}

function actionLabel(action) {
    return t(`pages.model-morph.rules.action_${action}`, action);
}

function stateBadge(on) {
    return on ? el("span", "badge success", t("pages.model-morph.common.enabled", "启用"))
              : el("span", "badge muted", t("pages.model-morph.common.disabled", "禁用"));
}

// ── 列表 ─────────────────────────────────────────────
function ruleActionsRow(r) {
    const box = el("div", "cell-actions");
    const sim = el("button", "btn btn-primary btn-sm", t("pages.model-morph.rules.simulate", "模拟"));
    sim.type = "button"; sim.addEventListener("click", () => prepopulateSim(r));
    const edit = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.common.edit", "编辑"));
    edit.type = "button"; edit.addEventListener("click", () => openEditor(JSON.parse(JSON.stringify(r))));
    const dup = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.common.duplicate", "复制"));
    dup.type = "button"; dup.addEventListener("click", () => duplicate(r.id));
    const del = el("button", "btn btn-danger btn-sm", t("pages.model-morph.common.delete", "删除"));
    del.type = "button"; del.addEventListener("click", () => removeRule(r.id, r));
    box.append(sim, edit, dup, del);
    return box;
}

function paintList(rules) {
    const body = document.getElementById("ruleListBody");
    const empty = document.getElementById("ruleListEmpty");
    body.replaceChildren();
    if (!rules.length) { empty.style.display = ""; return; }
    empty.style.display = "none";
    for (const r of rules) {
        const row = el("tr");
        const nameCell = el("td");
        nameCell.appendChild(el("strong", "cell-name", r.name || r.id));
        nameCell.appendChild(el("div", "hint mono", r.id));
        row.appendChild(nameCell);
        // 当
        const conds = r.when && r.when.conditions ? r.when.conditions : [];
        const whenCell = el("td", null, conds.length ? condLabel(conds[0].type) : "…");
        if (conds.length > 1) whenCell.appendChild(el("span", "hint", ` +${conds.length - 1}`));
        row.appendChild(whenCell);
        row.appendChild(el("td", null, r.then && r.then.action ? actionLabel(r.then.action) : "—"));
        row.appendChild(el("td", null, String(r.priority ?? 0)));
        const enTd = el("td");
        enTd.appendChild(stateBadge(r.enabled));
        row.appendChild(enTd);
        row.appendChild(el("td")).appendChild(ruleActionsRow(r));
        body.appendChild(row);
    }
}

async function load() {
    const mySeq = ++seq;
    const tbody = document.getElementById("ruleListBody");
    const lr = el("tr"); const td = el("td", "loading", t("pages.model-morph.common.loading", "加载中…"));
    td.colSpan = 6; lr.appendChild(td);
    tbody.replaceChildren(lr);
    try {
        const rules = await bridge.apiGet("rules");
        if (mySeq !== seq) return;
        paintList(Array.isArray(rules) ? rules : []);
    } catch (e) {
        if (mySeq !== seq) return;
        showToast(e.message || "未知错误", "error");
    }
}

// 模拟：设置 pending，然后跳转模拟器并让它预填。
// 跳转走 hash 路由（app.js 中 tab/scope/模拟统一由 hashchange → handleHash 驱动，
// hash 与当前实际视图保持一致）。
// 判断「是否已在模拟器页」依据当前可见页面（#page-simulator.active）而非 hash：
// 旧实现用 hash 判断，在「先跳模拟器 → tab 切回规则（当时 tab 点击不改 hash）→ 再点模拟」
// 时会出现 hash 仍是 #/simulator 而实际视图已是规则页的脱节，导致误判「已在模拟器」、
// 只派发事件不切页，表现为按钮失效。
let pendingSimRule = null;
function prepopulateSim(rule) {
    pendingSimRule = rule;
    const simVisible = document.getElementById("page-simulator")?.classList.contains("active");
    if (simVisible) {
        // 已在模拟器页（可见）：hash 不会变化而不触发路由，额外派发事件让模拟器重新预填当前规则。
        window.dispatchEvent(new CustomEvent("mm-simulate-request", { detail: rule }));
        return;
    }
    // 未在模拟器页：改写 hash → hashchange → handleHash 做 scope 切换 + 切页 + 加载（消费 pending 预填）。
    window.location.hash = "#/simulator";
}

// 供 simulator 视图消费
function consumePendingSim() {
    const r = pendingSimRule;
    pendingSimRule = null;
    return r;
}

// ── 编辑面板 ─────────────────────────────────────────
function openEditor(r) {
    if (!r.scope) r.scope = freshScope();
    if (!r.when) r.when = { op: "and", conditions: [freshCondition()] };
    if (!r.when.conditions || !r.when.conditions.length) r.when.conditions = [freshCondition()];
    if (!r.then) r.then = { action: "switch_group", group_id: "" };
    draft = r;
    renderEditor();
}

function textField(label, onChange) {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "input";
    inp.addEventListener("input", () => onChange(inp.value));
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(inp);
    return { f, inp };
}

function numberField(label, value, onChange) {
    const inp = document.createElement("input");
    inp.type = "number";
    inp.step = "1";
    inp.className = "input input-sm";
    inp.value = String(value ?? 0);
    inp.addEventListener("change", () => {
        const n = Number(inp.value);
        onChange(Number.isNaN(n) ? 0 : n);
    });
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(inp);
    return { f, inp };
}

function checkFieldLabel(label, checked, onChange) {
    const lbl = el("label", "all-mode-switch");
    const inp = document.createElement("input");
    inp.type = "checkbox";
    inp.checked = !!checked;
    inp.addEventListener("change", () => onChange(inp.checked));
    lbl.appendChild(inp);
    const track = el("span", "switch-track");
    track.appendChild(el("span", "switch-thumb"));
    lbl.appendChild(track);
    lbl.appendChild(el("span", null, label));
    return lbl;
}

function renderEditor() {
    const box = document.getElementById("ruleEditor");
    box.replaceChildren();
    const card = el("div", "section-card");
    card.appendChild(el("div", "section-title", t("pages.model-morph.rules.title", "规则引擎")));

    // 基本信息
    const grid = el("div", "form-grid");
    const nameRes = textField(t("pages.model-morph.common.name", "名称"), (v) => { draft.name = v; });
    nameRes.inp.value = draft.name || "";
    grid.appendChild(nameRes.f);
    const priRes = numberField(t("pages.model-morph.rules.priority", "优先级"), draft.priority ?? 0, (v) => { draft.priority = v; });
    grid.appendChild(priRes.f);
    const enF = el("div", "form-field");
    enF.appendChild(checkFieldLabel(t("pages.model-morph.common.enabled", "启用"), draft.enabled, (v) => { draft.enabled = v; }));
    grid.appendChild(enF);
    card.appendChild(grid);

    // WHEN
    card.appendChild(el("div", "editor-heading", t("pages.model-morph.rules.when", "当")));
    const opBar = el("div", "when-op-bar");
    const andLbl = el("label");
    const andR = document.createElement("input"); andR.type = "radio"; andR.name = "when-op"; andR.value = "and";
    andR.checked = draft.when.op !== "or";
    andR.addEventListener("change", () => { if (andR.checked) draft.when.op = "and"; });
    andLbl.appendChild(andR); andLbl.appendChild(el("span", null, t("pages.model-morph.rules.and", "全部满足 (AND)")));
    const orLbl = el("label");
    const orR = document.createElement("input"); orR.type = "radio"; orR.name = "when-op"; orR.value = "or";
    orR.checked = draft.when.op === "or";
    orR.addEventListener("change", () => { if (orR.checked) draft.when.op = "or"; });
    orLbl.appendChild(orR); orLbl.appendChild(el("span", null, t("pages.model-morph.rules.or", "任一满足 (OR)")));
    opBar.append(andLbl, orLbl);
    card.appendChild(opBar);

    const condList = el("div", "cond-list-editor");
    draft.when.conditions.forEach((c, idx) => condList.appendChild(buildConditionRow(idx)));
    card.appendChild(condList);

    const addCond = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.rules.add_condition", "添加条件"));
    addCond.type = "button";
    addCond.addEventListener("click", () => { draft.when.conditions.push(freshCondition()); renderEditor(); });
    const tbCond = el("div", "toolbar"); tbCond.appendChild(addCond);
    card.appendChild(tbCond);

    // SCOPE
    card.appendChild(el("div", "editor-heading", t("pages.model-morph.rules.scope_section", "作用域")));
    card.appendChild(el("div", "hint", t("pages.model-morph.rules.scope_hint", "留空表示所有会话")));
    const scopeGrid = el("div", "form-grid");
    const scopeFieldMeta = [
        ["groups", "pages.model-morph.rules.groups_include"],
        ["users", "pages.model-morph.rules.users_include"],
        ["sessions", "pages.model-morph.rules.sessions_include"],
        ["platforms", "pages.model-morph.rules.platforms_include"],
        ["exclude_groups", "pages.model-morph.rules.groups_exclude"],
        ["exclude_users", "pages.model-morph.rules.users_exclude"],
    ];
    for (const [key, labelKey] of scopeFieldMeta) {
        const res = textField(t(labelKey, key), (v) => { draft.scope[key] = commaList(v); });
        res.inp.value = (draft.scope[key] || []).join(",");
        res.inp.placeholder = t("pages.model-morph.rules.comma_separated", "逗号分隔");
        scopeGrid.appendChild(res.f);
    }
    card.appendChild(scopeGrid);

    // THEN
    card.appendChild(el("div", "editor-heading", t("pages.model-morph.rules.then", "执行")));
    card.appendChild(el("div", "hint", t("pages.model-morph.rules.action_note", "规则命中后：本次及之后生效，不清空上下文")));
    const thenRow = el("div", "then-row");
    const actionSel = document.createElement("select");
    actionSel.className = "input input-select";
    for (const a of ACTIONS) {
        const opt = el("option", null, actionLabel(a));
        opt.value = a;
        if (draft.then.action === a) opt.selected = true;
        actionSel.appendChild(opt);
    }
    actionSel.addEventListener("change", () => {
        draft.then.action = actionSel.value;
        draft.then.group_id = "";
        draft.then.provider_id = "";
        draft.then.lifecycle_id = "";
        draft.then.model = "";
        renderEditor();
    });
    const actionF = el("div", "form-field");
    actionF.appendChild(el("label", null, t("pages.model-morph.rules.action", "动作")));
    actionF.appendChild(actionSel);
    thenRow.appendChild(actionF);
    thenRow.appendChild(buildThenParams());
    const thenWrap = el("div", "form-grid");
    thenWrap.appendChild(thenRow);
    card.appendChild(thenWrap);

    // 动作
    const actionsBar = el("div", "toolbar");
    const save = el("button", "btn btn-primary", t("pages.model-morph.rules.save_rule", "保存规则"));
    save.type = "button"; save.addEventListener("click", () => saveRule());
    const cancel = el("button", "btn btn-ghost", t("pages.model-morph.common.cancel", "取消"));
    cancel.type = "button"; cancel.addEventListener("click", closeEditor);
    actionsBar.append(save, cancel);
    card.appendChild(actionsBar);

    box.appendChild(card);
}

function commaList(str) {
    return String(str || "").split(",").map((x) => x.trim()).filter(Boolean);
}

// THEN 动作参数面板
function buildThenParams() {
    const wrap = el("div", "then-params");
    const action = draft.then.action;
    if (action === "switch_group") {
        const sel = buildGroupSelect(draft.then.group_id);
        sel.addEventListener("change", () => { draft.then.group_id = sel.value; });
        const f = el("div", "form-field");
        f.appendChild(el("label", null, t("pages.model-morph.sessions.group", "模型组")));
        f.appendChild(sel);
        wrap.appendChild(f);
    } else if (action === "switch_provider") {
        const sel = buildProviderSelect(draft.then.provider_id);
        sel.addEventListener("change", () => { draft.then.provider_id = sel.value; });
        const f = el("div", "form-field");
        f.appendChild(el("label", null, t("pages.model-morph.groups.provider", "Provider")));
        f.appendChild(sel);
        wrap.appendChild(f);
    } else if (action === "apply_lifecycle") {
        const sel = buildLifecycleSelect(draft.then.lifecycle_id);
        sel.addEventListener("change", () => { draft.then.lifecycle_id = sel.value; });
        const f = el("div", "form-field");
        f.appendChild(el("label", null, t("pages.model-morph.lifecycles.title", "生命周期")));
        f.appendChild(sel);
        wrap.appendChild(f);
    } else if (action === "unlock") {
        // unlock 无参数
        wrap.appendChild(el("div", "hint", t("pages.model-morph.rules.action_unlock", "解锁会话锁定")));
    } else if (action === "replace_model") {
        // replace_model：Provider 下拉 + 模型名文本输入
        const pSel = buildProviderSelect(draft.then.provider_id);
        pSel.addEventListener("change", () => { draft.then.provider_id = pSel.value; });
        const fP = el("div", "form-field");
        fP.appendChild(el("label", null, t("pages.model-morph.groups.provider", "Provider")));
        fP.appendChild(pSel);
        wrap.appendChild(fP);
        const modelInp = document.createElement("input");
        modelInp.type = "text";
        modelInp.className = "input";
        modelInp.value = draft.then.model || "";
        modelInp.placeholder = t("pages.model-morph.rules.replace_model_model_hint", "目标模型名，如 gpt-5-mini");
        modelInp.addEventListener("input", () => { draft.then.model = modelInp.value; });
        const fM = el("div", "form-field");
        fM.appendChild(el("label", null, t("pages.model-morph.rules.replace_model_model", "目标模型")));
        fM.appendChild(modelInp);
        wrap.appendChild(fM);
    }
    return wrap;
}

// 条件行编辑器
function buildConditionRow(idx) {
    const cond = draft.when.conditions[idx];
    const row = el("div", "cond-row");
    const typeSel = document.createElement("select");
    typeSel.className = "cond-type input input-select";
    for (const ct of COND_TYPES) {
        const opt = el("option", null, condLabel(ct));
        opt.value = ct;
        if (cond.type === ct) opt.selected = true;
        typeSel.appendChild(opt);
    }
    typeSel.addEventListener("change", () => {
        draft.when.conditions[idx] = freshConditionFor(typeSel.value);
        renderEditor();
    });
    row.appendChild(typeSel);
    const params = el("div", "cond-params");
    buildCondParams(cond, params, renderEditor);
    row.appendChild(params);
    const rm = el("button", "btn btn-danger btn-sm", t("pages.model-morph.common.delete", "删除"));
    rm.type = "button";
    rm.addEventListener("click", () => {
        draft.when.conditions.splice(idx, 1);
        if (!draft.when.conditions.length) draft.when.conditions = [freshCondition()];
        renderEditor();
    });
    row.appendChild(rm);
    return row;
}

function buildCondParams(cond, params, onRefresh) {
    const addLabel = (s) => params.appendChild(el("span", "cond-param-hint", s));
    const addInput = (cls, value, onChange, type = "text", ph) => {
        const inp = document.createElement("input");
        inp.className = "cond-param " + cls;
        inp.type = type;
        inp.value = value == null ? "" : String(value);
        if (ph) inp.placeholder = ph;
        inp.addEventListener(type === "number" ? "change" : "input", () => onChange(inp.value));
        params.appendChild(inp);
        return inp;
    };
    const addSelect = (options, value, onChange) => {
        const sel = document.createElement("select");
        sel.className = "cond-param";
        for (const [v, label] of options) {
            const opt = el("option", null, label);
            opt.value = v;
            if (String(value) === v) opt.selected = true;
            sel.appendChild(opt);
        }
        sel.addEventListener("change", () => onChange(sel.value));
        params.appendChild(sel);
        return sel;
    };

    switch (cond.type) {
        case "time_range":
            addLabel(t("pages.model-morph.rules.time_start", "起始"));
            addInput("cond-param-input", cond.start || "09:00", (v) => { cond.start = v; }, "time");
            addLabel(t("pages.model-morph.rules.time_end", "结束"));
            addInput("cond-param-input", cond.end || "18:00", (v) => { cond.end = v; }, "time");
            break;
        case "date_weekday":
            addSelect([
                ["workday", t("pages.model-morph.rules.date_workday", "工作日")],
                ["weekend", t("pages.model-morph.rules.date_weekend", "周末")],
                ["", t("pages.model-morph.rules.date_days", "星期列表")],
            ], cond.mode, (v) => { cond.mode = v; });
            break;
        case "scope":
            addLabel(t("pages.model-morph.rules.groups_include", "包含群"));
            addInput("cond-param-input", (cond.groups || []).join(","), (v) => { cond.groups = commaList(v); }, "text", t("pages.model-morph.rules.comma_separated", "逗号分隔"));
            addLabel(t("pages.model-morph.rules.groups_exclude", "排除群"));
            addInput("cond-param-input", (cond.exclude_groups || []).join(","), (v) => { cond.exclude_groups = commaList(v); }, "text", t("pages.model-morph.rules.comma_separated", "逗号分隔"));
            break;
        case "keyword":
            addInput("cond-param-input", (cond.keywords || []).join(","), (v) => { cond.keywords = commaList(v); }, "text", t("pages.model-morph.rules.comma_separated", "逗号分隔"));
            break;
        case "command":
            addInput("cond-param-input", (cond.commands || []).join(","), (v) => { cond.commands = commaList(v); }, "text", t("pages.model-morph.rules.comma_separated", "逗号分隔"));
            break;
        case "at_bot":
            addSelect([
                ["true", t("pages.model-morph.common.yes", "是")],
                ["false", t("pages.model-morph.common.no", "否")],
            ], cond.value === false ? "false" : "true", (v) => { cond.value = v === "true"; });
            break;
        case "message_type":
            addSelect([
                ["group", t("pages.model-morph.simulator.type_group", "群消息")],
                ["private", t("pages.model-morph.simulator.type_private", "私聊消息")],
            ], cond.value, (v) => { cond.value = v; });
            break;
        case "round_gte":
            addInput("cond-param-input", cond.value ?? 0, (v) => { cond.value = Number(v) || 0; }, "number");
            break;
        case "context_length_gte":
            addInput("cond-param-input", cond.value ?? 0, (v) => { cond.value = Number(v) || 0; }, "number");
            break;
        case "lifecycle_event":
            addSelect([
                ["new", t("pages.model-morph.simulator.event_new", "新会话 (new)")],
                ["reset", t("pages.model-morph.simulator.event_reset", "重置 (reset)")],
            ], cond.event, (v) => { cond.event = v; });
            break;
        case "model_keyword":
            // 关键词（多值，逗号/换行/回车分隔，标签式输入）
            addLabel(t("pages.model-morph.rules.model_kw_keywords", "关键词"));
            const kwBox = buildModelKeywordInput(cond, (next) => { cond.keywords = next; });
            params.appendChild(kwBox);
            // mode 下拉
            addLabel(t("pages.model-morph.rules.model_kw_mode", "匹配模式"));
            addSelect([
                ["all", t("pages.model-morph.rules.mode_all", "包含全部 (all)")],
                ["any", t("pages.model-morph.rules.mode_any", "包含任一 (any)")],
                ["min_n", t("pages.model-morph.rules.mode_min_n", "至少 N 个 (min_n)")],
            ], cond.mode, (v) => {
                cond.mode = v;
                // 切换 min_n → 需要重渲染以显示/隐藏 min_n 输入框
                if (typeof onRefresh === "function") onRefresh();
            });
            // min_n：mode=min_n 时显示 1..len(keywords)
            const minNField = el("span", "cond-minn-wrap");
            if (cond.mode === "min_n") {
                minNField.appendChild(buildMinNInput(cond));
            }
            params.appendChild(minNField);
            break;
        default:
            break;
    }
}

// model_keyword 关键词输入：标签式（回车追加 / × 删除 / Backspace 删除最后一个）。
function buildModelKeywordInput(cond, onChange) {
    const box = el("div", "cond-tag-input");
    const chipsRow = el("div", "cond-tag-chips");
    const input = document.createElement("input");
    input.type = "text";
    input.className = "cond-param";
    input.placeholder = t("pages.model-morph.rules.model_kw_kw_hint", "回车添加，逗号/换行分隔");
    let values = (Array.isArray(cond.keywords) ? cond.keywords : []).slice();

    const emit = () => onChange(values.slice());
    const renderChips = () => {
        chipsRow.replaceChildren();
        for (const v of values) {
            const chip = el("span", "cond-tag-chip", v);
            const del = el("button", "cond-tag-chip-x", "×");
            del.type = "button";
            del.addEventListener("click", () => {
                values = values.filter((x) => x !== v);
                emit(); renderChips();
            });
            chip.appendChild(del);
            chipsRow.appendChild(chip);
        }
    };
    const addChip = (raw) => {
        // 兼容逗号/换行分隔，逐个去空白去重
        const parts = String(raw || "").split(/[,，\n]/).map((x) => x.trim()).filter(Boolean);
        for (const v of parts) { if (v && !values.includes(v)) values.push(v); }
        if (parts.length) emit();
        renderChips();
    };
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === "," || e.key === "，") {
            e.preventDefault();
            addChip(input.value); input.value = "";
        } else if (e.key === "Backspace" && !input.value) {
            if (values.length) { values.pop(); emit(); renderChips(); }
        }
    });
    input.addEventListener("blur", () => {
        if (input.value.trim()) { addChip(input.value); input.value = ""; }
    });
    box.appendChild(chipsRow);
    box.appendChild(input);
    renderChips();
    return box;
}

// min_n 数字输入（1..关键词数）。
function buildMinNInput(cond) {
    const inp = document.createElement("input");
    inp.type = "number";
    inp.min = "1";
    inp.max = String(Math.max(1, (cond.keywords || []).length));
    inp.className = "cond-param";
    inp.value = String(cond.min_n != null ? cond.min_n : 1);
    inp.addEventListener("change", () => {
        const n = Number(inp.value);
        const max = Math.max(1, (cond.keywords || []).length);
        cond.min_n = Math.min(Math.max(Number.isNaN(n) ? 1 : n, 1), max);
        inp.value = String(cond.min_n);
    });
    return inp;
}

function freshConditionFor(type) {
    switch (type) {
        case "time_range": return { type, start: "09:00", end: "18:00", weekdays: [] };
        case "date_weekday": return { type, mode: "workday" };
        case "scope": return { type, groups: [], users: [], sessions: [], platforms: [], exclude_groups: [], exclude_users: [] };
        case "keyword": return { type, keywords: [], mode: "contains" };
        case "command": return { type, commands: [] };
        case "at_bot": return { type, value: true };
        case "message_type": return { type, value: "group" };
        case "round_gte": return { type, value: 0 };
        case "context_length_gte": return { type, value: 0 };
        case "lifecycle_event": return { type, event: "new" };
        case "model_keyword": return { type, keywords: [], mode: "any", min_n: 1 };
        default: return { type };
    }
}

async function saveRule() {
    if (!draft.name) {
        showToast(t("pages.model-morph.rules.need_name", "请填写规则名称"), "error");
        return;
    }
    const payload = JSON.parse(JSON.stringify(draft));
    // 新建时 draft.id 为空串：剔除，避免后端把 ``id: ""`` 原样入库（空 id 规则不可删/不可改）。
    if (!payload.id) delete payload.id;
    try {
        await bridge.apiPost("rules/save", payload);
        showToast(t("pages.model-morph.common.save_success", "保存成功"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function duplicate(id) {
    try {
        await bridge.apiPost("rules/duplicate", { id });
        showToast(t("pages.model-morph.common.copy_success", "复制成功"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function removeRule(id, r) {
    const ok = await confirmDialog(
        t("pages.model-morph.rules.delete_rule", "删除规则") + "「" + (r.name || id) + "」？" + t("pages.model-morph.common.confirm_delete", "此操作不可撤销。"),
        { title: t("pages.model-morph.rules.delete_rule", "删除规则"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("rules/delete", { id });
        showToast(t("pages.model-morph.common.delete", "已删除"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

function closeEditor() {
    draft = null;
    document.getElementById("ruleEditor").replaceChildren();
}

function bind() {
    document.getElementById("ruleNew").addEventListener("click", () => {
        openEditor({ id: "", name: "", enabled: true, priority: 0, scope: freshScope(), when: { op: "and", conditions: [freshCondition()] }, then: { action: "switch_group", group_id: "" } });
    });
}

export { load, bind, consumePendingSim };
