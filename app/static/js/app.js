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
    // 另一个环境是否在线且支持单点登录：加载它的 /static/img/sso.png（图片跨域也能判断成功 / 失败）。
    // 对方没启动、或还是不支持单点登录的旧版本（没有这张图）时返回 false，不跳过去，避免停在对方的 404 页
    reachable(url, ms = 1500) {
        return new Promise(resolve => {
            const img = new Image();
            const done = ok => { clearTimeout(timer); img.onload = img.onerror = null; resolve(ok); };
            const timer = setTimeout(() => { img.src = ''; done(false); }, ms);
            img.onload = () => done(true);
            img.onerror = () => done(false);
            img.src = `${url}/static/img/sso.png?_=${Date.now()}`;
        });
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
            banner.innerHTML = '当前为 <b>生产环境</b>，只有项目管理员和超级管理员可以编辑、上线、下线 API。研发请在预发修改并验证后「发布到生产」，由生产管理员核对差异后审核上线。'
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
    // 左右并排 (v2.24)：左生产、右预发 / 待发布；rows 为后端 split_rows 的结果
    split(rows, bl, al) {
        const seg = (parts) => (parts || []).map(([t, hl]) => hl ? `<mark>${escapeHtml(t)}</mark>` : escapeHtml(t)).join('');
        const body = (rows || []).map(r => {
            if (r.t === 'skip') return `<tr class="sd-skip"><td colspan="4">⋯ ${r.n} 行相同 ⋯</td></tr>`;
            const lc = r.t === 'del' || r.t === 'chg' ? 'sd-del' : (r.t === 'add' ? 'sd-empty' : '');
            const rc = r.t === 'add' || r.t === 'chg' ? 'sd-add' : (r.t === 'del' ? 'sd-empty' : '');
            return `<tr><td class="sd-ln ${lc}">${r.ln || ''}</td><td class="sd-code ${lc}">${r.l ? seg(r.l) || '&nbsp;' : ''}</td>`
                 + `<td class="sd-ln ${rc}">${r.rn || ''}</td><td class="sd-code ${rc}">${r.r ? seg(r.r) || '&nbsp;' : ''}</td></tr>`;
        }).join('');
        return `<div class="sd-wrap"><table class="sd-table"><colgroup><col style="width:44px"><col><col style="width:44px"><col></colgroup>`
             + `<thead><tr><th colspan="2"><span class="sd-dot sd-dot-l"></span>${escapeHtml(bl)}</th><th colspan="2"><span class="sd-dot sd-dot-r"></span>${escapeHtml(al)}</th></tr></thead>`
             + `<tbody>${body}</tbody></table></div>`;
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
            html += `<div class="diff-field">${escapeHtml(f.label)}</div>` + (f.split ? this.split(f.split, bl, al) : this.lines(f.diff));
        });
        if (same.length && changed.length) {
            html += `<p class="text-muted text-sm" style="margin-top:12px;">相同：${same.map(f => escapeHtml(f.label)).join('、')}</p>`;
        }
        return html;
    },
};

// 同步状态徽标 (v2.24)
const SYNC_TIPS = {
    same: '预发和生产一致',
    pre_ahead: '生产没动过，预发有新的改动：待发布到生产',
    prod_ahead: '生产在上次同步后改过，预发还是旧内容：可拉取生产',
    both: '生产和预发在上次同步后都改过，请核对差异',
    diverged: '和生产不一致（没有同步记录，分不清是哪边改的）',
    prod_only: '生产有、预发没有：可拉取到预发',
    pre_only: '还没发布到生产',
};
function syncStateBadge(state, label) {
    return `<span class="sync-state ss-${state}" title="${escapeHtml(SYNC_TIPS[state] || '')}">${escapeHtml(label || state)}</span>`;
}

// 拉取生产到预发 (v2.24)：items=[{method, url_path}]；预发也改过的需确认覆盖。返回是否有成功拉取的
async function pullFromProd(projectId, items, overwrite) {
    const r = await API.post('/api/releases/pull', { project_id: projectId, items, overwrite: !!overwrite });
    if (!r || !r.status) { Toast.error((r && r.msg) || '拉取失败'); return false; }
    const res = r.data.results || [];
    const needOw = res.filter(x => x.need_overwrite);
    const failed = res.filter(x => !x.ok && !x.need_overwrite);
    if (failed.length) Toast.error(failed.map(x => `${x.url_path}：${x.msg}`).join('；'));
    if (needOw.length && !overwrite) {
        const ok = await confirmAsync('覆盖预发的改动？',
            `${needOw.map(x => x.url_path).join('、')} 在预发也有还没发布到生产的改动，拉取后这些改动会被生产的内容覆盖。`,
            { okText: '覆盖并拉取' });
        if (ok) return (await pullFromProd(projectId, needOw.map(x => ({ method: x.method, url_path: x.url_path })), true)) || r.data.ok_count > 0;
    }
    if (r.data.ok_count) Toast.success(r.msg || '已拉取');
    else if (!failed.length && !needOw.length) Toast.info('已是最新');
    return r.data.ok_count > 0;
}

// 分批拉取 (v2.24)：每批 size 个 API 一个请求，onProgress(已完成, 总数) 用来显示进度；
// 预发也改过、需要覆盖的放到最后统一确认一次再拉。返回成功拉取的个数
async function pullInBatches(projectId, items, onProgress, size = 5) {
    let ok = 0, done = 0;
    const needOw = [], failed = [];
    const total = items.length;
    const runChunks = async (list, overwrite) => {
        for (let i = 0; i < list.length; i += size) {
            const chunk = list.slice(i, i + size);
            const r = await API.post('/api/releases/pull', { project_id: projectId, items: chunk, overwrite });
            if (!r || !r.status) { failed.push(...chunk.map(c => ({ ...c, msg: (r && r.msg) || '拉取失败' }))); }
            else (r.data.results || []).forEach(x => {
                if (x.need_overwrite && !overwrite) needOw.push({ method: x.method, url_path: x.url_path });
                else if (x.ok) ok++;
                else failed.push(x);
            });
            done += chunk.length;
            if (onProgress) onProgress(Math.min(done, total), total);
        }
    };
    await runChunks(items, false);
    if (needOw.length && await confirmAsync('覆盖预发的改动？',
        `${needOw.map(x => x.url_path).join('、')} 在预发也有还没发布到生产的改动，拉取后这些改动会被生产的内容覆盖。`, { okText: '覆盖并拉取' })) {
        done = total - needOw.length;
        await runChunks(needOw, true);
    }
    if (failed.length) Toast.error(failed.map(x => `${x.url_path}：${x.msg}`).join('；'));
    if (ok) Toast.success(`已拉取 ${ok} 个`);
    return ok;
}

// 「对比生产 / 对比预发」：当前环境的 API 与另一环境同名 API 的差异（左生产 / 右预发）
async function compareWithOtherEnv(projectId, apiId, onPulled) {
    const r = await API.get(`/api/releases/compare?project_id=${projectId}&api_id=${apiId}&_t=${Date.now()}`);
    if (!r || !r.status) { Toast.error((r && r.msg) || '对比失败'); return; }
    const d = r.data;
    const otherLabel = d.other_env === 'prod' ? '生产' : '预发';
    const verOf = (env) => {
        const v = env === 'prod' ? (d.other_env === 'prod' ? d.other_version : d.version) : (d.other_env === 'pre' ? d.other_version : d.version);
        return v ? `v${v}` : '—';
    };
    let body = `<div class="diff-head">${methodBadge(d.method)} <code>${escapeHtml(d.url_path)}</code>
        <span class="text-muted text-sm">项目 ${escapeHtml(d.project_name)}（${escapeHtml(d.project_code)}）</span></div>`;
    if (d.sync_state) {
        body += `<div class="sync-bar">${syncStateBadge(d.sync_state, d.sync_state_label)}
            <span>${escapeHtml(SYNC_TIPS[d.sync_state] || '')}</span>
            <span style="margin-left:auto;">生产 <b>${verOf('prod')}</b> · 预发 <b>${verOf('pre')}</b>${d.base_at
                ? ` · 上次同步 ${escapeHtml(d.base_at)}（${d.base_source === 'pull' ? '拉取生产' : '发布到生产'}）` : ''}</span></div>`;
    }
    if (!d.other_project_exists) body += `<div class="note">${otherLabel}环境没有项目「${escapeHtml(d.project_code)}」</div>`;
    else if (!d.diff) body += `<div class="note">${otherLabel}环境还没有这个 API</div>`;
    else body += ReleaseDiff.render(d.diff, { beforeLabel: `生产 ${verOf('prod')}`, afterLabel: `预发 ${verOf('pre')}`, newText: '生产环境还没有该 API' });
    const ov = document.createElement('div');
    ov.className = 'modal-overlay';
    ov.style.display = 'flex';
    ov.innerHTML = `<div class="modal" style="max-width:1180px;width:95vw;">
        <div class="modal-header"><h3>对比${otherLabel} · ${escapeHtml(d.api_name)}</h3><button class="btn-icon" data-x>&times;</button></div>
        <div class="modal-body" style="max-height:72vh;overflow:auto;">${body}</div>
        <div class="modal-footer"><button class="btn btn-secondary" data-x>关闭</button>
        ${d.can_pull ? '<button class="btn btn-primary" data-pull>拉取生产到预发</button>' : ''}</div></div>`;
    ov.querySelectorAll('[data-x]').forEach(b => b.onclick = () => ov.remove());
    const pb = ov.querySelector('[data-pull]');
    if (pb) pb.onclick = async () => {
        if (await pullFromProd(projectId, [{ method: d.method, url_path: d.url_path }])) {
            ov.remove();
            if (typeof onPulled === 'function') onPulled(); else location.reload();
        }
    };
    document.body.appendChild(ov);
}

// ============================================================
// 交互按钮（v2.23，参考 InteractiveHoverButton）：
//   左侧小圆点，悬停时圆点扩散铺满按钮，原文字右滑淡出，同样文字 + 箭头滑入
// v2.24 起全局生效：主 / 次 / 亮绿 / 危险 / 成功 / 幽灵按钮（含小号、表格和卡片里的），以及用户表的操作按钮；
// 分段切换、编辑器页签、API 卡片图标按钮、纯图标按钮不处理；不想要效果的按钮加 class="no-fx"
// ============================================================
const UIButton = (() => {
    const SEL = '.btn.btn-primary, .btn.btn-secondary, .btn.btn-lime, .btn.btn-danger, .btn.btn-success, .btn.btn-ghost, .btn-action';
    const ARROW = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>';

    function eligible(b) {
        return b.matches && b.matches(SEL) && !b.matches('.no-fx, .btn-icon, .icon-act')
            && !b.closest('.seg, .api-actions, .editor-tabs, .seg-toggle, .sync-tabs, .pagination')
            && b.textContent.trim();
    }

    function enhance(b) {
        if (!eligible(b)) return;
        if (b.querySelector(':scope > .ihb-label')) return;   // 已处理（且结构完好）
        b.querySelectorAll(':scope > .ihb-dot, :scope > .ihb-hover').forEach(x => x.remove());
        const text = b.textContent.trim();
        const label = document.createElement('span');
        label.className = 'ihb-label';
        while (b.firstChild) label.appendChild(b.firstChild);
        const dot = document.createElement('span');
        dot.className = 'ihb-dot';
        const hover = document.createElement('span');
        hover.className = 'ihb-hover';
        hover.setAttribute('aria-hidden', 'true');
        hover.innerHTML = `<span></span>${ARROW}`;
        hover.firstChild.textContent = text;
        b.classList.add('ihb');
        b.classList.toggle('ihb-icon', !!label.querySelector('svg'));
        b.classList.toggle('ihb-sm', b.matches('.btn-sm, .btn-xs, .btn-action'));
        // 红色文字的幽灵按钮（删除等）用红色圆点
        b.classList.toggle('ihb-danger', b.matches('.btn-action.delete') || /danger|#dc2626|#b42318/.test(b.getAttribute('style') || ''));
        b.append(dot, label, hover);
    }

    function scan(root) {
        if (!root || root.nodeType !== 1) return;
        if (root.matches && root.matches(SEL)) enhance(root);
        if (root.querySelectorAll) root.querySelectorAll(SEL).forEach(enhance);
    }

    function init() {
        scan(document.body);
        // 新增的按钮自动处理；旧代码用 textContent 改按钮文字（如「登录中...」）会冲掉结构，这里重新处理
        new MutationObserver(ms => ms.forEach(m => {
            const t = m.target.nodeType === 1 ? m.target : m.target.parentElement;
            const btn = t && t.closest && t.closest('.btn');
            if (btn && !btn.querySelector(':scope > .ihb-label')) enhance(btn);
            m.addedNodes.forEach(n => { if (n.nodeType === 1) scan(n); });
        })).observe(document.body, { childList: true, subtree: true, characterData: true });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
    return { enhance };
})();

// ============================================================
// 日期选框（v2.23，参考 antd DatePicker）：替换原生 <input type="date">
// - 显示 YYYY/MM/DD，中文日历浮层（周一开头），今天 / 清除，悬停可一键清除
// - 原生 input 保留并隐藏，value 仍是 YYYY-MM-DD，读写 value / onchange 的旧代码不用改
// - 支持 min / max 属性；data-min-from="另一个日期框 id" / data-max-from 联动成日期范围
// - placeholder 写在原 input 的 placeholder 上（默认「选择日期」）
// ============================================================
const UIDate = (() => {
    const vDesc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    const CAL = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="16" rx="3"/><line x1="16" y1="2.5" x2="16" y2="6.5"/><line x1="8" y1="2.5" x2="8" y2="6.5"/><line x1="3" y1="10" x2="21" y2="10"/></svg>';
    const WEEK = ['一', '二', '三', '四', '五', '六', '日'];
    let panel = null, current = null, view = null;   // view: {y, m}

    const pad = n => String(n).padStart(2, '0');
    const iso = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;
    const parse = s => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || ''); return m ? { y: +m[1], m: +m[2] - 1, d: +m[3] } : null; };
    const todayIso = () => { const t = new Date(); return iso(t.getFullYear(), t.getMonth(), t.getDate()); };

    function bound(inp, which) {
        let v = inp.getAttribute(which) || '';
        const from = inp.dataset[which === 'min' ? 'minFrom' : 'maxFrom'];
        const other = from && document.getElementById(from);
        const ov = other ? vDesc.get.call(other) : '';
        if (ov && (!v || (which === 'min' ? ov > v : ov < v))) v = ov;
        return v;
    }

    function enhance(inp) {
        if (inp.dataset.ui === '1' || !inp.parentNode) return;
        inp.dataset.ui = '1';
        const wrap = document.createElement('div');
        wrap.className = 'ui-date ' + [...inp.classList].filter(c => c !== 'form-input').join(' ');
        wrap.style.cssText = inp.style.cssText;
        wrap.tabIndex = 0;
        wrap.innerHTML = `<span class="ui-date-text"></span><button type="button" class="ui-date-clear" tabindex="-1" aria-label="清除">×</button><span class="ui-date-icon">${CAL}</span>`;
        inp.parentNode.insertBefore(wrap, inp);
        wrap.appendChild(inp);
        inp.removeAttribute('style');
        inp.classList.add('ui-date-native');
        inp.tabIndex = -1;
        inp._ui = wrap; wrap._inp = inp;
        Object.defineProperty(inp, 'value', { configurable: true, get() { return vDesc.get.call(this); }, set(v) { vDesc.set.call(this, v); refresh(this); } });
        inp.addEventListener('change', () => refresh(inp));
        new MutationObserver(() => refresh(inp)).observe(inp, { attributes: true, attributeFilter: ['disabled', 'style', 'placeholder'] });
        wrap.addEventListener('mousedown', e => {
            if (e.target.closest('.ui-date-clear')) { e.preventDefault(); setValue(inp, ''); return; }
            e.preventDefault(); wrap.focus(); current === wrap ? close() : open(wrap);
        });
        wrap.addEventListener('keydown', e => {
            if (e.key === 'Escape') close();
            else if ((e.key === 'Enter' || e.key === ' ') && current !== wrap) { e.preventDefault(); open(wrap); }
        });
        refresh(inp);
    }

    function refresh(inp) {
        const wrap = inp._ui; if (!wrap) return;
        const v = parse(vDesc.get.call(inp));
        const text = wrap.querySelector('.ui-date-text');
        text.textContent = v ? `${v.y}/${pad(v.m + 1)}/${pad(v.d)}` : (inp.getAttribute('placeholder') || '选择日期');
        text.classList.toggle('is-placeholder', !v);
        wrap.classList.toggle('has-value', !!v);
        wrap.classList.toggle('is-disabled', inp.disabled);
        const d = inp.style.display;
        if (d) { wrap.style.display = d === 'none' ? 'none' : ''; inp.style.display = ''; }
        if (current === wrap) render();
    }

    function setValue(inp, v) {
        if (vDesc.get.call(inp) === v) { close(); return; }
        vDesc.set.call(inp, v);
        refresh(inp);
        close();
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function open(wrap) {
        if (wrap._inp.disabled) return;
        close();
        current = wrap;
        wrap.classList.add('is-open');
        const v = parse(vDesc.get.call(wrap._inp)) || parse(bound(wrap._inp, 'min')) || parse(todayIso());
        view = { y: v.y, m: v.m };
        if (!panel) {
            panel = document.createElement('div');
            panel.className = 'ui-date-panel';
            panel.addEventListener('mousedown', e => e.preventDefault());
            panel.addEventListener('click', e => {
                const t = e.target.closest('[data-act]');
                if (!t || !current) return;
                const act = t.dataset.act, inp = current._inp;
                if (act === 'day' && !t.classList.contains('is-disabled')) setValue(inp, t.dataset.v);
                else if (act === 'today' && !t.disabled) setValue(inp, todayIso());
                else if (act === 'clear') setValue(inp, '');
                else if (act === 'prevY') { view.y--; render(); }
                else if (act === 'nextY') { view.y++; render(); }
                else if (act === 'prevM') { view.m--; if (view.m < 0) { view.m = 11; view.y--; } render(); }
                else if (act === 'nextM') { view.m++; if (view.m > 11) { view.m = 0; view.y++; } render(); }
            });
            document.body.appendChild(panel);
        }
        render();
        panel.style.display = 'block';
        position();
    }

    function render() {
        if (!panel || !current) return;
        const inp = current._inp, sel = vDesc.get.call(inp), today = todayIso();
        const min = bound(inp, 'min'), max = bound(inp, 'max');
        const first = new Date(view.y, view.m, 1);
        const lead = (first.getDay() + 6) % 7;             // 周一开头
        const start = new Date(view.y, view.m, 1 - lead);
        let cells = '';
        for (let i = 0; i < 42; i++) {
            const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
            const v = iso(d.getFullYear(), d.getMonth(), d.getDate());
            const cls = ['ui-day'];
            if (d.getMonth() !== view.m) cls.push('is-other');
            if (v === today) cls.push('is-today');
            if (v === sel) cls.push('is-selected');
            if ((min && v < min) || (max && v > max)) cls.push('is-disabled');
            cells += `<span class="${cls.join(' ')}" data-act="day" data-v="${v}">${d.getDate()}</span>`;
        }
        const todayOff = (min && today < min) || (max && today > max);
        panel.innerHTML = `
            <div class="ui-date-head">
                <button type="button" data-act="prevY" title="上一年">«</button><button type="button" data-act="prevM" title="上个月">‹</button>
                <span class="ui-date-title">${view.y}年 ${view.m + 1}月</span>
                <button type="button" data-act="nextM" title="下个月">›</button><button type="button" data-act="nextY" title="下一年">»</button>
            </div>
            <div class="ui-date-week">${WEEK.map(w => `<span>${w}</span>`).join('')}</div>
            <div class="ui-date-grid">${cells}</div>
            <div class="ui-date-foot">
                <button type="button" data-act="clear">清除</button>
                <button type="button" data-act="today" ${todayOff ? 'disabled' : ''}>今天</button>
            </div>`;
    }

    function position() {
        if (!current || !panel) return;
        const r = current.getBoundingClientRect();
        if (r.bottom < 0 || r.top > window.innerHeight) { close(); return; }
        panel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - panel.offsetWidth - 8)) + 'px';
        const below = window.innerHeight - r.bottom - 8, h = panel.offsetHeight;
        panel.style.top = (below < h && r.top > below ? r.top - h - 6 : r.bottom + 6) + 'px';
    }

    function close() {
        if (current) current.classList.remove('is-open');
        current = null;
        if (panel) panel.style.display = 'none';
    }

    function scan(root) {
        if (!root || root.nodeType !== 1) return;
        if (root.matches && root.matches('input[type="date"]')) enhance(root);
        if (root.querySelectorAll) root.querySelectorAll('input[type="date"]').forEach(enhance);
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
    return { enhance };
})();
