# -*- coding: utf-8 -*-
"""
多源 SQL（catalog 模式，v2.22+）
================================

一条标准 MySQL 语法的 SQL 关联多个数据源的表，表名写成「数据源名.库名.表名」
（数据源名就是「数据源管理」里登记的名称；也可写「数据源名.表名」，库取数据源配置的默认库）：

    SELECT m.category, SUM(o.amount) AS gmv
      FROM 订单库.biz.orders o
      JOIN selectdb.crm.members u ON u.user_name = o.user_name
      JOIN 商户库.mall.merchants m ON m.id = o.merchant_id
     WHERE o.status = 'paid' AND o.created_at >= :start
     GROUP BY m.category

执行方式：
  1. 只涉及一个数据源：去掉 SQL 里的数据源名前缀，整句交给该数据源执行。
     执行路径与单 SQL 模式完全相同（只读防护、系统库保护、流式读取都一样），结果也一样。
  2. 涉及多个数据源：
     a. 整段都来自同一数据源、且不引用外层的子查询 / CTE，整段交给该数据源执行（聚合在源库完成）；
     b. 其余的表各自只取用到的列，只涉及这张表的 WHERE / ON 条件下推到源库；
     c. 先取「驱动表」，再把它的关联键作为 IN 条件下推到另一侧（动态过滤），只取能关联上的行；
     d. 取回的数据在本进程内用 DuckDB（嵌入式计算库，不是独立服务）完成关联、聚合、排序。
        DuckDB 禁止访问文件系统和安装插件，配置锁定；SQL 改写成与 MySQL 一致的语义（见 _MySQLCompat）。

下推到各数据源的 SQL 仍走 engine._query_mysql_raw：只读防护、系统库保护、连接池、超时与原来一致；
请求参数始终以绑定参数的方式传给源库，不拼进 SQL。
"""
import asyncio
import datetime
import gc
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pyarrow as pa
import sqlglot
from sqlglot import exp
from sqlglot.dialects.mysql import MySQL
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from app.core.config import settings
from app.core.logging import get_logger
from app.core.timezone import CST

log = get_logger("federated")


class FederatedError(Exception):
    pass


# ======================================================================
# 方言：在 sqlglot 的 MySQL 方言上，让个别函数保持原样（避免解析时被改写，
# 下推到源库的 SQL 与用户写的保持一致）
# ======================================================================

class _MySQL(MySQL):
    class Parser(MySQL.Parser):
        FUNCTIONS = {
            **MySQL.Parser.FUNCTIONS,
            "TO_DAYS": lambda args: exp.Anonymous(this="TO_DAYS", expressions=args),
        }


def _parse(sql: str) -> exp.Expression:
    try:
        stmts = [s for s in sqlglot.parse(sql, read=_MySQL) if s is not None]
    except (ParseError, TokenError) as e:
        raise FederatedError(f"SQL 解析失败：{_first_line(e)}")
    if len(stmts) != 1:
        raise FederatedError("只能写一条 SELECT 语句")
    ast = stmts[0]
    if not isinstance(ast, (exp.Select, exp.SetOperation)):
        raise FederatedError("多源 SQL 只支持查询（SELECT）")
    if ast.find(exp.Into):
        raise FederatedError("不支持 SELECT ... INTO")
    if ast.find(exp.Lock):
        raise FederatedError("不支持 FOR UPDATE / LOCK IN SHARE MODE")
    if ast.find(exp.Parameter, exp.SessionParameter):
        raise FederatedError("不支持 @变量 / @@系统变量")
    return ast


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__


# ======================================================================
# 数据源（catalog）解析
# ======================================================================

@dataclass
class _TableRef:
    node: exp.Table
    ds: Any                      # DataSource
    db: str
    table: str
    strip_span: Tuple[int, int]  # 原 SQL 中「数据源名.」的位置（单源整句下推时去掉）


def _ident_span(ident: exp.Identifier) -> Optional[Tuple[int, int]]:
    meta = getattr(ident, "meta", None) or {}
    if "start" in meta and "end" in meta:
        return meta["start"], meta["end"]
    return None


def _cte_names(ast: exp.Expression) -> set:
    return {cte.alias_or_name for cte in ast.find_all(exp.CTE)}


async def _resolve_tables(ast: exp.Expression, sql: str, db, project_code: str) -> List[_TableRef]:
    """找出 SQL 中所有真实表（排除 CTE 引用），按数据源名解析成数据源，并做权限检查。"""
    from sqlalchemy import select
    from app.models.models import DataSource
    from app.services import ds_scope
    from app.services.datasource_types import MYSQL_PROTOCOL_TYPES

    ctes = _cte_names(ast)
    raw = []
    for t in ast.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise FederatedError(f"不支持表函数：{t.sql(dialect=_MySQL)[:80]}")
        if not t.args.get("catalog") and not t.args.get("db") and t.name in ctes:
            continue  # WITH 子句定义的临时结果
        raw.append(t)
    if not raw:
        raise FederatedError("SQL 中没有引用任何表")

    names = set()
    for t in raw:
        if t.args.get("catalog"):
            names.add(t.catalog)
        elif t.args.get("db"):
            names.add(t.db)
    rows = (await db.execute(select(DataSource).where(DataSource.name.in_(names)))).scalars().all() if names else []
    by_name: Dict[str, Any] = {}
    for ds in rows:
        if ds.name in by_name:
            raise FederatedError(f"存在多个名为「{ds.name}」的数据源，请先改名区分")
        by_name[ds.name] = ds

    refs = []
    for t in raw:
        if t.args.get("catalog"):
            cat_ident = t.args["catalog"]
            ds = by_name.get(t.catalog)
            if ds is None:
                raise FederatedError(f"数据源「{t.catalog}」不存在（表 {t.sql(dialect=_MySQL)}）。表名格式：数据源名.库名.表名")
            dbname = t.db
        elif t.args.get("db") and t.db in by_name:
            # 「数据源名.表名」：库取数据源配置的默认库
            cat_ident = t.args["db"]
            ds = by_name[t.db]
            dbname = (ds.database_name or "").strip()
            if not dbname:
                raise FederatedError(f"数据源「{ds.name}」没有配置默认库，请写成 {ds.name}.库名.{t.name}")
            t.set("catalog", exp.to_identifier(ds.name, quoted=cat_ident.quoted))
            t.set("db", exp.to_identifier(dbname, quoted=True))
        else:
            raise FederatedError(
                f"表 {t.sql(dialect=_MySQL)} 需写成「数据源名.库名.表名」"
                + (f"（没有名为「{t.db}」的数据源）" if t.args.get("db") else "")
            )
        if (ds.type or "mysql").lower() not in MYSQL_PROTOCOL_TYPES:
            raise FederatedError(f"数据源「{ds.name}」类型为 {ds.type}，多源 SQL 只支持 MySQL 协议的数据源（MySQL / SelectDB / Doris / StarRocks）")
        ds_scope.ensure_allowed(ds, project_code)
        if dbname and dbname.lower() in ds_scope.protected_schemas() and dbname.lower() != (ds.database_name or "").lower():
            raise FederatedError(f"不允许访问系统库「{dbname}」")
        span = _ident_span(cat_ident)
        refs.append(_TableRef(node=t, ds=ds, db=dbname, table=t.name, strip_span=span))
    return refs


def _strip_catalogs(sql: str, refs: List[_TableRef]) -> Optional[str]:
    """单数据源：在原 SQL 文本上去掉「数据源名.」前缀，其余一个字符都不改。"""
    spans = []
    for r in refs:
        if r.strip_span is None:
            return None
        start, end = r.strip_span
        i = end + 1
        while i < len(sql) and sql[i].isspace():
            i += 1
        if i >= len(sql) or sql[i] != ".":
            return None
        i += 1
        while i < len(sql) and sql[i].isspace():
            i += 1
        spans.append((start, i))
    out = sql
    for start, stop in sorted(set(spans), reverse=True):
        out = out[:start] + out[stop:]
    return out


# ======================================================================
# 表结构（列名、类型）：SELECT * ... LIMIT 0 取列描述，按数据源缓存
# ======================================================================

@dataclass
class _Col:
    name: str
    sqltype: str          # 给 sqlglot 推导类型用（MySQL 写法）
    arrow: Any            # pyarrow 类型


_schema_cache: Dict[tuple, Tuple[float, List[_Col]]] = {}

# MySQL 协议字段类型码
_T_DECIMAL, _T_TINY, _T_SHORT, _T_LONG, _T_FLOAT, _T_DOUBLE, _T_NULL, _T_TIMESTAMP = 0, 1, 2, 3, 4, 5, 6, 7
_T_LONGLONG, _T_INT24, _T_DATE, _T_TIME, _T_DATETIME, _T_YEAR, _T_NEWDATE, _T_VARCHAR, _T_BIT = 8, 9, 10, 11, 12, 13, 14, 15, 16
_T_JSON, _T_NEWDECIMAL = 245, 246
_BLOB_TYPES = {249, 250, 251, 252, 253, 254, _T_VARCHAR}
_UNSIGNED_FLAG = 32
_BINARY_CHARSET = 63


def _field_col(f) -> _Col:
    code = getattr(f, "type_code", None)
    flags = getattr(f, "flags", 0) or 0
    name = f.name
    if code in (_T_TINY, _T_SHORT, _T_LONG, _T_INT24, _T_YEAR):
        return _Col(name, "BIGINT", pa.int64())
    if code == _T_LONGLONG:
        if flags & _UNSIGNED_FLAG:
            return _Col(name, "UBIGINT", pa.uint64())
        return _Col(name, "BIGINT", pa.int64())
    if code in (_T_DECIMAL, _T_NEWDECIMAL):
        scale = int(getattr(f, "scale", 0) or 0)
        length = int(getattr(f, "length", 0) or 0)
        prec = length - (1 if scale else 0) - (0 if flags & _UNSIGNED_FLAG else 1)
        prec = max(prec, scale, 1)
        if prec > 38 or scale > 38:
            return _Col(name, "DOUBLE", pa.float64())
        return _Col(name, f"DECIMAL({prec}, {scale})", pa.decimal128(prec, scale))
    if code in (_T_FLOAT, _T_DOUBLE):
        return _Col(name, "DOUBLE", pa.float64())
    if code in (_T_DATE, _T_NEWDATE):
        return _Col(name, "DATE", pa.date32())
    if code in (_T_DATETIME, _T_TIMESTAMP):
        return _Col(name, "DATETIME", pa.timestamp("us"))
    if code == _T_TIME:
        return _Col(name, "INTERVAL", pa.duration("us"))
    if code == _T_BIT:
        return _Col(name, "BLOB", pa.binary())
    if code in _BLOB_TYPES and getattr(f, "charsetnr", None) == _BINARY_CHARSET:
        return _Col(name, "BLOB", pa.binary())
    return _Col(name, "VARCHAR", pa.string())


async def _table_schema(ref: _TableRef, timeout: int) -> List[_Col]:
    from app.core.security import decrypt_value
    from app.services.engine import _query_mysql_raw

    key = (ref.ds.id, str(getattr(ref.ds, "updated_at", "")), ref.db, ref.table)
    hit = _schema_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    sql = "SELECT * FROM " + exp.Table(
        this=exp.to_identifier(ref.table, quoted=True), db=exp.to_identifier(ref.db, quoted=True),
    ).sql(dialect=_MySQL) + " LIMIT 0"
    password = decrypt_value(ref.ds.password_encrypted) if ref.ds.password_encrypted else ""
    try:
        fields, _, _ = await _query_mysql_raw(ref.ds, password, sql, {}, timeout=timeout, max_rows=1)
    except Exception as e:
        raise FederatedError(f"读取表结构失败：数据源「{ref.ds.name}」的 {ref.db}.{ref.table}：{e}")
    cols = [_field_col(f) for f in fields]
    if len(_schema_cache) > 5000:
        _schema_cache.clear()
    _schema_cache[key] = (time.monotonic() + max(0, settings.catalog.schema_cache_ttl), cols)
    return cols


def clear_schema_cache() -> None:
    _schema_cache.clear()


# ======================================================================
# 结果列名：与 MySQL 一致（未写别名的表达式，列名就是表达式原文）
# ======================================================================

_SELECT_MODIFIERS = {"DISTINCT", "ALL", "DISTINCTROW", "STRAIGHT_JOIN", "HIGH_PRIORITY", "SQL_SMALL_RESULT",
                     "SQL_BIG_RESULT", "SQL_BUFFER_RESULT", "SQL_NO_CACHE", "SQL_CACHE", "SQL_CALC_FOUND_ROWS"}


def _projection_texts(sql: str, n: int) -> Optional[List[str]]:
    """原 SQL 最外层 SELECT 列表每一项的原文（按顶层逗号切分），切分结果与列数不符时返回 None。"""
    try:
        tokens = sqlglot.tokenize(sql, read=_MySQL)
    except Exception:
        return None
    depth, i = 0, 0
    # 跳过 WITH ... 定义，找到深度 0 的第一个 SELECT
    while i < len(tokens):
        tk = tokens[i]
        if tk.token_type.name in ("L_PAREN",):
            depth += 1
        elif tk.token_type.name in ("R_PAREN",):
            depth -= 1
        elif depth == 0 and tk.text.upper() == "SELECT":
            break
        i += 1
    i += 1
    while i < len(tokens) and tokens[i].text.upper() in _SELECT_MODIFIERS:
        i += 1
    segs, start, depth = [], i, 0
    while i < len(tokens):
        tk = tokens[i]
        name = tk.token_type.name
        if name == "L_PAREN":
            depth += 1
        elif name == "R_PAREN":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (name == "COMMA" or tk.text.upper() in ("FROM", "UNION", "INTERSECT", "EXCEPT", "LIMIT", "ORDER", "WHERE", "GROUP", "HAVING", "INTO", "WINDOW", "FOR")):
            if start < i:
                segs.append(sql[tokens[start].start:tokens[i - 1].end + 1])
            if name != "COMMA":
                break
            start = i + 1
        i += 1
    else:
        if start < i:
            segs.append(sql[tokens[start].start:tokens[i - 1].end + 1])
    return segs if len(segs) == n else None


def _leftmost_select(ast: exp.Expression) -> Optional[exp.Select]:
    node = ast
    while isinstance(node, exp.SetOperation):
        node = node.this
    while isinstance(node, exp.Subquery):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def _name_projections(ast: exp.Expression, sql: str) -> None:
    """给最外层 SELECT 中没写别名的表达式加上 MySQL 会用的列名，保证跨源计算的结果字段名与 MySQL 一致。"""
    sel = _leftmost_select(ast)
    if sel is None:
        return
    texts = _projection_texts(sql, len(sel.expressions))
    for idx, p in enumerate(sel.expressions):
        if isinstance(p, exp.Alias) or isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)):
            continue
        if isinstance(p, exp.Column):
            name = p.name
        elif texts is not None:
            name = texts[idx].strip()
        else:
            name = p.sql(dialect=_MySQL)
        p.replace(exp.alias_(p.copy(), exp.to_identifier(name, quoted=True)))


def _normalize_column_case(ast: exp.Expression, schemas: Dict[tuple, List[_Col]]) -> None:
    """MySQL 列名不区分大小写：把写法与表结构大小写不一致的列名改成表结构里的写法。"""
    actual = {}
    for cols in schemas.values():
        for c in cols:
            actual.setdefault(c.name.lower(), c.name)
    exact = {c.name for cols in schemas.values() for c in cols}
    for col in ast.find_all(exp.Column):
        ident = col.this
        if isinstance(ident, exp.Identifier) and ident.name not in exact and ident.name.lower() in actual:
            col.set("this", exp.to_identifier(actual[ident.name.lower()], quoted=ident.quoted))


_QUALIFY_ERR_PATTERNS = [
    (re.compile(r"Column '(.+?)' could not be resolved"), "列 {0} 无法确定属于哪张表（不存在或多张表都有该列），请写成「表别名.列名」"),
    (re.compile(r"Unknown column: (.+)"), "列 {0} 不存在"),
    (re.compile(r"Unknown table: (.+)"), "表 {0} 不存在"),
    (re.compile(r"Ambiguous column '?([^']+)'?"), "列 {0} 在多张表中都存在，请写成「表别名.列名」"),
]


def _qualify(ast: exp.Expression, schema: dict) -> exp.Expression:
    try:
        return qualify(ast, schema=schema, dialect=_MySQL, quote_identifiers=False,
                       validate_qualify_columns=True, identify=False)
    except OptimizeError as e:
        msg = _first_line(e)
        for pat, tpl in _QUALIFY_ERR_PATTERNS:
            m = pat.search(msg)
            if m:
                raise FederatedError(tpl.format(m.group(1)))
        raise FederatedError(f"SQL 无法解析列与表的对应关系：{msg}")


# ======================================================================
# 执行计划：哪些部分下推到哪个数据源
# ======================================================================

@dataclass
class _Source:
    """一个从数据源取数的单元：一张表（base），或整段下推的子查询（query）。"""
    idx: int
    kind: str                         # "table" / "query"
    ds: Any
    scope: Optional[Scope] = None     # 表所在的查询层（table）
    node: Optional[exp.Expression] = None   # 表节点（table）或被整段下推的查询（query）
    alias: str = ""
    ref: Optional[_TableRef] = None
    columns: List[str] = field(default_factory=list)
    preds: List[exp.Expression] = field(default_factory=list)
    order: int = 0                    # 在所在查询层 FROM/JOIN 中的位置
    base_sql: str = ""                # 下推 SQL：「SELECT 列 FROM 表 AS 别名」（整段下推的子查询为完整 SQL）
    preds_sql: str = ""               # 本表的过滤条件（AND 连接）

    @property
    def local(self) -> str:
        return f"__src_{self.idx}"

    def label(self) -> str:
        if self.kind == "table":
            return f"{self.ds.name}.{self.ref.db}.{self.ref.table} AS {self.alias}"
        return f"{self.ds.name}（子查询）"


def _children(scope: Scope):
    for attr in ("cte_scopes", "set_operation_scopes", "derived_table_scopes", "subquery_scopes", "udtf_scopes"):
        yield from getattr(scope, attr)


def _all_scopes(scope: Scope):
    yield scope
    for c in _children(scope):
        yield from _all_scopes(c)


def _resolve_source(scope: Scope, name: str):
    """沿查询层向外查找名字对应的来源（exp.Table 或 Scope）。"""
    s = scope
    while s is not None:
        src = s.sources.get(name)
        if src is not None:
            return s, src
        s = s.parent
    return None, None


class _Planner:
    def __init__(self, ast: exp.Expression, refs: List[_TableRef]):
        self.ast = ast
        self.by_node = {id(r.node): r for r in refs}
        self.sources: List[_Source] = []
        self.table_src: Dict[int, _Source] = {}    # id(Table 节点) -> 来源
        self.scope_src: Dict[int, _Source] = {}    # id(Scope.expression) -> 整段下推的来源
        self.pushed_scopes: set = set()            # 被整段下推的 Scope（含其内部各层）的 id(expression)
        self.equi: List[tuple] = []                # (目标来源, 目标列, 提供键的来源, 键列)

    # ---------- 整段下推 ----------
    def _tables_in(self, scope: Scope):
        out = []
        for s in _all_scopes(scope):
            for src in s.sources.values():
                if isinstance(src, exp.Table) and id(src) in self.by_node:
                    out.append(src)
        return out

    def _pushable(self, scope: Scope) -> Optional[Any]:
        if scope.is_root or scope.is_udtf:
            return None
        if not isinstance(scope.expression, (exp.Select, exp.SetOperation)):
            return None
        inner = list(_all_scopes(scope))
        defined = set()
        for s in inner:
            for name, src in s.sources.items():
                if isinstance(src, Scope) and src.is_cte and src not in inner:
                    # 引用了外面定义的 CTE：CTE 不能随这段 SQL 一起下推
                    if any(c.table == name for c in s.columns) or any(
                            t.name == name and not t.args.get("catalog") for t in s.expression.find_all(exp.Table)):
                        return None
                defined.add(name)
        for s in inner:
            for c in s.external_columns:
                if c.table not in defined:
                    return None   # 关联子查询，引用了外层的表
        tables = self._tables_in(scope)
        if not tables:
            return None
        ds_ids = {self.by_node[id(t)].ds.id for t in tables}
        if len(ds_ids) != 1:
            return None
        return self.by_node[id(tables[0])].ds

    def _walk(self, scope: Scope):
        ds = self._pushable(scope)
        if ds is not None:
            src = _Source(idx=len(self.sources), kind="query", ds=ds, node=scope.expression, scope=scope)
            self.sources.append(src)
            self.scope_src[id(scope.expression)] = src
            for s in _all_scopes(scope):
                self.pushed_scopes.add(id(s.expression))
            return
        for c in _children(scope):
            self._walk(c)

    # ---------- 逐表下推 ----------
    def build(self):
        root = build_scope(self.ast)
        if root is None:
            raise FederatedError("无法分析该 SQL 的结构")
        self.root = root
        self._walk(root)
        live = [s for s in _all_scopes(root) if id(s.expression) not in self.pushed_scopes]
        for scope in live:
            order = self._join_order(scope)
            for name, src in scope.sources.items():
                if isinstance(src, exp.Table) and id(src) in self.by_node and id(src) not in self.table_src:
                    ref = self.by_node[id(src)]
                    s = _Source(idx=len(self.sources), kind="table", ds=ref.ds, scope=scope, node=src,
                                alias=src.alias_or_name, ref=ref, order=order.get(name, 0))
                    self.sources.append(s)
                    self.table_src[id(src)] = s
        # 每张表用到的列
        for scope in live:
            for col in scope.columns:
                if not col.table:
                    continue
                _, src = _resolve_source(scope, col.table)
                if isinstance(src, exp.Table) and id(src) in self.table_src:
                    s = self.table_src[id(src)]
                    if col.name not in s.columns:
                        s.columns.append(col.name)
        for scope in live:
            if isinstance(scope.expression, exp.Select):
                self._push_predicates(scope)
        return self

    def _join_order(self, scope: Scope) -> Dict[str, int]:
        sel = scope.expression
        if not isinstance(sel, exp.Select):
            return {}
        order = {}
        frm = sel.args.get("from_") or sel.args.get("from")
        if frm is not None:
            order[frm.this.alias_or_name] = 0
        for i, j in enumerate(sel.args.get("joins") or [], 1):
            order[j.this.alias_or_name] = i
        return order

    def _own_source(self, scope: Scope, name: str):
        """名字在本层直接对应的来源对象（表来源或整段下推的子查询来源）。"""
        src = scope.sources.get(name)
        if isinstance(src, exp.Table):
            return self.table_src.get(id(src))
        if isinstance(src, Scope):
            return self.scope_src.get(id(src.expression))
        return None

    def _single_owner(self, scope: Scope, cond: exp.Expression) -> Optional[_Source]:
        """条件只涉及本层的一张表、可以交给源库算时，返回那张表的来源。"""
        if cond.find(exp.Select, exp.Subquery, exp.AggFunc, exp.Window) is not None:
            return None
        cols = list(cond.find_all(exp.Column))
        if not cols:
            return None
        owners = {c.table for c in cols}
        if len(owners) != 1:
            return None
        src = scope.sources.get(owners.pop())
        if isinstance(src, exp.Table):
            return self.table_src.get(id(src))
        return None

    def _push_predicates(self, scope: Scope):
        sel = scope.expression
        joins = sel.args.get("joins") or []
        frm = sel.args.get("from_") or sel.args.get("from")
        seen = [frm.this.alias_or_name] if frm is not None else []
        nullable = set()
        for j in joins:
            right = j.this.alias_or_name
            side = (j.side or "").upper()
            if side == "LEFT":
                nullable.add(right)
            elif side == "RIGHT":
                nullable.update(seen)
            elif side == "FULL":
                nullable.update(seen + [right])
            seen.append(right)

        def conjuncts(e):
            return list(e.flatten()) if isinstance(e, exp.And) else [e]

        def drop(cond):
            cond.replace(exp.true())

        def tidy(node, key):
            """去掉已下推（被替换成 TRUE）的条件；WHERE 全部下推后整个去掉，ON 全部下推后保留 ON TRUE。"""
            holder = node.args.get(key)
            if holder is None:
                return
            cond = holder.this if isinstance(holder, exp.Where) else holder
            keep = [c for c in conjuncts(cond) if not (isinstance(c, exp.Boolean) and c.this is True)]
            if len(keep) == len(conjuncts(cond)):
                return
            new = exp.and_(*keep) if keep else None
            if isinstance(holder, exp.Where):
                if new is None:
                    node.set(key, None)
                else:
                    holder.set("this", new)
            else:
                node.set(key, new if new is not None else exp.true())

        where = sel.args.get("where")
        if where is not None:
            for cond in conjuncts(where.this):
                owner = self._single_owner(scope, cond)
                if owner is not None and owner.alias not in nullable:
                    owner.preds.append(cond.copy())
                    drop(cond)
                else:
                    self._collect_equi(scope, cond)
        for j in joins:
            on = j.args.get("on")
            if on is None:
                continue
            right = j.this.alias_or_name
            side = (j.side or "").upper()
            for cond in conjuncts(on):
                owner = self._single_owner(scope, cond)
                # 内连接：两侧（非外连接可空侧）都可下推；LEFT JOIN 只下推到右表；RIGHT JOIN 只下推到左侧的表
                ok = owner is not None and (
                    (side in ("", "INNER", "CROSS") and owner.alias not in nullable)
                    or (side == "LEFT" and owner.alias == right)
                    or (side == "RIGHT" and owner.alias != right)
                )
                if ok:
                    owner.preds.append(cond.copy())
                    drop(cond)
                else:
                    self._collect_equi(scope, cond, side=side, right=right)
            tidy(j, "on")
        tidy(sel, "where")

    def _any_source(self, scope: Scope, name: str):
        """名字对应的来源（可以是外层查询的表，用于关联子查询）及其所在层。"""
        s, src = _resolve_source(scope, name)
        if s is None:
            return None, None
        return s, self._own_source(s, name)

    def _collect_equi(self, scope: Scope, cond: exp.Expression, side: Optional[str] = None, right: Optional[str] = None):
        """记录等值关联 a.x = b.y，以及哪一侧可以按另一侧的键过滤（动态过滤）：
          - WHERE 中的等值、内连接 ON 中的等值：每行都必须满足，两侧都可以；
          - LEFT JOIN 的 ON：只能过滤右表（左表是保留侧，没关联上的行也要输出）；RIGHT JOIN 反之；FULL JOIN 都不行；
          - 一侧在本层、另一侧在外层（关联子查询）：只能按外层的键过滤本层的表。
        WHERE 中的「列 IN (整段下推的子查询)」也记录下来：子查询结果就是该列的过滤键。"""
        if side == "FULL":
            return
        if isinstance(cond, exp.In) and cond.args.get("query") is not None and isinstance(cond.this, exp.Column):
            q = cond.args["query"]
            q = q.this if isinstance(q, exp.Subquery) else q
            prov = self.scope_src.get(id(q))
            tgt = self._own_source(scope, cond.this.table) if cond.this.table else None
            if prov is not None and tgt is not None and tgt.kind == "table" and isinstance(q, exp.Select) and q.expressions:
                self.equi.append((tgt, cond.this.name, prov, q.expressions[0].alias_or_name))
            return
        if not isinstance(cond, exp.EQ):
            return
        a, b = cond.this, cond.expression
        if not (isinstance(a, exp.Column) and isinstance(b, exp.Column)) or not a.table or not b.table:
            return
        la, sa = self._any_source(scope, a.table)
        lb, sb = self._any_source(scope, b.table)
        if sa is None or sb is None or sa is sb:
            return
        if la is scope and lb is scope:
            # (目标, 目标列, 提供键的一侧, 键列)；外连接只允许过滤可空的一侧
            for tgt, tcol, prov, pcol in ((sa, a.name, sb, b.name), (sb, b.name, sa, a.name)):
                if side == "LEFT" and tgt.alias != right:
                    continue
                if side == "RIGHT" and tgt.alias == right:
                    continue
                self.equi.append((tgt, tcol, prov, pcol))
        elif la is scope:
            self.equi.append((sa, a.name, sb, b.name))
        elif lb is scope:
            self.equi.append((sb, b.name, sa, a.name))


# ======================================================================
# DuckDB 运行环境：进程内一个内存库，禁止访问文件系统 / 安装插件，配置锁定
# ======================================================================

# MySQL 有、DuckDB 没有或语义不同的函数，用宏实现成与 MySQL 一致的结果
_MACROS = [
    # LENGTH 是字节数（CHAR_LENGTH 才是字符数）
    "CREATE MACRO __my_length(x) AS strlen(CAST(x AS VARCHAR))",
    # DAYOFWEEK：1 = 周日；WEEKDAY：0 = 周一
    "CREATE MACRO __my_dayofweek(d) AS dayofweek(CAST(d AS DATE)) + 1",
    "CREATE MACRO __my_weekday(d) AS (dayofweek(CAST(d AS DATE)) + 6) % 7",
    # WEEK(d) / WEEK(d, 0)：周日为一周开始，本年第一个周日之前是第 0 周
    "CREATE MACRO __my_week0(d) AS (CASE WHEN dayofyear(CAST(d AS DATE)) < 1 + (7 - dayofweek(date_trunc('year', CAST(d AS DATE)))) % 7 "
    "THEN 0 ELSE (dayofyear(CAST(d AS DATE)) - 1 - (7 - dayofweek(date_trunc('year', CAST(d AS DATE)))) % 7) // 7 + 1 END)",
    # WEEK(d, 1)：周一为一周开始，含 4 天以上的第一周为第 1 周，范围 0~53
    "CREATE MACRO __my_week1(d) AS (CASE WHEN month(CAST(d AS DATE)) = 1 AND week(CAST(d AS DATE)) >= 52 THEN 0 "
    "WHEN month(CAST(d AS DATE)) = 12 AND week(CAST(d AS DATE)) = 1 THEN 53 ELSE week(CAST(d AS DATE)) END)",
    "CREATE MACRO __my_find_in_set(s, l) AS (CASE WHEN s IS NULL OR l IS NULL THEN NULL WHEN CAST(l AS VARCHAR) = '' THEN 0 "
    "ELSE coalesce(list_position(string_split(lower(CAST(l AS VARCHAR)), ','), lower(CAST(s AS VARCHAR))), 0) END)",
    "CREATE MACRO __my_substring_index(s, d, n) AS (CASE WHEN n > 0 THEN array_to_string(string_split(CAST(s AS VARCHAR), d)[1:n], d) "
    "WHEN n < 0 THEN array_to_string(string_split(CAST(s AS VARCHAR), d)[n:], d) ELSE '' END)",
    # UNIX_TIMESTAMP / FROM_UNIXTIME：按北京时间解释日期时间（off = 时区偏移秒数）
    "CREATE MACRO __my_unix_timestamp(x, off) AS CAST(floor(epoch(CAST(x AS TIMESTAMP))) AS BIGINT) - off",
    "CREATE MACRO __my_from_unixtime(x, off) AS make_timestamp(CAST(round((CAST(x AS DOUBLE) + off) * 1000000) AS BIGINT))",
    "CREATE MACRO __my_makedate(y, n) AS (CASE WHEN n <= 0 THEN NULL ELSE make_date(CAST(y AS INTEGER), 1, 1) + CAST(n - 1 AS INTEGER) END)",
    "CREATE MACRO __my_to_days(d) AS date_diff('day', DATE '0001-01-01', CAST(d AS DATE)) + 366",
    "CREATE MACRO __my_from_days(n) AS DATE '0001-01-01' + CAST(n - 366 AS INTEGER)",
    # 字符串转数字（MySQL 规则）：取开头的数字部分，没有数字时为 0；__my_int 只取整数部分
    "CREATE MACRO __my_num(s) AS (CASE WHEN s IS NULL THEN NULL ELSE coalesce(TRY_CAST(nullif(regexp_extract("
    "ltrim(CAST(s AS VARCHAR)), '^[+-]?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][+-]?[0-9]+)?'), '') AS DOUBLE), 0) END)",
    "CREATE MACRO __my_int(s) AS (CASE WHEN s IS NULL THEN NULL ELSE coalesce(TRY_CAST(nullif(regexp_extract("
    "ltrim(CAST(s AS VARCHAR)), '^[+-]?[0-9]+'), '') AS HUGEINT), 0) END)",
    # ASCII：第一个字节的值（UTF-8 编码）
    "CREATE MACRO __my_ascii(s) AS (CASE WHEN s IS NULL THEN NULL WHEN CAST(s AS VARCHAR) = '' THEN 0 ELSE ("
    "CASE WHEN unicode(CAST(s AS VARCHAR)) < 128 THEN unicode(CAST(s AS VARCHAR)) "
    "WHEN unicode(CAST(s AS VARCHAR)) < 2048 THEN 192 + (unicode(CAST(s AS VARCHAR)) >> 6) "
    "WHEN unicode(CAST(s AS VARCHAR)) < 65536 THEN 224 + (unicode(CAST(s AS VARCHAR)) >> 12) "
    "ELSE 240 + (unicode(CAST(s AS VARCHAR)) >> 18) END) END)",
    # YEARWEEK(d) / WEEK 模式 2：周日为一周开始，本年第一个周日之前的几天算上一年的最后一周
    "CREATE MACRO __my_yearweek0(d) AS (CASE WHEN __my_week0(d) = 0 "
    "THEN (year(CAST(d AS DATE)) - 1) * 100 + __my_week0(make_date(year(CAST(d AS DATE)) - 1, 12, 31)) "
    "ELSE year(CAST(d AS DATE)) * 100 + __my_week0(d) END)",
    "CREATE MACRO __my_day_suffix(d) AS (CAST(day(CAST(d AS DATE)) AS VARCHAR) || CASE "
    "WHEN day(CAST(d AS DATE)) IN (11, 12, 13) THEN 'th' WHEN day(CAST(d AS DATE)) % 10 = 1 THEN 'st' "
    "WHEN day(CAST(d AS DATE)) % 10 = 2 THEN 'nd' WHEN day(CAST(d AS DATE)) % 10 = 3 THEN 'rd' ELSE 'th' END)",
    # TIMESTAMPDIFF：MySQL 按「满」多少个单位计算（DuckDB 的 date_diff 是数跨过了几个边界）
    "CREATE MACRO __my_tsdiff(a, b, unit_us) AS CAST(trunc((epoch_us(CAST(b AS TIMESTAMP)) - epoch_us(CAST(a AS TIMESTAMP))) / unit_us) AS BIGINT)",
    "CREATE MACRO __my_month_diff(a, b) AS (((year(CAST(b AS TIMESTAMP)) - year(CAST(a AS TIMESTAMP))) * 12 "
    "+ month(CAST(b AS TIMESTAMP)) - month(CAST(a AS TIMESTAMP))) + CASE "
    "WHEN ((year(CAST(b AS TIMESTAMP)) - year(CAST(a AS TIMESTAMP))) * 12 + month(CAST(b AS TIMESTAMP)) - month(CAST(a AS TIMESTAMP))) > 0 "
    "AND strftime(CAST(b AS TIMESTAMP), '%d%H%M%S%f') < strftime(CAST(a AS TIMESTAMP), '%d%H%M%S%f') THEN -1 "
    "WHEN ((year(CAST(b AS TIMESTAMP)) - year(CAST(a AS TIMESTAMP))) * 12 + month(CAST(b AS TIMESTAMP)) - month(CAST(a AS TIMESTAMP))) < 0 "
    "AND strftime(CAST(b AS TIMESTAMP), '%d%H%M%S%f') > strftime(CAST(a AS TIMESTAMP), '%d%H%M%S%f') THEN 1 ELSE 0 END)",
    "CREATE MACRO __my_json_unquote(x) AS (CASE WHEN x IS NULL THEN NULL "
    "WHEN json_valid(CAST(x AS VARCHAR)) AND json_type(CAST(x AS VARCHAR)) = 'VARCHAR' THEN json_extract_string(CAST(x AS VARCHAR), '$') "
    "ELSE CAST(x AS VARCHAR) END)",
    "CREATE MACRO __my_json_length(x) AS (CASE WHEN x IS NULL THEN NULL WHEN json_type(CAST(x AS VARCHAR)) = 'ARRAY' THEN json_array_length(CAST(x AS VARCHAR)) "
    "WHEN json_type(CAST(x AS VARCHAR)) = 'OBJECT' THEN len(json_keys(CAST(x AS VARCHAR))) ELSE 1 END), "
    "(x, p) AS (CASE WHEN x IS NULL OR json_extract(CAST(x AS VARCHAR), p) IS NULL THEN NULL "
    "WHEN json_type(json_extract(CAST(x AS VARCHAR), p)) = 'ARRAY' THEN json_array_length(json_extract(CAST(x AS VARCHAR), p)) "
    "WHEN json_type(json_extract(CAST(x AS VARCHAR), p)) = 'OBJECT' THEN len(json_keys(json_extract(CAST(x AS VARCHAR), p))) ELSE 1 END)",
]

# 跨源计算的字符串比较规则（catalog.string_compare）：
#   nocase          不区分大小写（默认，与 MySQL *_ci 排序规则对中文 / 英文数据的结果一致）
#   nocase_noaccent 再加上不区分重音（é = e），与 utf8mb4_general_ci 完全一致；字符串关联约慢一倍
#   binary          区分大小写
_COLLATIONS = {"nocase": "nocase", "nocase_noaccent": "nocase.noaccent", "binary": ""}

_duck = None
_duck_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def _duck_db():
    """进程内共享的 DuckDB 内存库；每次计算用独立的 cursor（独立连接，互不干扰）。"""
    global _duck, _executor
    if _duck is not None:
        return _duck
    with _duck_lock:
        if _duck is None:
            import duckdb
            cfg = settings.catalog
            con = duckdb.connect(":memory:", config={
                "enable_external_access": False,        # 禁止读写文件、ATTACH、COPY
                "autoinstall_known_extensions": False,  # 禁止下载 / 加载插件
                "autoload_known_extensions": False,
                "memory_limit": cfg.memory_limit,
                "threads": cfg.threads,
            })
            for m in _MACROS:
                con.execute(m)
            collation = _COLLATIONS.get(cfg.string_compare, "nocase")
            if collation:
                con.execute(f"SET GLOBAL default_collation = '{collation}'")
            # MySQL：升序时 NULL 在前，降序时 NULL 在后
            con.execute("SET GLOBAL default_null_order = 'nulls_first_on_asc_last_on_desc'")
            con.execute("SET lock_configuration = true")
            _executor = ThreadPoolExecutor(max_workers=cfg.max_concurrency, thread_name_prefix="federated")
            _duck = con
    return _duck


# ======================================================================
# MySQL 语义兼容：把语法树改写成在 DuckDB 上与 MySQL 结果一致的形式
# ======================================================================

_DATE_UNITS = {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}
_UNSUPPORTED_FUNCS = {
    "LAST_INSERT_ID", "ROW_COUNT", "FOUND_ROWS", "CONNECTION_ID", "USER", "SESSION_USER", "SYSTEM_USER",
    "DATABASE", "VERSION", "SLEEP", "BENCHMARK", "GET_LOCK", "RELEASE_LOCK", "IS_FREE_LOCK",
    "IS_USED_LOCK", "LOAD_FILE", "MASTER_POS_WAIT", "UUID_SHORT", "CONV", "OCT", "CRC32", "QUOTE", "TIME_FORMAT",
}
_US = {"MICROSECOND": 1, "SECOND": 1_000_000, "MINUTE": 60_000_000, "HOUR": 3_600_000_000,
       "DAY": 86_400_000_000, "WEEK": 604_800_000_000}
_NUM_PREFIX_RE = re.compile(r"^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _mysql_str_num(text: str):
    """MySQL 把字符串当数字用时的取值：开头的数字部分，没有则为 0。"""
    m = _NUM_PREFIX_RE.match(text or "")
    if not m:
        return 0
    try:
        v = float(m.group(0))
    except ValueError:
        return 0
    return int(v) if v.is_integer() and "e" not in m.group(0).lower() and "." not in m.group(0) else v
_DUCK_ONLY_FUNCS = {"glob", "getenv", "current_setting", "sniff_csv", "query", "query_table", "which_secret", "enable_logging"}


def _F(name: str, *args) -> exp.Anonymous:
    return exp.Anonymous(this=name, expressions=list(args))


def _dtype(node) -> Optional[exp.DataType]:
    t = getattr(node, "type", None)
    if t is None or t.this in (exp.DataType.Type.UNKNOWN, exp.DataType.Type.NULL):
        return None
    return t


def _family(node) -> Optional[str]:
    t = _dtype(node)
    if t is None:
        return None
    if t.this == exp.DataType.Type.BOOLEAN:
        return "bool"
    if t.is_type(*exp.DataType.TEXT_TYPES):
        return "str"
    if t.is_type(*exp.DataType.NUMERIC_TYPES):
        return "num"
    if t.is_type(*exp.DataType.TEMPORAL_TYPES):
        return "time"
    return None


def _exact_scale(node) -> Optional[int]:
    """整数 / DECIMAL 返回小数位数，其它类型返回 None。"""
    t = _dtype(node)
    if t is None:
        return None
    if t.is_type(*exp.DataType.INTEGER_TYPES):
        return 0
    if t.this == exp.DataType.Type.DECIMAL:
        params = t.expressions
        if len(params) >= 2:
            try:
                return int(params[1].name)
            except (TypeError, ValueError):
                return None
        return 0
    return None


_ARITH = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv, exp.Paren, exp.Neg)


def _is_arith(node) -> bool:
    return isinstance(node, _ARITH)


def _mysql_scale(node) -> Optional[int]:
    """按 MySQL 的 DECIMAL 运算规则推算算术表达式结果的小数位；含浮点数 / 未知类型时返回 None。"""
    if isinstance(node, (exp.Paren, exp.Neg)):
        return _mysql_scale(node.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return None
        t = node.name.lower()
        if "e" in t:
            return None
        return len(t.split(".", 1)[1]) if "." in t else 0
    if isinstance(node, (exp.Div, exp.Mul, exp.Add, exp.Sub, exp.Mod)):
        l, r = _mysql_scale(node.this), _mysql_scale(node.expression)
        if l is None or r is None:
            return None
        if isinstance(node, exp.Div):
            return l + 4
        if isinstance(node, exp.Mul):
            return l + r
        return max(l, r)
    if isinstance(node, exp.Round):
        d = node.args.get("decimals")
        if d is None:
            return 0
        return int(d.name) if isinstance(d, exp.Literal) and not d.is_string else None
    return _exact_scale(node)


def _cast(node, to: str, try_: bool = False):
    cls = exp.TryCast if try_ else exp.Cast
    return cls(this=node, to=exp.DataType.build(to))


def _is_lit(node) -> bool:
    return isinstance(node, (exp.Literal, exp.Null, exp.Boolean))


class _MySQLCompat:
    def __init__(self, nocase: bool, markers: bool = False):
        self.nocase = nocase
        # 当前时间：markers=True 时写成占位标记，每次执行时代入（SQL 模板会被缓存复用）
        self.markers = markers
        now = datetime.datetime.now(CST)
        self.now = now.replace(tzinfo=None, microsecond=0)
        self.utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
        self.tz_offset = int(now.utcoffset().total_seconds())
        self.epoch = int(now.timestamp())

    def _ts(self, v: datetime.datetime):
        if self.markers:
            return _marker("t_utc_ts" if v is self.utc_now else "t_now_ts")
        return _cast(exp.Literal.string(v.strftime("%Y-%m-%d %H:%M:%S")), "TIMESTAMP")

    def _date(self, v: datetime.datetime):
        if self.markers:
            return _marker("t_utc_date" if v is self.utc_now else "t_now_date")
        return _cast(exp.Literal.string(v.strftime("%Y-%m-%d")), "DATE")

    def _time(self, v: datetime.datetime):
        if self.markers:
            return _marker("t_utc_time" if v is self.utc_now else "t_now_time")
        return _cast(exp.Literal.string(v.strftime("%H:%M:%S")), "TIME")

    def apply(self, ast: exp.Expression) -> exp.Expression:
        # 子节点先于父节点改写；改写后的节点沿用原节点推导出的类型，供父节点判断
        for node in list(ast.dfs())[::-1]:
            if node is ast or node.parent is None:
                continue
            # 先摘下原节点再改写：改写结果常把原节点包在里面（如 ROUND(原表达式)），
            # 包装时原节点的父指针会被改掉，不能再用 node.replace
            slot = exp.Null()
            node.replace(slot)
            self.parent = slot.parent
            new = self.rewrite(node)
            if new is None:
                new = node
            if new is not node and getattr(new, "type", None) is None and getattr(node, "type", None) is not None:
                new.type = node.type
            slot.replace(new)
        return ast

    def rewrite(self, n: exp.Expression):  # noqa: C901 - 逐类改写，按类型分派最直观
        # ---- 当前时间：整条 SQL 用同一个时刻（与 MySQL 一致），按北京时间 ----
        if isinstance(n, (exp.CurrentTimestamp, exp.CurrentDatetime, exp.Localtime, exp.Localtimestamp)):
            return self._ts(self.now)
        if isinstance(n, exp.CurrentDate):
            return self._date(self.now)
        if isinstance(n, exp.CurrentTime):
            return self._time(self.now)
        if isinstance(n, exp.UtcTimestamp):
            return self._ts(self.utc_now)
        if isinstance(n, exp.UtcDate):
            return self._date(self.utc_now)
        if isinstance(n, exp.UtcTime):
            return self._time(self.utc_now)
        if isinstance(n, (exp.CurrentSchema, exp.CurrentUser, exp.CurrentVersion)):
            raise FederatedError(f"跨数据源查询不支持 {n.sql(dialect=_MySQL)}")

        if isinstance(n, exp.Anonymous):
            return self._anonymous(n)
        if isinstance(n, exp.Length) and n.args.get("binary"):
            return _F("__my_length", n.this)
        if isinstance(n, exp.DayOfWeek):
            return _F("__my_dayofweek", n.this)
        if isinstance(n, exp.Week):
            mode = n.args.get("mode")
            m = 0
            if mode is not None:
                if not isinstance(mode, exp.Literal) or mode.is_string:
                    raise FederatedError("WEEK() 的第二个参数需是常量")
                m = int(mode.name)
            if m == 0:
                return _F("__my_week0", n.this)
            if m == 1:
                return _F("__my_week1", n.this)
            if m == 3:
                return _F("week", _cast(n.this, "DATE"))
            raise FederatedError(f"跨数据源查询中 WEEK() 暂只支持模式 0 / 1 / 3（当前 {m}）")
        if isinstance(n, exp.UnixToTime):
            base = _F("__my_from_unixtime", n.this, exp.Literal.number(self.tz_offset))
            fmt = n.args.get("format")
            if fmt is None:
                return base
            if not isinstance(fmt, exp.Literal):
                raise FederatedError("FROM_UNIXTIME() 的格式参数需是常量")
            tpl = sqlglot.parse_one(f"SELECT DATE_FORMAT(__x__, {fmt.sql(dialect=_MySQL)})", read=_MySQL).expressions[0]
            tpl.this.replace(base)
            return tpl
        if isinstance(n, exp.Pad):
            n.set("this", _cast(n.this, "VARCHAR"))
            return n
        if self.nocase and isinstance(n, exp.Like):
            return exp.ILike(**n.args)
        if self.nocase and isinstance(n, exp.RegexpLike):
            return _F("regexp_matches", n.this, n.expression, exp.Literal.string("i"))

        # ---- 日期加减：DATE 加减天/月/年，MySQL 结果仍是 DATE ----
        if isinstance(n, (exp.DateAdd, exp.DateSub)):
            unit = (n.args.get("unit").name if n.args.get("unit") is not None else "DAY").upper()
            if unit in _DATE_UNITS and _dtype(n.this) is not None and _dtype(n.this).this == exp.DataType.Type.DATE:
                return _cast(n, "DATE")
            return None
        if isinstance(n, (exp.Add, exp.Sub)) and isinstance(n.expression, exp.Interval):
            unit = n.expression.args.get("unit")
            if unit is not None and unit.name.upper() in _DATE_UNITS and _dtype(n.this) is not None \
                    and _dtype(n.this).this == exp.DataType.Type.DATE:
                return _cast(n, "DATE")
            return None

        # ---- 隐式类型转换：字符串参与算术、字符串与数字/日期比较时按 MySQL 规则转换 ----
        if isinstance(n, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv)):
            for side in ("this", "expression"):
                child = n.args.get(side)
                fam = _family(child)
                if fam == "str":
                    n.set(side, self._as_num(child))
                elif fam == "bool":
                    n.set(side, _cast(child, "INTEGER"))
            # MySQL 整数 / DECIMAL 相除：结果保留「被除数小数位 + 4」位（中间计算保留更多位）。
            # 在整个算术表达式的最外层按 MySQL 推算的结果小数位四舍五入一次
            if not _is_arith(self.parent) and n.find(exp.Div) is not None:
                sc = _mysql_scale(n)
                if sc is not None:
                    return exp.Round(this=n, decimals=exp.Literal.number(min(sc, 30)))
            return None
        if isinstance(n, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ)):
            a, b = n.this, n.expression
            fa, fb = _family(a), _family(b)
            if {fa, fb} == {"str", "num"}:
                # 字符串与数字比较：MySQL 按数字比较（'abc' 当作 0）
                for side, child, fam in (("this", a, fa), ("expression", b, fb)):
                    if fam == "str":
                        n.set(side, self._as_num(child))
            elif {fa, fb} == {"str", "time"}:
                for side, child, fam in (("this", a, fa), ("expression", b, fb)):
                    if fam == "str" and not _is_lit(child):
                        n.set(side, _cast(child, "TIMESTAMP", try_=True))
            elif "bool" in (fa, fb) and {fa, fb} != {"bool"}:
                for side, child, fam in (("this", a, fa), ("expression", b, fb)):
                    if fam == "bool":
                        n.set(side, _cast(child, "INTEGER"))
            return None
        if isinstance(n, (exp.Sum, exp.Avg)):
            if _family(n.this) == "bool":
                n.set("this", _cast(n.this, "INTEGER"))
            elif _family(n.this) == "str":
                n.set("this", self._as_num(n.this))
            if isinstance(n, exp.Avg):
                sc = _exact_scale(n.this)
                if sc is not None:
                    return exp.Round(this=n, decimals=exp.Literal.number(min(sc + 4, 30)))
            return None
        if isinstance(n, (exp.Cast, exp.TryCast)) and _family(n.this) == "str":
            to = n.to
            if to.is_type(*exp.DataType.INTEGER_TYPES):
                # CAST('3.5' AS SIGNED) = 3，CAST('abc' AS SIGNED) = 0
                src = _F("__my_int", n.this) if not _is_lit(n.this) else exp.Literal.number(int(_mysql_str_num(n.this.name)))
                return exp.Cast(this=src, to=to)
            if to.is_type(*exp.DataType.REAL_TYPES) or to.this == exp.DataType.Type.DECIMAL:
                return exp.Cast(this=self._as_num(n.this), to=to)
            if to.this in (exp.DataType.Type.BINARY, exp.DataType.Type.VARBINARY, exp.DataType.Type.BLOB):
                return _F("encode", n.this)
            return None

        # ---- 其它函数 ----
        if isinstance(n, exp.SubstringIndex):
            return _F("__my_substring_index", n.this, n.args.get("delimiter"), n.args.get("count"))
        if isinstance(n, exp.TimestampDiff):
            unit = (n.args.get("unit").name if n.args.get("unit") is not None else "SECOND").upper()
            a, b = n.expression, n.this   # TIMESTAMPDIFF(unit, a, b) = b - a
            if unit in _US:
                return _F("__my_tsdiff", a, b, exp.Literal.number(_US[unit]))
            months = _F("__my_month_diff", a, b)
            if unit == "MONTH":
                return months
            if unit in ("QUARTER", "YEAR"):
                div = 3 if unit == "QUARTER" else 12
                return exp.Cast(this=_F("trunc", exp.Div(this=months, expression=exp.Literal.number(div))), to=exp.DataType.build("BIGINT"))
            raise FederatedError(f"TIMESTAMPDIFF 不支持单位 {unit}")
        if isinstance(n, exp.TimeToStr):
            return self._date_format(n)
        if isinstance(n, (exp.StrToDate, exp.StrToTime)):
            n.set("safe", True)   # 解析不了时与 MySQL 一样返回 NULL，而不是报错
            return n
        if isinstance(n, exp.NumberToStr):
            # FORMAT(x, d)：先按 MySQL 规则四舍五入（DECIMAL 不经过二进制浮点）
            d = n.args.get("format")
            if isinstance(d, exp.Literal) and not d.is_string:
                n.set("this", exp.Round(this=n.this, decimals=d.copy()))
            return n
        if isinstance(n, exp.Ln):
            return exp.If(this=exp.LTE(this=n.this.copy(), expression=exp.Literal.number(0)), true=exp.Null(), false=n)
        if isinstance(n, exp.Log):
            x = n.expression if n.expression is not None else n.this
            return exp.If(this=exp.LTE(this=x.copy(), expression=exp.Literal.number(0)), true=exp.Null(), false=n)
        if isinstance(n, exp.Sqrt):
            return exp.If(this=exp.LT(this=n.this.copy(), expression=exp.Literal.number(0)), true=exp.Null(), false=n)
        if isinstance(n, exp.Ascii):
            return _F("__my_ascii", n.this)
        if isinstance(n, exp.Elt):
            case = exp.Case()
            for i, a in enumerate(n.expressions, 1):
                case = case.when(exp.EQ(this=n.this.copy(), expression=exp.Literal.number(i)), a)
            return case
        if isinstance(n, exp.Extract) and isinstance(n.this, exp.Var):
            unit = n.this.name.upper()
            d = _cast(n.expression, "TIMESTAMP")
            if unit == "YEAR_MONTH":
                return exp.Add(this=exp.Mul(this=_F("year", d), expression=exp.Literal.number(100)), expression=_F("month", d.copy()))
            if "_" in unit:
                raise FederatedError(f"跨数据源查询暂不支持 EXTRACT({unit} FROM ...)")
        return None

    def _as_num(self, node):
        """MySQL 把字符串当数字用：字面量直接换算，列 / 表达式用 __my_num。"""
        if isinstance(node, exp.Literal) and node.is_string:
            v = _mysql_str_num(node.name)
            return exp.Literal.number(v)
        return _F("__my_num", node)

    def _date_format(self, n: exp.TimeToStr):
        """DATE_FORMAT：%D %u %V %v %X 这几个 DuckDB strftime 没有或含义不同，单独计算后拼接。"""
        fmt = n.args.get("format")
        if not isinstance(fmt, exp.Literal):
            return None
        text = fmt.name
        if not re.search(r"%[DWVvX]", text):
            return None
        ts = n.this
        parts, buf = [], ""
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "%" and i + 1 < len(text):
                spec = text[i + 1]
                if spec in "DWVvX":
                    if buf:
                        parts.append(_F("strftime", ts.copy(), exp.Literal.string(buf)))
                        buf = ""
                    if spec == "D":
                        parts.append(_F("__my_day_suffix", ts.copy()))
                    elif spec == "W":       # MySQL %u：WEEK(d, 1)
                        parts.append(_F("lpad", _cast(_F("__my_week1", ts.copy()), "VARCHAR"), exp.Literal.number(2), exp.Literal.string("0")))
                    elif spec == "V":       # MySQL %V：WEEK(d, 2)
                        parts.append(_F("lpad", _cast(exp.Mod(this=_F("__my_yearweek0", ts.copy()), expression=exp.Literal.number(100)), "VARCHAR"),
                                        exp.Literal.number(2), exp.Literal.string("0")))
                    elif spec == "X":       # MySQL %X：%V 对应的年份
                        parts.append(_cast(exp.IntDiv(this=_F("__my_yearweek0", ts.copy()), expression=exp.Literal.number(100)), "VARCHAR"))
                    else:                   # MySQL %v：ISO 周
                        parts.append(_F("strftime", ts.copy(), exp.Literal.string("%V")))
                    i += 2
                    continue
                buf += text[i:i + 2]
                i += 2
                continue
            buf += ch
            i += 1
        if buf:
            parts.append(_F("strftime", ts.copy(), exp.Literal.string(buf)))
        out = parts[0]
        for p in parts[1:]:
            out = exp.DPipe(this=out, expression=p, safe=True)
        return out

    def _anonymous(self, n: exp.Anonymous):
        name = (n.name or "").upper()
        args = n.expressions
        lower = name.lower()
        if lower in _DUCK_ONLY_FUNCS or lower.startswith(("duckdb_", "pragma_", "read_")):
            raise FederatedError(f"不支持的函数：{name}()")
        if name in _UNSUPPORTED_FUNCS:
            raise FederatedError(f"跨数据源查询不支持 {name}()")
        if name in ("NOW", "SYSDATE", "CURRENT_TIMESTAMP", "LOCALTIME", "LOCALTIMESTAMP"):
            return self._ts(self.now)
        if name in ("CURDATE", "CURRENT_DATE"):
            return self._date(self.now)
        if name in ("CURTIME", "CURRENT_TIME"):
            return self._time(self.now)
        if name == "UNIX_TIMESTAMP":
            if not args:
                return _marker("t_epoch") if self.markers else exp.Literal.number(self.epoch)
            return _F("__my_unix_timestamp", args[0], exp.Literal.number(self.tz_offset))
        if name == "WEEKDAY":
            return _F("__my_weekday", *args)
        if name in ("ADDDATE", "SUBDATE") and len(args) == 2:
            delta = args[1]
            if not isinstance(delta, exp.Interval):
                delta = exp.Interval(this=delta, unit=exp.var("DAY"))
            node = (exp.Add if name == "ADDDATE" else exp.Sub)(this=args[0], expression=delta)
            unit = delta.args.get("unit")
            if unit is not None and unit.name.upper() in _DATE_UNITS and _dtype(args[0]) is not None \
                    and _dtype(args[0]).this == exp.DataType.Type.DATE:
                return _cast(node, "DATE")
            return node
        if name == "FIND_IN_SET":
            return _F("__my_find_in_set", *args)
        if name == "FIELD" and args:
            if len(args) == 1:
                return exp.Literal.number(0)
            case = exp.Case()
            for i, a in enumerate(args[1:], 1):
                case = case.when(exp.EQ(this=args[0].copy(), expression=a), exp.Literal.number(i))
            return case.else_(exp.Literal.number(0))
        if name == "SUBSTRING_INDEX":
            return _F("__my_substring_index", *args)
        if name == "MAKEDATE":
            return _F("__my_makedate", *args)
        if name == "TO_DAYS":
            return _F("__my_to_days", *args)
        if name == "FROM_DAYS":
            return _F("__my_from_days", *args)
        if name == "JSON_UNQUOTE":
            return _F("__my_json_unquote", *args)
        if name == "JSON_LENGTH":
            return _F("__my_json_length", *args)
        if name == "YEARWEEK":
            if len(args) > 1 and not (isinstance(args[1], exp.Literal) and args[1].name in ("0", "2")):
                raise FederatedError("跨数据源查询中 YEARWEEK() 暂只支持默认模式")
            return _F("__my_yearweek0", args[0])
        if name == "MID":
            return exp.Substring(this=args[0], start=args[1], length=args[2] if len(args) > 2 else None)
        if name == "OCTET_LENGTH":
            return _F("__my_length", *args)
        if name == "ELT" and args:
            case = exp.Case()
            for i, a in enumerate(args[1:], 1):
                case = case.when(exp.EQ(this=args[0].copy(), expression=exp.Literal.number(i)), a)
            return case
        if name == "STRCMP" and len(args) == 2:
            a, b = args
            return exp.Case().when(exp.Or(this=exp.Is(this=a.copy(), expression=exp.Null()), expression=exp.Is(this=b.copy(), expression=exp.Null())), exp.Null()) \
                .when(exp.EQ(this=a.copy(), expression=b.copy()), exp.Literal.number(0)) \
                .when(exp.LT(this=a.copy(), expression=b.copy()), exp.Literal.number(-1)).else_(exp.Literal.number(1))
        if name in ("TIMEDIFF",) and len(args) == 2:
            ta, tb = _dtype(args[0]), _dtype(args[1])
            if ta is not None and tb is not None and _family(args[0]) == _family(args[1]) == "time" and ta.this != tb.this:
                return exp.Null()   # MySQL：两个参数类型不同（如 DATETIME 与 DATE）时返回 NULL
            return exp.Sub(this=_cast(args[0], "TIMESTAMP"), expression=_cast(args[1], "TIMESTAMP"))
        if name in ("ADDTIME", "SUBTIME") and len(args) == 2:
            node = exp.Add if name == "ADDTIME" else exp.Sub
            return node(this=_cast(args[0], "TIMESTAMP"), expression=_cast(args[1], "INTERVAL"))
        return None


def _wrap_any_value(ast: exp.Expression) -> None:
    """MySQL（未开 ONLY_FULL_GROUP_BY）允许聚合查询里出现未分组的列，取任意一行的值；
    DuckDB 会报错，这里把这类列包成 ANY_VALUE(列)，结果与 MySQL 一致。"""
    def has_agg(e):
        for x in e.walk(prune=lambda y: isinstance(y, (exp.Select, exp.Subquery, exp.Window)) and y is not e):
            if isinstance(x, exp.AggFunc):
                return True
        return False

    for sel in list(ast.find_all(exp.Select)):
        group = sel.args.get("group")
        having = sel.args.get("having")
        order = sel.args.get("order")
        if group is None and not any(has_agg(p) for p in sel.expressions) and not (having is not None and has_agg(having)):
            continue
        keys = {g.sql() for g in (group.expressions if group is not None else [])}
        targets = list(sel.expressions) + ([having] if having is not None else []) + ([order] if order is not None else [])
        for tgt in targets:
            for col in list(tgt.find_all(exp.Column)):
                if not col.table or col.sql() in keys:
                    continue
                skip, p = False, col.parent
                while p is not None and p is not tgt.parent:
                    if isinstance(p, (exp.AggFunc, exp.Window, exp.Select, exp.Subquery)) or p.sql() in keys:
                        skip = True
                        break
                    if p is tgt:
                        break
                    p = p.parent
                if not skip:
                    col.replace(exp.AnyValue(this=col.copy()))


# ======================================================================
# 取数与计算
# ======================================================================

def _arrow_family(t) -> str:
    if pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "num"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "str"
    if pa.types.is_date(t) or pa.types.is_timestamp(t):
        return "time"
    return str(t)


def _to_arrow(values, col: _Col):
    """按列类型转成 Arrow 数组；值与类型不符（如 0000-00-00 这类非法日期）时整列按字符串处理。"""
    try:
        return pa.array(values, type=col.arrow), col
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, OverflowError, TypeError, ValueError):
        def s(v):
            if v is None:
                return None
            if isinstance(v, (bytes, bytearray)):
                return v.decode("utf-8", errors="replace")
            return str(v)
        return pa.array([s(v) for v in values], type=pa.string()), _Col(col.name, "VARCHAR", pa.string())


def _quote(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _source_parts(src: _Source) -> Tuple[str, str]:
    """生成下推 SQL 的两部分（准备阶段做一次，执行时只拼接字符串）。"""
    if src.kind == "query":
        node = src.node.copy()
        for t in node.find_all(exp.Table):
            if t.args.get("catalog"):
                t.set("catalog", None)
        return node.sql(dialect=_MySQL, identify=True), ""
    cols = [exp.column(c, table=src.alias, quoted=True) for c in src.columns]
    if not cols:
        cols = [exp.alias_(exp.Literal.number(1), "__one", quoted=True)]
    head = exp.select(*cols).from_(exp.Table(
        this=exp.to_identifier(src.ref.table, quoted=True),
        db=exp.to_identifier(src.ref.db, quoted=True),
        alias=exp.TableAlias(this=exp.to_identifier(src.alias, quoted=True)),
    )).sql(dialect=_MySQL, identify=True)
    preds = " AND ".join("(" + p.sql(dialect=_MySQL, identify=True) + ")" for p in src.preds)
    return head, preds


def _assemble(src: _Source, extra: List[str]) -> str:
    if src.kind == "query":
        return src.base_sql
    conds = ([src.preds_sql] if src.preds_sql else []) + list(extra)
    return src.base_sql + (" WHERE " + " AND ".join(conds) if conds else "")


_est_cache: Dict[tuple, Tuple[float, Optional[float]]] = {}
_CARDINALITY_RE = re.compile(r"cardinality\s*[=:]\s*(\d+)", re.IGNORECASE)


async def _estimate_rows(ds, sql: str, bound: dict) -> Optional[float]:
    from app.core.security import decrypt_value
    from app.services.engine import _query_mysql_raw

    key = (ds.id, sql)
    hit = _est_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    n = None
    try:
        password = decrypt_value(ds.password_encrypted) if ds.password_encrypted else ""
        fields, rows, _ = await _query_mysql_raw(ds, password, sql, bound, timeout=5, max_rows=50)
        names = [f.name.lower() for f in fields]
        if rows and "rows" in names:          # MySQL / MariaDB
            r = rows[0]
            n = float(r[names.index("rows")] or 0)
            if "filtered" in names and r[names.index("filtered")] is not None:
                n = n * float(r[names.index("filtered")]) / 100
        elif rows:                            # SelectDB / Doris / StarRocks：执行计划文本里的 cardinality
            found = [int(m.group(1)) for r in rows for cell in r if isinstance(cell, str)
                     for m in _CARDINALITY_RE.finditer(cell)]
            if found:
                n = float(max(found))
    except Exception as e:
        log.debug(f"多源 SQL 行数估算失败 | ds={ds.name} | {e}")
    if len(_est_cache) > 2000:
        _est_cache.clear()
    _est_cache[key] = (time.monotonic() + 60, n)
    return n


# ======================================================================
# 准备好的执行计划（按「项目 + 渲染后的 SQL」缓存；参数值单独绑定，同一 API 的 SQL 文本通常不变）
# ======================================================================

@dataclass
class _Prepared:
    mode: str                                   # single / federated
    expires: float = 0.0
    ds: Any = None                              # single：数据源
    native: str = ""                            # single：交给数据源执行的 SQL
    sources: List[_Source] = field(default_factory=list)
    equi: List[tuple] = field(default_factory=list)   # (目标 idx, 目标列, 提供键的 idx, 键列)
    schema_map: Dict[tuple, List[_Col]] = field(default_factory=dict)
    ast: Optional[exp.Expression] = None        # 计划后的语法树（只读；生成 DuckDB SQL 时复制）
    names: List[str] = field(default_factory=list)
    duck_sqls: Dict[tuple, str] = field(default_factory=dict)   # 源数据类型签名 -> DuckDB SQL 模板


_plan_cache: Dict[tuple, _Prepared] = {}
_plan_loading: Dict[tuple, "asyncio.Future"] = {}


def clear_plan_cache() -> None:
    _plan_cache.clear()


def _plan_ttl() -> float:
    # 与网关接口配置缓存一致：本进程内改数据源 / 项目配置后立即清空（gateway_cache.invalidate），
    # 多 worker 时其它进程最多延迟这么多秒；0 = 不缓存
    return float(getattr(settings.gateway, "config_cache_ttl", 0) or 0)


async def _build_prepared(sql: str, db, project_id: int, timeout: int) -> _Prepared:
    ast, refs = await _prepare(sql, db, project_id, timeout)
    if len({r.ds.id for r in refs}) == 1:
        return _Prepared(mode="single", ds=refs[0].ds, native=_single_source_sql(ast, refs, sql))
    ast, planner, schema_map = await _plan_cross(ast, refs, sql, timeout)
    for src in planner.sources:
        src.base_sql, src.preds_sql = _source_parts(src)
        src.node.meta["fed_src"] = src.idx
        src.scope = None   # 不再需要，释放分析结构
    return _Prepared(
        mode="federated", sources=planner.sources, schema_map=schema_map, ast=ast, names=_output_names(ast),
        equi=[(t.idx, tc, p.idx, pc) for t, tc, p, pc in planner.equi],
    )


async def _get_prepared(sql: str, db, project_id: int, timeout: int) -> _Prepared:
    ttl = _plan_ttl()
    key = (project_id, sql)
    if ttl > 0:
        hit = _plan_cache.get(key)
        if hit is not None and hit.expires > time.monotonic():
            return hit
        waiting = _plan_loading.get(key)
        if waiting is not None:
            return await asyncio.shield(waiting)
    fut = asyncio.get_running_loop().create_future() if ttl > 0 else None
    if fut is not None:
        _plan_loading[key] = fut
    try:
        prep = await _build_prepared(sql, db, project_id, timeout)
        if ttl > 0:
            prep.expires = time.monotonic() + ttl
            if len(_plan_cache) >= 2000:
                _plan_cache.clear()
            _plan_cache[key] = prep
            fut.set_result(prep)
        return prep
    except BaseException as e:
        if fut is not None:
            fut.set_exception(e if isinstance(e, Exception) else FederatedError("生成执行计划被中断，请重试"))
            fut.exception()
        raise
    finally:
        if fut is not None:
            _plan_loading.pop(key, None)


# ======================================================================
# 一次执行：分轮从各数据源取数（动态过滤）
# ======================================================================

@dataclass
class _State:
    src: _Source
    sql: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    dyn_filters: List[str] = field(default_factory=list)
    fields: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    ms: float = 0.0
    round: int = 0


class _Run:
    def __init__(self, prep: _Prepared, bound: dict, deadline: float):
        self.prep = prep
        self.bound = bound
        self.deadline = deadline
        cfg = settings.catalog
        self.cap = max(1, cfg.max_rows_per_source)
        self.max_keys = max(0, cfg.dynamic_filter_max_keys)
        self.st: Dict[int, _State] = {s.idx: _State(src=s) for s in prep.sources}
        self.partners: Dict[int, list] = {}
        for t, tcol, p, pcol in prep.equi:
            if self.st[t].src.kind == "table":
                self.partners.setdefault(t, []).append((tcol, p, pcol))
        self.est: Dict[int, float] = {}

    def _target_col(self, src: _Source, name: str) -> Optional[_Col]:
        for c in self.prep.schema_map.get((src.ds.id, src.ref.db, src.ref.table), []):
            if c.name == name:
                return c
        return None

    def _dyn_preds(self, st: _State, fetched: set) -> List[str]:
        """动态过滤：用已取回的另一侧数据的关联键，生成「关联列 IN (...)」条件下推（键值以绑定参数传入）。"""
        preds = []
        src = st.src
        for tcol, pidx, pcol in self.partners.get(src.idx, []):
            if pidx not in fetched or self.max_keys <= 0:
                continue
            prov = self.st[pidx]
            names = [f.name for f in prov.fields]
            if pcol not in names:
                continue
            i = names.index(pcol)
            target = self._target_col(src, tcol)
            if target is None or _arrow_family(target.arrow) != _arrow_family(_field_col(prov.fields[i]).arrow):
                continue   # 两侧类型不同（如数字关联字符串）时按 MySQL 规则比较，下推 IN 可能漏行，不做
            keys = {r[i] for r in prov.rows if r[i] is not None}
            if len(keys) > self.max_keys:
                continue
            est = self.est.get(src.idx)
            if est is not None and len(keys) * 10 > est and est <= self.cap:
                continue   # 关联键太多、过滤效果有限：大 IN 列表在源库的解析和查找反而比直接取更慢
            try:
                keys = sorted(keys)
            except TypeError:
                keys = list(keys)
            holders = []
            for v in keys:
                k = f"__df{src.idx}_{len(st.params)}"
                st.params[k] = v
                holders.append(":" + k)
            preds.append(f"{_quote(src.alias)}.{_quote(tcol)} IN ({', '.join(holders) or 'NULL'})")
            st.dyn_filters.append(f"{tcol} IN（{len(keys)} 个关联键，来自 {prov.src.label()}）")
        return preds

    async def _fetch(self, st: _State):
        import math
        from app.core.security import decrypt_value
        from app.services.engine import _query_mysql_raw

        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise FederatedError("查询超时")
        src = st.src
        password = decrypt_value(src.ds.password_encrypted) if src.ds.password_encrypted else ""
        t0 = time.perf_counter()
        fields, rows, truncated = await _query_mysql_raw(
            src.ds, password, st.sql, {**self.bound, **st.params} if st.params else self.bound,
            timeout=max(1, math.ceil(remaining)), max_rows=self.cap,
        )
        st.ms = (time.perf_counter() - t0) * 1000
        if truncated:
            raise FederatedError(
                f"{src.label()} 符合条件的数据超过 {self.cap} 行，跨数据源关联需要把这部分数据取回计算。"
                "请增加过滤条件，或请管理员调大 catalog.max_rows_per_source"
            )
        st.fields, st.rows = fields, rows

    async def estimate(self):
        """用 EXPLAIN 估算各表要取的行数（结果缓存 60 秒），用于决定取数顺序和是否下推关联键。"""
        if not self.partners:
            return
        todo = [s for s in self.prep.sources if s.kind == "table"]
        if not todo:
            return
        rows = await asyncio.gather(*[_estimate_rows(s.ds, "EXPLAIN " + _assemble(s, []), self.bound) for s in todo])
        for s, n in zip(todo, rows):
            if n is not None:
                self.est[s.idx] = n

    def first_round(self) -> List[int]:
        srcs = self.prep.sources
        # 第一轮只取「驱动表」：不会被别的表过滤的表；都会被过滤时取估算行数最少的一张。
        # 其余的表等驱动表的关联键作为 IN 条件下推后再取，取回的行数最少
        first = [s for s in srcs if s.idx not in self.partners]
        if not first:
            known = [s for s in srcs if s.idx in self.est]
            if known:
                first = [min(known, key=lambda s: self.est[s.idx])]
            else:
                # 没有估算：先取带过滤条件的表；都没有则取 FROM 的第一张
                first = [s for s in srcs if s.preds] or [min(srcs, key=lambda s: (s.order, s.idx))]
        # 关联方的数据量不比自己少一个数量级时，按关联键过滤省不了多少，不如和驱动表并行取
        chosen = {s.idx for s in first}
        for s in srcs:
            if s.idx in chosen or s.idx not in self.est:
                continue
            p_est = [self.est.get(p) for _, p, _ in self.partners.get(s.idx, [])]
            if p_est and all(e is not None and e * 10 >= self.est[s.idx] for e in p_est):
                first.append(s)
                chosen.add(s.idx)
        return [s.idx for s in first]

    async def fetch_all(self):
        await self.estimate()
        fetched: set = set()
        pending = [s.idx for s in self.prep.sources]
        batch = self.first_round()
        rnd = 0
        while batch:
            rnd += 1
            for i in batch:
                st = self.st[i]
                st.round = rnd
                st.sql = _assemble(st.src, self._dyn_preds(st, fetched))
            await asyncio.gather(*[self._fetch(self.st[i]) for i in batch])
            fetched.update(batch)
            pending = [i for i in pending if i not in fetched]
            if not pending:
                break
            batch = [i for i in pending if any(p in fetched for _, p, _ in self.partners.get(i, []))] or pending


def _output_names(ast: exp.Expression) -> List[str]:
    """结果字段名：同名列从第二个起用「表别名.列名」（与 MySQL 字典游标一致）。"""
    sel = _leftmost_select(ast)
    names, seen = [], set()
    for p in sel.expressions if sel is not None else []:
        name = p.alias_or_name
        if name in seen:
            inner = p.this if isinstance(p, exp.Alias) else p
            if isinstance(inner, exp.Column) and inner.table:
                name = f"{inner.table}.{name}"
        seen.add(p.alias_or_name)
        names.append(name)
    return names


# DuckDB SQL 模板中的占位标记：请求参数（p_名称）和当前时间（t_种类），每次执行时代入
_MARK = "__PH__"
_MARK_RE = re.compile(r'"__PH__(p|t)_(\w+)"')


def _marker(name: str) -> exp.Column:
    return exp.column(exp.to_identifier(_MARK + name, quoted=True))


def _build_duck_template(prep: _Prepared, local_schema: dict) -> str:
    """生成 DuckDB SQL 模板：源表换成本地表、按类型做 MySQL 语义改写；参数和当前时间留占位标记。"""
    ast = prep.ast.copy()
    tagged = [n for n in ast.walk() if n.meta_get("fed_src") is not None]
    by_idx = {s.idx: s for s in prep.sources}
    for node in tagged:
        s = by_idx[node.meta["fed_src"]]
        if s.kind == "table":
            node.replace(exp.Table(this=exp.to_identifier(s.local),
                                   alias=exp.TableAlias(this=exp.to_identifier(s.alias, quoted=True))))
        else:
            node.replace(exp.select("*").from_(exp.to_identifier(s.local)))
    try:
        annotate_types(ast, schema=local_schema, dialect=_MySQL)
    except Exception as e:   # 类型推导失败不影响执行，只是少做依赖类型的兼容改写
        log.debug(f"多源 SQL 类型推导失败 | {e}")
    _wrap_any_value(ast)
    _MySQLCompat(_COLLATIONS.get(settings.catalog.string_compare, "nocase") != "", markers=True).apply(ast)
    for ph in list(ast.find_all(exp.Placeholder)):
        ph.replace(_marker("p_" + ph.name))
    return ast.sql(dialect="duckdb", identify=True)


def _fill_template(tpl: str, bound: dict, display: bool = False) -> str:
    """代入请求参数（DuckDB 端以常量写入，由语法树生成器转义；源库端始终用绑定参数）和当前时间。"""
    now = datetime.datetime.now(CST).replace(microsecond=0)
    local = now.replace(tzinfo=None)
    utc = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    times = {
        "now_ts": f"CAST('{local:%Y-%m-%d %H:%M:%S}' AS TIMESTAMP)", "now_date": f"CAST('{local:%Y-%m-%d}' AS DATE)",
        "now_time": f"CAST('{local:%H:%M:%S}' AS TIME)", "utc_ts": f"CAST('{utc:%Y-%m-%d %H:%M:%S}' AS TIMESTAMP)",
        "utc_date": f"CAST('{utc:%Y-%m-%d}' AS DATE)", "utc_time": f"CAST('{utc:%H:%M:%S}' AS TIME)",
        "epoch": str(int(now.timestamp())),
    }
    lits: Dict[str, str] = {}

    def repl(m):
        kind, name = m.group(1), m.group(2)
        if kind == "t":
            return times[name]
        if display:
            return ":" + name
        if name not in lits:
            lits[name] = exp.convert(bound.get(name)).sql(dialect="duckdb")
        return lits[name]
    return _MARK_RE.sub(repl, tpl)


_DUCK_ERR_PATTERNS = [
    (re.compile(r"No function matches the given name and argument types '(\w+)\("), "跨数据源计算中函数 {0}() 的参数类型不支持"),
    (re.compile(r"Scalar Function with name (\w+) does not exist"), "跨数据源查询暂不支持函数 {0}()"),
    (re.compile(r"Table Function with name (\w+) does not exist"), "不支持表函数 {0}()"),
    (re.compile(r"Aggregate Function with name (\w+) does not exist"), "跨数据源查询暂不支持聚合函数 {0}()"),
]


def _duck_error(e: Exception) -> FederatedError:
    msg = _first_line(e)
    for pat, tpl in _DUCK_ERR_PATTERNS:
        m = pat.search(msg)
        if m:
            return FederatedError(tpl.format(m.group(1)))
    if "Permission Error" in msg:
        return FederatedError("跨数据源计算不允许访问文件或扩展")
    if "Out of Memory" in msg or "could not allocate" in msg.lower():
        return FederatedError("跨数据源计算内存不足：请增加过滤条件，或请管理员调大 catalog.memory_limit")
    return FederatedError(f"跨数据源计算失败：{msg}")


# 行数很少的源直接以 VALUES 常量写进 DuckDB SQL：DuckDB 每扫描一张 Arrow 表约有 1ms 多的固定开销，
# 而常量每行约 0.025ms，几十行以内更快（跨源点查这类高频请求每个源通常只有几行）
_INLINE_ROWS = 32
_DUCK_TYPES = {"DATETIME": "TIMESTAMP"}


def _duck_lit(v) -> str:
    if v is None:
        return "NULL"
    t = type(v)
    if t is int:
        return str(v)
    if t is str:
        return "'" + v.replace("'", "''") + "'"
    if t is float:
        return repr(v) if v == v and v not in (float("inf"), float("-inf")) else f"'{v}'"
    if t is bool:
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime.datetime):
        return "'" + v.isoformat(sep=" ") + "'"
    if isinstance(v, datetime.date):
        return "'" + v.isoformat() + "'"
    if isinstance(v, datetime.timedelta):
        return f"'{(v.days * 86400 + v.seconds) * 1_000_000 + v.microseconds} microseconds'"
    if isinstance(v, (bytes, bytearray)):
        return "'" + "".join(f"\\x{b:02X}" for b in v) + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _inline_cte(local: str, cols: List[_Col], rows) -> str:
    def q(n):
        return '"' + n.replace('"', '""') + '"'
    if not cols:
        return f'{q(local)} AS (SELECT 1 AS "__one" WHERE FALSE)'
    sel = ", ".join(f"CAST(c{i} AS {_DUCK_TYPES.get(c.sqltype, c.sqltype)}) AS {q(c.name)}" for i, c in enumerate(cols))
    if not rows:
        nulls = ", ".join(f"NULL AS c{i}" for i in range(len(cols)))
        return f"{q(local)} AS (SELECT {sel} FROM (SELECT {nulls}) AS v WHERE FALSE)"
    def as_text(x):   # 字符串列（含类型不符、整列按字符串处理的列）统一写成字符串常量
        if x is None or type(x) is str:
            return x
        return x.decode("utf-8", errors="replace") if isinstance(x, (bytes, bytearray)) else str(x)
    text_cols = [i for i, c in enumerate(cols) if c.sqltype == "VARCHAR"]
    if text_cols:
        rows = [tuple(as_text(x) if i in text_cols else x for i, x in enumerate(r)) for r in rows]
    values = ", ".join("(" + ", ".join(_duck_lit(x) for x in r) + ")" for r in rows)
    names = ", ".join(f"c{i}" for i in range(len(cols)))
    return f"{q(local)} AS (SELECT {sel} FROM (VALUES {values}) AS v({names}))"


def _convert_sources(states: List[_State], tables: dict, local_schema: dict, inline: List[str]) -> None:
    for st in states:
        cols = [_field_col(f) for f in st.fields]
        names = [c.name for c in cols]
        if len(set(names)) != len(names):
            raise FederatedError(f"{st.src.label()} 的结果中有重名列，请给列起不同的别名")
        rows = st.rows
        arrays, types, final = [], {}, []
        for i, c in enumerate(cols):
            arr, c2 = _to_arrow([r[i] for r in rows], c)
            arrays.append(arr)
            types[c.name] = c2.sqltype
            final.append(c2)
        if len(rows) <= _INLINE_ROWS:
            inline.append(_inline_cte(st.src.local, final, rows))
        else:
            tables[st.src.local] = pa.Table.from_arrays(arrays, names=names)
        local_schema[st.src.local] = types


def _with_ctes(sql: str, ctes: List[str]) -> str:
    """把内联数据的 CTE 加到 DuckDB SQL 前面（SQL 本身有 WITH 时合并进去）。"""
    if not ctes:
        return sql
    defs = ", ".join(ctes)
    for head in ("WITH RECURSIVE ", "WITH "):
        if sql.startswith(head):
            return head + defs + ", " + sql[len(head):]
    return "WITH " + defs + " " + sql


def _compute(prep: _Prepared, states: List[_State], bound: dict, max_rows: int, holder: dict):
    """在线程池里执行：数据转 Arrow → 取（或生成）DuckDB SQL → 计算。返回 (列名, 行, DuckDB SQL, 各阶段耗时)。"""
    t0 = time.perf_counter()
    tables, local_schema, inline = {}, {}, []
    # 转换期间暂停分代垃圾回收：几万行数据会产生大量小对象，频繁触发的回收扫描
    # 比转换本身还慢（实测 5 万行从约 70ms 降到约 10ms）；这些对象都没有循环引用，转换完即释放
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        _convert_sources(states, tables, local_schema, inline)
    finally:
        if gc_was_enabled:
            gc.enable()
    sig = tuple(sorted((k, tuple(v.items())) for k, v in local_schema.items()))
    tpl = prep.duck_sqls.get(sig)
    if tpl is None:
        tpl = _build_duck_template(prep, local_schema)
        if len(prep.duck_sqls) < 16:
            prep.duck_sqls[sig] = tpl
    duck_sql = _with_ctes(_fill_template(tpl, bound), inline)
    t1 = time.perf_counter()

    cur = _duck_db().cursor()
    holder["cur"] = cur
    try:
        if holder.get("cancelled"):
            raise FederatedError("查询超时")
        for name, tbl in tables.items():
            cur.register(name, tbl)
        try:
            cur.execute(duck_sql)
            rows = cur.fetchmany(max_rows)
        except FederatedError:
            raise
        except Exception as e:
            if holder.get("cancelled"):
                raise FederatedError("查询超时")
            raise _duck_error(e)
        width = len(cur.description or [])
    finally:
        holder["cur"] = None
        cur.close()
    names = prep.names
    if len(names) != width:
        names = list(names[:width]) + [f"col_{i}" for i in range(len(names), width)]
    return names, rows, duck_sql, {"转换": (t1 - t0) * 1000, "计算": (time.perf_counter() - t1) * 1000}


# ======================================================================
# 对外接口
# ======================================================================

async def _prepare(sql: str, db, project_id: int, timeout: int):
    from app.services import ds_scope
    project_code = await ds_scope.project_code(db, project_id)
    ast = _parse(sql)
    refs = await _resolve_tables(ast, sql, db, project_code)
    return ast, refs


async def _plan_cross(ast, refs, sql: str, timeout: int):
    """跨数据源：取表结构、限定列、生成执行计划。"""
    uniq = {}
    for r in refs:
        uniq.setdefault((r.ds.id, r.db, r.table), r)
    cols_list = await asyncio.gather(*[_table_schema(r, timeout) for r in uniq.values()])
    schema_map = dict(zip(uniq.keys(), cols_list))
    sg_schema: dict = {}
    for r in refs:
        cols = schema_map[(r.ds.id, r.db, r.table)]
        sg_schema.setdefault(r.node.catalog, {}).setdefault(r.db, {})[r.table] = {c.name: c.sqltype for c in cols}
    ds_by_catalog = {r.node.catalog: r.ds for r in refs}

    _name_projections(ast, sql)
    _normalize_column_case(ast, schema_map)
    ast = _qualify(ast, sg_schema)
    # 限定后重新对应表节点
    new_refs = []
    for t in ast.find_all(exp.Table):
        if t.args.get("catalog") and t.catalog in ds_by_catalog:
            new_refs.append(_TableRef(node=t, ds=ds_by_catalog[t.catalog], db=t.db, table=t.name, strip_span=None))
    planner = _Planner(ast, new_refs).build()
    return ast, planner, schema_map


def _single_source_sql(ast, refs, sql: str) -> str:
    native = _strip_catalogs(sql, refs)
    if native is None:
        for r in refs:
            r.node.set("catalog", None)
        native = ast.sql(dialect=_MySQL)
    return native


async def execute(sql: str, bound: dict, *, db, project_id: int, timeout: int, max_rows: int):
    """执行多源 SQL（sql 为模板渲染后的 SQL，参数为 :name 占位符；bound 为绑定参数）。
    返回 (行列表, 执行计划)。"""
    from app.core.security import decrypt_value
    from app.services.engine import _clean_value, _execute_mysql

    t0 = time.perf_counter()
    deadline = time.monotonic() + max(1, timeout)
    prep = await _get_prepared(sql, db, project_id, timeout)

    if prep.mode == "single":
        ds = prep.ds
        password = decrypt_value(ds.password_encrypted) if ds.password_encrypted else ""
        data = await _execute_mysql(ds, password, prep.native, bound, timeout=timeout, max_rows=max_rows)
        return data, {"mode": "single", "datasource": ds.name, "sql": prep.native,
                      "ms": round((time.perf_counter() - t0) * 1000, 1)}

    t1 = time.perf_counter()
    run = _Run(prep, bound, deadline)
    await run.fetch_all()
    t2 = time.perf_counter()

    _duck_db()
    holder: dict = {}
    states = [run.st[s.idx] for s in prep.sources]
    fut = asyncio.get_running_loop().run_in_executor(_executor, _compute, prep, states, bound, max_rows, holder)
    try:
        names, rows, duck_sql, ms = await asyncio.wait_for(asyncio.shield(fut), max(0.1, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        holder["cancelled"] = True
        cur = holder.get("cur")
        if cur is not None:
            try:
                cur.interrupt()
            except Exception:
                pass
        fut.cancel()
        raise FederatedError(f"查询超时（超过 {timeout}s）")

    data = [{k: _clean_value(int(v) if type(v) is bool else v) for k, v in zip(names, r)} for r in rows]
    plan = {
        "mode": "federated",
        "sources": [{
            "datasource": st.src.ds.name, "object": st.src.label(), "sql": st.sql, "rows": len(st.rows),
            "ms": round(st.ms, 1), "round": st.round, "dynamic_filters": st.dyn_filters,
        } for st in states],
        "duckdb_sql": duck_sql,
        "ms": {"计划": round((t1 - t0) * 1000, 1), "源库取数": round((t2 - t1) * 1000, 1),
               **{k: round(v, 1) for k, v in ms.items()}, "合计": round((time.perf_counter() - t0) * 1000, 1)},
    }
    return data, plan


async def explain(sql: str, *, db, project_id: int, timeout: int = 30) -> dict:
    """执行计划（不取数据）：各数据源要执行的 SQL、关联计算的 SQL。"""
    prep = await _build_prepared(sql, db, project_id, timeout)
    if prep.mode == "single":
        return {"mode": "single", "datasource": prep.ds.name, "sql": prep.native}
    run = _Run(prep, {}, time.monotonic() + timeout)
    first = set(run.first_round())
    local_schema = {}
    for s in prep.sources:
        if s.kind == "table":
            cols = {c.name: c.sqltype for c in prep.schema_map[(s.ds.id, s.ref.db, s.ref.table)]}
            local_schema[s.local] = {c: cols.get(c, "UNKNOWN") for c in s.columns} or {"__one": "BIGINT"}
        else:
            local_schema[s.local] = {n: "UNKNOWN" for n in s.node.named_selects}
    sources = [{
        "datasource": s.ds.name, "object": s.label(), "sql": _assemble(s, []),
        "round": 1 if s.idx in first else 2,
        "dynamic_filters": [f"{t} IN（运行时取 {run.st[p].src.label()} 的 {pc}）" for t, p, pc in run.partners.get(s.idx, [])]
                           if s.idx not in first else [],
    } for s in prep.sources]
    duck_sql = _fill_template(_build_duck_template(prep, local_schema), {}, display=True)
    return {"mode": "federated", "sources": sources, "duckdb_sql": duck_sql}


def plan_text(plan: dict) -> str:
    """执行计划转成便于阅读的文本（写入调用日志的「执行 SQL」）。"""
    if plan.get("mode") == "single":
        return f"-- 单数据源，整句交给「{plan.get('datasource')}」执行\n{plan.get('sql', '')}"
    lines = ["-- 多源 SQL：各数据源取数（第 N 轮）"]
    for s in plan.get("sources", []):
        rows = f"，{s['rows']} 行，{s['ms']}ms" if "rows" in s else ""
        lines.append(f"-- [{s['datasource']}] 第 {s.get('round', 1)} 轮{rows}")
        for f in s.get("dynamic_filters") or []:
            lines.append(f"--   动态过滤：{f}")
        lines.append(s.get("sql", "") + ";")
    lines.append("-- 关联计算（DuckDB）")
    lines.append(plan.get("duckdb_sql", ""))
    if plan.get("ms"):
        lines.append("-- 耗时(ms)：" + "，".join(f"{k} {v}" for k, v in plan["ms"].items()))
    return "\n".join(lines)


_CATALOG_REF_RE = re.compile(
    r"(?<![\w`.])(`[^`]+`|[A-Za-z_一-鿿][\w一-鿿]*)\s*\.\s*(`[^`]+`|\w+)\s*\.\s*(`[^`]+`|\w+)")


def referenced_catalogs(sql_template: str) -> List[str]:
    """SQL 模板里「数据源名.库名.表名」写法引用到的数据源名（不渲染模板，用于发布前检查）。"""
    from app.services.ds_scope import _STR_OR_COMMENT_RE
    code = _STR_OR_COMMENT_RE.sub(" ", sql_template or "")   # 去掉字符串和注释，保留反引号标识符
    names = []
    for m in _CATALOG_REF_RE.finditer(code):
        name = m.group(1).strip("`")
        if name not in names:
            names.append(name)
    return names


def warmup() -> None:
    """预热（在后台线程执行）：加载 DuckDB / Arrow / sqlglot 方言，初始化计算库，
    避免第一个多源请求多花几百毫秒。"""
    t0 = time.perf_counter()
    con = _duck_db()
    sample = {
        "i": [1, None], "s": ["a", None], "d": [Decimal("1.50"), None], "f": [1.5, None],
        "dt": [datetime.date(2026, 1, 1), None], "ts": [datetime.datetime(2026, 1, 1, 8), None],
        "t": [datetime.timedelta(hours=1), None], "b": [b"x", None],
    }
    types = {"i": "BIGINT", "s": "VARCHAR", "d": "DECIMAL(10, 2)", "f": "DOUBLE", "dt": "DATE", "ts": "DATETIME", "t": "INTERVAL", "b": "BLOB"}
    arrows = {"i": pa.int64(), "s": pa.string(), "d": pa.decimal128(10, 2), "f": pa.float64(), "dt": pa.date32(),
              "ts": pa.timestamp("us"), "t": pa.duration("us"), "b": pa.binary()}
    tbl = pa.Table.from_arrays([_to_arrow(v, _Col(k, types[k], arrows[k]))[0] for k, v in sample.items()], names=list(sample))
    ast = qualify(_parse("SELECT a.s, DATE_FORMAT(a.ts, '%Y-%m') AS m, SUM(a.d) / COUNT(*) AS x, MAX(b.dt) AS y "
                         "FROM __w AS a JOIN __w AS b ON b.i = a.i WHERE a.s LIKE 'a%' GROUP BY a.s"),
                  schema={"__w": types}, dialect=_MySQL, quote_identifiers=False)
    annotate_types(ast, schema={"__w": types}, dialect=_MySQL)
    _wrap_any_value(ast)
    _MySQLCompat(True).apply(ast)
    cur = con.cursor()
    try:
        cur.register("__w", tbl)
        cur.execute(ast.sql(dialect="duckdb", identify=True)).fetchall()
    finally:
        cur.close()
    log.info(f"多源 SQL 计算引擎已预热 | {(time.perf_counter() - t0) * 1000:.0f}ms")
