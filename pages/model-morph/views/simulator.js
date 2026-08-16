// ==========================================================================
// Model Morph · 模拟器视图（views/simulator.js）
// 表单（时间/群/用户/UMO/轮数/事件/消息类型/@机器人/消息文本）→ POST simulate →
// 结果区：命中规则 / 被拒绝规则及失败条件 / 最终 Provider、组 / 原因 / 耗时。
// 规则列表「模拟」按钮 → rules.js 设置 pending → 本视图消费预填。
// 全部动态文本走 textContent / el()，防 XSS。
// ==========================================================================
import { bridge, t, el, showToast } from "../common.js";
import { refData, refreshRefData, initUmoPlatformSelect, renderUmoPreview } from "./shared.js";
import { consumePendingSim } from "./rules.js";

const S_SIM = "pages.model-morph.simulator";

// F2：在群 / 用户 / 会话输入下方注入平台下拉 + UMO 实时预览。
// 模拟器输入在 index.html 静态声明，故在 bind()（仅执行一次）注入，避免重复。
function injectUmoPreviews() {
    const grid = document.querySelector("#page-simulator .form-grid");
    if (!grid) return;
    // 防重复注入（bind 只执行一次，双保险）
    if (grid.querySelector(".umo-platform-field")) return;

    const umoPlatSel = el("select", "input input-select");
    const umoPlatField = el("div", "form-field umo-platform-field");
    umoPlatField.appendChild(el("label", null, t(`${S_SIM}.umo_platform`, "平台")));
    umoPlatField.appendChild(umoPlatSel);
    grid.prepend(umoPlatField);

    const refreshAll = [];
    for (const pid of ["simGroup", "simUser", "simUmo"]) {
        const input = document.getElementById(pid);
        if (!input) continue;
        const preview = el("div", "umo-preview");
        input.parentNode.appendChild(preview);

        const hint = t(`${S_SIM}.umo_preview_hint`, "输入群号/QQ 后显示 UMO 预览");
        const sessionHint = t(`${S_SIM}.umo_session_hint`, "会话 UMO 将原样使用");
        const refresh = () => {
            const val = input.value.trim();
            if (pid === "simGroup") {
                renderUmoPreview(preview, { platformId: umoPlatSel.value, groupId: val, userId: "", sessionId: "", emptyHint: hint });
            } else if (pid === "simUser") {
                renderUmoPreview(preview, { platformId: umoPlatSel.value, groupId: "", userId: val, sessionId: "", emptyHint: hint });
            } else {
                renderUmoPreview(preview, { platformId: "", groupId: "", userId: "", sessionId: val, emptyHint: sessionHint });
            }
        };
        input.addEventListener("input", refresh);
        refreshAll.push(refresh);
    }
    umoPlatSel.addEventListener("change", () => refreshAll.forEach((fn) => fn()));
    initUmoPlatformSelect(umoPlatSel).then(() => refreshAll.forEach((fn) => fn()));
}

function localDateTimeInput() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 预填表单（含规则「模拟」跳转）
function applyPrefill(ruleOverride) {
    const tEl = document.getElementById("simTime");
    if (!tEl.value) tEl.value = localDateTimeInput();
    const rule = ruleOverride || consumePendingSim();
    if (rule) {
        document.getElementById("simMessage").value = rule.name || "";
        document.getElementById("simRound").value = "0";
    }
}

function condListUl(results) {
    const ul = el("ul", "cond-list");
    for (const c of results || []) {
        const li = el("li");
        li.appendChild(el("span", c.matched ? "ok" : "no", c.matched ? "✓" : "✗"));
        li.appendChild(document.createTextNode(" " + (c.reason || "")));
        ul.appendChild(li);
    }
    return ul;
}

function renderResult(trace) {
    const box = document.getElementById("simResult");
    box.replaceChildren();
    box.appendChild(el("div", "editor-heading", t("pages.model-morph.simulator.result", "模拟结果")));
    const S = "pages.model-morph.simulator";

    // skipped
    if (trace.skipped_reason) {
        const card = el("div", "section-card");
        card.appendChild(el("span", "badge warning", t(`${S}.skipped`, "跳过调度") + ": " + trace.skipped_reason));
        box.appendChild(card);
    }

    // matched rule
    const matched = trace.matched_rule;
    if (matched) {
        const card = el("div", "section-card sim-rule-card matched");
        card.appendChild(el("strong", null, t(`${S}.matched_rule`, "命中规则") + ": " + (matched.name || matched.id)));
        card.appendChild(condListUl(trace.condition_results));
        box.appendChild(card);
    } else {
        const card = el("div", "section-card");
        card.appendChild(el("div", "empty-state", t(`${S}.no_matched_rule`, "未命中任何规则")));
        box.appendChild(card);
    }

    // rejected rules
    const rejected = trace.rejected_rules || [];
    if (rejected.length) {
        const card = el("div", "section-card");
        card.appendChild(el("div", "editor-heading", t(`${S}.rejected_rules`, "被拒绝规则")));
        for (const r of rejected) {
            const sub = el("div", "sim-rule-card rejected");
            sub.appendChild(el("strong", null, r.rule ? (r.rule.name || r.rule.id) : "?"));
            sub.appendChild(condListUl(r.results));
            card.appendChild(sub);
        }
        box.appendChild(card);
    }

    // decision summary
    const sumCard = el("div", "section-card");
    appendKV(sumCard, t(`${S}.final_provider`, "最终 Provider"), trace.final_provider_id || t("pages.model-morph.common.none", "无"));
    appendKV(sumCard, t(`${S}.final_group`, "最终模型组"), trace.final_group_id || t("pages.model-morph.common.none", "无"));
    appendKV(sumCard, t(`${S}.stage`, "阶段"), trace.stage || "—");
    appendKV(sumCard, t(`${S}.reason`, "决策原因"), trace.reason || "—");
    appendKV(sumCard, t(`${S}.elapsed_ms`, "耗时"), (trace.elapsed_ms ?? "") + " ms");
    box.appendChild(sumCard);
}

function appendKV(card, k, v) {
    const div = el("div", "key-value");
    div.appendChild(el("span", "k", k));
    div.appendChild(el("span", "v", String(v)));
    card.appendChild(div);
}

async function run() {
    const g = (id) => document.getElementById(id);
    const payload = {
        time_iso: g("simTime").value || undefined,
        group_id: g("simGroup").value,
        sender_id: g("simUser").value,
        umo: g("simUmo").value || "sim",
        round: Number(g("simRound").value || 0),
        lifecycle_event: g("simEvent").value,
        message_str: g("simMessage").value,
        message_type: g("simMtype").value,
        at_bot: g("simAtbot").checked,
    };
    const box = document.getElementById("simResult");
    box.replaceChildren(el("div", "loading", t("pages.model-morph.common.loading", "加载中…")));
    try {
        const trace = await bridge.apiPost("simulate", payload);
        renderResult(trace);
    } catch (e) {
        box.replaceChildren(el("div", "empty-state", e.message || "请求失败"));
        showToast(e.message || "未知错误", "error");
    }
}

async function load() {
    applyPrefill();
}

function bind() {
    document.getElementById("simRun").addEventListener("click", run);
    // F2：注入平台下拉 + 群/用户/会话 UMO 实时预览
    injectUmoPreviews();
    // 首次进入时刷新下拉参考数据（providers/groups/lifecycles）
    refreshRefData();
    // 已在模拟器页时的下一次模拟请求（规则「模拟」按钮派发）
    window.addEventListener("mm-simulate-request", (e) => applyPrefill(e.detail || null));
}

export { load, bind, refreshRefData };
