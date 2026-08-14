// ==========================================================================
// Model Morph · 会话视图（views/sessions.js）
// 表格：umo / 当前 Provider+模型名 / 当前组 / 轮数 / stage / lifecycle / 最近规则 / 锁定
//   + 行操作 Lock（选组或 Provider 弹窗）/ Unlock / Reset / Remove + 搜索过滤。
// 全部动态文本走 textContent / el()，防 XSS；破坏性操作走 confirmDialog()。
// ==========================================================================
import { bridge, t, el, showToast, confirmDialog } from "../common.js";
import { refData, buildGroupSelect, buildProviderSelect } from "./shared.js";

let cache = [];
let seq = 0;

function paint() {
    const q = (document.getElementById("sessSearch").value || "").toLowerCase();
    const rows = cache.filter((s) => {
        if (!q) return true;
        return (s.umo + " " + (s.current_group_name || "") + " " + (s.current_provider_model || "")).toLowerCase().includes(q);
    });
    const body = document.getElementById("sessListBody");
    const empty = document.getElementById("sessListEmpty");
    body.replaceChildren();
    if (!rows.length) { empty.style.display = ""; return; }
    empty.style.display = "none";
    for (const s of rows) {
        const row = el("tr");
        row.appendChild(el("td", "mono", s.umo));
        row.appendChild(el("td", null, s.current_provider_model || s.current_provider_id || "—"));
        row.appendChild(el("td", null, s.current_group_name || s.current_group_id || "—"));
        row.appendChild(el("td", null, String(s.round ?? "")));
        row.appendChild(el("td", null, s.stage || "—"));
        row.appendChild(el("td", null, s.lifecycle_id || "—"));
        row.appendChild(el("td", null, s.last_rule_id || t("pages.model-morph.sessions.no_rule", "—")));
        const lockTd = el("td");
        lockTd.appendChild(s.locked
            ? el("span", "badge warning", t("pages.model-morph.sessions.locked", "已锁定"))
            : el("span", "badge muted", t("pages.model-morph.sessions.lock", "未锁定")));
        row.appendChild(lockTd);
        const actTd = el("td", "cell-actions");
        const lockBtn = el("button", "btn btn-ghost btn-sm", s.locked ? t("pages.model-morph.sessions.unlock", "解锁") : t("pages.model-morph.sessions.lock", "锁定"));
        lockBtn.type = "button";
        lockBtn.addEventListener("click", () => s.locked ? unlock(s.umo) : doLock(s.umo));
        const resetBtn = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.sessions.reset", "重置"));
        resetBtn.type = "button"; resetBtn.addEventListener("click", () => reset(s.umo));
        const rmBtn = el("button", "btn btn-danger btn-sm", t("pages.model-morph.sessions.remove", "移除"));
        rmBtn.type = "button"; rmBtn.addEventListener("click", () => remove(s.umo));
        actTd.append(lockBtn, resetBtn, rmBtn);
        row.appendChild(actTd);
        body.appendChild(row);
    }
}

async function load() {
    const mySeq = ++seq;
    try {
        cache = await bridge.apiGet("sessions");
        if (mySeq !== seq) return;
        cache = Array.isArray(cache) ? cache : [];
        paint();
    } catch (e) {
        if (mySeq !== seq) return;
        showToast(e.message || "未知错误", "error");
    }
}

// 锁定弹窗：选模型组（或 Provider），可同时留空其一
function lockTargetDialog(umo) {
    return new Promise((resolve) => {
        const mask = el("div", "modal-mask");
        const card = el("div", "modal-card");
        card.appendChild(el("div", "modal-title", t("pages.model-morph.sessions.lock_group", "锁定到模型组")));
        const gSel = buildGroupSelect("");
        card.appendChild(lbField(t("pages.model-morph.sessions.lock_group", "模型组"), gSel));
        const pSel = buildProviderSelect("");
        card.appendChild(lbField(t("pages.model-morph.sessions.lock_provider", "锁定到 Provider"), pSel));
        const actions = el("div", "modal-actions");
        const cancel = el("button", "btn btn-ghost", t("pages.model-morph.common.cancel", "取消"));
        cancel.type = "button"; cancel.addEventListener("click", () => { mask.remove(); resolve(null); });
        const ok = el("button", "btn btn-primary", t("pages.model-morph.common.confirm", "确认"));
        ok.type = "button"; ok.addEventListener("click", () => {
            const group_id = gSel.value;
            const provider_id = pSel.value;
            mask.remove();
            resolve({ group_id, provider_id });
        });
        actions.append(cancel, ok);
        card.appendChild(actions);
        mask.appendChild(card);
        document.body.appendChild(mask);
    });
}

function lbField(label, control) {
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(control);
    return f;
}

async function doLock(umo) {
    const pick = await lockTargetDialog(umo);
    if (!pick) return;
    if (!pick.group_id && !pick.provider_id) {
        showToast(t("pages.model-morph.sessions.need_lock_target", "请选择模型组或 Provider"), "error");
        return;
    }
    try {
        await bridge.apiPost("sessions/lock", { umo, group_id: pick.group_id, provider_id: pick.provider_id });
        showToast(t("pages.model-morph.common.save_success", "已保存"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function unlock(umo) {
    try {
        await bridge.apiPost("sessions/unlock", { umo });
        showToast(t("pages.model-morph.sessions.unlock", "已解锁"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function reset(umo) {
    const ok = await confirmDialog(
        t("pages.model-morph.sessions.confirmed_reset", "重置该会话的调度状态（round/stage 归零，保留锁定）？"),
        { title: t("pages.model-morph.sessions.reset", "重置会话"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("sessions/reset", { umo });
        showToast(t("pages.model-morph.sessions.reset", "已重置"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

async function remove(umo) {
    const ok = await confirmDialog(
        t("pages.model-morph.sessions.confirmed_remove", "确认移除该会话的全部调度状态？"),
        { title: t("pages.model-morph.sessions.remove", "移除会话"), okText: t("pages.model-morph.common.confirm", "确认") },
    );
    if (!ok) return;
    try {
        await bridge.apiPost("sessions/remove", { umo });
        showToast(t("pages.model-morph.common.delete", "已移除"), "success");
        await load();
    } catch (e) {
        showToast(e.message || "未知错误", "error");
    }
}

function bind() {
    document.getElementById("sessSearch").addEventListener("input", () => paint());
    document.getElementById("sessRefresh").addEventListener("click", load);
}

export { load, bind };
