/* ============================================================
 * CodeEditor —— 统一代码编辑器组件
 * 特性:
 *   - 实时语法高亮 (SQL / JSON)，输入即着色，无需点击
 *   - 自动补全提示: $ 模板函数 / ${ 步骤引用 / : 请求参数
 *   - 全屏编辑 (模态大窗口)
 *   - textarea(输入层) + pre(高亮层) 叠加方案，光标稳定
 *
 * 用法:
 *   const ed = new CodeEditor(containerEl, {
 *       language: 'sql' | 'json',
 *       value: '初始内容',
 *       onChange: (val) => {...},
 *       suggest: {                       // 可选，自动提示数据源
 *           templateFns: true,           // 启用 $ 模板函数提示 (SQL)
 *           stepRefs: () => [...],       // 返回可引用的步骤名/字段 (${)
 *           params:   () => [...],       // 返回可用请求参数名 (:)
 *       }
 *   });
 *   ed.getValue() / ed.setValue(str)
 * ============================================================ */

(function () {
'use strict';

// ---- 模板函数候选 (SQL $ 提示) ----
const TEMPLATE_FUNCTIONS = [
    { label: '$if(条件)$ ... $endif$', insert: '$if()$\n\n$endif$', desc: '条件块：满足才保留中间 SQL', moveCursor: 4 },
    { label: '$if/$elseif/$else$', insert: '$if()$\n\n$elseif()$\n\n$else$\n\n$endif$', desc: '多分支条件', moveCursor: 4 },
    { label: '$else$', insert: '$else$', desc: '否则分支' },
    { label: '$elseif(条件)$', insert: '$elseif()$', desc: '否则如果', moveCursor: 8 },
    { label: '$endif$', insert: '$endif$', desc: '结束条件块' },
    { label: '$for(x in 列表)$ ... $endfor$', insert: '$for( in )$\n\n$endfor$', desc: '循环', moveCursor: 5 },
    { label: '$sep$', insert: '$sep$', desc: '循环分隔符（如逗号）' },
    { label: '$endfor$', insert: '$endfor$', desc: '结束循环' },
];

// 条件表达式里常用函数（在 $if( 内提示）
const COND_FUNCTIONS = [
    { label: 'len(x)', insert: 'len()', desc: '长度（字符串/数组），缺失为0', moveCursor: 4 },
    { label: 'defined(x)', insert: 'defined()', desc: '是否传了该参数', moveCursor: 8 },
    { label: 'empty(x)', insert: 'empty()', desc: '是否为空', moveCursor: 6 },
];

function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

class CodeEditor {
    constructor(container, opts) {
        this.opts = opts || {};
        this.language = this.opts.language || 'sql';
        this.onChange = this.opts.onChange || function () {};
        this.suggest = this.opts.suggest || {};
        this.height = this.opts.height || 220;
        this._build(container);
        this.setValue(this.opts.value || '');
    }

    _build(container) {
        const wrap = document.createElement('div');
        wrap.className = 'ce-wrap';
        wrap.innerHTML = `
            <div class="ce-toolbar">
                <span class="ce-lang">${this.language.toUpperCase()}</span>
                <span class="ce-hint">实时高亮 · 输入 $ \${ : 触发提示</span>
                <span style="flex:1"></span>
                <button type="button" class="ce-btn" data-act="format">格式化</button>
                <button type="button" class="ce-btn" data-act="max">⛶ 最大化</button>
            </div>
            <div class="ce-body" style="height:${this.height}px">
                <pre class="ce-pre hljs language-${this.language}" aria-hidden="true"></pre>
                <textarea class="ce-ta" spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off"></textarea>
                <div class="ce-pop" style="display:none"></div>
            </div>`;
        container.innerHTML = '';
        container.appendChild(wrap);

        this.wrap = wrap;
        this.ta = wrap.querySelector('.ce-ta');
        this.pre = wrap.querySelector('.ce-pre');
        this.code = this.pre;          // 高亮 HTML 直接写进 pre（无 code 嵌套）
        this.pop = wrap.querySelector('.ce-pop');

        // 输入 → 高亮 + onChange
        this.ta.addEventListener('input', () => {
            this._render();
            this.onChange(this.ta.value);
            this._maybeSuggest();
        });
        this.ta.addEventListener('scroll', () => this._syncScroll());
        this.ta.addEventListener('keydown', (e) => this._onKey(e));
        this.ta.addEventListener('blur', () => setTimeout(() => this._closePop(), 150));
        this.ta.addEventListener('click', () => this._closePop());

        // Tab 缩进
        this.ta.addEventListener('keydown', (e) => {
            if (e.key === 'Tab' && this.pop.style.display === 'none') {
                e.preventDefault();
                this._insertAtCursor('  ');
            }
        });

        // 工具栏
        wrap.querySelector('[data-act=format]').addEventListener('click', () => this.format());
        wrap.querySelector('[data-act=max]').addEventListener('click', () => this._toggleMax());

        // 关键：用 JS 把 textarea 的真实计算样式强制同步到高亮层，
        // 杜绝浏览器默认样式差异导致的选区/文字错位。
        this._syncMetrics();
        // 字体加载完成后再同步一次（等宽字体异步加载会改变行宽）
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(() => this._syncMetrics());
        }
        window.addEventListener('resize', () => this._syncScroll());
    }

    // 把 textarea 实际生效的排版样式逐项复制给高亮层 pre，确保像素级一致
    _syncMetrics() {
        const cs = getComputedStyle(this.ta);
        const p = this.pre.style;
        p.fontFamily = cs.fontFamily;
        p.fontSize = cs.fontSize;
        p.fontWeight = cs.fontWeight;
        p.fontStyle = cs.fontStyle;
        p.lineHeight = cs.lineHeight;
        p.letterSpacing = cs.letterSpacing;
        p.tabSize = cs.tabSize;
        p.paddingTop = cs.paddingTop;
        p.paddingRight = cs.paddingRight;
        p.paddingBottom = cs.paddingBottom;
        p.paddingLeft = cs.paddingLeft;
        p.borderTopWidth = cs.borderTopWidth;
        p.borderRightWidth = cs.borderRightWidth;
        p.borderBottomWidth = cs.borderBottomWidth;
        p.borderLeftWidth = cs.borderLeftWidth;
        p.textIndent = cs.textIndent;
        p.wordSpacing = cs.wordSpacing;
        this._syncScroll();
    }

    // ---- 值存取 ----
    getValue() { return this.ta.value; }
    setValue(v) {
        this.ta.value = v == null ? '' : String(v);
        this._render();
    }

    // 外部在编辑器从隐藏变为可见后调用，重新对齐度量（隐藏时 getComputedStyle 不准）
    refresh() {
        this._syncMetrics();
        this._render();
        this._syncScroll();
    }

    // ---- 渲染高亮 ----
    _render() {
        let txt = this.ta.value;
        // 末尾换行补一个空格，否则最后一行高度塌陷、滚动高度不一致
        if (txt.endsWith('\n') || txt === '') txt += ' ';
        let html;
        if (typeof hljs !== 'undefined') {
            try {
                html = hljs.highlight(txt, { language: this.language }).value;
            } catch { html = esc(txt); }
        } else {
            html = esc(txt);
        }
        this.code.innerHTML = html;
        // 内容变化可能改变滚动高度，立即对齐，避免高亮层与文字错位
        this._syncScroll();
    }

    // ---- 格式化 ----
    format() {
        if (this.language === 'json') {
            try {
                this.setValue(JSON.stringify(JSON.parse(this.ta.value), null, 2));
                this.onChange(this.ta.value);
                if (window.Toast) Toast.success('JSON 已格式化');
            } catch (e) {
                if (window.Toast) Toast.error('JSON 格式错误: ' + e.message);
            }
        } else {
            this.setValue(window.formatSqlText ? window.formatSqlText(this.ta.value) : this.ta.value);
            this.onChange(this.ta.value);
            if (window.Toast) Toast.success('SQL 已格式化');
        }
    }

    // ---- 滚动同步：把高亮层滚动位置跟 textarea 对齐 ----
    _syncScroll() {
        this.pre.scrollTop = this.ta.scrollTop;
        this.pre.scrollLeft = this.ta.scrollLeft;
    }

    // ---- 全屏 ----
    _toggleMax() {
        const selS = this.ta.selectionStart;
        const selE = this.ta.selectionEnd;
        const maxBtn = this.wrap.querySelector('[data-act=max]');
        const isMax = this.wrap.classList.contains('ce-maxed');

        if (isMax) {
            // 退出全屏
            this.wrap.classList.remove('ce-maxed');
            document.body.style.overflow = '';
            if (this._mask) { this._mask.remove(); this._mask = null; }
            if (maxBtn) maxBtn.textContent = '⛶ 最大化';
        } else {
            // 进入全屏：不移动 DOM（祖先无 transform，fixed 可正常工作），
            // 仅加 class + 遮罩。移动 DOM 会破坏可视化编排里步骤卡片的重渲染。
            this._mask = document.createElement('div');
            this._mask.className = 'ce-mask';
            this._mask.addEventListener('click', () => this._toggleMax());
            document.body.appendChild(this._mask);
            this.wrap.classList.add('ce-maxed');
            document.body.style.overflow = 'hidden';
            if (maxBtn) maxBtn.textContent = '⤢ 还原';
        }

        // 尺寸变化后：下一帧重排完成再重新对齐度量 + 恢复光标 + 同步滚动
        requestAnimationFrame(() => {
            this._syncMetrics();
            this._render();
            try {
                this.ta.focus();
                this.ta.selectionStart = selS;
                this.ta.selectionEnd = selE;
            } catch (e) {}
            this._syncScroll();
        });
    }

    // ---- 光标处插入文本 ----
    _insertAtCursor(text, selectBack) {
        const s = this.ta.selectionStart, e = this.ta.selectionEnd;
        const v = this.ta.value;
        this.ta.value = v.slice(0, s) + text + v.slice(e);
        const caret = selectBack != null ? (s + selectBack) : (s + text.length);
        this.ta.selectionStart = this.ta.selectionEnd = caret;
        this._render();
        this.onChange(this.ta.value);
        this.ta.focus();
    }

    // ---- 自动提示 ----
    _maybeSuggest() {
        const pos = this.ta.selectionStart;
        const before = this.ta.value.slice(0, pos);

        // ${ 步骤引用提示
        const refM = before.match(/\$\{([\w.]*)$/);
        if (refM && this.suggest.stepRefs) {
            this._showPop(this._buildRefItems(refM[1]), refM[1].length, '${');
            return;
        }
        // : 请求参数提示（前面不是 :: 且不在 ${ 内）
        const paramM = before.match(/(^|[\s(,=])\:(\w*)$/);
        if (paramM && this.suggest.params) {
            this._showPop(this._buildParamItems(paramM[2]), paramM[2].length, ':');
            return;
        }
        // $ 模板函数提示（SQL）
        if (this.language === 'sql' && this.suggest.templateFns) {
            const fnM = before.match(/\$(\w*)$/);
            // 排除 ${ 情况
            if (fnM && before[before.length - fnM[0].length - 1] !== '{' && !before.endsWith('${')) {
                // 是否在 $if( ... ) 内 → 提示条件函数
                const inCond = /\$(?:if|elseif)\(\s*[^)]*$/.test(before);
                const pool = inCond ? COND_FUNCTIONS : TEMPLATE_FUNCTIONS;
                this._showPop(this._filterItems(pool, fnM[1]), fnM[1].length, '$');
                return;
            }
        }
        this._closePop();
    }

    _filterItems(pool, q) {
        q = (q || '').toLowerCase();
        return pool.filter(it => !q || it.label.toLowerCase().includes(q));
    }

    _buildRefItems(q) {
        let refs = [];
        try { refs = this.suggest.stepRefs() || []; } catch { refs = []; }
        // refs: [{step:'q1', fields:['id','cnt']}, ...]
        const items = [];
        refs.forEach(r => {
            items.push({ label: '${' + r.step + '.*.字段}', insert: r.step + '.*.', desc: '该步所有行的某列（数组）', _ref: true });
            items.push({ label: '${' + r.step + '.0.字段}', insert: r.step + '.0.', desc: '该步第1行的字段', _ref: true });
            (r.fields || []).forEach(f => {
                items.push({ label: '${' + r.step + '.*.' + f + '}', insert: r.step + '.*.' + f + '}', desc: '列 ' + f, _ref: true, _close: true });
            });
        });
        items.push({ label: '${params.参数名}', insert: 'params.', desc: '引用 API 请求参数', _ref: true });
        const ql = (q || '').toLowerCase();
        return items.filter(it => !ql || it.label.toLowerCase().includes(ql));
    }

    _buildParamItems(q) {
        let ps = [];
        try { ps = this.suggest.params() || []; } catch { ps = []; }
        const ql = (q || '').toLowerCase();
        return ps.filter(p => !ql || p.toLowerCase().includes(ql))
                 .map(p => ({ label: ':' + p, insert: p, desc: '请求参数', _param: true }));
    }

    _showPop(items, replaceLen, trigger) {
        if (!items || !items.length) { this._closePop(); return; }
        this._popItems = items;
        this._popReplaceLen = replaceLen;
        this._popTrigger = trigger;
        this._popIdx = 0;
        this.pop.innerHTML = items.map((it, i) =>
            `<div class="ce-pop-item${i === 0 ? ' active' : ''}" data-i="${i}">
                <span class="ce-pop-label">${esc(it.label)}</span>
                <span class="ce-pop-desc">${esc(it.desc || '')}</span>
            </div>`).join('');
        // 定位到光标附近（坐标系 = .ce-body）
        const coords = this._caretXY();
        const body = this.wrap.querySelector('.ce-body');
        const maxLeft = Math.max(0, body.clientWidth - 260);
        this.pop.style.left = Math.min(Math.max(0, coords.x), maxLeft) + 'px';
        this.pop.style.top = (coords.y + 22) + 'px';
        this.pop.style.display = 'block';
        this.pop.querySelectorAll('.ce-pop-item').forEach(el => {
            el.addEventListener('mousedown', (e) => {
                e.preventDefault();
                this._applyPop(parseInt(el.dataset.i, 10));
            });
        });
    }

    _closePop() { this.pop.style.display = 'none'; this._popItems = null; }

    _applyPop(idx) {
        const it = this._popItems[idx];
        if (!it) return;
        // 删掉已输入的 query 片段，再插入
        const pos = this.ta.selectionStart;
        const v = this.ta.value;
        const delFrom = pos - this._popReplaceLen;
        let insert = it.insert;
        let back = it.moveCursor;
        this.ta.value = v.slice(0, delFrom) + insert + v.slice(pos);
        const caret = back != null ? delFrom + back : delFrom + insert.length;
        this.ta.selectionStart = this.ta.selectionEnd = caret;
        this._render();
        this.onChange(this.ta.value);
        this._closePop();
        this.ta.focus();
    }

    _onKey(e) {
        if (this.pop.style.display === 'none' || !this._popItems) {
            if (e.key === 'Escape' && this.wrap.classList.contains('ce-maxed')) this._toggleMax();
            return;
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            this._popIdx = (this._popIdx + 1) % this._popItems.length;
            this._refreshPopActive();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            this._popIdx = (this._popIdx - 1 + this._popItems.length) % this._popItems.length;
            this._refreshPopActive();
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            e.preventDefault();
            this._applyPop(this._popIdx);
        } else if (e.key === 'Escape') {
            e.preventDefault();
            this._closePop();
        }
    }

    _refreshPopActive() {
        this.pop.querySelectorAll('.ce-pop-item').forEach((el, i) => {
            el.classList.toggle('active', i === this._popIdx);
            if (i === this._popIdx) el.scrollIntoView({ block: 'nearest' });
        });
    }

    // 估算光标像素位置（镜像 div 法，严格复制 textarea 排版样式）
    _caretXY() {
        const ta = this.ta;
        const cs = getComputedStyle(ta);
        const mirror = document.createElement('div');
        const props = [
            'boxSizing', 'width', 'paddingTop', 'paddingRight', 'paddingBottom',
            'paddingLeft', 'borderTopWidth', 'borderRightWidth', 'borderBottomWidth',
            'borderLeftWidth', 'fontFamily', 'fontSize', 'fontWeight', 'fontStyle',
            'lineHeight', 'letterSpacing', 'tabSize', 'textTransform', 'wordSpacing',
            'textIndent'
        ];
        props.forEach(p => { mirror.style[p] = cs[p]; });
        mirror.style.position = 'absolute';
        mirror.style.visibility = 'hidden';
        mirror.style.whiteSpace = 'pre';        // 与 textarea 实际行为一致（不软换行）
        mirror.style.overflow = 'hidden';
        mirror.style.top = '0';
        mirror.style.left = '0';

        const pos = ta.selectionStart;
        // 用文本节点保留制表/空格；末尾放 marker
        mirror.textContent = ta.value.slice(0, pos);
        const marker = document.createElement('span');
        marker.textContent = '\u200b';
        mirror.appendChild(marker);

        // 挂到 ce-body（与 textarea 同一定位上下文）
        const body = this.wrap.querySelector('.ce-body');
        body.appendChild(mirror);
        const x = marker.offsetLeft - ta.scrollLeft;
        const y = marker.offsetTop - ta.scrollTop;
        mirror.remove();
        return { x, y };
    }
}

window.CodeEditor = CodeEditor;
})();
