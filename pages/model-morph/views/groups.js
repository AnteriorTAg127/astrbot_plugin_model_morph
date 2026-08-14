// ==========================================================================
// Model Morph · 模型组视图（views/groups.js）
// 列表（启停 + 编辑/复制/删除）+ 新建/编辑面板：策略下拉、成员表格、fallbacks 多选。
// 成员数单元格：数字 + 备注 hint 摘要（取自各成员 note，截断 24 字符）。
// 全部动态文本走 textContent / el()，防 XSS。删除为破坏性操作 → confirmDialog()。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";
import { refData, buildProviderSelect } from "./shared.js";

const STRATEGIES = ["priority", "round_robin", "weighted", "random", "fallback"];
let seq = 0;                 // 异步竞态序号
let draft = null;            // 编辑中的组草稿

function freshMember() {
    return { provider_id: "", model_override: "", priority: 0, weight: 1, max_uses: 0, cooldown_seconds: 0, enabled: true, note: "" };
}

function strategyLabel(id) {
    return t(`pages.model-morph.groups.strategy_${id}`, id);
}

// 状态徽章：只显示当前态
function stateBadge(on) {
    return el("span", on ? "badge success" : "badge muted",
        on ? t("pages.model-morph.common.enabled", "启用") : t("pages.model-morph.common.disabled", "禁用"));
}

function groupActionsRow(g) {
    const box = el("div", "cell-actions");
    const edit = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.common.edit", "编辑"));
    edit.type = "button"; edit.addEventListener("click", () => openEditor(JSON.parse(JSON.stringify(g))));
    const dup = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.common.duplicate", "复制"));
    dup.type = "button"; dup.addEventListener("click", () => duplicate(g.id));
    const del = el("button", "btn btn-danger btn-sm", t("pages.model-morph.common.delete", "删除"));
    del.type = "button"; del.addEventListener("click", () => removeGroup(g.id, g));
    box.append(edit, dup, del);
    return box;
}

// 渲染列表（替换 tbody / 空态）
function paintList(groups) {
    const body = document.getElementById("groupListBody");
    const empty = document.getElementById("groupListEmpty");
    body.replaceChildren();
    if (!groups.length) {
        empty.style.display = "";
        return;
    }
    empty.style.display = "none";
    for (const g of groups) {
        const row = el("tr");
        const nameCell = el("td");
        nameCell.appendChild(el("strong", "cell-name", g.name || g.id));
        if (g.desc) nameCell.appendChild(el("div", "hint", g.desc));
        row.appendChild(nameCell);
        row.appendChild(el("td", "mono", g.id));
        row.appendChild(el("td", null, strategyLabel(g.strategy)));
        const memCell = el("td");
        memCell.appendChild(el("span", null, String((g.providers || []).length)));
        const notes = (g.providers || [])
            .map((m) => (m.note || "").trim())
            .filter(Boolean);
        if (notes.length) {
            const joined = notes.join(" / ");
            memCell.appendChild(el("div", "hint",
                t("pages.model-morph.groups.member_notes", "备注") + "：" +
                joined.slice(0, 24) + (joined.length > 24 ? "…" : "")));
        }
        row.appendChild(memCell);
        row.appendChild(el("td")).appendChild(stateBadge(g.enabled));
        row.appendChild(el("td")).appendChild(groupActionsRow(g));
        body.appendChild(row);
    }
}

async function load() {
    const mySeq = ++seq;
    const tbody = document.getElementById("groupListBody");
    const loadRow = el("tr");
    const loadTd = el("td", "loading", t("pages.model-morph.common.loading", "加载中…"));
    loadTd.colSpan = 6;
    loadRow.appendChild(loadTd);
    tbody.replaceChildren(loadRow);
    try {
        // v0.1.7：groups 拉取失败不再静默吞掉（旧行为会显示「暂无模型组」误导），
        // 改为 toast 提示并渲染错误空态。
        let groups = [];
        let providers = [];
        try {
            groups = await bridge.apiGet("groups");
        } catch (e) {
            showToast(
                (e.message || "") + " — " + t("pages.model-morph.groups.load_failed", "模型组列表加载失败"),
                "error",
            );
        }
        try {
            providers = await bridge.apiGet("providers");
        } catch (e) {
            showToast(t("pages.model-morph.common.providers_failed", "Provider 列表加载失败"), "error");
        }
        if (mySeq !== seq) return;
        refData.providers = Array.isArray(providers) ? providers : [];
        refData.providerOptions = refData.providers.map((p) => ({ id: p.id, name: p.model || p.id }));
        refData.groupOptions = (Array.isArray(groups) ? groups : []).map((g) => ({ id: g.id, name: g.name || g.id }));
        paintList(Array.isArray(groups) ? groups : []);
    } catch (e) {
        if (mySeq !== seq) return;
        showToast(e.message || "未知错误", "error");
    }
}

// ── 编辑面板 ──────────────────────────────────────────────

function openEditor(g) {
    if (!g.providers || !g.providers.length) g.providers = [freshMember()];
    if (!g.fallbacks) g.fallbacks = [];
    if (!g.strategy) g.strategy = "priority";
    draft = g;
    renderEditor();
}

function renderEditor() {
    const box = document.getElementById("groupEditor");
    box.replaceChildren();
    const card = el("div", "section-card");
    card.appendChild(el("div", "section-title", t("pages.model-morph.groups.member_table", "组成员")));
    const grid = el("div", "form-grid");
    grid.appendChild(field(t("pages.model-morph.common.name", "名称"), textInput(draft.name, "name")));
    grid.appendChild(strategyField());
    grid.appendChild(checkField(t("pages.model-morph.common.enabled", "启用"), draft.enabled, "enabled"));
    grid.appendChild(checkField(t("pages.model-morph.groups.allow_fallback", "允许自动降级"), draft.allow_auto_fallback, "allow_auto_fallback"));
    card.appendChild(grid);
    const desc = field(t("pages.model-morph.groups.group_desc", "组描述"), textInput(draft.desc || "", "desc"));
    card.appendChild(desc);
    card.appendChild(memberTable());
    const addBtn = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.groups.add_member", "添加成员"));
    addBtn.type = "button";
    addBtn.addEventListener("click", () => { draft.providers.push(freshMember()); renderEditor(); });
    const tb = el("div", "toolbar");
    tb.appendChild(addBtn);
    card.appendChild(tb);

    // fallbacks 多选
    card.appendChild(el("div", "editor-heading", t("pages.model-morph.groups.fallbacks_label", "降级 Provider")));
    const fbSelect = el("select", "multi-select");
    fbSelect.multiple = true;
    for (const p of refData.providers) {
        const opt = el("option", null, p.model || p.id);
        opt.value = p.id;
        if (draft.fallbacks.includes(p.id)) opt.selected = true;
        fbSelect.appendChild(opt);
    }
    if (!refData.providers.length) {
        const opt = el("option", null, t("pages.model-morph.groups.no_providers", "暂无 Provider 可选"));
        opt.value = ""; opt.disabled = true; opt.selected = true;
        fbSelect.appendChild(opt);
    }
    card.appendChild(fbSelect);
    card.appendChild(el("div", "hint", t("pages.model-morph.groups.fallbacks_hint", "组内无可用成员且允许自动降级时按序使用")));

    const actions = el("div", "toolbar");
    const save = el("button", "btn btn-primary", t("pages.model-morph.groups.save_group", "保存模型组"));
    save.type = "button"; save.addEventListener("click", () => saveGroup(fbSelect));
    const cancel = el("button", "btn btn-ghost", t("pages.model-morph.common.cancel", "取消"));
    cancel.type = "button"; cancel.addEventListener("click", closeEditor);
    actions.append(save, cancel);
    card.appendChild(actions);
    box.appendChild(card);
}

function field(label, control) {
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(control);
    return f;
}

function textInput(value, key) {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.value = value == null ? "" : String(value);
    inp.className = "input";
    inp.addEventListener("input", () => {
        const v = inp.value;
        if (key === "desc" || key === "name") draft[key] = v;
        else {
            const n = Number(v);
            draft[key] = v === "" || Number.isNaN(n) ? v : n;
        }
    });
    return inp;
}

function numberInput(value, key, min) {
    const inp = document.createElement("input");
    inp.type = "number";
    inp.value = String(value ?? 0);
    inp.min = String(min ?? 0);
    inp.step = "1";
    inp.className = "input input-sm";
    inp.addEventListener("change", () => {
        const n = Number(inp.value);
        draft[key] = Number.isNaN(n) ? 0 : n;
    });
    return inp;
}

function strategyField() {
    const sel = document.createElement("select");
    sel.className = "input input-select";
    for (const s of STRATEGIES) {
        const opt = el("option", null, strategyLabel(s));
        opt.value = s;
        if (draft.strategy === s) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.addEventListener("change", () => { draft.strategy = sel.value; });
    return field(t("pages.model-morph.groups.strategy", "策略"), sel);
}

function checkField(label, checked, key) {
    const lbl = el("label", "all-mode-switch");
    const inp = document.createElement("input");
    inp.type = "checkbox";
    inp.checked = !!checked;
    inp.addEventListener("change", () => { draft[key] = inp.checked; });
    lbl.appendChild(inp);
    lbl.appendChild(el("span", "switch-track")).appendChild(el("span", "switch-thumb"));
    lbl.appendChild(el("span", null, label));
    const f = el("div", "form-field");
    f.appendChild(lbl);
    return f;
}

// 成员表格：header 行 + 每成员一行（grid 布局）
function memberTable() {
    const wrap = el("div");
    const header = el("div", "member-grid-header");
    for (const k of [
        "groups.provider", "groups.model_override", "groups.priority", "groups.weight",
        "groups.max_uses", "groups.cooldown", "common.enabled", "groups.note", "",
    ]) {
        header.appendChild(el("div", null, k ? t(`pages.model-morph.${k}`, k.split(".")[1]) : ""));
    }
    wrap.appendChild(header);
    draft.providers.forEach((m, idx) => wrap.appendChild(memberRow(m, idx)));
    return wrap;
}

function memberRow(m, idx) {
    const row = el("div", "member-grid");
    // provider 下拉（el() 构建，无注入面）
    const pSel = buildProviderSelect(m.provider_id, { className: "" });
    pSel.value = m.provider_id;
    pSel.addEventListener("change", () => { m.provider_id = pSel.value; });
    row.appendChild(pSel);
    row.appendChild(mgText(m, "model_override", String(m.model_override || "")));
    row.appendChild(mgNumber(m, "priority", m.priority ?? 0));
    row.appendChild(mgNumber(m, "weight", m.weight ?? 1));
    row.appendChild(mgNumber(m, "max_uses", m.max_uses ?? 0));
    row.appendChild(mgNumber(m, "cooldown_seconds", m.cooldown_seconds ?? 0));
    // enabled checkbox
    const en = el("div", "mg-enable");
    const chk = document.createElement("input");
    chk.type = "checkbox"; chk.checked = !!m.enabled;
    chk.addEventListener("change", () => { m.enabled = chk.checked; });
    en.appendChild(chk);
    row.appendChild(en);
    row.appendChild(mgText(m, "note", String(m.note || "")));
    // remove
    const rm = el("button", "btn btn-danger btn-sm mg-remove", "✕");
    rm.type = "button";
    rm.addEventListener("click", () => {
        draft.providers.splice(idx, 1);
        if (!draft.providers.length) draft.providers = [freshMember()];
        renderEditor();
    });
    row.appendChild(rm);
    return row;
}

function mgText(m, key, value) {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.value = value;
    inp.addEventListener("input", () => { m[key] = inp.value; });
    return inp;
}

function mgNumber(m, key, value) {
    const inp = document.createElement("input");
    inp.type = "number";
    inp.value = String(value ?? 0);
    inp.addEventListener("change", () => {
        const n = Number(inp.value);
        m[key] = Number.isNaN(n) ? 0 : n;
    });
    return inp;
}

async function saveGroup(fbSelect) {
    draft.fallbacks = Array.from(fbSelect.selectedOptions).map((o) => o.value);
    // 前端基础校验：至少一个成员且成员须选中 Provider
    const validMembers = draft.providers.filter((m) => m.provider_id);
    if (!validMembers.length) {
        showToast(t("pages.model-morph.groups.need_member_provider", "请为至少一个成员选择 Provider"), "error");
        return;
    }
    const payload = JSON.parse(JSON.stringify(draft));
    try {
        await bridge.apiPost("groups/save", payload);
        showToast(t("pages.model-morph.common.save_success", "保存成功"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function duplicate(id) {
    try {
        await bridge.apiPost("groups/duplicate", { id });
        showToast(t("pages.model-morph.common.copy_success", "复制成功"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function removeGroup(id, g) {
    const ok = await confirmDialog(
        t("pages.model-morph.groups.delete_group", "删除模型组") + "「" + (g.name || id) + "」？" + t("pages.model-morph.common.confirm_delete", "此操作不可撤销。"),
        { title: t("pages.model-morph.groups.delete_group", "删除模型组"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("groups/delete", { id });
        showToast(t("pages.model-morph.common.delete", "已删除"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

function closeEditor() {
    draft = null;
    document.getElementById("groupEditor").replaceChildren();
}

function bind() {
    document.getElementById("groupNew").addEventListener("click", () => {
        openEditor({ id: "", name: "", desc: "", enabled: true, strategy: "priority", allow_auto_fallback: false, providers: [freshMember()], fallbacks: [] });
    });
    document.getElementById("groupRefresh").addEventListener("click", () => {
        load().catch((e) => showToast(e.message || "未知错误", "error"));
    });
}

export { load, bind };
