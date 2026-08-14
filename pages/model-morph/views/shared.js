// ==========================================================================
// Model Morph 前端 · 视图共享参考数据（views/shared.js）
// 各视图下拉需要 providers / groups / lifecycles 的选项池。
// refreshRefData() 拉取 providers 与 lifecycles（次要接口失败降级为空数组，
// 不拖垮页面，符合 playbook 硬性规则 8）。组列表在 groups 视图加载时刷新。
// 所有 option 用 el() 构建，不用 innerHTML 拼后端数据（防 XSS）。
// ==========================================================================
import { bridge, showToast, t, el } from "../common.js";

const refData = {
    providers: [],          // { id, model, enabled? }
    lifecycleOptions: [],   // { id, name } 生命周期下拉（规则 THEN 用）
    groupOptions: [],       // { id, name } 模型组下拉
};

// 刷新 providers / lifecycles（供规则/模拟器等引用；组由 groups 视图刷新）
async function refreshRefData() {
    // providers 拉取失败 → 空数组 + toast（降级，不抛）
    try {
        const provs = await bridge.apiGet("providers");
        refData.providers = Array.isArray(provs) ? provs : [];
    } catch (e) {
        refData.providers = [];
        showToast(t("pages.model-morph.common.providers_failed", "Provider 列表加载失败"), "error");
    }
    try {
        const lcs = await bridge.apiGet("lifecycles");
        refData.lifecycleOptions = (Array.isArray(lcs) ? lcs : []).map((l) => ({ id: l.id, name: l.name || l.id }));
    } catch (e) {
        refData.lifecycleOptions = [];
    }
}

// 刷新模型组下拉选项（供设置 base_group / 规则 THEN / 生命周期组 等引用）
async function refreshGroupRefs() {
    try {
        const groups = await bridge.apiGet("groups");
        refData.groupOptions = (Array.isArray(groups) ? groups : []).map((g) => ({ id: g.id, name: g.name || g.id }));
    } catch (e) {
        refData.groupOptions = [];
    }
}

// 构建 Provider 下拉（el() 构建，含「无」项与已失效保留项）。返回新 <select>。
function buildProviderSelect(selectedId, { allowEmpty = true, className = "input input-select" } = {}) {
    const sel = document.createElement("select");
    sel.className = className;
    let found = false;
    if (allowEmpty) {
        const opt = el("option", null, t("pages.model-morph.common.none", "无"));
        opt.value = "";
        sel.appendChild(opt);
    }
    for (const p of refData.providers) {
        const opt = el("option", null, p.model || p.id);
        opt.value = p.id;
        if (selectedId === p.id) { opt.selected = true; found = true; }
        sel.appendChild(opt);
    }
    if (selectedId && !found) {
        const opt = el("option", null, String(selectedId) + "（已失效）");
        opt.value = String(selectedId);
        opt.selected = true;
        opt.disabled = true;
        sel.appendChild(opt);
    }
    return sel;
}

// 构建模型组下拉
function buildGroupSelect(selectedId, { allowEmpty = true, className = "input input-select" } = {}) {
    const sel = document.createElement("select");
    sel.className = className;
    if (allowEmpty) {
        const opt = el("option", null, t("pages.model-morph.common.none", "无"));
        opt.value = "";
        sel.appendChild(opt);
    }
    for (const g of refData.groupOptions) {
        const opt = el("option", null, g.name);
        opt.value = g.id;
        if (selectedId === g.id) opt.selected = true;
        sel.appendChild(opt);
    }
    return sel;
}

// 构建生命周期下拉
function buildLifecycleSelect(selectedId, allowEmpty = true) {
    const sel = document.createElement("select");
    sel.className = "input input-select";
    if (allowEmpty) {
        const opt = el("option", null, t("pages.model-morph.common.none", "无"));
        opt.value = "";
        sel.appendChild(opt);
    }
    for (const l of refData.lifecycleOptions) {
        const opt = el("option", null, l.name);
        opt.value = l.id;
        if (selectedId === l.id) opt.selected = true;
        sel.appendChild(opt);
    }
    return sel;
}

export { refData, refreshRefData, refreshGroupRefs, buildProviderSelect, buildGroupSelect, buildLifecycleSelect };
