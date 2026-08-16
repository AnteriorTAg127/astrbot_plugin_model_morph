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

// ==========================================================================
// UMO 实时预览工具（模块 F2）
// 在群号 / QQ / 会话输入下方实时换算 UMO 格式：`platform:GroupMessage:群号`、
// `platform:FriendMessage:QQ`、会话 UMO 原样输出。平台列表来自 GET platforms，
// 失败降级为空（预览回退 aiocqhttp）。全部动态文本走 textContent 防 XSS。
// ==========================================================================

// 平台列表缓存（模块级）：首次拉取后缓存，避免每次渲染都请求。
let platformsCache = [];
let platformsLoaded = false;

// 拉取已注册平台实例列表 [{id,name}]；失败返回 []（降级，不抛）。
async function fetchPlatforms() {
    if (platformsLoaded) return platformsCache;
    try {
        const pl = await bridge.apiGet("platforms");
        platformsCache = (Array.isArray(pl) ? pl : []).map((p) => ({ id: p.id, name: p.name || p.id }));
    } catch (e) {
        platformsCache = [];
    }
    platformsLoaded = true;
    return platformsCache;
}

// 填充平台下拉（UMO 预览用）。返回当前选中平台 id：
// 有平台实例则优先选中 `aiocqhttp`，否则第一个；无平台实例时回退 aiocqhttp 并禁用。
async function initUmoPlatformSelect(selectEl) {
    const plats = await fetchPlatforms();
    selectEl.replaceChildren();
    const list = plats.length ? plats : [{ id: "aiocqhttp", name: "aiocqhttp" }];
    for (const p of list) {
        const opt = el("option", null, p.name || p.id);
        opt.value = p.id;
        selectEl.appendChild(opt);
    }
    if (selectEl.querySelector('option[value="aiocqhttp"]')) selectEl.value = "aiocqhttp";
    selectEl.disabled = !plats.length;
    return selectEl.value || "aiocqhttp";
}

// 纯函数：按 群号 / QQ / 会话三输入组合生成 UMO 预览文本（换行分隔多行）。
// - groupId 非空   → `${platformId}:GroupMessage:${groupId}`
// - userId 非空    → `${platformId}:FriendMessage:${userId}`
// - sessionId 非空 → 原样输出该行
// - 全部为空       → 返回 ""（由调用方用 emptyHint 兜底）
function umoPreviewText(platformId, groupId, userId, sessionId) {
    const pid = platformId || "aiocqhttp";
    const lines = [];
    const g = String(groupId || "").trim();
    const u = String(userId || "").trim();
    const s = String(sessionId || "").trim();
    if (g) lines.push(`${pid}:GroupMessage:${g}`);
    if (u) lines.push(`${pid}:FriendMessage:${u}`);
    if (s) lines.push(s);
    return lines.join("\n");
}

// 把 UMO 预览写入 container（textContent 写入防 XSS）。
// opts: {platformId, groupId, userId, sessionId, emptyHint}
function renderUmoPreview(container, opts = {}) {
    const { platformId, groupId, userId, sessionId, emptyHint } = opts;
    const text = umoPreviewText(platformId, groupId, userId, sessionId);
    container.textContent = text || String(emptyHint || "");
}

export { refData, refreshRefData, refreshGroupRefs, buildProviderSelect, buildGroupSelect, buildLifecycleSelect, buildTagInput, fetchPlatforms, initUmoPlatformSelect, umoPreviewText, renderUmoPreview };
