// ==========================================================================
// Model Morph · 设置视图（views/settings.js）
// 表单范式：加载 → render → 收集 → 校验 → 保存；保存成功 toast + 刷新。
// timezone(auto+常见 IANA) / base_group(组下拉) / log_retention / state_persist。
// enabled / debug 由 AstrBot 原生配置面板（_conf_schema.json）持有，此处只读展示
// （settings GET 已实时注入原生值，页面不提交这两个字段）。
// 导出 bridge.download("export", {}, 文件名)；导入读取文件 → apiPost("import", {content})，
// 严格对照 web/api.py _handler_import 契约（content 须为配置对象）。
// 全部动态文本走 textContent / el()，防 XSS。
// ==========================================================================
import { bridge, t, el, showToast } from "../common.js";
import { refData } from "./shared.js";

const TZ_VALUES = ["Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore", "Asia/Kolkata", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "UTC", "GMT"];

// 渲染出的控件引用（收集时读取；避免用脆弱的 DOM 选择器）
let ctl = {
    timezone: null, base_group: null, log_retention: null, state_persist: null,
    agent_confirm: null, audit_retention: null,
    agent_provider_id: null, default_lifecycle: null,
};

function lbField(label, control) {
    const f = el("div", "form-field");
    f.appendChild(el("label", null, label));
    f.appendChild(control);
    return f;
}

function switchControl(checked, onChange) {
    const lbl = el("label", "all-mode-switch");
    const inp = document.createElement("input");
    inp.type = "checkbox"; inp.checked = !!checked;
    inp.addEventListener("change", () => onChange(inp.checked));
    lbl.appendChild(inp);
    const track = el("span", "switch-track"); track.appendChild(el("span", "switch-thumb"));
    lbl.appendChild(track);
    return { el: lbl, inp };
}

function renderSettings(st) {
    const form = document.getElementById("settingsForm");
    form.replaceChildren();
    const S = "pages.model-morph.settings";

    // enabled / debug 由 AstrBot 原生配置面板（_conf_schema.json）持有，只读展示。
    const roItem = (name, desc, on) => {
        const item = el("div", "setting-item");
        const info = el("div", "setting-info");
        info.appendChild(el("div", "setting-name", name));
        info.appendChild(el("div", "setting-desc", desc));
        item.appendChild(info);
        const ctlDiv = el("div", "setting-control");
        ctlDiv.appendChild(
            el("span", on ? "badge success" : "badge muted",
                on ? t(`${S}.ro_on`, "开启") : t(`${S}.ro_off`, "关闭"))
        );
        item.appendChild(ctlDiv);
        form.appendChild(item);
    };
    const nativeHint = t(`${S}.native_hint`, "此开关由 AstrBot 插件配置面板管理，此处只读展示");
    roItem(t(`${S}.enabled`, "启用调度器"), nativeHint, !!st.enabled);
    roItem(t(`${S}.debug`, "调试模式"), nativeHint, !!st.debug);

    // 时区
    const tzSel = document.createElement("select");
    tzSel.className = "input input-select";
    const auto = el("option", null, t(`${S}.timezone_auto`, "自动（跟随 AstrBot）"));
    auto.value = "auto";
    if (st.timezone === "auto" || !st.timezone) auto.selected = true;
    tzSel.appendChild(auto);
    for (const z of TZ_VALUES) {
        const opt = el("option", null, z);
        opt.value = z;
        if (st.timezone === z) opt.selected = true;
        tzSel.appendChild(opt);
    }
    tzSel.addEventListener("change", () => { st.timezone = tzSel.value; });
    ctl.timezone = tzSel;
    form.appendChild(lbField(t(`${S}.timezone`, "时区"), tzSel));

    // 默认模型组
    const bgSel = document.createElement("select");
    bgSel.className = "input input-select";
    const none = el("option", null, t(`${S}.base_group_none`, "（不干预，继承原生行为）"));
    none.value = "";
    if (!st.base_group) none.selected = true;
    bgSel.appendChild(none);
    for (const g of refData.groupOptions) {
        const opt = el("option", null, g.name);
        opt.value = g.id;
        if (st.base_group === g.id) opt.selected = true;
        bgSel.appendChild(opt);
    }
    bgSel.addEventListener("change", () => { st.base_group = bgSel.value; });
    ctl.base_group = bgSel;
    form.appendChild(lbField(t(`${S}.base_group`, "默认模型组"), bgSel));

    // 配置助手模型（agent_provider_id；空 = 跟随默认聊天 Provider）
    const apSel = document.createElement("select");
    apSel.className = "input input-select";
    const apNone = el("option", null, t(`${S}.agent_provider_none`, "默认（跟随聊天模型）"));
    apNone.value = "";
    if (st.agent_provider_id === undefined || st.agent_provider_id === "") apNone.selected = true;
    apSel.appendChild(apNone);
    for (const p of refData.providers) {
        const opt = el("option", null, p.model || p.id);
        opt.value = p.id;
        if (st.agent_provider_id === p.id) opt.selected = true;
        apSel.appendChild(opt);
    }
    apSel.addEventListener("change", () => { st.agent_provider_id = apSel.value; });
    ctl.agent_provider_id = apSel;
    form.appendChild(lbField(t(`${S}.agent_provider`, "配置助手模型"), apSel));
    form.appendChild(el("div", "setting-desc", t(`${S}.agent_provider_hint`, "Web 配置助手与 SubAgent 使用的模型，空则跟随默认聊天 Provider")));

    // 全局默认生命周期（default_lifecycle；空 = 不启用）
    const dlSel = document.createElement("select");
    dlSel.className = "input input-select";
    const dlNone = el("option", null, t(`${S}.default_lifecycle_none`, "无"));
    dlNone.value = "";
    if (st.default_lifecycle === undefined || st.default_lifecycle === "") dlNone.selected = true;
    dlSel.appendChild(dlNone);
    for (const l of refData.lifecycleOptions) {
        const opt = el("option", null, l.name);
        opt.value = l.id;
        if (st.default_lifecycle === l.id) opt.selected = true;
        dlSel.appendChild(opt);
    }
    dlSel.addEventListener("change", () => { st.default_lifecycle = dlSel.value; });
    ctl.default_lifecycle = dlSel;
    form.appendChild(lbField(t(`${S}.default_lifecycle`, "全局默认生命周期"), dlSel));
    form.appendChild(el("div", "setting-desc", t(`${S}.default_lifecycle_hint`, "设置后所有无显式生命周期的会话按此降级预设调度，空则不启用")));

    // 日志保留条数
    const retInp = document.createElement("input");
    retInp.type = "number"; retInp.min = "1"; retInp.step = "1"; retInp.className = "input input-sm";
    retInp.value = String(st.log_retention ?? 500);
    retInp.addEventListener("change", () => {
        const n = Number(retInp.value);
        st.log_retention = Number.isNaN(n) || n < 1 ? 500 : n;
    });
    ctl.log_retention = retInp;
    const retItem = el("div", "setting-item");
    const retInfo = el("div", "setting-info");
    retInfo.appendChild(el("div", "setting-name", t(`${S}.log_retention`, "日志保留条数")));
    retInfo.appendChild(el("div", "setting-desc", t(`${S}.log_retention_hint`, "内存环形缓冲最多保留的日志条数")));
    retItem.appendChild(retInfo);
    const retCtl = el("div", "setting-control");
    retCtl.appendChild(retInp);
    retCtl.appendChild(el("span", "unit", t(`${S}.count_unit`, "条")));
    retItem.appendChild(retCtl);
    form.appendChild(retItem);

    // 状态持久化
    const item5 = el("div", "setting-item");
    const info5 = el("div", "setting-info");
    info5.appendChild(el("div", "setting-name", t(`${S}.state_persist`, "会话状态持久化")));
    info5.appendChild(el("div", "setting-desc", t(`${S}.state_persist_hint`, "保存时会话的调度状态重启后保持")));
    item5.appendChild(info5);
    const ctl5 = el("div", "setting-control");
    const sw5 = switchControl(st.state_persist, (v) => { st.state_persist = v; });
    ctl.state_persist = sw5.inp;
    ctl5.appendChild(sw5.el);
    item5.appendChild(ctl5);
    form.appendChild(item5);

    // Agent 配置确认（高危操作须先 preview 再 apply）
    const item6 = el("div", "setting-item");
    const info6 = el("div", "setting-info");
    info6.appendChild(el("div", "setting-name", t(`${S}.agent_confirm`, "Agent 配置确认")));
    info6.appendChild(el("div", "setting-desc", t(`${S}.agent_confirm_hint`, "开启后高危操作须先预览再应用")));
    item6.appendChild(info6);
    const ctl6 = el("div", "setting-control");
    const sw6 = switchControl(st.agent_confirm, (v) => { st.agent_confirm = v; });
    ctl.agent_confirm = sw6.inp;
    ctl6.appendChild(sw6.el);
    item6.appendChild(ctl6);
    form.appendChild(item6);

    // 审计日志保留条数
    const ret2Inp = document.createElement("input");
    ret2Inp.type = "number"; ret2Inp.min = "1"; ret2Inp.step = "1"; ret2Inp.className = "input input-sm";
    ret2Inp.value = String(st.audit_retention ?? 500);
    ret2Inp.addEventListener("change", () => {
        const n = Number(ret2Inp.value);
        st.audit_retention = Number.isNaN(n) || n < 1 ? 500 : n;
    });
    ctl.audit_retention = ret2Inp;
    const ret2Item = el("div", "setting-item");
    const ret2Info = el("div", "setting-info");
    ret2Info.appendChild(el("div", "setting-name", t(`${S}.audit_retention`, "审计日志保留条数")));
    ret2Info.appendChild(el("div", "setting-desc", t(`${S}.audit_retention_hint`, "审计环形缓冲最多保留的日志条数")));
    ret2Item.appendChild(ret2Info);
    const ret2Ctl = el("div", "setting-control");
    ret2Ctl.appendChild(ret2Inp);
    ret2Ctl.appendChild(el("span", "unit", t(`${S}.count_unit`, "条")));
    ret2Item.appendChild(ret2Ctl);
    form.appendChild(ret2Item);
}

async function load() {
    const form = document.getElementById("settingsForm");
    form.replaceChildren(el("div", "loading", t("pages.model-morph.common.loading", "加载中…")));
    try {
        const st = await bridge.apiGet("settings");
        renderSettings(st && typeof st === "object" ? st : {});
    } catch (e) {
        form.replaceChildren(el("div", "empty-state", e.message || "请求失败"));
        showToast(e.message || "未知错误", "error");
    }
}

async function save() {
    if (!ctl.timezone || !ctl.base_group || !ctl.log_retention || !ctl.state_persist ||
        !ctl.agent_confirm || !ctl.audit_retention || !ctl.agent_provider_id || !ctl.default_lifecycle) {
        showToast(t("pages.model-morph.common.error", "请求失败"), "error");
        return;
    }
    const st = {
        timezone: ctl.timezone.value,
        base_group: ctl.base_group.value,
        log_retention: Number(ctl.log_retention.value || 500),
        state_persist: ctl.state_persist.checked,
        agent_confirm: ctl.agent_confirm.checked,
        audit_retention: Number(ctl.audit_retention.value || 500),
        agent_provider_id: ctl.agent_provider_id.value,
        default_lifecycle: ctl.default_lifecycle.value,
    };
    try {
        const saved = await bridge.apiPost("settings", st);
        if (saved && typeof saved === "object") renderSettings(saved);
        showToast(t("pages.model-morph.common.save_success", "保存成功"), "success");
    } catch (e) {
        showToast(e.message || t("pages.model-morph.common.save_failed", "保存失败"), "error");
    }
}

function exportConfig() {
    return bridge.download("export", {}, t("pages.model-morph.settings.export_file", "model-morph-config.json"));
}

async function importFile(file) {
    const text = await file.text();
    let content;
    try {
        content = JSON.parse(text);
    } catch (e) {
        showToast(t("pages.model-morph.common.import_failed", "导入失败") + ": " + t("pages.model-morph.common.invalid_json", "JSON 解析失败"), "error");
        return;
    }
    try {
        await bridge.apiPost("import", { content });
        showToast(t("pages.model-morph.common.import_success", "导入成功"), "success");
        // 导入后刷新相关列表与设置
        await load();
    } catch (e) {
        showToast((e.message || "") + " — " + t("pages.model-morph.common.import_failed", "导入失败"), "error");
    }
}

function bind() {
    document.getElementById("settingsSave").addEventListener("click", save);
    document.getElementById("settingsExport").addEventListener("click", () => {
        exportConfig().catch((e) => showToast(e.message || "请求失败", "error"));
    });
    document.getElementById("settingsImportFile").addEventListener("change", (e) => {
        const file = e.target.files && e.target.files[0];
        e.target.value = "";
        if (file) importFile(file).catch((err) => showToast((err.message || "") + " — " + t("pages.model-morph.common.import_failed", "导入失败"), "error"));
    });
}

export { load, bind };
