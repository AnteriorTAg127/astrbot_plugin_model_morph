// ==========================================================================
// Model Morph · 预设视图（views/presets.js）
// apiGet("presets") 渲染 5 张预设卡（name/desc + 动态参数表单）。
// 每张卡按 PRESETS 条目的 params[] 生成控件：
//   provider 类 → buildProviderSelect、group 类 → buildGroupSelect、
//   time 类 → input type=time、int → input type=number、string(date/name) → text/date、
//   list(weekdays) → 星期复选(0=周一..6).
// [应用预设] → apiPost("presets/apply", {id, params}) → toast 成功（提示可在审计页查看）。
// PRESETS 结构见 scheduler/presets.py（id/name/desc/kind/times/params[].{key,label,type,required,default}）。
// 全部动态文本 textContent / el()，防 XSS。
// ==========================================================================
import { bridge, t, el, showToast } from "../common.js";
import { buildGroupSelect, buildProviderSelect } from "./shared.js";

const S = "pages.model-morph.presets";
const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];

// 每个预设生成控件并返回收集函数（读取当前值）
function buildParamControl(param, controls) {
    const field = el("div", "preset-param");
    field.appendChild(el("div", "preset-param-label",
        param.label + (param.required ? " *" : "")));

    // provider 类型
    if (param.type === "provider") {
        const sel = buildProviderSelect(String(param.default || ""));
        controls[param.key] = () => sel.value;
        field.appendChild(sel);
        return field;
    }
    // group 类型
    if (param.type === "group") {
        const sel = buildGroupSelect(String(param.default || ""));
        controls[param.key] = () => sel.value;
        field.appendChild(sel);
        return field;
    }
    // time 类型
    if (param.type === "time") {
        const inp = document.createElement("input");
        inp.type = "time"; inp.className = "input input-sm";
        if (param.default) inp.value = String(param.default);
        controls[param.key] = () => inp.value;
        field.appendChild(inp);
        return field;
    }
    // int 类型
    if (param.type === "int") {
        const inp = document.createElement("input");
        inp.type = "number"; inp.step = "1"; inp.className = "input input-sm";
        inp.value = String(param.default ?? 200);
        controls[param.key] = () => Number(inp.value) || 200;
        field.appendChild(inp);
        return field;
    }
    // list（weekdays）复选
    if (param.type === "list") {
        const chkBar = el("div", "preset-weekday-row");
        for (const d of WEEKDAYS) {
            const lbl = el("label", "preset-weekday");
            const cb = document.createElement("input");
            cb.type = "checkbox"; cb.value = String(d);
            lbl.appendChild(cb);
            lbl.appendChild(el("span", null, String(d)));
            chkBar.appendChild(lbl);
        }
        controls[param.key] = () => {
            const sel = [];
            chkBar.querySelectorAll("input:checked").forEach((c) => sel.push(Number(c.value)));
            return sel;
        };
        field.appendChild(chkBar);
        return field;
    }
    // string（name 文本 / date 日期）
    const inp = document.createElement("input");
    if (param.key === "date") {
        inp.type = "date"; inp.className = "input input-sm";
        if (param.default) inp.value = String(param.default);
    } else {
        inp.type = "text"; inp.className = "input";
        if (param.default) inp.value = String(param.default);
    }
    controls[param.key] = () => inp.value;
    field.appendChild(inp);
    return field;
}

function renderPreset(preset) {
    const card = el("div", "preset-card");
    card.appendChild(el("div", "preset-name", preset.name || preset.id));
    card.appendChild(el("div", "preset-desc", preset.desc || ""));

    const controls = {};   // key → () => value

    if (preset.times) {
        card.appendChild(el("div", "preset-times", "⌚ " + preset.times));
    }

    const params = Array.isArray(preset.params) ? preset.params : [];
    if (params.length) {
        const form = el("div", "preset-params");
        for (const p of params) form.appendChild(buildParamControl(p, controls));
        card.appendChild(form);
    }

    const applyBtn = el("button", "btn btn-primary btn-sm", t(`${S}.apply`, "应用预设"));
    applyBtn.addEventListener("click", () => applyPreset(preset, controls));
    card.appendChild(applyBtn);
    return card;
}

async function applyPreset(preset, controls) {
    // 收集参数（required 项做前端校验）
    const params = {};
    for (const p of (preset.params || [])) {
        params[p.key] = controls[p.key] ? controls[p.key]() : p.default;
        const v = params[p.key];
        if (p.required && (v == null || v === "")) {
            showToast(t(`${S}.need_param`, "请填写必填参数") + "：" + p.label, "error");
            return;
        }
    }
    try {
        const res = await bridge.apiPost("presets/apply", { id: preset.id, params });
        if (res && typeof res === "object" && res.ok === false) {
            showToast((res.error || t(`${S}.failed`, "应用失败")), "error");
            return;
        }
        showToast(t(`${S}.success`, "预设已应用，规则已创建（可在审计日志查看）"), "success");
    } catch (e) {
        showToast(e.message || t(`${S}.failed`, "应用失败"), "error");
    }
}

async function load() {
    const grid = document.getElementById("presetsGrid");
    grid.replaceChildren(el("div", "loading", t("pages.model-morph.common.loading", "加载中…")));
    try {
        const data = await bridge.apiGet("presets");
        const list = Array.isArray(data) ? data : (data && typeof data === "object" ? Object.values(data) : []);
        grid.replaceChildren();
        if (!list.length) {
            grid.appendChild(el("div", "empty-state", t(`${S}.empty`, "暂无预设")));
            return;
        }
        for (const p of list) grid.appendChild(renderPreset(p));
    } catch (e) {
        grid.replaceChildren(el("div", "empty-state",
            t("pages.model-morph.common.error", "请求失败") + ": " + (e.message || "未知错误")));
        showToast(e.message || "未知错误", "error");
    }
}

function bind() {
    // 无静态控件；按钮在 renderPreset 中动态绑定
}

export { load, bind };
