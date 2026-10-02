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
                window.location.href = '/login';
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
function logout() {
    localStorage.removeItem('token');
    localStorage.removeItem('user_nickname');
    window.location.href = '/login';
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
        link.href = otherUrl + (env.is_prod ? '/admin/projects' : '/admin/releases');
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
                + (otherUrl ? ` <a href="${otherUrl}/admin/projects" target="_blank">前往预发 ↗</a>` : '');
            banner.style.display = '';
        }
    }
});
