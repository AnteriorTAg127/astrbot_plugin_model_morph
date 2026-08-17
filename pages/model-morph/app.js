// ==========================================================================
// Model Morph 前端 · 入口 bootstrap（app.js）
// - await bridge.ready(); bridge.onContext 响应语言/主题切换（重渲染 + i18n hydrate）
// - 未在 DOM 写死的静态文案通过 data-i18n / data-i18n-ph hydrate（双语）
// - 顶层三段式 scope（调度编排/运行状态/工具）滑动高光 + 各自子 tab 行切换
// - 惰性加载：非当前 tab 的数据不启动即请求（首次进入才 load）
// - hash 路由（规则「模拟」→ #/simulator）
// ==========================================================================
import { bridge, t } from "./common.js";
import { refreshRefData, refreshGroupRefs } from "./views/shared.js";
import * as dash from "./views/dashboard.js";
import * as groups from "./views/groups.js";
import * as rules from "./views/rules.js";
import * as temporal from "./views/temporal.js";
import * as lifecycles from "./views/lifecycles.js";
import * as sessions from "./views/sessions.js";
import * as logs from "./views/logs.js";
import * as simulator from "./views/simulator.js";
import * as settings from "./views/settings.js";
import * as assistant from "./views/assistant.js";
import * as wizard from "./views/wizard.js";
import * as presets from "./views/presets.js";
import * as audit from "./views/audit.js";

await bridge.ready();

// ========== 配置：tab → scope、scope → nav 行、tab → 视图 ==========
const VIEWS = { dashboard: dash, groups, rules, temporal, lifecycles, sessions, logs, simulator, settings, assistant, wizard, presets, audit };

const TAB_TO_SCOPE = {
    dashboard: "schedule", groups: "schedule", rules: "schedule", temporal: "schedule", lifecycles: "schedule",
    sessions: "runtime", logs: "runtime",
    simulator: "tools", settings: "tools",
    assistant: "tools", wizard: "tools", presets: "tools", audit: "tools",
};

const SCOPE_NAV = { schedule: ".tabs-schedule", runtime: ".tabs-runtime", tools: ".tabs-tools" };

// 各 tab 惰性加载（首次进入才请求）
const loadedTabs = new Set();
const TAB_LAZY = {
    groups: () => groups.load(),
    rules: () => rules.load(),
    temporal: () => temporal.load(),
    lifecycles: () => lifecycles.load(),
    sessions: () => sessions.load(),
    logs: () => logs.load(),
    simulator: () => simulator.load(),
    settings: () => settings.load(),
    assistant: () => assistant.load(),
    wizard: () => wizard.load(),
    presets: () => presets.load(),
    audit: () => audit.load(),
};

// 进入 tab 需要确保 reference data 已就绪的视图
function ensureRefs() {
    return refreshRefData().then(() => refreshGroupRefs());
}

// ========== i18n hydrate：给静态节点填入双语文本 ==========
function hydrateI18n(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((n) => {
        n.textContent = t(n.getAttribute("data-i18n"), n.textContent);
    });
    root.querySelectorAll("[data-i18n-ph]").forEach((n) => {
        n.setAttribute("placeholder", t(n.getAttribute("data-i18n-ph"), n.getAttribute("placeholder") || ""));
    });
}

// ========== scope / tab 切换 ==========
let currentScope = "schedule";
let currentTab = "dashboard";

function moveScopeGlow(activeBtn) {
    const glow = document.querySelector(".scope-glow");
    if (!glow || !activeBtn) return;
    glow.style.width = `${activeBtn.offsetWidth}px`;
    glow.style.transform = `translateX(${activeBtn.offsetLeft}px)`;
    glow.style.opacity = "1";
}

function switchScope(scope) {
    if (!SCOPE_NAV[scope]) return;
    currentScope = scope;
    document.querySelectorAll(".scope-btn").forEach((b) => {
        const on = b.dataset.scope === scope;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
        if (on) moveScopeGlow(b);
    });
    for (const [key, sel] of Object.entries(SCOPE_NAV)) {
        const nav = document.querySelector(sel);
        if (nav) nav.classList.toggle("hidden", key !== scope);
    }
    // 注意：不再在此 `activateTab` 该 scope 首个 tab。分区/页面的切换统一由
    // hash 路由（scope 按钮 / tab 点击 / 规则「模拟」都改写 location.hash →
    // hashchange → handleHash → switchScope + activateTab(route)）驱动，
    // 因为路由目标是确定的那个 tab。若 switchScope 内再激活首 tab，会造成
    // 跨分区跳转时的二次激活与重复加载（如从「规则」跳「模拟器」会先闪「AI 助手」）。
}

async function activateTab(name, { forceReload = false } = {}) {
    if (!VIEWS[name]) return;
    currentTab = name;
    document.querySelectorAll(".tab").forEach((tab) =>
        tab.classList.toggle("active", tab.dataset.tab === name),
    );
    document.querySelectorAll(".page").forEach((p) =>
        p.classList.toggle("active", p.id === `page-${name}`),
    );
    // v0.1.7：每次切入 tab 都重新拉取数据（AI 助手/向导/预设可能在后台创建了
    // 模型组/规则等，若沿用旧懒加载缓存，切回列表页会看到过期数据）。
    // 参考数据（providers/groups/lifecycles 下拉）一并刷新，保证下拉池最新。
    await ensureRefs();
    if (name === "dashboard") {
        await dash.load();
    } else if (TAB_LAZY[name]) {
        await TAB_LAZY[name]();
    }
    loadedTabs.add(name);
}

function currentRoute() {
    const hash = window.location.hash.replace(/^#\/?/, "");
    const seg = hash.split("/")[0] || "";
    return VIEWS[seg] ? seg : "dashboard";
}

function handleHash() {
    const route = currentRoute();
    const scope = TAB_TO_SCOPE[route];
    if (scope !== currentScope) switchScope(scope);
    activateTab(route);
}

// ========== 初始化 ==========
async function init() {
    // 绑定各视图行内事件（一次）
    for (const name of Object.keys(VIEWS)) {
        if (VIEWS[name].bind) VIEWS[name].bind();
    }
    // tab 点击 → 改写 hash，由 hashchange → handleHash 统一驱动 switchScope + activateTab。
    // 这样 hash 永远与当前实际视图一致：规则「模拟」按钮据此判断是否已停留在模拟器页，
    // 避免「先跳模拟器 → tab 切回规则（旧代码不改 hash）→ 再点模拟」时 hash 与视图脱节而失效。
    document.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            window.location.hash = "#/" + tab.dataset.tab;
        });
    });
    // scope 按钮点击 → 跳转该分区首个 tab（同样走 hash 路由）。已在本分区时保持当前 tab 不动。
    document.querySelectorAll(".scope-btn").forEach((b) => {
        b.addEventListener("click", () => {
            if (b.dataset.scope === currentScope) return;
            const first = document.querySelector(`${SCOPE_NAV[b.dataset.scope]} .tab`);
            window.location.hash = "#/" + (first ? first.dataset.tab : "dashboard");
        });
    });
    window.addEventListener("hashchange", handleHash);
    window.addEventListener("resize", () => {
        const on = document.querySelector(".scope-btn.active");
        if (on) moveScopeGlow(on);
    });
    // bridge 上下文（语言/主题）变化 → 重渲染当前 tab + i18n hydrate
    bridge.onContext(() => {
        hydrateI18n();
        // 全量重载当前 tab 数据（强刷，丢弃缓存态）
        loadedTabs.clear();
        activateTab(currentTab, { forceReload: true }).catch(() => {});
        // 刷新参考数据下拉
        ensureRefs();
    });
    hydrateI18n();
    document.title = t("pages.model-morph.title", "Model Morph");
    // 启动逻辑：进入 dashboard 并加载首屏
    loadedTabs.add("dashboard");
    await ensureRefs();
    switchScope("schedule");
    activateTab("dashboard");
    await dash.load();
}

init();
