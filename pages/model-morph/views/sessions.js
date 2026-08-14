// ==========================================================================
// Model Morph · 会话视图（views/sessions.js）
// 表格：umo / 当前 Provider+模型名 / 当前组 / 轮数 / stage / lifecycle / 最近规则 / 校准 / 锁定
//   + 行操作 Lock（选组或 Provider 弹窗）/ Unlock / Reset / Remove / 详情（detailDialog）+ 搜索过滤。
// 锁定单元格显示锁定目标（lock_group_name 或 lock_provider_model）。
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
        // 校准列
        const calibTd = el("td");
        if (s.calibration_rounds_left > 0) {
            const calibName = s.calibration_group_name || s.calibration_group_id || "—";
            let calibTxt = calibName + " · 剩" + s.calibration_rounds_left + "轮";
            if (s.calibration_reason) calibTxt += " · " + s.calibration_reason;
            calibTd.appendChild(el("span", "badge info", calibTxt));
        } else {
            calibTd.appendChild(el("span", null, "—"));
        }
        row.appendChild(calibTd);
        const lockTd = el("td");
        lockTd.appendChild(s.locked
            ? el("span", "badge warning", t("pages.model-morph.sessions.lock_target", "锁定到") + " " + (s.lock_group_name || s.lock_provider_model || "—"))
            : el("span", "badge muted", t("pages.model-morph.sessions.lock", "未锁定")));
        row.appendChild(lockTd);
        const actTd = el("td", "cell-actions");
        const lockBtn = el("button", "btn btn-ghost btn-sm", s.locked ? t("pages.model-morph.sessions.unlock", "解锁") : t("pages.model-morph.sessions.lock", "锁定"));
        lockBtn.type = "button";
        lockBtn.addEventListener("click", () => s.locked ? unlock(s.umo) : doLock(s.umo));
        const resetBtn = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.sessions.reset", "重置"));
        resetBtn.type = "button"; resetBtn.addEventListener("click", () => reset(s.umo));
        const detailBtn = el("button", "btn btn-ghost btn-sm", t("pages.model-morph.sessions.detail", "详情"));
        detailBtn.type = "button"; detailBtn.addEventListener("click", () => detailDialog(s));
        const rmBtn = el("button", "btn btn-danger btn-sm", t("pages.model-morph.sessions.remove", "移除"));
        rmBtn.type = "button"; rmBtn.addEventListener("click", () => remove(s.umo));
        actTd.append(lockBtn, resetBtn, detailBtn, rmBtn);
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

// 详情弹窗：展示校准、最近切换、组游标与决策轨迹（文本一律 textContent / el()）
function detailDialog(s) {
    const mask = el("div", "modal-mask");
    const card = el("div", "modal-card");
    card.appendChild(el("div", "modal-title", t("pages.model-morph.sessions.detail", "详情")));

    // 最近切换
    const switchField = el("div", "form-field");
    switchField.appendChild(el("label", null, t("pages.model-morph.sessions.last_switch", "最近切换")));
    switchField.appendChild(el("div", null, formatEpoch(s.last_switch_at)));
    card.appendChild(switchField);

    // 校准
    const calibField = el("div", "form-field");
    calibField.appendChild(el("label", null, t("pages.model-morph.sessions.calibration", "校准")));
    const crl = s.calibration_rounds_left;
    let calibTxt = "—";
    if (crl > 0) {
        const name = s.calibration_group_name || s.calibration_group_id || "—";
        const parts = ["剩" + crl + "轮", name];
        if (s.calibration_reason) parts.push(s.calibration_reason);
        calibTxt = parts.join(" / ");
    }
    calibField.appendChild(el("div", null, calibTxt));
    card.appendChild(calibField);

    // 组游标
    const cursorField = el("div", "form-field");
    cursorField.appendChild(el("label", null, t("pages.model-morph.sessions.cursor", "组游标")));
    cursorField.appendChild(jsonPre(s.group_cursor));
    card.appendChild(cursorField);

    // 决策轨迹
    const traceField = el("div", "form-field");
    traceField.appendChild(el("label", null, t("pages.model-morph.sessions.trace", "决策轨迹")));
    traceField.appendChild(jsonPre(s.last_trace));
    card.appendChild(traceField);

    const actions = el("div", "modal-actions");
    const close = el("button", "btn btn-primary", t("pages.model-morph.audit.close", "关闭"));
    close.type = "button"; close.addEventListener("click", () => mask.remove());
    actions.appendChild(close);
    card.appendChild(actions);
    mask.appendChild(card);
    document.body.appendChild(mask);
}

// 格式化 epoch 秒 → "YYYY-MM-DD HH:MM"；非法或 <=0 返回 "—"
function formatEpoch(epochSec) {
    if (typeof epochSec !== "number" || !(epochSec > 0)) return "—";
    const dt = new Date(epochSec * 1000);
    if (Number.isNaN(dt.getTime())) return "—";
    const pad = (n) => String(n).padStart(2, "0");
    return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
}

// 构造 JSON 预览 pre：对象无键或非对象返回 "—"
function jsonPre(obj) {
    const pre = el("pre", "pending-op-pre mono");
    if (obj && typeof obj === "object" && Object.keys(obj).length > 0) {
        pre.textContent = JSON.stringify(obj, null, 2);
    } else {
        pre.textContent = "—";
    }
    return pre;
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
