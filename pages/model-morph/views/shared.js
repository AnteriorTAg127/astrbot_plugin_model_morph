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

// 标签式输入（限定群组等）：上方蓝底白字标签行 + 下方文本输入。
// - 回车（或失焦）把当前输入追加为标签（去空白、去重）；
// - 点标签上的 × 删除；输入为空时按 Backspace 删除最后一个标签；
// - 每次增删回调 onChange(values: string[])，调用方自行保存状态。
// 返回容器元素（tag-input）。
function buildTagInput(initialValues, onChange) {
    const box = el("div", "tag-input");
    const chipsRow = el("div", "tag-chips");
    const input = document.createElement("input");
    input.type = "text";
    input.className = "tag-input-field";
    input.placeholder = t("pages.model-morph.common.tag_add_hint", "输入后回车添加");

    let values = (Array.isArray(initialValues) ? initialValues : []).slice();
    const emit = () => { if (typeof onChange === "function") onChange(values.slice()); };

    const renderChips = () => {
        chipsRow.replaceChildren();
        for (const v of values) {
            const chip = el("span", "tag-chip", v);
            const del = el("button", "tag-chip-x", "×");
            del.type = "button";
            del.addEventListener("click", () => {
                values = values.filter((x) => x !== v);
                emit();
                renderChips();
            });
            chip.appendChild(del);
            chipsRow.appendChild(chip);
        }
    };

    const addChip = (raw) => {
        const v = String(raw || "").trim();
        if (!v || values.includes(v)) return;
        values.push(v);
        emit();
        renderChips();
    };

    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            addChip(input.value);
            input.value = "";
        } else if (e.key === "Backspace" && !input.value) {
            if (values.length) {
                values.pop();
                emit();
                renderChips();
            }
        }
    });
    // 失焦兜底：残留文本自动挂为标签（点 × 等操作前先提交）。
    input.addEventListener("blur", () => {
        if (input.value.trim()) {
            addChip(input.value);
            input.value = "";
        }
    });

    box.appendChild(chipsRow);
    box.appendChild(input);
    renderChips();
    return box;
}

export { refData, refreshRefData, refreshGroupRefs, buildProviderSelect, buildGroupSelect, buildLifecycleSelect, buildTagInput };
