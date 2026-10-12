"""
SQL 模板预处理器 (v1.5)

v1.5 新增（全部向后兼容）::
    1. 嵌套参数点路径访问：表达式 / #{} / :{} 中均可用 a.b.0.c 形式
       $for(ch in channels)$ ... $if(len(ch.prdId) > 0)$ ... $endif$ ... $endfor$
    2. :{表达式} 安全绑定插值：把任意表达式的值(含循环变量的嵌套字段)注册为
       参数化绑定占位符，值走 prepared statement，天然防注入；list 值自动 IN 展开。
       例: :{ch.channelName} as channel / spu_id in (:{ch.prdId})
    3. 字面量感知工具 split_literals()/sub_outside_literals()：供执行引擎在做
       占位符替换时跳过 '字符串'、"字符串"、`标识符`、-- / # / C 风格注释，
       解决 SQL 字符串里的英文冒号(如 DATE_FORMAT '%H:%i')被误认成占位符的问题。
    4. 反斜杠+冒号 转义：模板里写 \\:xxx 表示字面冒号，不作为占位符（执行前还原为 :xxx）。

支持的语法块::

    $if(条件)$ ... $endif$
    $if(条件)$ ... $else$ ... $endif$
    $if(条件)$ ... $elseif(条件)$ ... $else$ ... $endif$
    $for(item in items)$ ... $endfor$
    $for(item in items)$ ... $sep$, $endfor$       -- 中间分隔符

执行流程::
    1. 词法分析: 把模板拆成 TEXT / IF / ELSEIF / ELSE / ENDIF / FOR / SEP / ENDFOR token
    2. 语法分析: 构建 AST (Text / If / For)
    3. 求值渲染: 走 AST 把每个分支按 params 拼出最终 SQL

表达式语法（用于 $if$ / $elseif$ / $for$ 内）::

    比较     : ==, !=, <, <=, >, >=
    布尔     : and, or, not  (也支持 &&, ||, !)
    包含     : x in [a, b, c]   /   x in y    /   x not in [...]
    函数     : len(x), defined(x), empty(x)
    字面量   : 数字 / 字符串 / true / false / null / [...]
    变量     : 标识符（从 params 取值）

设计原则::
    - **永不 eval()**：纯手写的递归下降解析器
    - **空集合处理**：for 循环空数组 → 块整体丢弃；in [] → 总是 false
    - **未定义视为 None**：defined() 单独检测「键存在且非 None」
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


# ============================================================
# 错误
# ============================================================

class SqlTplError(Exception):
    """模板语法/求值错误。带 line/col/snippet 供前端弹窗显示。"""

    def __init__(self, msg: str, line: int = 0, col: int = 0, snippet: str = ""):
        self.raw_msg = msg
        self.line = line
        self.col = col
        self.snippet = snippet
        if line > 0:
            super().__init__(f"[行 {line}:{col}] {msg}" + (f"\n  ↳ {snippet}" if snippet else ""))
        else:
            super().__init__(msg)


def _locate(text: str, pos: int) -> tuple[int, int, str]:
    """从位置 pos 算出 (行, 列, 片段)。"""
    if pos < 0 or pos > len(text):
        return 0, 0, ""
    line = text.count("\n", 0, pos) + 1
    last_nl = text.rfind("\n", 0, pos)
    col = pos - last_nl
    line_start = last_nl + 1
    next_nl = text.find("\n", pos)
    line_end = next_nl if next_nl >= 0 else len(text)
    snippet = text[line_start:line_end].strip()
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return line, col, snippet


# ============================================================
# AST 节点
# ============================================================

@dataclass
class Text:
    """纯文本块（包括 :param 占位）。"""
    value: str


@dataclass
class IfBranch:
    condition: str         # 条件字符串
    cond_pos: int          # 在源中的偏移
    body: list             # list[Node]


@dataclass
class If:
    branches: list                     # list[IfBranch]，至少一支
    else_body: Optional[list] = None   # 全 false 时执行


@dataclass
class For:
    var: str                # 循环变量名
    iter_expr: str          # 集合表达式
    iter_pos: int
    body: list
    sep: Optional[list] = None   # 元素之间的分隔片段


Node = object   # Text | If | For；Python 3.10 联合类型在某些环境上有兼容问题，简化为 object


# ============================================================
# 词法：扫描 $...$ 指令 token
# ============================================================

_DIRECTIVE_RE = re.compile(
    r"""
    \$
    (?:
        (?P<if>if)        \s* \( \s* (?P<if_cond>.*?) \s* \)
      | (?P<elseif>elseif|elif) \s* \( \s* (?P<elseif_cond>.*?) \s* \)
      | (?P<else>else)
      | (?P<endif>endif)
      | (?P<for>for)      \s* \( \s* (?P<for_decl>.*?) \s* \)
      | (?P<sep>sep)
      | (?P<endfor>endfor)
    )
    \$
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass
class Token:
    kind: str
    value: str = ""
    var: str = ""
    pos: int = 0


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    for m in _DIRECTIVE_RE.finditer(sql):
        if m.start() > pos:
            tokens.append(Token("text", sql[pos:m.start()], pos=pos))
        g = m.groupdict()
        if g["if"]:
            tokens.append(Token("if", g["if_cond"] or "", pos=m.start()))
        elif g["elseif"]:
            tokens.append(Token("elseif", g["elseif_cond"] or "", pos=m.start()))
        elif g["else"]:
            tokens.append(Token("else", pos=m.start()))
        elif g["endif"]:
            tokens.append(Token("endif", pos=m.start()))
        elif g["for"]:
            decl = g["for_decl"] or ""
            mm = re.fullmatch(r"\s*([a-zA-Z_]\w*)\s+in\s+(.+?)\s*", decl, re.DOTALL)
            if not mm:
                line, col, snip = _locate(sql, m.start())
                raise SqlTplError(f"$for$ 语法错误，期望 'var in expr'，得到 '{decl}'", line, col, snip)
            tokens.append(Token("for", value=mm.group(2), var=mm.group(1), pos=m.start()))
        elif g["sep"]:
            tokens.append(Token("sep", pos=m.start()))
        elif g["endfor"]:
            tokens.append(Token("endfor", pos=m.start()))
        pos = m.end()
    if pos < len(sql):
        tokens.append(Token("text", sql[pos:], pos=pos))
    return tokens


# ============================================================
# 语法分析：token → AST
# ============================================================

class _AstBuilder:
    def __init__(self, tokens: list[Token], source: str):
        self.tokens = tokens
        self.pos = 0
        self.source = source

    def peek(self) -> Optional[Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def eat(self) -> Token:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def _err(self, msg: str, tok: Optional[Token] = None) -> SqlTplError:
        p = tok.pos if tok else (self.tokens[self.pos - 1].pos if self.pos else 0)
        line, col, snip = _locate(self.source, p)
        return SqlTplError(msg, line, col, snip)

    def build(self) -> list:
        nodes = self.parse_block(set())
        if self.peek() is not None:
            tok = self.peek()
            raise self._err(f"未匹配的 ${tok.kind}$", tok)
        return nodes

    def parse_block(self, stop_at: set) -> list:
        nodes: list = []
        while True:
            tok = self.peek()
            if tok is None:
                return nodes
            if tok.kind in stop_at:
                return nodes
            if tok.kind == "text":
                self.eat()
                nodes.append(Text(tok.value))
            elif tok.kind == "if":
                nodes.append(self.parse_if())
            elif tok.kind == "for":
                nodes.append(self.parse_for())
            else:
                raise self._err(f"意外的 ${tok.kind}$", tok)

    def parse_if(self) -> If:
        if_tok = self.eat()
        branches: list = []
        else_body: Optional[list] = None

        body = self.parse_block({"elseif", "else", "endif"})
        branches.append(IfBranch(if_tok.value, if_tok.pos, body))

        while self.peek() and self.peek().kind == "elseif":
            ei = self.eat()
            body = self.parse_block({"elseif", "else", "endif"})
            branches.append(IfBranch(ei.value, ei.pos, body))

        if self.peek() and self.peek().kind == "else":
            self.eat()
            else_body = self.parse_block({"endif"})

        if not self.peek() or self.peek().kind != "endif":
            raise self._err("$if$ 缺少配对的 $endif$", if_tok)
        self.eat()

        return If(branches=branches, else_body=else_body)

    def parse_for(self) -> For:
        for_tok = self.eat()
        body = self.parse_block({"sep", "endfor"})
        sep = None
        if self.peek() and self.peek().kind == "sep":
            self.eat()
            sep = self.parse_block({"endfor"})
        if not self.peek() or self.peek().kind != "endfor":
            raise self._err("$for$ 缺少配对的 $endfor$", for_tok)
        self.eat()
        return For(var=for_tok.var, iter_expr=for_tok.value, iter_pos=for_tok.pos, body=body, sep=sep)


def parse(sql: str) -> list:
    return _AstBuilder(tokenize(sql), sql).build()


# ============================================================
# 表达式求值器
# ============================================================

_TOKEN_RE = re.compile(
    r"""
    \s+
    | (?P<num>-?\d+\.\d+|-?\d+)
    | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
    | (?P<op><=|>=|==|!=|<|>|&&|\|\||!)
    | (?P<lbr>\[) | (?P<rbr>\])
    | (?P<paren>[()])
    | (?P<comma>,)
    | (?P<dot>\.)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


def _walk_value(base: Any, path_tokens: list) -> Any:
    """沿点路径逐级取值：dict 键 / list 索引 / 对象属性；缺失返回 None。"""
    cur = base
    for tok in path_tokens:
        if cur is None:
            return None
        if isinstance(tok, int) or (isinstance(tok, str) and tok.isdigit()):
            idx = int(tok)
            if isinstance(cur, (list, tuple)):
                cur = cur[idx] if 0 <= idx < len(cur) else None
            elif isinstance(cur, dict):
                cur = cur.get(tok) if isinstance(tok, str) else cur.get(str(tok))
            else:
                return None
            continue
        if isinstance(cur, dict):
            cur = cur.get(tok)
        elif hasattr(cur, tok):
            cur = getattr(cur, tok)
        else:
            return None
    return cur


def _expr_tokenize(s: str) -> list:
    tokens = []
    pos = 0
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            raise SqlTplError(f"表达式非法字符: {s[pos]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind is None:
            continue
        val = m.group()
        if kind == "ident":
            kw = val.lower()
            if kw in ("and", "or", "not", "in", "true", "false", "null"):
                tokens.append((kw, val))
            else:
                tokens.append(("ident", val))
        else:
            tokens.append((kind, val))
    return tokens


class _ExprParser:
    def __init__(self, tokens, params):
        self.tokens = tokens; self.pos = 0; self.params = params
        # >0 时处于「短路跳过」状态：照常解析语法（推进 token），但不做可能出错的求值
        self.skip = 0

    def _skipped(self, parse_fn):
        """解析一个子表达式但不求值（and/or 短路时使用）。"""
        self.skip += 1
        try:
            parse_fn()
        finally:
            self.skip -= 1

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def eat(self, kind=None, val=None):
        tok = self.peek()
        if tok is None:
            raise SqlTplError("表达式提前结束")
        if kind is not None and tok[0] != kind:
            raise SqlTplError(f"期望 {kind}，得到 {tok!r}")
        if val is not None and tok[1] != val:
            raise SqlTplError(f"期望 {val!r}，得到 {tok!r}")
        self.pos += 1
        return tok

    def parse(self):
        v = self.or_expr()
        if self.peek() is not None:
            raise SqlTplError(f"表达式末尾多余 token: {self.peek()}")
        return v

    def or_expr(self):
        v = self.and_expr()
        while True:
            t = self.peek()
            if t and (t[0] == "or" or (t[0] == "op" and t[1] == "||")):
                self.pos += 1
                # 原写法 `bool(v) or bool(self.and_expr())` 会被 Python 短路：左边为真时右边
                # 根本没被解析，剩余 token 被当成「表达式末尾多余 token」报错
                if v:
                    self._skipped(self.and_expr)
                    v = True
                else:
                    v = bool(self.and_expr())
            else:
                return v

    def and_expr(self):
        v = self.not_expr()
        while True:
            t = self.peek()
            if t and (t[0] == "and" or (t[0] == "op" and t[1] == "&&")):
                self.pos += 1
                if not v:
                    self._skipped(self.not_expr)   # 同上：左边为假时右边仍要解析
                    v = False
                else:
                    v = bool(self.not_expr())
            else:
                return v

    def not_expr(self):
        t = self.peek()
        if t and (t[0] == "not" or (t[0] == "op" and t[1] == "!")):
            self.pos += 1
            return not bool(self.not_expr())
        return self.in_expr()

    def in_expr(self):
        v = self.cmp_expr()
        t = self.peek()
        if t and t[0] == "not":
            savepos = self.pos
            self.pos += 1
            t2 = self.peek()
            if t2 and t2[0] == "in":
                self.pos += 1
                container = self.cmp_expr()
                if self.skip:
                    return False
                return v not in (container or [])
            self.pos = savepos
            return v
        if t and t[0] == "in":
            self.pos += 1
            container = self.cmp_expr()
            if self.skip:
                return False
            return v in (container or [])
        return v

    def cmp_expr(self):
        v = self.unary()
        t = self.peek()
        if t and t[0] == "op" and t[1] in ("==", "!=", "<", "<=", ">", ">="):
            op = t[1]; self.pos += 1
            return _cmp(v, op, self.unary())
        return v

    def unary(self):
        t = self.peek()
        if t and t[0] == "op" and t[1] == "-":
            self.pos += 1
            v = self.unary()
            try: return -v
            except TypeError: raise SqlTplError(f"无法对 {v!r} 取负")
        return self.primary()

    def primary(self):
        t = self.peek()
        if t is None:
            raise SqlTplError("表达式缺少操作数")
        kind, val = t

        if kind == "num":
            self.pos += 1
            return float(val) if "." in val else int(val)
        if kind == "str":
            self.pos += 1
            raw = val[1:-1]
            return raw.encode("utf-8").decode("unicode_escape", errors="replace") if "\\" in raw else raw
        if kind == "true": self.pos += 1; return True
        if kind == "false": self.pos += 1; return False
        if kind == "null": self.pos += 1; return None
        if kind == "lbr":
            # 数组字面量 [a, b, c]
            self.pos += 1
            items = []
            if self.peek() and self.peek()[0] == "rbr":
                self.pos += 1
                return items
            items.append(self.or_expr())
            while self.peek() and self.peek()[0] == "comma":
                self.pos += 1
                items.append(self.or_expr())
            self.eat("rbr")
            return items
        if kind == "paren" and val == "(":
            self.pos += 1
            v = self.or_expr()
            self.eat("paren", ")")
            return v
        if kind == "ident":
            self.pos += 1
            name = val
            nxt = self.peek()
            if nxt and nxt[0] == "paren" and nxt[1] == "(":
                self.pos += 1
                if name == "defined":
                    inner = self.peek()
                    if inner is None or inner[0] != "ident":
                        raise SqlTplError("defined() 需要变量名")
                    self.pos += 1
                    root = inner[1]
                    # 支持 defined(a.b.c) 点路径
                    path = self._eat_dot_path()
                    self.eat("paren", ")")
                    if root not in self.params:
                        return False
                    if not path:
                        return self.params[root] is not None
                    return _walk_value(self.params[root], path) is not None
                arg = self.or_expr()
                self.eat("paren", ")")
                if self.skip and name in ("len", "empty"):
                    return None
                if name == "len":
                    if arg is None: return 0
                    if isinstance(arg, (str, list, tuple, dict, set)):
                        return len(arg)
                    raise SqlTplError(f"len() 不支持 {type(arg).__name__}")
                if name == "empty":
                    return arg is None or arg == "" or arg == [] or arg == {} or arg is False
                raise SqlTplError(f"未知函数: {name}")
            # 变量取值，支持点路径: ch.channelName / ch.prdId.0
            value = self.params.get(name)
            path = self._eat_dot_path()
            if path:
                return _walk_value(value, path)
            return value

        raise SqlTplError(f"非法 token: {t!r}")

    def _eat_dot_path(self) -> list:
        """吃掉后续 .ident / .数字 链，返回路径 token 列表（可能为空）。"""
        path = []
        while True:
            t = self.peek()
            if not (t and t[0] == "dot"):
                return path
            self.pos += 1
            nxt = self.peek()
            if nxt is None or nxt[0] not in ("ident", "num"):
                raise SqlTplError("点号后需要字段名或索引")
            self.pos += 1
            if nxt[0] == "num":
                if "." in nxt[1]:
                    raise SqlTplError(f"点路径索引必须是整数: {nxt[1]}")
                path.append(int(nxt[1]))
            else:
                path.append(nxt[1])


def _cmp(a, op, b):
    if op == "==": return a == b
    if op == "!=": return a != b
    if a is None or b is None:
        return False
    try:
        if op == "<":  return a < b
        if op == "<=": return a <= b
        if op == ">":  return a > b
        if op == ">=": return a >= b
    except TypeError:
        return False
    raise SqlTplError(f"未知比较: {op}")


def eval_expr(expr: str, params: dict):
    """对外接口：求值一个条件表达式。"""
    expr = (expr or "").strip()
    if not expr:
        return False
    tokens = _expr_tokenize(expr)
    return _ExprParser(tokens, params).parse()


# ============================================================
# 渲染：AST → 最终 SQL
# ============================================================

def _eval_with_source(expr: str, params: dict, source: str, pos: int):
    try:
        return eval_expr(expr, params)
    except SqlTplError as e:
        if e.line == 0:
            line, col, snip = _locate(source, pos)
            raise SqlTplError(e.raw_msg, line, col, snip)
        raise


def _render_nodes(nodes: list, params: dict, source: str, out: list,
                  rctx: Optional[_RenderCtx] = None) -> None:
    for n in nodes:
        if isinstance(n, Text):
            out.append(_interpolate(n.value, params, source, 0, rctx))
        elif isinstance(n, If):
            executed = False
            for branch in n.branches:
                if bool(_eval_with_source(branch.condition, params, source, branch.cond_pos)):
                    _render_nodes(branch.body, params, source, out, rctx)
                    executed = True
                    break
            if not executed and n.else_body is not None:
                _render_nodes(n.else_body, params, source, out, rctx)
        elif isinstance(n, For):
            iter_value = _eval_with_source(n.iter_expr, params, source, n.iter_pos)
            if iter_value is None:
                iter_value = []
            if not isinstance(iter_value, (list, tuple, set)):
                iter_value = [iter_value]
            items = list(iter_value)
            for i, item in enumerate(items):
                inner = {**params, n.var: item}
                if i > 0 and n.sep is not None:
                    _render_nodes(n.sep, inner, source, out, rctx)
                _render_nodes(n.body, inner, source, out, rctx)


# ============================================================
# 文本插值: #{var}
# ============================================================
# 用途:
#   - 循环变量值要直接拼进 SQL 文本: SELECT $for(c in cols)$#{c}$sep$, $endfor$
#   - 动态字段/表名/排序方向（必须配合白名单使用）
#
# 安全:
#   - 默认仅允许合法标识符（字母/数字/下划线），其他字符直接报错
#   - 反 SQL 注入: 把  ' " ; \ 等危险字符显式拒绝，强制用 :param 占位符
#
# 与 :param 占位符的区别:
#   - :param  —— 走数据库参数化绑定，值是数据
#   - #{var}  —— 走字符串插值，值是 SQL 结构（标识符/方向/有限关键词）

# v1.5: 支持点路径 #{ch.channelName} / #{ch.prdId.0}
_INTERP_RE = re.compile(r"#\{\s*([a-zA-Z_]\w*(?:\.\w+)*)\s*\}")
# 默认允许：字母/数字/下划线/英文逗号/空格/星号，覆盖了表名/字段/列名列表/简单 SQL 关键字
# 危险字符: ' " ; \ -- /* */ 等一律拒绝
_SAFE_INTERP_RE = re.compile(r"^[A-Za-z0-9_\,\.\s\*]+$")
# v2.20: 字符白名单仍允许字母+空格，拼得出 "id UNION SELECT pwd FROM users" 这类注入。
# 再拦一道 SQL 关键字（都是 MySQL 保留字，不可能是合法的未加引号列名/排序方向）
_INTERP_KEYWORD_RE = re.compile(
    r"\b(UNION|SELECT|FROM|WHERE|INTO|HAVING|SLEEP|BENCHMARK|INSERT|UPDATE|DELETE|REPLACE|"
    r"DROP|ALTER|CREATE|TRUNCATE|RENAME|GRANT|REVOKE|OUTFILE|DUMPFILE|PROCEDURE)\b",
    re.IGNORECASE,
)


def _unsafe_interp_value(s: str) -> bool:
    return not _SAFE_INTERP_RE.match(s) or _INTERP_KEYWORD_RE.search(s) is not None

# v1.5: :{表达式} 安全绑定插值（渲染成自动生成的 :__tpl_bN 占位符，值走参数化绑定）
_BIND_INTERP_RE = re.compile(r":\{([^{}]+)\}")

# ============================================================
# v1.5: SQL 字面量感知工具
# 匹配 '字符串'(支持\转义) / "字符串" / `反引号标识符` / -- 行注释 / # 行注释 / /* 块注释 */
# ============================================================
_SQL_LITERAL_RE = re.compile(
    r"('(?:[^'\\]|\\.)*'"          # 单引号字符串
    r'|"(?:[^"\\]|\\.)*"'          # 双引号字符串
    r"|`[^`]*`"                    # 反引号标识符
    r"|--[^\n]*"                   # -- 行注释
    r"|#(?!\{)[^\n]*"              # # 行注释 (MySQL)；#{var} 是文本插值不是注释（v2.20 修正：
                                    #   原来同一行 #{} 之后的 :{expr} 会被当成注释内容而不替换）
    r"|/\*.*?\*/)",                # /* 块注释 */
    re.DOTALL,
)


def split_literals(sql: str) -> list:
    """把 SQL 切成 [代码段, 字面量段, 代码段, ...]。
    re.split 带捕获组：偶数下标是代码段，奇数下标是字符串/注释字面量。
    """
    return _SQL_LITERAL_RE.split(sql or "")


_QUOTED_PARAM_RE = re.compile(r"""^(['"])\s*:([A-Za-z_]\w*)\s*\1$""")


def unquote_param_literals(sql: str):
    """把整个字符串只有一个参数占位符的写法（':scenicId' / ":scenicId"）还原成 :scenicId (v2.24)。

    加了引号就成了普通字符串：不会被识别成参数，执行时也不会替换成传入的值，查询永远查不到数据。
    只处理「引号里只有 :参数名」的字符串，'10:30' 之类的正常字符串和注释不受影响。
    返回 (修正后的 SQL, 去掉引号的参数名列表)。
    """
    parts = split_literals(sql)
    fixed = []
    for i in range(1, len(parts), 2):
        m = _QUOTED_PARAM_RE.match(parts[i])
        if m:
            parts[i] = ":" + m.group(2)
            if m.group(2) not in fixed:
                fixed.append(m.group(2))
    return ("".join(parts), fixed) if fixed else (sql, [])


def sub_outside_literals(pattern, repl, sql: str) -> str:
    """仅在字符串/注释之外做正则替换，字面量原样保留。
    pattern: 已编译正则或字符串; repl: 替换串或函数。
    """
    if isinstance(pattern, str):
        pattern = re.compile(pattern)
    parts = split_literals(sql)
    for i in range(0, len(parts), 2):
        parts[i] = pattern.sub(repl, parts[i])
    return "".join(parts)


class _RenderCtx:
    """一次渲染的上下文：收集 :{} 生成的绑定值。"""
    __slots__ = ("binds", "counter", "allow_binds")

    def __init__(self, binds: Optional[dict]):
        self.binds = binds
        self.counter = 0
        self.allow_binds = binds is not None

    def register(self, value) -> str:
        name = f"__tpl_b{self.counter}"
        self.counter += 1
        self.binds[name] = value
        return name


def _resolve_dot_path(params: dict, dotted: str):
    """按 a.b.0.c 从 params 取值；根键不存在返回 (False, None)。"""
    parts = dotted.split(".")
    root = parts[0]
    if root not in params:
        return False, None
    return True, _walk_value(params[root], parts[1:])


def _interpolate(text: str, params: dict, source: str, pos: int,
                 rctx: Optional["_RenderCtx"] = None) -> str:
    """处理 #{var} 文本插值和 :{expr} 安全绑定插值。"""
    # ---- :{expr} 绑定插值（先处理，避免和 #{} 混淆）----
    # 注意：
    #   1. rctx 为 None 时静默跳过（保持 v1.4 行为，:{ 原样输出）
    #   2. 只处理字符串/注释之外的 :{}，避免误伤 SQL 里的 JSON 字面量（如 '{"a":{"b":1}}'）
    if ":{" in text and rctx is not None and rctx.allow_binds:

        def bind_replace(m):
            expr = m.group(1).strip()
            try:
                value = eval_expr(expr, params)
            except SqlTplError as e:
                line, col, snip = _locate(source, pos)
                raise SqlTplError(f":{{{expr}}} 求值失败: {e.raw_msg}", line, col, snip)
            key = rctx.register(value)
            return f":{key}"

        text = sub_outside_literals(_BIND_INTERP_RE, bind_replace, text)

    # ---- #{var} 文本插值 ----
    if "#{" not in text:
        return text

    def replace(m):
        name = m.group(1)
        exists, val = _resolve_dot_path(params, name)
        if not exists:
            line, col, snip = _locate(source, pos)
            raise SqlTplError(f"#{{{name}}} 未定义变量", line, col, snip)
        if val is None:
            return ""
        # list/tuple: 逐元素校验后用 ", " 拼接（常用于列名列表）
        if isinstance(val, (list, tuple, set)):
            pieces = []
            for item in val:
                s_item = str(item)
                if _unsafe_interp_value(s_item):
                    line, col, snip = _locate(source, pos)
                    raise SqlTplError(
                        f"#{{{name}}} 列表元素含非法字符: {s_item!r}，"
                        "请改用 :{" + name + "} 参数化绑定",
                        line, col, snip,
                    )
                pieces.append(s_item)
            return ", ".join(pieces)
        s = str(val)
        if _unsafe_interp_value(s):
            line, col, snip = _locate(source, pos)
            raise SqlTplError(
                f"#{{{name}}} 值含非法字符或 SQL 关键字: {s!r}，"
                "#{} 只允许字母/数字/下划线/逗号/点/空格/*（且不能含 SELECT/UNION 等关键字），"
                "值类数据请改用 :{" + name + "} 参数化绑定",
                line, col, snip,
            )
        return s

    return _INTERP_RE.sub(replace, text)


def render_template(sql: str, params: dict, collect_binds: Optional[dict] = None) -> str:
    """对外接口：模板渲染。

    collect_binds: 传入 dict 时启用 :{expr} 安全绑定插值 —— 渲染过程中每个
                   :{expr} 会被替换为自动生成的 :__tpl_bN 占位符，值写入该 dict，
                   由调用方合并进绑定参数走参数化查询。
                   不传（None）时保持 v1.4 行为完全一致。
    """
    if not sql:
        return sql
    has_directive = "$" in sql
    has_interp = "#{" in sql
    has_bind = collect_binds is not None and ":{" in sql
    if not has_directive and not has_interp and not has_bind:
        return sql
    rctx = _RenderCtx(collect_binds) if collect_binds is not None else None
    if has_directive:
        nodes = parse(sql)
        out: list = []
        _render_nodes(nodes, params, sql, out, rctx)
        return "".join(out)
    # 只有插值，无控制块 —— 直接做插值
    return _interpolate(sql, params, sql, 0, rctx)


# ============================================================
# 工具：提取占位符 / 收集表达式
# ============================================================

_PLACEHOLDER_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")
# 带 modifier 后缀的占位符：:name|like / :name|like_left / :name|like_right
_PLACEHOLDER_WITH_MOD_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)\|([a-zA-Z_][a-zA-Z0-9_]*)")


def extract_placeholders(rendered_sql: str) -> list:
    """从渲染后 SQL 抓 :name 占位符。忽略字符串字面量和注释。

    返回去重后的占位符基础名（不含 |modifier 后缀）。
    """
    s = rendered_sql
    s = re.sub(r"'(?:[^'\\]|\\.)*'", "", s)
    s = re.sub(r'"(?:[^"\\]|\\.)*"', "", s)
    s = re.sub(r"--[^\n]*", "", s)
    s = re.sub(r"#[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    seen, out = set(), []
    # 先匹配带 modifier 的（更长，优先），再匹配普通的
    # 注意：用 finditer 顺序扫描，每次匹配较长形式优先
    # 实现方式：把 :name|mod 整体看作 :name 占位（modifier 不影响占位符名）
    combined = re.compile(r"(?<!\\):([a-zA-Z_][a-zA-Z0-9_]*)(?:\|[a-zA-Z_][a-zA-Z0-9_]*)?")
    for m in combined.finditer(s):
        name = m.group(1)
        if name not in seen:
            seen.add(name); out.append(name)
    return out


def extract_placeholders_with_modifiers(rendered_sql: str) -> list:
    """提取带 modifier 信息的占位符列表，返回 [(name, modifier_or_None, full_match), ...]
    用于执行引擎区分处理 LIKE 等语法糖。
    """
    s = rendered_sql
    # 同样去掉字符串字面量和注释
    # 但要保留位置信息 —— 用占位字符替换而非删除，否则 finditer 的 span 会失真
    s_masked = re.sub(r"'(?:[^'\\]|\\.)*'", lambda m: " " * len(m.group()), s)
    s_masked = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: " " * len(m.group()), s_masked)
    s_masked = re.sub(r"--[^\n]*", lambda m: " " * len(m.group()), s_masked)
    s_masked = re.sub(r"#[^\n]*", lambda m: " " * len(m.group()), s_masked)
    s_masked = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group()), s_masked, flags=re.DOTALL)

    combined = re.compile(r"(?<!\\):([a-zA-Z_][a-zA-Z0-9_]*)(?:\|([a-zA-Z_][a-zA-Z0-9_]*))?")
    out = []
    for m in combined.finditer(s_masked):
        out.append((m.group(1), m.group(2), m.group(0)))
    return out


# 老代码 sql_tools.py 用了 IF_OPEN_RE，保留向后兼容
IF_OPEN_RE = re.compile(r"\$if\s*\(\s*(.*?)\s*\)\s*\$", re.DOTALL)


def collect_expressions(sql: str) -> list:
    """扫描模板里所有控制块表达式，按出现顺序返回 (kind, expr) 列表。
    kind ∈ {'if', 'elseif', 'for'}
    """
    out = []
    for tok in tokenize(sql):
        if tok.kind in ("if", "elseif"):
            out.append((tok.kind, tok.value))
        elif tok.kind == "for":
            out.append(("for", f"{tok.var} in {tok.value}"))
    return out
