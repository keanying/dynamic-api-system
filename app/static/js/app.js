/**
 * OneData Portal - 前端通用 JS
 */

// ========== Trace ID 工具 ==========
// 全局 trace_id 生成 + 透传：
//   - 每次前端发起请求时自动生成一个 32 位 hex trace_id 并通过 X-Trace-Id 头透传
//   - 后端会在响应头 X-Trace-Id 中回写（保持一致或服务端生成），写入应用日志和调用日志
//   - 这样：前端 console / 后端日志 / 调用日志表 / 浏览器 Network 可以用同一个 trace_id 串联
window.Trace = {
    // 最近一次请求的 trace_id（便于 Toast / 错误上报回显）
    last: '',

    // 生成 32 位无连字符 UUID（兼容老浏览器）
    newId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID().replace(/-/g, '');
        }
        // fallback: 时间戳 + 随机
        const rand = () => Math.floor((1 + Math.random()) * 0x10000).toString(16).slice(1);
        return (
            rand() + rand() + rand() + rand() +
            rand() + rand() + rand() + rand()
        );
    },
};

// ========== API 请求封装 ==========
const API = {
    token: localStorage.getItem('token') || '',

    async request(url, options = {}) {
        // 每次请求生成独立 trace_id，并允许调用方通过 options.traceId 强制指定
        const traceId = (options.traceId || Trace.newId());
        Trace.last = traceId;

        const headers = {
            'Content-Type': 'application/json',
            'X-Trace-Id': traceId,
            ...options.headers,
        };
        if (this.token) {
            headers['Authorization'] = `Bearer ${this.token}`;
        }

        // 前端发起日志（带 trace_id）
        try {
            console.debug(`[trace=${traceId}] --> ${options.method || 'GET'} ${url}`);
        } catch (_) {}

        try {
            const resp = await fetch(url, { ...options, headers });
            // 取服务端回写的 trace_id（理论上和发出的一致）
            const respTrace = resp.headers.get('X-Trace-Id') || traceId;
            Trace.last = respTrace;
            try {
                console.debug(`[trace=${respTrace}] <-- ${resp.status} ${url}`);
            } catch (_) {}

            if (resp.status === 401) {
                this.token = '';
                localStorage.removeItem('token');
                window.location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
                return null;
            }
            const data = await resp.json();
            // 把 trace_id 透传到返回体上，便于上层定位
            if (data && typeof data === 'object' && !data.trace_id) {
                data.trace_id = respTrace;
            }
            return data;
        } catch (e) {
            try {
                console.error(`[trace=${traceId}] !! network error: ${e.message}`);
            } catch (_) {}
            Toast.error('网络请求失败: ' + e.message);
            return null;
        }
    },

    get(url) { return this.request(url); },
    post(url, body) { return this.request(url, { method: 'POST', body: JSON.stringify(body) }); },
    put(url, body) { return this.request(url, { method: 'PUT', body: JSON.stringify(body) }); },
    del(url) { return this.request(url, { method: 'DELETE' }); },

    setToken(token) {
        this.token = token;
        localStorage.setItem('token', token);
    },

    setNickname(nickname) {
        localStorage.setItem('user_nickname', nickname);
        const el = document.getElementById('sidebarUserName');
        if (el) el.textContent = nickname;
    },
};

// ========== Toast 消息 ==========
const Toast = {
    show(msg, type = 'info', duration = 3000) {
        const container = document.getElementById('toast-container');
        if (!container) return;
        const el = document.createElement('div');
        el.className = `toast toast-${type}`;
        el.textContent = msg;
        container.appendChild(el);
        setTimeout(() => {
            el.style.opacity = '0';
            el.style.transform = 'translateX(100%)';
            el.style.transition = '0.3s ease';
            setTimeout(() => el.remove(), 300);
        }, duration);
    },
    success(msg) { this.show(msg, 'success'); },
    error(msg) { this.show(msg, 'error'); },
    warning(msg) { this.show(msg, 'warning'); },
    info(msg) { this.show(msg, 'info'); },
};

// ========== 确认弹窗 ==========
let _confirmCallback = null;

function showConfirm(title, message, callback) {
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    document.getElementById('confirmModal').style.display = 'flex';
    _confirmCallback = callback;
}

function closeConfirm() {
    document.getElementById('confirmModal').style.display = 'none';
    _confirmCallback = null;
}

function doConfirm() {
    if (_confirmCallback) _confirmCallback();
    closeConfirm();
}

// Promise 版输入弹窗：平替原生 prompt()，确定返回输入内容，取消返回 null
// opts 可选: { value, placeholder, okText, cancelText, multiline, required }
function promptAsync(title, message, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
        const ov = document.createElement('div');
        ov.className = 'modal-overlay';
        ov.style.display = 'flex';
        const field = opts.multiline
            ? `<textarea class="form-textarea" rows="4" placeholder="${escapeHtml(opts.placeholder || '')}"></textarea>`
            : `<input type="text" class="form-input" placeholder="${escapeHtml(opts.placeholder || '')}">`;
        ov.innerHTML = `
            <div class="modal" style="max-width:480px;">
                <div class="modal-header">
                    <h3>${escapeHtml(title || '')}</h3>
                    <button type="button" class="btn-icon" data-x>&times;</button>
                </div>
                <div class="modal-body">
                    ${message ? `<p class="text-sm text-muted" style="margin-bottom:12px;white-space:pre-line;">${escapeHtml(message)}</p>` : ''}
                    ${field}
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-cancel>${escapeHtml(opts.cancelText || '取消')}</button>
                    <button type="button" class="btn btn-primary" data-ok>${escapeHtml(opts.okText || '确定')}</button>
                </div>
            </div>`;
        document.body.appendChild(ov);
        const inp = ov.querySelector('input, textarea');
        inp.value = opts.value || '';
        const finish = (val) => {
            document.removeEventListener('keydown', onKey, true);
            ov.remove();
            resolve(val);
        };
        const ok = () => {
            if (opts.required && !inp.value.trim()) { inp.focus(); Toast.warning('请填写内容'); return; }
            finish(inp.value);
        };
        const onKey = (e) => {
            if (e.key === 'Escape') { e.preventDefault(); finish(null); }
            else if (e.key === 'Enter' && (!opts.multiline || e.ctrlKey || e.metaKey) && document.activeElement === inp) { e.preventDefault(); ok(); }
        };
        document.addEventListener('keydown', onKey, true);
        ov.querySelector('[data-ok]').onclick = ok;
        ov.querySelector('[data-cancel]').onclick = () => finish(null);
        ov.querySelector('[data-x]').onclick = () => finish(null);
        setTimeout(() => inp.focus(), 0);
    });
}

// Promise 版确认弹窗：可用 `if (!await confirmAsync(...)) return;` 平替原生 confirm()
// opts 可选: { okText, cancelText, danger }
function confirmAsync(title, message, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
        const modal = document.getElementById('confirmModal');
        const okBtn = modal.querySelector('[data-confirm-ok]');
        const cancelBtn = modal.querySelector('[data-confirm-cancel]');
        const xBtn = modal.querySelector('[data-confirm-x]');

        document.getElementById('confirmTitle').textContent = title;
        document.getElementById('confirmMessage').textContent = message;
        // 每次重置按钮文案/样式，避免上次自定义残留
        okBtn.textContent = opts.okText || '确认';
        cancelBtn.textContent = opts.cancelText || '取消';
        okBtn.classList.toggle('btn-danger', opts.danger !== false);

        let done = false;
        const finish = (val) => {
            if (done) return;
            done = true;
            okBtn.onclick = oldOk;
            cancelBtn.onclick = oldCancel;
            xBtn.onclick = oldX;
            modal.style.display = 'none';
            resolve(val);
        };
        // 备份原 onclick（其他页面的 doConfirm/closeConfirm 流程）
        const oldOk = okBtn.onclick, oldCancel = cancelBtn.onclick, oldX = xBtn.onclick;
        okBtn.onclick = () => finish(true);
        cancelBtn.onclick = () => finish(false);
        xBtn.onclick = () => finish(false);

        modal.style.display = 'flex';
    });
}

// ========== 侧边栏折叠 ==========
document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.getElementById('sidebar');
    if (sidebar) {
        const collapsed = localStorage.getItem('sidebar_collapsed') === 'true';
        if (collapsed) sidebar.classList.add('collapsed');

        // Clicking logo area expands when collapsed
        const logoArea = sidebar.querySelector('.sidebar-header .logo');
        if (logoArea) {
            logoArea.addEventListener('click', () => {
                if (sidebar.classList.contains('collapsed')) {
                    sidebar.classList.remove('collapsed');
                    localStorage.setItem('sidebar_collapsed', 'false');
                }
            });
        }

        // Clicking footer area toggles collapse/expand
        const footerToggle = document.getElementById('sidebarFooterToggle');
        if (footerToggle) {
            footerToggle.addEventListener('click', (e) => {
                // Don't toggle if clicking the logout button
                if (e.target.closest('.logout-btn')) return;
                sidebar.classList.toggle('collapsed');
                localStorage.setItem('sidebar_collapsed', sidebar.classList.contains('collapsed'));
            });
        }
    }
});

// ========== 登出 ==========
// 单点登录 (v2.23)：退出时顺带退出另一个环境（先跳到对方 /sso/logout 清掉登录，再回本环境登录页）；
// 另一个环境访问不到时只退出本环境。
async function logout() {
    localStorage.removeItem('token');
    localStorage.removeItem('user_nickname');
    API.token = '';
    const other = SSO.otherEnv();
    const url = other ? AppEnv.urlOf(other) : '';
    if (url && await SSO.reachable(url)) {
        window.location.href = `${url}/sso/logout?to=${encodeURIComponent(AppEnv.info().env)}`;
        return;
    }
    window.location.href = '/login?sso=none';
}

// ========== 分页渲染 ==========
function renderPagination(containerId, page, totalPages, onPageChange) {
    const container = document.getElementById(containerId);
    if (!container) return;

    let html = '';
    html += `<button ${page <= 1 ? 'disabled' : ''} onclick="${onPageChange}(${page - 1})">上一页</button>`;

    const start = Math.max(1, page - 2);
    const end = Math.min(totalPages, page + 2);

    if (start > 1) {
        html += `<button onclick="${onPageChange}(1)">1</button>`;
        if (start > 2) html += '<span class="page-info">...</span>';
    }

    for (let i = start; i <= end; i++) {
        html += `<button class="${i === page ? 'active' : ''}" onclick="${onPageChange}(${i})">${i}</button>`;
    }

    if (end < totalPages) {
        if (end < totalPages - 1) html += '<span class="page-info">...</span>';
        html += `<button onclick="${onPageChange}(${totalPages})">${totalPages}</button>`;
    }

    html += `<button ${page >= totalPages ? 'disabled' : ''} onclick="${onPageChange}(${page + 1})">下一页</button>`;
    html += `<span class="page-info">共 ${totalPages} 页</span>`;

    container.innerHTML = html;
}

// ========== 加载当前用户名 ==========
document.addEventListener('DOMContentLoaded', async () => {
    const nameEl = document.getElementById('sidebarUserName');
    if (!nameEl || !API.token) return;

    // 先从 localStorage 快速显示
    const cached = localStorage.getItem('user_nickname');
    if (cached) {
        nameEl.textContent = cached;
    }

    // 再从接口获取最新名称
    try {
        const result = await API.get('/api/auth/me');
        if (result && result.status === true && result.data.nickname) {
            nameEl.textContent = result.data.nickname;
            localStorage.setItem('user_nickname', result.data.nickname);
        }
    } catch (e) { /* ignore */ }
});

// ========== 工具函数 ==========
function formatDate(dateStr) {
    if (!dateStr) return '-';
    const d = new Date(dateStr);
    return d.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function formatMs(ms) {
    if (ms === null || ms === undefined) return '-';
    if (ms < 1) return '<1ms';
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(2) + 's';
}

// 大数字按中文单位缩写：1234567 → 123万
function compactNum(n) {
    n = Number(n) || 0;
    if (n >= 1e8) return (n / 1e8).toFixed(n >= 1e9 ? 0 : 1).replace(/\.0$/, '') + '亿';
    if (n >= 1e4) return (n / 1e4).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, '') + '万';
    return n.toLocaleString();
}

function methodBadge(method) {
    return `<span class="badge badge-${method.toLowerCase()}">${method}</span>`;
}

function statusBadge(status) {
    if (status === 'success') return '<span class="badge badge-success">成功</span>';
    if (status === 'error') return '<span class="badge badge-error">失败</span>';
    return `<span class="badge badge-info">${status}</span>`;
}

function connStatusDot(status) {
    return `<span class="status-dot ${status}"></span>${status === 'connected' ? '正常' : status === 'error' ? '异常' : '未测试'}`;
}

// v2.20: 同时转义引号。原实现借 textContent→innerHTML，不转义 " 和 '，
// 用在属性里（如 data-api-name="${escapeHtml(name)}"）时名称带引号即可跳出属性注入脚本
const _HTML_ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/[&<>"']/g, c => _HTML_ESC[c]);
}

function jsonPretty(obj) {
    try {
        if (typeof obj === 'string') obj = JSON.parse(obj);
        return JSON.stringify(obj, null, 2);
    } catch {
        return String(obj);
    }
}

// ========== 多环境 (v2.18+) ==========
// window.APP_ENV 由布局模板注入：{env, label, is_prod, is_pre, prod_port, pre_port, prod_url, pre_url}
const AppEnv = {
    info() { return window.APP_ENV || {}; },
    // 另一个环境的访问地址：优先用配置的 url，否则「当前主机名 + 对方端口」
    urlOf(env) {
        const e = this.info();
        const conf = env === 'prod' ? e.prod_url : e.pre_url;
        if (conf) return conf;
        const port = env === 'prod' ? e.prod_port : e.pre_port;
        return port ? `${location.protocol}//${location.hostname}:${port}` : '';
    },
    isAdminRole(role) { return role === 'super_admin' || role === 'admin'; },
};

// ========== 单点登录 (v2.23) ==========
// 预发和生产共用用户表；在一个环境登录后，去另一个环境不用再输密码：
// 本环境签发一次性票据 → 打开对方 /sso?ticket=... → 对方换成自己的登录凭证。
const SSO = {
    safeNext(n) { return (typeof n === 'string' && /^\/(?![\/\\])/.test(n)) ? n : '/admin/dashboard'; },
    otherEnv() { const e = AppEnv.info(); return e.env ? (e.is_prod ? 'pre' : 'prod') : ''; },
    // 对方环境在浏览器里能否访问（no-cors 只看网络是否可达）
    async reachable(url, ms = 1500) {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), ms);
        try { await fetch(url + '/api/releases/env', { mode: 'no-cors', cache: 'no-store', signal: ctl.signal }); return true; }
        catch (e) { return false; }
        finally { clearTimeout(timer); }
    },
    // 带登录状态打开另一个环境的页面；win 传入预先打开的新窗口（避免被拦截弹窗）
    async go(next, win) {
        const other = this.otherEnv();
        const url = AppEnv.urlOf(other);
        if (!url) { if (win) win.close(); return; }
        let target = `${url}/login?next=${encodeURIComponent(next)}`;
        try {
            const r = await API.post('/api/auth/sso/ticket', { target_env: other });
            if (r && r.status) target = `${url}/sso?ticket=${encodeURIComponent(r.data.ticket)}&next=${encodeURIComponent(next)}`;
        } catch (e) { /* 退回对方登录页 */ }
        if (win) win.location = target; else location.href = target;
    },
};

// 带 data-sso-next 的链接：新窗口打开另一个环境并自动登录
document.addEventListener('click', (e) => {
    const a = e.target.closest('a[data-sso-next]');
    if (!a || !API.token) return;
    e.preventDefault();
    SSO.go(a.dataset.ssoNext, window.open('about:blank', '_blank'));
});

let _mePromise = null;
function getMe() {
    if (!_mePromise) _mePromise = API.get('/api/auth/me').then(r => (r && r.data) || {}).catch(() => ({}));
    return _mePromise;
}

document.addEventListener('DOMContentLoaded', async () => {
    const env = AppEnv.info();
    if (!env.env) return;

    // 顶栏：跳到另一个环境（预发 → 生产的发布审核页；生产 → 预发的项目页）
    const link = document.getElementById('envSwitchLink');
    const other = env.is_prod ? 'pre' : 'prod';
    const otherUrl = AppEnv.urlOf(other);
    if (link && otherUrl) {
        const nextPath = env.is_prod ? '/admin/projects' : '/admin/approvals?tab=release';
        link.href = otherUrl + nextPath;
        link.dataset.ssoNext = nextPath;
        link.target = '_blank';
        link.textContent = env.is_prod ? '前往预发 ↗' : '前往生产 ↗';
        link.style.display = '';
    }

    // 生产环境 + 非管理员：只读提示（后端同样会拦截写操作）
    const banner = document.getElementById('envReadonlyBanner');
    if (env.is_prod && banner && API.token) {
        const me = await getMe();
        if (me.global_role && !AppEnv.isAdminRole(me.global_role)) {
            banner.innerHTML = '当前为 <b>生产环境</b>，非管理员只能查看。如需修改，请在预发环境修改并验证后「发布到生产」，由生产管理员核对差异后审核上线。'
                + (otherUrl ? ` <a href="${otherUrl}/admin/projects" data-sso-next="/admin/projects" target="_blank">前往预发 ↗</a>` : '');
            banner.style.display = '';
        }
    }
});

// ============================================================
// 下拉框美化（v2.23）：原生 <select> 外面包一层自定义外观 + 浮层选项列表
// - 原生 select 仍保留在页面里（隐藏），取值 / 赋值 / onchange 等旧代码都不用改
// - 页面上动态生成的 select（弹窗、列表重绘）自动处理
// - 选项多于 8 个时浮层顶部带搜索框
// - 不想处理的 select 加 class="no-ui"
// ============================================================
const UISelect = (() => {
    const SEL = 'select:not([multiple]):not([size]):not(.no-ui)';
    const vDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
    const iDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
    const ARROW = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
    let panel = null, current = null, active = -1, items = [];

    function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

    function enhance(sel) {
        if (sel.dataset.ui === '1' || !sel.parentNode) return;
        sel.dataset.ui = '1';
        const wrap = document.createElement('div');
        const cls = [...sel.classList].filter(c => c !== 'no-ui');
        wrap.className = 'ui-select ' + (cls.length ? cls.join(' ') : 'ui-select-plain');
        wrap.style.cssText = sel.style.cssText;
        if (sel.title) wrap.title = sel.title;
        wrap.tabIndex = 0;
        wrap.innerHTML = `<span class="ui-select-text"></span><span class="ui-select-arrow">${ARROW}</span>`;
        sel.parentNode.insertBefore(wrap, sel);
        wrap.appendChild(sel);
        sel.removeAttribute('style');
        sel.classList.add('ui-select-native');
        sel.tabIndex = -1;
        sel._ui = wrap;
        wrap._sel = sel;
        // 代码里直接给 value / selectedIndex 赋值时同步显示
        Object.defineProperty(sel, 'value', { configurable: true, get() { return vDesc.get.call(this); }, set(v) { vDesc.set.call(this, v); refresh(this); } });
        Object.defineProperty(sel, 'selectedIndex', { configurable: true, get() { return iDesc.get.call(this); }, set(v) { iDesc.set.call(this, v); refresh(this); } });
        sel.addEventListener('change', () => refresh(sel));
        new MutationObserver(() => refresh(sel)).observe(sel, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled', 'style'] });
        wrap.addEventListener('mousedown', e => { if (!e.target.closest('.ui-select-panel')) { e.preventDefault(); wrap.focus(); toggle(wrap); } });
        wrap.addEventListener('keydown', e => onKey(e, wrap));
        refresh(sel);
    }

    function refresh(sel) {
        const wrap = sel._ui;
        if (!wrap) return;
        const opt = sel.options[iDesc.get.call(sel)];
        const text = wrap.querySelector('.ui-select-text');
        text.textContent = opt ? opt.textContent : '';
        text.classList.toggle('is-placeholder', !opt || opt.value === '');
        wrap.classList.toggle('is-disabled', sel.disabled);
        // 旧代码用 style.display 隐藏 select 时，外壳跟着隐藏
        const d = sel.style.display;
        if (d) { wrap.style.display = d === 'none' ? 'none' : ''; sel.style.display = ''; }
        fitWidth(wrap);
        if (current === wrap) renderList();
    }

    // 自适应宽度的下拉（小号 / 顶部信息条）：像原生 select 一样按最长选项定宽，切换选项时不跳动
    let canvas = null;
    function fitWidth(wrap) {
        if (wrap.style.width || !(wrap.classList.contains('form-select-sm') || wrap.classList.contains('pill-select') || wrap.closest('.idbar-field'))) return;
        const cs = getComputedStyle(wrap);
        if (!cs.font) return;
        canvas = canvas || document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        ctx.font = cs.font;
        let max = 0;
        [...wrap._sel.options].forEach(o => { if (!o.hidden) max = Math.max(max, ctx.measureText(o.textContent).width); });
        if (wrap._cssMin === undefined) wrap._cssMin = parseFloat(cs.minWidth) || 0;
        if (max) wrap.style.minWidth = Math.max(wrap._cssMin, Math.ceil(max + (parseFloat(cs.paddingLeft) || 12) + 34 + 2)) + 'px';
    }

    function toggle(wrap) { current === wrap ? close() : open(wrap); }

    function open(wrap) {
        const sel = wrap._sel;
        if (sel.disabled) return;
        close();
        current = wrap;
        wrap.classList.add('is-open');
        if (!panel) {
            panel = document.createElement('div');
            panel.className = 'ui-select-panel';
            panel.addEventListener('mousedown', e => { if (!e.target.closest('.ui-select-search')) e.preventDefault(); });
            panel.addEventListener('click', e => {
                const li = e.target.closest('.ui-option');
                if (li && !li.classList.contains('is-disabled')) choose(+li.dataset.i);
            });
            document.body.appendChild(panel);
        }
        const many = [...sel.options].filter(o => !o.hidden).length > 8;
        panel.innerHTML = (many ? '<div class="ui-select-search"><input type="text" placeholder="搜索..."></div>' : '') + '<div class="ui-select-list"></div>';
        if (many) {
            const inp = panel.querySelector('input');
            inp.addEventListener('input', () => renderList(inp.value));
            inp.addEventListener('keydown', e => onKey(e, wrap));
        }
        renderList();
        panel.style.display = 'block';
        position();
        if (many) setTimeout(() => panel.querySelector('input').focus(), 0);
        const on = panel.querySelector('.ui-option.is-selected');
        if (on) on.scrollIntoView({ block: 'nearest' });
    }

    function renderList(keyword) {
        if (!current || !panel) return;
        const sel = current._sel;
        const kw = (keyword ?? (panel.querySelector('.ui-select-search input')?.value || '')).trim().toLowerCase();
        const cur = iDesc.get.call(sel);
        let html = '', lastGroup = null;
        items = [];
        [...sel.options].forEach((o, i) => {
            if (o.hidden) return;
            if (kw && !o.textContent.toLowerCase().includes(kw)) return;
            const g = o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : null;
            if (g !== lastGroup && g) html += `<div class="ui-optgroup">${esc(g)}</div>`;
            lastGroup = g;
            const dis = o.disabled || (o.parentElement.tagName === 'OPTGROUP' && o.parentElement.disabled);
            html += `<div class="ui-option${i === cur ? ' is-selected' : ''}${dis ? ' is-disabled' : ''}${o.value === '' ? ' is-placeholder' : ''}" data-i="${i}">${esc(o.textContent)}</div>`;
            if (!dis) items.push(i);
        });
        panel.querySelector('.ui-select-list').innerHTML = html || '<div class="ui-option-empty">无匹配项</div>';
        active = items.indexOf(cur);
        highlight();
    }

    function highlight() {
        if (!panel) return;
        panel.querySelectorAll('.ui-option.is-active').forEach(el => el.classList.remove('is-active'));
        if (active < 0 || active >= items.length) return;
        const el = panel.querySelector(`.ui-option[data-i="${items[active]}"]`);
        if (el) { el.classList.add('is-active'); el.scrollIntoView({ block: 'nearest' }); }
    }

    function position() {
        if (!current || !panel) return;
        const r = current.getBoundingClientRect();
        if (r.bottom < 0 || r.top > window.innerHeight || (!r.width && !r.height)) { close(); return; }
        panel.style.minWidth = r.width + 'px';
        panel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - panel.offsetWidth - 8)) + 'px';
        const below = window.innerHeight - r.bottom - 8, h = panel.offsetHeight;
        panel.style.top = (below < h && r.top > below ? r.top - h - 6 : r.bottom + 6) + 'px';
    }

    function choose(i) {
        const sel = current._sel, wrap = current;
        close();
        if (i !== iDesc.get.call(sel)) {
            iDesc.set.call(sel, i);
            refresh(sel);
            sel.dispatchEvent(new Event('input', { bubbles: true }));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
        wrap.focus();
    }

    function close() {
        if (current) current.classList.remove('is-open');
        current = null;
        if (panel) panel.style.display = 'none';
    }

    function onKey(e, wrap) {
        const isOpen = current === wrap;
        if (!isOpen) {
            if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(e.key)) { e.preventDefault(); open(wrap); }
            return;
        }
        if (e.key === 'Escape') { e.preventDefault(); close(); wrap.focus(); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(items.length - 1, active + 1); highlight(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(0, active - 1); highlight(); }
        else if (e.key === 'Enter') { e.preventDefault(); if (active >= 0) choose(items[active]); }
        else if (e.key === 'Tab') close();
    }

    function scan(root) {
        if (!root || root.nodeType !== 1) return;
        if (root.matches && root.matches(SEL)) enhance(root);
        root.querySelectorAll && root.querySelectorAll(SEL).forEach(enhance);
    }

    function init() {
        scan(document.body);
        new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => {
            if (n.nodeType === 1 && !(panel && panel.contains(n))) scan(n);
        }))).observe(document.body, { childList: true, subtree: true });
        document.addEventListener('mousedown', e => {
            if (current && !current.contains(e.target) && !(panel && panel.contains(e.target))) close();
        });
        window.addEventListener('scroll', e => { if (!(panel && panel.contains(e.target))) position(); }, true);
        window.addEventListener('resize', close);
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    return { enhance, refresh, close };
})();

// ============================================================
// 配置差异展示 (v2.23)：发布单审核、发布前确认、「对比生产 / 对比预发」共用
// diff: 后端 diff_snapshots 的结果 {is_new, changed_count, fields:[...]}
// ============================================================
const ReleaseDiff = {
    lines(lines) {
        return '<div class="diff-block">' + (lines || []).map(l => {
            let cls = '';
            if (l.startsWith('+++') || l.startsWith('---')) cls = 'd-meta';
            else if (l.startsWith('@@')) cls = 'd-hunk';
            else if (l.startsWith('+')) cls = 'd-add';
            else if (l.startsWith('-')) cls = 'd-del';
            return `<div class="${cls}">${escapeHtml(l) || '&nbsp;'}</div>`;
        }).join('') + '</div>';
    },
    val(v) {
        if (v === null || v === undefined) return '<span class="text-muted">—</span>';
        if (v === true) return '是';
        if (v === false) return '否';
        if (v === '') return '<span class="text-muted">（空）</span>';
        return escapeHtml(String(v));
    },
    // opts: { beforeLabel, afterLabel, newText }
    render(diff, opts) {
        opts = opts || {};
        const bl = opts.beforeLabel || '生产当前', al = opts.afterLabel || '待发布';
        if (!diff) return '';
        let html = `<div class="diff-summary">${diff.is_new
            ? `<span class="badge badge-violet">${escapeHtml(opts.newText || '生产环境还没有该 API，发布后新建')}</span>`
            : (diff.changed_count
                ? `<span class="badge badge-warning">${diff.changed_count} 项不同</span>`
                : '<span class="badge badge-success">完全一致</span>')}</div>`;
        const changed = diff.fields.filter(f => f.changed);
        const same = diff.fields.filter(f => !f.changed);
        const simple = changed.filter(f => !f.multiline);
        if (simple.length) {
            html += `<table class="data-table diff-table"><thead><tr><th style="width:160px;">配置项</th><th>${escapeHtml(bl)}</th><th>${escapeHtml(al)}</th></tr></thead><tbody>`
                 + simple.map(f => `<tr><td>${escapeHtml(f.label)}</td><td class="d-before">${this.val(f.before)}</td><td class="d-after">${this.val(f.after)}</td></tr>`).join('')
                 + '</tbody></table>';
        }
        changed.filter(f => f.multiline).forEach(f => {
            html += `<div class="diff-field">${escapeHtml(f.label)}</div>` + this.lines(f.diff);
        });
        if (same.length && changed.length) {
            html += `<p class="text-muted text-sm" style="margin-top:12px;">相同：${same.map(f => escapeHtml(f.label)).join('、')}</p>`;
        }
        return html;
    },
};

// 「对比生产 / 对比预发」：当前环境的 API 与另一环境同名 API 的差异
async function compareWithOtherEnv(projectId, apiId) {
    const r = await API.get(`/api/releases/compare?project_id=${projectId}&api_id=${apiId}&_t=${Date.now()}`);
    if (!r || !r.status) { Toast.error((r && r.msg) || '对比失败'); return; }
    const d = r.data;
    const otherLabel = d.other_env === 'prod' ? '生产' : '预发';
    let body = `<div class="diff-head">${methodBadge(d.method)} <code>${escapeHtml(d.url_path)}</code>
        <span class="text-muted text-sm">项目 ${escapeHtml(d.project_name)}（${escapeHtml(d.project_code)}）</span></div>`;
    if (!d.other_project_exists) body += `<div class="note">${otherLabel}环境没有项目「${escapeHtml(d.project_code)}」</div>`;
    else if (!d.diff) body += `<div class="note">${otherLabel}环境还没有这个 API</div>`;
    else body += ReleaseDiff.render(d.diff, { beforeLabel: '生产当前', afterLabel: '预发当前', newText: '生产环境还没有该 API' });
    const ov = document.createElement('div');
    ov.className = 'modal-overlay';
    ov.style.display = 'flex';
    ov.innerHTML = `<div class="modal" style="max-width:960px;width:94vw;">
        <div class="modal-header"><h3>对比${otherLabel} · ${escapeHtml(d.api_name)}</h3><button class="btn-icon" data-x>&times;</button></div>
        <div class="modal-body" style="max-height:70vh;overflow:auto;">${body}</div>
        <div class="modal-footer"><button class="btn btn-secondary" data-x>关闭</button></div></div>`;
    ov.querySelectorAll('[data-x]').forEach(b => b.onclick = () => ov.remove());
    document.body.appendChild(ov);
}
