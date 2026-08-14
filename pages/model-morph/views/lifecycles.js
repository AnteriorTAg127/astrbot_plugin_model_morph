// ==========================================================================
// Model Morph · 生命周期视图（views/lifecycles.js）
// 列表（编辑/复制/删除）+ 新建/编辑面板：initial_group / initial_rounds /
// main_group / periodic_group / periodic_interval + 模板一键载入 + 启停。
// 全部动态文本走 textContent / el()，防 XSS；删除走 confirmDialog()。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";
import { refData, buildGroupSelect } from "./shared.js";

let seq = 0;
let draft = null;

function stateBadge(on) {
    return on ? el("span", "badge success", t("pages.model-morph.common.enabled", "启用"))
              : el("span", "badge muted", t("pages.model-morph.common.disabled", "禁用"));
}

function actionsRow(it) {
    const box = el("div", "cell-actions");
    const edit = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.common.edit", "编辑"));
    edit.type = "button"; edit.addEventListener("click", () => openEditor(JSON.parse(JSON.stringify(it))));
    const dup = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.common.duplicate", "复制"));
    dup.type = "button"; dup.addEventListener("click", () => duplicate(it.id));
    const del = el("button", "btn btn-danger btn-sm", t("pages.model-morph.common.delete", "删除"));
    del.type = "button"; del.addEventListener("click", () => removeLifecycle(it.id, it));
    box.append(edit, dup, del);
    return box;
}

function paintList(items) {
    const body = document.getElementById("lcListBody");
    const empty = document.getElementById("lcListEmpty");
    body.replaceChildren();
    if (!items.length) { empty.style.display = ""; return; }
    empty.style.display = "none";
    for (const it of items) {
        const row = el("tr");
        const nameCell = el("td");
        nameCell.appendChild(el("strong", "cell-name", it.name || it.id));
        nameCell.appendChild(el("div", "hint mono", it.id));
        row.appendChild(nameCell);
        const staged = Array.isArray(it.stages) && it.stages.length > 0;
        row.appendChild(el("td", null, staged
            ? t("pages.model-morph.lifecycles.mode_staged", "多阶段")
            : t("pages.model-morph.lifecycles.mode_classic", "经典")));
        row.appendChild(el("td", null, `${it.initial_group || "—"} × ${it.initial_rounds}`));
        row.appendChild(el("td", null, it.main_group || "—"));
        row.appendChild(el("td", null, `${it.periodic_group || "—"} / ${it.periodic_interval}`));
        const enTd = el("td");
        enTd.appendChild(stateBadge(it.enabled));
        row.appendChild(enTd);
        row.appendChild(el("td")).appendChild(actionsRow(it));
        body.appendChild(row);
    }
}

async function load() {
    const mySeq = ++seq;
    const tbody = document.getElementById("lcListBody");
    const lr = el("tr"); const td = el("td", "loading", t("pages.model-morph.common.loading", "加载中…"));
    td.colSpan = 7; lr.appendChild(td);
    tbody.replaceChildren(lr);
    try {
        const items = await bridge.apiGet("lifecycles");
        if (mySeq !== seq) return;
        refData.lifecycleOptions = (Array.isArray(items) ? items : []).map((l) => ({ id: l.id, name: l.name || l.id }));
        paintList(Array.isArray(items) ? items : []);
    } catch (e) {
        if (mySeq !== seq) return;
        showToast(e.message || "未知错误", "error");
    }
}

// ── 编辑面板 ─────────────────────────────────────────
function grpSelect(key, value) {
    const sel = buildGroupSelect(value);
    sel.addEventListener("change", () => { draft[key] = sel.value; });
    return sel;
}

function renderEditor() {
    const box = document.getElementById("lcEditor");
    box.replaceChildren();
    const card = el("div", "section-card");
    card.appendChild(el("div", "section-title", t("pages.model-morph.lifecycles.title", "生命周期策略")));
    const grid = el("div", "form-grid");

    const nameInp = document.createElement("input");
    nameInp.type = "text"; nameInp.className = "input"; nameInp.value = draft.name || "";
    nameInp.addEventListener("input", () => { draft.name = nameInp.value; });
    grid.appendChild(lbField(t("pages.model-morph.common.name", "名称"), nameInp));

    const enF = el("div", "form-field");
    const enLbl = el("label", "all-mode-switch");
    const enChk = document.createElement("input"); enChk.type = "checkbox"; enChk.checked = !!draft.enabled;
    enChk.addEventListener("change", () => { draft.enabled = enChk.checked; });
    enLbl.appendChild(enChk);
    const track = el("span", "switch-track"); track.appendChild(el("span", "switch-thumb"));
    enLbl.appendChild(track);
    enLbl.appendChild(el("span", null, t("pages.model-morph.lifecycles.enable_lifecycle", "启用")));
    enF.appendChild(enLbl);
    grid.appendChild(enF);

    grid.appendChild(lbField(t("pages.model-morph.lifecycles.initial_group", "初始组"), grpSelect("initial_group", draft.initial_group)));
    grid.appendChild(lbIntField(t("pages.model-morph.lifecycles.initial_rounds", "初始轮数"), draft.initial_rounds ?? 2, (v) => { draft.initial_rounds = v; }));
    grid.appendChild(lbField(t("pages.model-morph.lifecycles.main_group", "主组"), grpSelect("main_group", draft.main_group)));
    grid.appendChild(lbField(t("pages.model-morph.lifecycles.periodic_group", "周期组"), grpSelect("periodic_group", draft.periodic_group)));
    grid.appendChild(lbIntField(t("pages.model-morph.lifecycles.periodic_interval", "周期间隔(轮)"), draft.periodic_interval ?? 5, (v) => { draft.periodic_interval = v; }));
    card.appendChild(grid);

    // ── 多阶段降级区块 ────────────────────────────────
    const stagedCard = el("div", "section-card");
    stagedCard.appendChild(el("div", "section-title", t("pages.model-morph.lifecycles.stages", "多阶段降级")));

    // final_group：阶段耗尽后使用的组（含「无」空值）
    const fgSel = buildGroupSelect(draft.final_group);
    fgSel.addEventListener("change", () => { draft.final_group = fgSel.value; });
    stagedCard.appendChild(lbField(t("pages.model-morph.lifecycles.final_group", "最终组（阶段耗尽后）"), fgSel));

    // stages 行列表：每行 = buildGroupSelect + rounds 数字(min 1) + 删除
    const stagesBox = el("div", "stages-box");
    const stagesLabel = el("div", "form-field-label", t("pages.model-morph.lifecycles.rounds", "轮数"));
    stagesBox.appendChild(stagesLabel);
    const stagesList = el("div", "stages-list");
    stagesBox.appendChild(stagesList);
    const addBtn = el("button", "btn btn-ghost btn-sm", "[+] " + t("pages.model-morph.lifecycles.add_stage", "添加阶段"));
    addBtn.type = "button";
    addBtn.addEventListener("click", () => appendStageRow(stagesList, { group_id: "", rounds: 1 }));
    stagesBox.appendChild(addBtn);
    stagedCard.appendChild(stagesBox);

    // 按 draft.stages 渲染已有行
    const prevStages = Array.isArray(draft.stages) ? draft.stages : [];
    for (const s of prevStages) appendStageRow(stagesList, s);

    // ── 事件校准区块 ─────────────────────────────────
    const calCard = el("div", "section-card");
    calCard.appendChild(el("div", "section-title", t("pages.model-morph.lifecycles.calibration", "事件校准")));

    const ceSel = document.createElement("select");
    ceSel.className = "input input-select";
    const ceNone = el("option", null, t("pages.model-morph.lifecycles.calibration_none", "关闭"));
    ceNone.value = "";
    if (!draft.calibration_event) ceNone.selected = true;
    ceSel.appendChild(ceNone);
    const ceComp = el("option", null, t("pages.model-morph.lifecycles.calibration_compression", "上下文压缩后"));
    ceComp.value = "context_compression";
    if (draft.calibration_event === "context_compression") ceComp.selected = true;
    ceSel.appendChild(ceComp);
    ceSel.addEventListener("change", () => { draft.calibration_event = ceSel.value; });
    calCard.appendChild(lbField(t("pages.model-morph.lifecycles.calibration_event", "校准触发事件"), ceSel));

    const cgSel = buildGroupSelect(draft.calibration_group);
    cgSel.addEventListener("change", () => { draft.calibration_group = cgSel.value; });
    calCard.appendChild(lbField(t("pages.model-morph.lifecycles.calibration_group", "校准组"), cgSel));

    const crInp = document.createElement("input");
    crInp.type = "number"; crInp.min = "1"; crInp.step = "1"; crInp.className = "input input-sm";
    crInp.value = String(draft.calibration_rounds ?? 0);
    crInp.addEventListener("change", () => {
        const n = Number(crInp.value);
        draft.calibration_rounds = Number.isNaN(n) || n < 1 ? 0 : n;
    });
    calCard.appendChild(lbField(t("pages.model-morph.lifecycles.calibration_rounds", "校准轮数"), crInp));

    card.appendChild(stagedCard);
    card.appendChild(calCard);

    const actions = el("div", "toolbar");
    const save = el("button", "btn btn-primary", t("pages.model-morph.lifecycles.save_lifecycle", "保存生命周期"));
    save.type = "button"; save.addEventListener("click", () => saveLifecycle());
    const cancel = el("button", "btn btn-ghost", t("pages.model-morph.common.cancel", "取消"));
    cancel.type = "button"; cancel.addEventListener("click", closeEditor);
    actions.append(save, cancel);
    card.appendChild(actions);
    box.appendChild(card);
}

// 追加/渲染一行 stages 条目；返回该行容器（供 add 与 remove 复用）
// appendStageRow 构建 buildGroupSelect + rounds 数字(min 1) + 删除按钮，
// 把控件引用挂到行 data 上，saveLifecycle() 统一收集。
function appendStageRow(listEl, stage) {
    const row = el("div", "stage-row");
    const groupSel = buildGroupSelect(stage.group_id);
    const roundsInp = document.createElement("input");
    roundsInp.type = "number"; roundsInp.min = "1"; roundsInp.step = "1";
    roundsInp.className = "input input-sm";
    roundsInp.value = String(stage.rounds && stage.rounds >= 1 ? stage.rounds : 1);
    const del = el("button", "btn btn-danger btn-sm", t("pages.model-morph.common.delete", "删除"));
    del.type = "button";
    del.addEventListener("click", () => row.remove());
    row.appendChild(groupSel);
    row.appendChild(roundsInp);
    row.appendChild(del);
    // 收集元数据：本轮保存时从行内控件取值
    row._groupSel = groupSel;
    row._roundsInp = roundsInp;
    listEl.appendChild(row);
    return row;
}

function lbField(label, control) {
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(control);
    return f;
}

function lbIntField(label, value, onChange) {
    const inp = document.createElement("input");
    inp.type = "number"; inp.step = "1"; inp.className = "input input-sm"; inp.value = String(value ?? 0);
    inp.addEventListener("change", () => {
        const n = Number(inp.value);
        onChange(Number.isNaN(n) ? 0 : n);
    });
    return lbField(label, inp);
}

function openEditor(lc) {
    draft = lc;
    renderEditor();
}

// 载入模板：拉取模板列表 → 自建选择弹窗（el() 构建，Promise 返回）
function loadTemplates() {
    return new Promise((resolve) => {
        const mask = el("div", "modal-mask");
        const card = el("div", "modal-card");
        card.appendChild(el("div", "modal-title", t("pages.model-morph.lifecycles.load_template", "载入模板")));
        const sel = document.createElement("select");
        sel.className = "input input-select";
        card.appendChild(lbField(t("pages.model-morph.common.name", "模板"), sel));
        const actions = el("div", "modal-actions");
        const cancel = el("button", "btn btn-ghost", t("pages.model-morph.common.cancel", "取消"));
        cancel.type = "button"; cancel.addEventListener("click", () => { mask.remove(); resolve(null); });
        const ok = el("button", "btn btn-primary", t("pages.model-morph.common.confirm", "确认"));
        ok.type = "button"; ok.addEventListener("click", () => { const v = sel.value; mask.remove(); resolve(v); });
        actions.append(cancel, ok);
        card.appendChild(actions);
        mask.appendChild(card);
        document.body.appendChild(mask);
    });
}

async function chooseTemplate() {
    let tmpls;
    try {
        tmpls = await bridge.apiGet("lifecycles/templates");
    } catch (e) {
        showToast(e.message || "未知错误", "error");
        return;
    }
    if (!tmpls || typeof tmpls !== "object") { showToast("无可用模板", "error"); return; }
    const keys = Object.keys(tmpls);
    if (!keys.length) { showToast(t("pages.model-morph.common.empty", "暂无数据"), "error"); return; }
    const pick = await loadTemplates();
    if (!pick) return;
    const src = tmpls[pick];
    if (!src) return;
    openEditor({
        id: "", name: src.name || pick, enabled: true, initial_group: "", initial_rounds: src.initial_rounds || 2,
        main_group: "", periodic_group: "", periodic_interval: src.periodic_interval || 5,
        stages: [], final_group: "", calibration_event: "", calibration_group: "", calibration_rounds: 0,
    });
    showToast(t("pages.model-morph.lifecycles.template_hint", "模板提供名称与轮数，组字段载入后由你填写"), "success");
}

async function saveLifecycle() {
    // 收集 stages 行（过滤 group_id 空的行），校准字段已由控件写入 draft
    const rows = Array.from(document.querySelectorAll(".stages-list .stage-row"));
    draft.stages = rows
        .map((r) => ({
            group_id: r._groupSel ? r._groupSel.value : "",
            rounds: r._roundsInp ? Number(r._roundsInp.value || 1) : 1,
        }))
        .filter((s) => !!s.group_id);
    const payload = JSON.parse(JSON.stringify(draft));
    try {
        await bridge.apiPost("lifecycles/save", payload);
        showToast(t("pages.model-morph.common.save_success", "保存成功"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function duplicate(id) {
    try {
        await bridge.apiPost("lifecycles/duplicate", { id });
        showToast(t("pages.model-morph.common.copy_success", "复制成功"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function removeLifecycle(id, it) {
    const ok = await confirmDialog(
        t("pages.model-morph.lifecycles.delete_lifecycle", "删除生命周期") + "「" + (it.name || id) + "」？" + t("pages.model-morph.common.confirm_delete", "此操作不可撤销。"),
        { title: t("pages.model-morph.lifecycles.delete_lifecycle", "删除生命周期"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("lifecycles/delete", { id });
        showToast(t("pages.model-morph.common.delete", "已删除"), "success");
        closeEditor();
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

function closeEditor() {
    draft = null;
    document.getElementById("lcEditor").replaceChildren();
}

function bind() {
    document.getElementById("lcNew").addEventListener("click", () => {
        openEditor({
            id: "", name: "", enabled: true, initial_group: "", initial_rounds: 2,
            main_group: "", periodic_group: "", periodic_interval: 5,
            stages: [], final_group: "", calibration_event: "", calibration_group: "", calibration_rounds: 0,
        });
    });
    document.getElementById("lcTemplates").addEventListener("click", chooseTemplate);
}

export { load, bind };
