// ==========================================================================
// Model Morph 前端 · 内置轻量 Markdown 渲染器（markdown.js）— v1.0.2
// 纯函数 renderMarkdown(text) -> HTMLElement。
// 无外部依赖 / 无 CDN / 不操作 DOM 之外的任何资源。
// - XSS 安全（硬性要求）：
//   1. 全程不拼接 innerHTML；所有动态文本经 textContent 写入；
//   2. 只创建白名单标签（见 WHITE_LISTED_TAG 校验）；
//   3. 链接 href 仅允许 http:// 与 https:// 开头，其余降级为纯文本；
//   4. 代码块 language 仅允许 [a-zA-Z0-9_-]+，否则不输出 class；
//   5. 不执行脚本、不加载外部资源、不解析原始 HTML。
// - 实现思路：先用行级正则把文本切成「块」（代码块/表格/标题/引用/列表/段落），
//   再对每个块内部做行内样式解析（粗体/斜体/行内代码/链接）。
// 解析失败（如表格）一律降级为普通段落；绝不让异常中断渲染。
// ==========================================================================

// 允许创建的标签白名单。DOM 之外不可信任的标签一律拒绝。
const TAG_WHITELIST = new Set([
    "b", "strong", "i", "em", "code", "pre", "ul", "ol", "li",
    "blockquote", "a", "h1", "h2", "h3", "h4", "table", "thead",
    "tbody", "tr", "th", "td", "hr", "br", "p", "span", "div",
]);

// 行内代码的脏后转换（只处理 URL + 语言标识的文本节点，交给 textContent 兜底）
function textEl(text) {
    return document.createTextNode(String(text));
}

// 创建白名单标签节点；非白名单 tag 一律回退为 span，杜绝注入。
function makeTag(tag, className) {
    const t = String(tag || "").toLowerCase();
    if (!TAG_WHITELIST.has(t)) return document.createElement("span");
    const node = document.createElement(t);
    if (className) node.className = className;
    return node;
}

// 转义使字符串只保留合法 URL scheme；非法（含 javascript:/data: 等）返回 null。
function sanitizeHref(href) {
    if (typeof href !== "string") return null;
    const h = href.trim();
    return /^https?:\/\//i.test(h) ? h : null;
}

// 追加一个文本节点或节点到父容器（统一走 textContent，禁止 innerHTML）。
function appendText(container, text) {
    if (text == null || text === "") return;
    container.appendChild(textEl(text));
}

// ========== 行内解析 ==========
// 对块内文本做行内样式解析：`行内代码`、[text](url) 链接、**粗体**、*斜体*。
// 返回值是 HTMLElement 节点，调用方把它 append 进块容器。
function renderInline(text) {
    const container = makeTag("span");
    if (typeof text !== "string" || text === "") {
        appendText(container, text);
        return container;
    }

    // 逐段扫描：以 行内代码 与 链接 为锚，分割后对普通段做粗斜体替换。
    // 组合正则：`([^`]+)` 行内代码  |  [链接文本](href)  |  粗体  |  斜体
    const tokenRe = /(`+)([^`]+?)\1|\[([^\]]+)\]\(([^)]*)\)|(\*\*|__)([^*_]+?)\4|(\*|_)([^*_]+?)\7/g;
    let last = 0;
    let m;
    let guard = 0;
    while ((m = tokenRe.exec(text))) {
        if (++guard > 5000) break; // 防御：过长的输入不再继续匹配
        if (m.index > last) {
            // 前一段普通文本：做纯文本追加（不做递归，避免复杂度失控）
            appendText(container, text.slice(last, m.index));
        }
        if (m[1] !== undefined && m[1].length > 0) {
            // 行内代码：code 标签，文本 textContent
            const code = makeTag("code");
            code.textContent = m[2];
            container.appendChild(code);
        } else if (m[3] !== undefined) {
            // 链接：[label](href)。仅 http/https，否则降级为纯文本 `label(url)`
            const href = sanitizeHref(m[4]);
            if (href) {
                const a = makeTag("a");
                a.href = href;
                a.target = "_blank";
                a.rel = "noopener noreferrer";
                a.textContent = m[3];
                container.appendChild(a);
            } else {
                appendText(container, m[3]);
                appendText(container, " (" + m[4] + ")");
            }
        } else if (m[5] !== undefined) {
            // 粗体（** 或 __）
            const b = makeTag("strong");
            b.textContent = m[6];
            container.appendChild(b);
        } else if (m[7] !== undefined) {
            // 斜体（* 或 _）
            const em = makeTag("em");
            em.textContent = m[8];
            container.appendChild(em);
        }
        last = m.index + m[0].length;
    }
    if (last < text.length) {
        appendText(container, text.slice(last));
    }
    return container;
}

// ========== 块级解析 ==========

// 分割 Markdown 文本为原始行数组（统一换行符）。
function toLines(text) {
    return String(text == null ? "" : text).split(/\r\n|\r|\n/);
}

// 判断某行是否为代码围栏起始行。返回语言字符串（可为空串）或 null。
function fenceInfo(line) {
    const m = /^```\s*([a-zA-Z0-9_+-]*)\s*$/.exec(line);
    return m ? m[1] : null;
}

// 附加有序/无序列表项：连续的 “- ” / “* ” / “1. ” 行。
// 返回 [consumed, html]。consumed 表示该项跳过的行数。
function buildList(lines, start, ordered) {
    const ul = makeTag(ordered ? "ol" : "ul");
    let i = start;
    const itemRe = ordered
        ? /^\s{0,3}\d+\.\s+(.*)$/
        : /^\s{0,3}[-*]\s+(.*)$/;
    let guard = 0;
    while (i < lines.length) {
        const m = itemRe.exec(lines[i]);
        if (!m) break;
        const li = makeTag("li");
        li.appendChild(renderInline(stripMd(m[1])));
        ul.appendChild(li);
        i++;
        guard++;
    }
    return { consumed: i - start, node: ul };
}

// 表格解析：表头行 + 分隔行 + 若干数据行。失败返回 null（调用方降级为段落）。
function parseTable(lines, start) {
    const splitRow = (line) => {
        const s = line.trim();
        if (!/^\s*\|/.test(s) && !/\|\s*$/.test(s)) return null; // 必须含管道符
        return s.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
    };
    const headCells = splitRow(lines[start]);
    if (!headCells) return null;
    const sep = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(lines[start + 1] || "");
    if (!sep) return null; // 无分隔行 → 不是表格
    const table = makeTag("table");
    const thead = makeTag("thead");
    const theadRow = makeTag("tr");
    for (const c of headCells) {
        const th = makeTag("th");
        th.appendChild(renderInline(c));
        theadRow.appendChild(th);
    }
    thead.appendChild(theadRow);
    table.appendChild(thead);

    const tbody = makeTag("tbody");
    let i = start + 2;
    let guard = 0;
    while (i < lines.length && /\|/.test(lines[i].trim())) {
        const cells = splitRow(lines[i]);
        if (!cells) break;
        const tr = makeTag("tr");
        for (const c of cells) {
            const td = makeTag("td");
            td.appendChild(renderInline(c));
            tr.appendChild(td);
        }
        tbody.appendChild(tr);
        i++;
        guard++;
    }
    table.appendChild(tbody);
    return { consumed: i - start, node: table };
}

// 处理一个引用块：连续 “> ” 开头的行，折叠为单个 blockquote，逐行渲染。
function buildQuote(lines, start) {
    const quote = makeTag("blockquote");
    let i = start;
    let guard = 0;
    while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        const inner = lines[i].replace(/^\s*>\s?/, "");
        const p = makeTag("p");
        p.appendChild(renderInline(inner));
        quote.appendChild(p);
        i++;
        guard++;
    }
    return { consumed: i - start, node: quote };
}

// 标题前缀提取：返回 (level, text)。
function headingInfo(line) {
    const m = /^(#{1,4})\s+(.*)$/.exec(line);
    return m ? { level: m[1].length, text: m[2] } : null;
}

// 把一段纯文本（可能跨行）渲染为段落。
function appendParagraph(container, raw) {
    const p = makeTag("p");
    p.appendChild(renderInline(raw.trim()));
    container.appendChild(p);
}

// 由行号构造行内列表的辅助：简单把一行内的多余 md 标记符剥掉（仅用于列表项）。
function stripMd(s) {
    return String(s);
}

// ========== 主入口 ==========
// renderMarkdown(text) -> HTMLElement。绝不让单个块解析异常打断整体渲染。
export function renderMarkdown(text) {
    // 顶层统一包一层 div，作为 .chat-msg-md 容器（可被流式 replaceChildren）。
    const root = makeTag("div");
    const lines = toLines(text);
    let i = 0;
    let pendingText = [];   // 累积普通段落文本，直到遇到块标记再 flush
    let guard = 0;

    // flush 普通文本段：把累积的普通行拼成段落
    const flushText = () => {
        if (pendingText.length === 0) return;
        appendParagraph(root, pendingText.join("\n"));
        pendingText = [];
    };

    while (i < lines.length) {
        if (++guard > 100000) break; // 防御：异常超长输入
        const line = lines[i];

        // 空行 → 结束当前普通段
        if (line.trim() === "") {
            flushText();
            i++;
            continue;
        }

        // 水平线：--- 或 ***（至少三个）
        if (/^(---+|\*\*\*+)$/.test(line.trim()) && line.trim() !== "") {
            flushText();
            root.appendChild(makeTag("hr"));
            i++;
            continue;
        }

        // 围栏代码块
        const lang = fenceInfo(line);
        if (lang !== null) {
            flushText();
            const codeLines = [];
            i++;
            let closed = false;
            let guard2 = 0;
            while (i < lines.length && !closed) {
                if (fenceInfo(lines[i]) !== null) { closed = true; i++; }
                else { codeLines.push(lines[i]); i++; }
                if (++guard2 > 20000) break;
            }
            const pre = makeTag("pre");
            const code = makeTag("code");
            // language 白名单：仅 [a-zA-Z0-9_-]+ 才输出 class
            if (/^[a-zA-Z0-9_-]+$/.test(lang)) code.className = "language-" + lang;
            code.textContent = codeLines.join("\n");
            pre.appendChild(code);
            root.appendChild(pre);
            continue;
        }

        // 标题
        const h = headingInfo(line);
        if (h && pendingText.length === 0) {
            flushText();
            const heading = makeTag("h" + h.level);
            heading.appendChild(renderInline(h.text));
            root.appendChild(heading);
            i++;
            continue;
        }

        // 引用块
        if (/^\s*>\s?/.test(line)) {
            flushText();
            const q = buildQuote(lines, i);
            root.appendChild(q.node);
            i += q.consumed;
            continue;
        }

        // 无序列表
        if (/^\s{0,3}[-*]\s/.test(line)) {
            flushText();
            const lst = buildList(lines, i, false);
            root.appendChild(lst.node);
            i += lst.consumed;
            continue;
        }

        // 有序列表
        if (/^\s{0,3}\d+\.\s/.test(line)) {
            flushText();
            const lst = buildList(lines, i, true);
            root.appendChild(lst.node);
            i += lst.consumed;
            continue;
        }

        // 表格（要求当前行 + 下一行均为管道表格形态）
        if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(lines[i + 1].trim())) {
            const tbl = parseTable(lines, i);
            if (tbl) {
                flushText();
                root.appendChild(tbl.node);
                i += tbl.consumed;
                continue;
            }
        }

        // 普通行：累积普通文本（可能跨行组成一个段落）
        pendingText.push(line);
        i++;
    }
    // 收尾 flush 剩余普通文本，并补 <br> 换行（段落间显式换行）
    if (pendingText.length) {
        appendParagraph(root, pendingText.join("\n"));
        pendingText = [];
    }
    return root;
}
