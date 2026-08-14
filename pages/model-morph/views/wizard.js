// ==========================================================================
// Model Morph · 配置向导视图（views/wizard.js，六步向导，动态渲染，不需要新后端接口）
// 步骤：
//   1 目标类型（radio 卡）→ 2 模型组（group 下拉）→ 3 原模型（provider，仅 model_override）
//   → 4 替代模型(provider) 或 目标组(group，切组类型) → 5 时间设置 + 限定群组（可选） → 6 确认(预览 JSON)
// 完成后 apiPost("temporal/save", rule)，toast 成功并重置向导。
// 生成的 temporal rule dict 字段名严格按 v0.1.5 一致契约：
//   {name, enabled, kind, group_id, source_provider, target_provider, target_group,
//    scope:{groups:[],users:[],sessions:[]}, schedule:{type,start,end,weekdays,date,timezone:""},
//    priority, metadata:{created_by:"wizard", created_at:<ISO>, source:"wizard"}}
// scope 二段式语义：groups / users / sessions 为逗号分隔字符串解析后的数组；
//   三者均留空 = 全局规则；限定（命中任意一个元素）的规则优先于全局规则生效。
// 路由：POST temporal/save（与模块 T6 注册一致）。
// 全部动态文本 textContent / el()，防 XSS。
// ==========================================================================
import { bridge, t, el, showToast } from "../common.js";
import { refData, buildGroupSelect, buildProviderSelect, buildTagInput } from "./shared.js";

const S = "pages.model-morph.wizard";

// 逗号串 → 去空白数组（标签式输入的 state 存逗号串，提交时展开）。
const splitCsv = (s) => String(s || "").split(",").map((x) => x.trim()).filter(Boolean);

// 目标类型定义：kind 决定 步骤3/4；schedule 默认值用于步骤5 初始化。
const TARGETS = [
    { id: "peak_saving",    i18n: "wizard.target_peak",    kind: "model_override", sched: { type: "daily", start: "18:00", end: "23:00" }, nameDef: "高峰省钱模式" },
    { id: "night_saving",   i18n: "wizard.target_night",   kind: "model_override", sched: { type: "daily", start: "23:00", end: "08:00" }, nameDef: "夜间省钱模式" },
    { id: "failure_switch", i18n: "wizard.target_failure", kind: "model_override", sched: { type: "daily", start: "", end: "" }, nameDef: "模型故障自动切换" },
    { id: "scheduled_replace", i18n: "wizard.target_replace", kind: "model_override", sched: { type: "daily", start: "", end: "" }, nameDef: "定时替换模型" },
    { id: "switch_group",   i18n: "wizard.target_group",   kind: "group_switch",  sched: { type: "daily", start: "", end: "" }, nameDef: "切换整个模型组" },
    { id: "custom",         i18n: "wizard.target_custom", kind: "model_override", sched: { type: "daily", start: "", end: "" }, nameDef: "自定义" },
];

// 向导状态
let state = {
    step: 1,
    type: null,          // TARGETS 条目
    group_id: "",
    source_provider: "",
    target_provider: "",
    target_group: "",
    // 时间设置
    schedType: "daily",
    start: "",
    end: "",
    weekdays: [],        // 0=周一..6=周日
    date: "",
    priority: 200,
    name: "",
    // 限定群组（可选，逗号分隔文本，留空=全局）
    scopeGroups: "",
    scopeUsers: "",
    scopeSessions: "",
};

const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];

function tStep(key) {
    return t(`${S}.${key}`, key);
}

// 步骤是否显示 步骤3（选择原模型）
function needsSource() {
    return state.type && state.type.kind === "model_override";
}

// ========== 步骤渲染 ==========
function lb(label, control) {
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(control);
    return f;
}

function render() {
    const root = document.getElementById("wizardRoot");
    root.replaceChildren();
    root.appendChild(buildStepper());

    let body;
    switch (state.step) {
        case 1: body = renderStep1(); break;
        case 2: body = renderStep2(); break;
        case 3: body = renderStep3(); break;
        case 4: body = renderStep4(); break;
        case 5: body = renderStep5(); break;
        case 6: body = renderStep6(); break;
    }
    const card = el("div", "wizard-body");
    card.appendChild(el("div", "wizard-step-title", stepTitle(state.step)));
    card.appendChild(body);
    root.appendChild(card);
    root.appendChild(buildNav());
}

function stepTitle(step) {
    return t(`${S}.step${step}`, `步骤 ${step}`);
}

function buildStepper() {
    const steps = [1, 2, 3, 4, 5, 6];
    const bar = el("div", "wizard-steps");
    for (const n of steps) {
        const chip = el("span", "wizard-step" + (n === state.step ? " active" : ""), String(n));
        bar.appendChild(chip);
    }
    return bar;
}

function buildNav() {
    const nav = el("div", "toolbar wizard-nav");
    if (state.step > 1) {
        const back = el("button", "btn btn-ghost", t(`${S}.prev`, "上一步"));
        back.addEventListener("click", () => {
            state.step -= 1;
            // 切组类型跳过步骤3（无「原模型」）
            if (state.step === 3 && !needsSource()) state.step = 2;
            render();
        });
        nav.appendChild(back);
    }
    if (state.step < 6) {
        const next = el("button", "btn btn-primary", t(`${S}.next`, "下一步"));
        next.addEventListener("click", () => {
            if (!validateStep(state.step)) return;
            state.step += 1;
            // 切组类型跳过步骤3（无「原模型」）
            if (state.step === 3 && !needsSource()) state.step = 4;
            render();
        });
        nav.appendChild(next);
    } else {
        const confirm = el("button", "btn btn-primary", t(`${S}.confirm`, "确认生成"));
        confirm.addEventListener("click", submit);
        nav.appendChild(confirm);
    }
    return nav;
}

// ===== 步骤1：目标类型（radio 卡） =====
function renderStep1() {
    const wrap = el("div", "wizard-radio-grid");
    for (const tg of TARGETS) {
        const label = el("label", "wizard-radio-card" + (state.type && state.type.id === tg.id ? " active" : ""));
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "wizardTarget";
        radio.value = tg.id;
        if (state.type && state.type.id === tg.id) radio.checked = true;
        radio.addEventListener("change", () => {
            state.type = tg;
            // 初始化时间默认值
            state.schedType = tg.sched.type;
            state.start = tg.sched.start;
            state.end = tg.sched.end;
            state.name = tg.nameDef;
            // 重绘卡选中态
            document.querySelectorAll(".wizard-radio-card").forEach((c) => c.classList.remove("active"));
            label.classList.add("active");
        });
        label.appendChild(radio);
        label.appendChild(el("span", "wizard-radio-label", t(`${S}.${tg.i18n.split(".").pop()}`, tg.id)));
        wrap.appendChild(label);
    }
    return wrap;
}

function validateStep1() {
    if (!state.type) {
        showToast(t(`${S}.need_target`, "请选择目标类型"), "error");
        return false;
    }
    return true;
}

function validateStep(step) {
    switch (step) {
        case 1: return validateStep1();
        case 2: if (!state.group_id) { showToast(t(`${S}.need_group`, "请选择模型组"), "error"); return false; } return true;
        case 3: if (!state.source_provider) { showToast(t(`${S}.need_source`, "请选择原模型"), "error"); return false; } return true;
        case 4:
            if (state.type.kind === "group_switch") {
                if (!state.target_group) { showToast(t(`${S}.need_switch_group`, "请选择目标模型组"), "error"); return false; }
            } else if (!state.target_provider) {
                showToast(t(`${S}.need_target_provider`, "请选择替代模型"), "error"); return false;
            }
            return true;
        case 5: return validateTime();
        default: return true;
    }
}

// ===== 步骤2：模型组 =====
function renderStep2() {
    const sel = buildGroupSelect(state.group_id);
    sel.addEventListener("change", () => { state.group_id = sel.value; });
    return lb(t(`${S}.group`, "选择模型组"), sel);
}

function validateStep2() {
    if (!state.group_id) { showToast(t(`${S}.need_group`, "请选择模型组"), "error"); return false; }
    return true;
}

// ===== 步骤3：原模型（仅 model_override） =====
function renderStep3() {
    const sel = buildProviderSelect(state.source_provider);
    sel.addEventListener("change", () => { state.source_provider = sel.value; });
    return lb(t(`${S}.source_provider`, "原模型（被替换的模型）"), sel);
}

// ===== 步骤4：替代模型 或 目标组 =====
function renderStep4() {
    if (state.type.kind === "group_switch") {
        const sel = buildGroupSelect(state.target_group);
        sel.addEventListener("change", () => { state.target_group = sel.value; });
        return lb(t(`${S}.target_group_label`, "切换到的目标模型组"), sel);
    }
    const sel = buildProviderSelect(state.target_provider);
    sel.addEventListener("change", () => { state.target_provider = sel.value; });
    return lb(t(`${S}.target_provider`, "替代模型（替换后使用）"), sel);
}

// ===== 步骤5：时间设置 =====
function schedSelect(onChange) {
    const sel = document.createElement("select");
    sel.className = "input input-select";
    for (const v of ["daily", "weekly", "date"]) {
        const opt = el("option", null, t(`${S}.sched_${v}`, v));
        opt.value = v;
        if (state.schedType === v) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.addEventListener("change", () => { state.schedType = sel.value; onChange(); });
    return sel;
}

function timeInput(value, onInput) {
    const inp = document.createElement("input");
    inp.type = "time"; inp.className = "input input-sm";
    if (value) inp.value = value;
    inp.addEventListener("input", () => onInput(inp.value));
    return inp;
}

function renderStep5() {
    const box = el("div", "form-grid wizard-timegrid");

    // 时间类型
    box.appendChild(lb(t(`${S}.sched_type`, "类型"), schedSelect(() => render())));

    // 开始 / 结束
    const startWrap = el("div", "form-field");
    startWrap.appendChild(el("label", null, t(`${S}.start`, "开始时间")));
    const startInp = timeInput(state.start, (v) => { state.start = v; });
    startWrap.appendChild(startInp);
    box.appendChild(startWrap);

    const endWrap = el("div", "form-field");
    endWrap.appendChild(el("label", null, t(`${S}.end`, "结束时间")));
    const endInp = timeInput(state.end, (v) => { state.end = v; });
    endWrap.appendChild(endInp);
    box.appendChild(endWrap);

    // weekdays 复选（weekly 时显示或始终显示）
    if (state.schedType === "daily" || state.schedType === "weekly") {
        const wd = el("div", "form-field wizard-weekdays");
        wd.appendChild(el("label", null, t(`${S}.weekdays`, "星期（0=周一 .. 6=周日）")));
        const chkBar = el("div", "wizard-weekday-row");
        for (const d of WEEKDAYS) {
            const lbl = el("label", "wizard-weekday");
            const cb = document.createElement("input");
            cb.type = "checkbox"; cb.value = String(d);
            if (state.weekdays.includes(d)) cb.checked = true;
            cb.addEventListener("change", () => {
                if (cb.checked && !state.weekdays.includes(d)) state.weekdays.push(d);
                else if (!cb.checked) state.weekdays = state.weekdays.filter((x) => x !== d);
            });
            lbl.appendChild(cb);
            lbl.appendChild(el("span", null, String(d)));
            chkBar.appendChild(lbl);
        }
        wd.appendChild(chkBar);
        box.appendChild(wd);
    }

    // date（date 类型）
    if (state.schedType === "date") {
        const dateWrap = el("div", "form-field");
        dateWrap.appendChild(el("label", null, t(`${S}.date`, "指定日期")));
        const dateInp = document.createElement("input");
        dateInp.type = "date"; dateInp.className = "input input-sm";
        if (state.date) dateInp.value = state.date;
        dateInp.addEventListener("input", () => { state.date = dateInp.value; });
        dateWrap.appendChild(dateInp);
        box.appendChild(dateWrap);
    }

    // 优先级
    const priWrap = el("div", "form-field");
    priWrap.appendChild(el("label", null, t(`${S}.priority`, "优先级")));
    const priInp = document.createElement("input");
    priInp.type = "number"; priInp.min = "0"; priInp.step = "1"; priInp.className = "input input-sm";
    priInp.value = String(state.priority);
    priInp.addEventListener("input", () => { state.priority = Number(priInp.value) || 0; });
    priWrap.appendChild(priInp);
    box.appendChild(priWrap);

    // 规则名
    const nameWrap = el("div", "form-field wizard-namefield");
    nameWrap.appendChild(el("label", null, t(`${S}.rule_name`, "规则名")));
    const nameInp = document.createElement("input");
    nameInp.type = "text"; nameInp.className = "input";
    nameInp.value = state.name;
    nameInp.addEventListener("input", () => { state.name = nameInp.value; });
    nameWrap.appendChild(nameInp);
    box.appendChild(nameWrap);

    // 限定群组（可选）
    const scopeTitle = el("div", "form-field-label", t(`${S}.scope_title`, "限定群组（可选）"));
    box.appendChild(scopeTitle);
    const scopeHint = el("div", "hint", t(`${S}.scope_hint`, "留空=全局规则；限定命中的规则优先于全局规则生效"));
    box.appendChild(scopeHint);

    const scopeFields = [
        { key: "scopeGroups", label: t(`${S}.scope_groups`, "限定群组（逗号分隔）") },
        { key: "scopeUsers", label: t(`${S}.scope_users`, "限定用户（逗号分隔）") },
        { key: "scopeSessions", label: t(`${S}.scope_sessions`, "限定会话（逗号分隔）") },
    ];
    for (const sf of scopeFields) {
        const wrap = el("div", "form-field");
        wrap.appendChild(el("label", null, sf.label));
        // 标签式输入：回车挂标签；state 存逗号串，提交时展开为数组。
        wrap.appendChild(buildTagInput(splitCsv(state[sf.key]), (arr) => { state[sf.key] = arr.join(","); }));
        box.appendChild(wrap);
    }

    return box;
}

function validateTime() {
    if (state.schedType === "weekly" && !state.weekdays.length) {
        showToast(t(`${S}.need_weekdays`, "weekly 类型必须至少选择一个星期"), "error");
        return false;
    }
    if (state.schedType === "date" && !state.date) {
        showToast(t(`${S}.need_date`, "date 类型必须指定日期"), "error");
        return false;
    }
    return true;
}

// ===== 步骤6：确认预览 =====
function buildRule() {
    const isGroup = state.type.kind === "group_switch";
    const rule = {
        name: state.name,
        enabled: true,
        kind: state.type.kind,
        group_id: state.group_id,
        source_provider: isGroup ? "" : state.source_provider,
        target_provider: isGroup ? "" : state.target_provider,
        target_group: isGroup ? state.target_group : "",
        scope: {
            groups: splitCsv(state.scopeGroups),
            users: splitCsv(state.scopeUsers),
            sessions: splitCsv(state.scopeSessions),
        },
        schedule: {
            type: state.schedType,
            start: state.start,
            end: state.end,
            weekdays: state.schedType === "weekly" ? state.weekdays.slice() : [],
            date: state.schedType === "date" ? state.date : "",
            timezone: "",
        },
        // v0.1.9：优先级 0 是合法档位（PRIORITY_DEFAULT），仅非法输入回退 200。
        priority: Number.isFinite(Number(state.priority)) ? Number(state.priority) : 200,
        metadata: {
            created_by: "wizard",
            created_at: new Date().toISOString(),
            source: "wizard",
        },
    };
    return rule;
}

function renderStep6() {
    const box = el("div", "wizard-confirm");
    box.appendChild(el("div", "wizard-confirm-label", t(`${S}.confirm_hint`, "请确认生成的规则：" )));
    const rule = buildRule();
    const pre = document.createElement("pre");
    pre.className = "pending-op-pre mono";
    pre.textContent = JSON.stringify(rule, null, 2);
    box.appendChild(pre);
    return box;
}

// ===== 提交 =====
async function submit() {
    if (!validateStep2()) return;
    const rule = buildRule();
    try {
        const res = await bridge.apiPost("temporal/save", rule);
        if (res && typeof res === "object" && res.ok === false) {
            showToast((res.error || t(`${S}.save_failed`, "保存失败")), "error");
            return;
        }
        showToast(t(`${S}.success`, "规则已创建成功"), "success");
        reset();
        render();
    } catch (e) {
        showToast(e.message || t(`${S}.save_failed`, "保存失败"), "error");
    }
}

function reset() {
    state = {
        step: 1,
        type: null,
        group_id: "",
        source_provider: "",
        target_provider: "",
        target_group: "",
        schedType: "daily",
        start: "",
        end: "",
        weekdays: [],
        date: "",
        priority: 200,
        name: "",
        scopeGroups: "",
        scopeUsers: "",
        scopeSessions: "",
    };
}

// ========== load / bind ==========
async function load() {
    render();
}

function bind() {
    // 无静态控件；全部在 render() 内动态绑定
}

export { load, bind };
