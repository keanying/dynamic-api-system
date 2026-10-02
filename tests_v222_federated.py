# -*- coding: utf-8 -*-
"""v2.22 多源 SQL 回归测试（不依赖数据库，源表数据在测试里构造）：python tests_v222_federated.py"""
import asyncio
import datetime
import sys
from decimal import Decimal
from types import SimpleNamespace as N

sys.path.insert(0, ".")

from sqlglot import exp

from app.services import federated as F

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def raises(fn, exc=Exception, contains=""):
    try:
        fn()
    except exc as e:
        return contains in str(e)
    return False


# ---------- 离线规划：用给定的表结构代替从数据源读取 ----------
_TYPE_CODES = {"BIGINT": (8, 0, 20, 63), "VARCHAR": (253, 0, 255, 45), "DATE": (10, 0, 10, 63),
               "DATETIME": (12, 0, 19, 63), "DOUBLE": (5, 31, 22, 63)}


def field(name, sqltype):
    if sqltype.startswith("DECIMAL"):
        p, s = [int(x) for x in sqltype[sqltype.index("(") + 1:-1].split(",")]
        return N(name=name, type_code=246, flags=0, scale=s, length=p + (1 if s else 0) + 1, charsetnr=63)
    code, scale, length, cs = _TYPE_CODES[sqltype]
    return N(name=name, type_code=code, flags=0, scale=scale, length=length, charsetnr=cs)


DS = {name: N(id=i, name=name, type=t, database_name=db, project_scope="", password_encrypted="", updated_at="")
      for i, (name, t, db) in enumerate([("shop", "mysql", "biz"), ("crm", "selectdb", "crm"), ("mall", "mysql", "mall")], 1)}


def plan(sql, tables):
    """tables: {(数据源, 库, 表): [(列, 类型), ...]} → (_Prepared, 规划器)"""
    ast = F._parse(sql)
    refs = []
    for t in ast.find_all(exp.Table):
        if t.args.get("catalog"):
            refs.append(F._TableRef(node=t, ds=DS[t.catalog], db=t.db, table=t.name, strip_span=None))
    schema_map = {(DS[c].id, d, t): [F._field_col(field(n, ty)) for n, ty in cols] for (c, d, t), cols in tables.items()}
    sg = {}
    for (c, d, t), cols in tables.items():
        sg.setdefault(c, {}).setdefault(d, {})[t] = dict(cols)
    F._name_projections(ast, sql)
    F._normalize_column_case(ast, schema_map)
    ast = F._qualify(ast, sg)
    new_refs = [F._TableRef(node=t, ds=DS[t.catalog], db=t.db, table=t.name, strip_span=None)
                for t in ast.find_all(exp.Table) if t.catalog in DS]
    planner = F._Planner(ast, new_refs).build()
    for s in planner.sources:
        s.base_sql, s.preds_sql = F._source_parts(s)
        s.node.meta["fed_src"] = s.idx
    prep = F._Prepared(mode="federated", sources=planner.sources, schema_map=schema_map, ast=ast,
                       names=F._output_names(ast), equi=[(t.idx, tc, p.idx, pc) for t, tc, p, pc in planner.equi])
    return prep, planner


def run(sql, tables, data, bound=None):
    """data: {表别名: [行元组...]}（按该表下推 SQL 的列顺序给出整表数据，测试不带 WHERE）→ 结果 [dict]"""
    prep, _ = plan(sql, tables)
    states = []
    for s in prep.sources:
        cols = dict(tables[(s.ds.name, s.ref.db, s.ref.table)])
        st = F._State(src=s, fields=[field(c, cols[c]) for c in s.columns])
        full = data[s.alias]
        names = [c for c, _ in tables[(s.ds.name, s.ref.db, s.ref.table)]]
        st.rows = [tuple(r[names.index(c)] for c in s.columns) for r in full]
        states.append(st)
    names, rows, duck_sql, _ = F._compute(prep, states, bound or {}, 1000, {})
    return [dict(zip(names, r)) for r in rows], duck_sql


ORDERS = [("id", "BIGINT"), ("user_name", "VARCHAR"), ("amount", "DECIMAL(10,2)"), ("merchant_id", "BIGINT"),
          ("status", "VARCHAR"), ("created_at", "DATETIME")]
MEMBERS = [("id", "BIGINT"), ("user_name", "VARCHAR"), ("city_level", "VARCHAR"), ("vip", "BIGINT")]
MERCHANTS = [("id", "BIGINT"), ("merchant_name", "VARCHAR"), ("category", "VARCHAR")]
T3 = {("shop", "biz", "orders"): ORDERS, ("crm", "crm", "members"): MEMBERS, ("mall", "mall", "merchants"): MERCHANTS}

print("== 1. 只允许一条 SELECT ==")
for bad, msg in [("DELETE FROM shop.biz.orders", "SELECT"), ("SELECT 1; DROP TABLE x", "一条"),
                 ("SELECT id INTO @x FROM shop.biz.orders", ""), ("SELECT * FROM shop.biz.orders FOR UPDATE", "FOR UPDATE"),
                 ("SELECT @@version FROM shop.biz.orders", "变量")]:
    check(f"拒绝 {bad[:40]}", raises(lambda: F._parse(bad), F.FederatedError, msg))

print("== 2. 单数据源：只去掉数据源名前缀，其余原样 ==")
sql = "SELECT  `o`.id ,DATE_FORMAT(o.created_at,'%Y') y  FROM `shop` . biz.orders o JOIN shop.crm.members u ON u.id=o.id -- shop.x.y\nWHERE o.id = :id"
ast = F._parse(sql)
refs = [F._TableRef(node=t, ds=DS["shop"], db=t.db, table=t.name, strip_span=F._ident_span(t.args["catalog"]))
        for t in ast.find_all(exp.Table)]
check("去掉前缀", F._strip_catalogs(sql, refs) ==
      "SELECT  `o`.id ,DATE_FORMAT(o.created_at,'%Y') y  FROM biz.orders o JOIN crm.members u ON u.id=o.id -- shop.x.y\nWHERE o.id = :id",
      F._strip_catalogs(sql, refs))
check("发布前检查：识别引用的数据源（忽略字符串和注释）",
      F.referenced_catalogs("SELECT * FROM 订单库.biz.orders o JOIN `crm db`.crm.m u ON 1 WHERE x = 'a.b.c' -- z.y.x") == ["订单库", "crm db"])

print("== 3. 结果列名与 MySQL 一致 ==")
prep, _ = plan("SELECT o.ID, count(*), SUM(o.amount)  total, o.amount * 2 FROM shop.biz.orders o JOIN mall.mall.merchants m "
               "ON m.id = o.merchant_id GROUP BY o.id", T3)
check("未写别名的表达式用原文作列名、列名大小写按写法", prep.names == ["ID", "count(*)", "total", "o.amount * 2"], prep.names)
prep, _ = plan("SELECT o.id, m.id FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id", T3)
check("重名列第二个起为「表别名.列名」", prep.names == ["id", "m.id"], prep.names)

print("== 4. 下推规则 ==")
prep, pl = plan("SELECT o.id, u.city_level FROM shop.biz.orders o JOIN crm.crm.members u ON u.user_name = o.user_name AND u.vip = 1 "
                "WHERE o.status = :st AND o.amount > 10 AND u.city_level <> o.status", T3)
src = {s.alias: s for s in prep.sources}
check("WHERE 单表条件下推到对应源", "`o`.`status` = :st" in src["o"].preds_sql and "`o`.`amount` > 10" in src["o"].preds_sql, src["o"].preds_sql)
check("内连接 ON 单表条件下推", "`u`.`vip` = 1" in src["u"].preds_sql, src["u"].preds_sql)
check("只取用到的列", src["o"].columns == ["id", "user_name", "status", "amount"] or set(src["o"].columns) == {"id", "user_name", "status", "amount"}, src["o"].columns)
check("跨表条件留在关联计算", "<>" in prep.ast.sql() and "vip" not in prep.ast.sql(), prep.ast.sql())
check("内连接两侧都可按对方关联键过滤", {(t, p) for t, _, p, _ in prep.equi} == {(src["o"].idx, src["u"].idx), (src["u"].idx, src["o"].idx)})

prep, _ = plan("SELECT u.id FROM crm.crm.members u LEFT JOIN shop.biz.orders o ON o.user_name = u.user_name AND o.status = 'paid' "
               "WHERE u.vip = 1 AND o.id IS NULL", T3)
src = {s.alias: s for s in prep.sources}
check("LEFT JOIN：右表的 ON 条件下推到右表", "'paid'" in src["o"].preds_sql)
check("LEFT JOIN：右表的 WHERE 条件（IS NULL）不下推", "IS NULL" not in src["o"].preds_sql and "IS NULL" in prep.ast.sql())
check("LEFT JOIN：左表 WHERE 条件下推", "`u`.`vip` = 1" in src["u"].preds_sql)
check("LEFT JOIN：只按左表的键过滤右表", [(t, p) for t, _, p, _ in prep.equi] == [(src["o"].idx, src["u"].idx)], prep.equi)

prep, _ = plan("SELECT o.id FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id "
               "LEFT JOIN crm.crm.members u ON u.user_name = o.user_name", T3)
src = {s.alias: s for s in prep.sources}
pairs = {(t, p) for t, _, p, _ in prep.equi}
check("内连接表即使是后面 LEFT JOIN 的保留侧，也可按内连接对方的键过滤", (src["m"].idx, src["o"].idx) in pairs and (src["o"].idx, src["m"].idx) in pairs)
check("……但不能按 LEFT JOIN 右表的键过滤", (src["o"].idx, src["u"].idx) not in pairs and (src["u"].idx, src["o"].idx) in pairs)

prep, _ = plan("WITH x AS (SELECT merchant_id, SUM(amount) s FROM shop.biz.orders WHERE status = 'paid' GROUP BY merchant_id) "
               "SELECT m.merchant_name, x.s FROM x JOIN mall.mall.merchants m ON m.id = x.merchant_id", T3)
q = [s for s in prep.sources if s.kind == "query"]
check("同一数据源的 CTE 整段下推（聚合在源库完成）", len(q) == 1 and "GROUP BY" in q[0].base_sql and "SUM" in q[0].base_sql, [s.base_sql for s in prep.sources])
check("CTE 结果可作为另一侧的过滤键", any(p == q[0].idx for _, _, p, _ in prep.equi))

prep, _ = plan("SELECT m.id, (SELECT COUNT(*) FROM shop.biz.orders o WHERE o.merchant_id = m.id) c FROM mall.mall.merchants m", T3)
src = {s.alias: s for s in prep.sources}
check("关联子查询不整段下推，按外层的键过滤内层表", [(t, p) for t, _, p, _ in prep.equi] == [(src["o"].idx, src["m"].idx)], prep.equi)

prep, _ = plan("SELECT o.id FROM shop.biz.orders o WHERE o.user_name IN (SELECT user_name FROM crm.crm.members WHERE vip = 1)", T3)
q = [s for s in prep.sources if s.kind == "query"]
check("IN (子查询) 整段下推并作为过滤键", len(q) == 1 and any(p == q[0].idx for _, _, p, _ in prep.equi))

check("歧义列给出明确提示", raises(lambda: plan("SELECT id FROM shop.biz.orders o JOIN crm.crm.members u ON u.user_name = o.user_name", T3),
                                    F.FederatedError, "表别名.列名"))

print("== 5. 取数顺序 ==")
prep, _ = plan("SELECT o.id, m.category, u.vip FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id "
               "LEFT JOIN crm.crm.members u ON u.user_name = o.user_name WHERE o.id = :id", T3)
r = F._Run(prep, {}, 0)
idx = {s.alias: s.idx for s in prep.sources}
r.est = {idx["o"]: 1, idx["m"]: 100, idx["u"]: 50000}
check("点查：只取驱动表，其余等关联键", r.first_round() == [idx["o"]], r.first_round())
r.est = {idx["o"]: 200000, idx["m"]: 100, idx["u"]: 50000}
check("小表做驱动；关联方并不更小的表并行取", set(r.first_round()) == {idx["m"], idx["u"]}, r.first_round())

print("== 6. 跨源计算结果与 MySQL 一致 ==")
orders = [(1, "Alice", Decimal("10.25"), 1, "paid", datetime.datetime(2026, 2, 27, 13, 45, 30)),
          (2, "bob", Decimal("-0.50"), 2, "paid", datetime.datetime(2024, 12, 31, 0, 0, 1)),
          (3, "ALICE", Decimal("7.00"), 1, "refund", datetime.datetime(2025, 1, 1, 23, 59, 59)),
          (4, "carol", None, 3, "paid", None)]
members = [(1, "alice", "一线", 1), (2, "BOB", "二线", 0)]
merchants = [(1, "商户1", "餐饮"), (2, "商户2", "零售"), (3, "商户3", "服务")]
DATA = {"o": orders, "u": members, "m": merchants}
rows, _ = run("SELECT o.id, u.city_level FROM shop.biz.orders o JOIN crm.crm.members u ON u.user_name = o.user_name ORDER BY o.id", T3, DATA)
check("字符串关联不区分大小写（MySQL *_ci）", [(r["id"], r["city_level"]) for r in rows] == [(1, "一线"), (2, "二线"), (3, "一线")], rows)
rows, _ = run("SELECT m.category, COUNT(*) n, SUM(o.amount) s, AVG(o.amount) a, SUM(o.amount) / COUNT(*) r, 7 / 3 x "
              "FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id GROUP BY m.category ORDER BY m.category", T3, DATA)
by = {r["category"]: r for r in rows}
check("AVG / 除法按 MySQL 的小数位", by["餐饮"]["a"] == Decimal("8.625000") or float(by["餐饮"]["a"]) == 8.625, by["餐饮"])
check("整数相除保留 4 位", float(by["餐饮"]["x"]) == 2.3333, by["餐饮"]["x"])
check("NULL 不参与 SUM", by["服务"]["s"] is None and by["服务"]["n"] == 1, by["服务"])
rows, _ = run("SELECT o.id, m.merchant_name FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id "
              "ORDER BY o.created_at", T3, DATA)
check("升序时 NULL 在前（MySQL）", rows[0]["id"] == 4, rows)
rows, _ = run("SELECT o.id, DAYOFWEEK(o.created_at) dw, WEEKDAY(o.created_at) wd, WEEK(o.created_at) w, YEARWEEK(o.created_at) yw, "
              "DATE_FORMAT(o.created_at, '%D %u %V %X %W %M %e %H:%i') f, DATE_ADD(DATE(o.created_at), INTERVAL 1 MONTH) nm, "
              "TIMESTAMPDIFF(MONTH, '2025-11-15', o.created_at) md, LENGTH(m.merchant_name) bl, CHAR_LENGTH(m.merchant_name) cl, "
              "FIND_IN_SET('b', 'a,B,c') fs, SUBSTRING_INDEX('a.b.c', '.', -2) si, '3abc' + 1 sn, o.user_name LIKE 'al%' lk "
              "FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id WHERE o.id <= 3 ORDER BY o.id", T3, DATA)
want = [
    dict(dw=6, wd=4, w=8, yw=202608, f="27th 09 08 2026 Friday February 27 13:45", nm=datetime.date(2026, 3, 27), md=3, bl=7, cl=3, fs=2, si="b.c", sn=4.0, lk=1),
    dict(dw=3, wd=1, w=52, yw=202452, f="31st 53 52 2024 Tuesday December 31 00:00", nm=datetime.date(2025, 1, 31), md=-10, bl=7, cl=3, fs=2, si="b.c", sn=4.0, lk=0),
    dict(dw=4, wd=2, w=0, yw=202452, f="1st 01 52 2024 Wednesday January 1 23:59", nm=datetime.date(2025, 2, 1), md=-10, bl=7, cl=3, fs=2, si="b.c", sn=4.0, lk=1),
]
for r, w in zip(rows, want):
    got = {k: (int(v) if isinstance(v, bool) else v) for k, v in r.items() if k in w}
    check(f"日期 / 字符串函数（第 {r['id']} 行）", got == w, {k: (got.get(k), w[k]) for k in w if got.get(k) != w[k]})

rows, sql1 = run("SELECT o.id, NOW() >= '2020-01-01' AS t, UNIX_TIMESTAMP() > 0 AS e FROM shop.biz.orders o "
                 "JOIN mall.mall.merchants m ON m.id = o.merchant_id WHERE o.id = 1", T3, DATA)
check("NOW() 每次执行时代入（SQL 模板不固化时间）", rows and rows[0]["t"] and "__PH__" not in sql1 and "CAST('20" in sql1, sql1[:300])
rows, _ = run("SELECT o.id, :label AS label FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id "
              "WHERE o.id = 1", T3, DATA, bound={"label": "it's \\ 测试"})
check("参数以常量写入关联计算（引号 / 反斜杠安全）", rows and rows[0]["label"] == "it's \\ 测试", rows)

print("== 7. 少量数据内联 VALUES 与 Arrow 两条路径结果一致 ==")
big = [(i, f"u{i}", Decimal(f"{i}.50"), i % 3 + 1, "paid", datetime.datetime(2026, 1, 1) + datetime.timedelta(hours=i)) for i in range(1, 101)]
sql = ("SELECT m.category, COUNT(*) n, SUM(o.amount) s, MAX(o.created_at) mx FROM shop.biz.orders o "
       "JOIN mall.mall.merchants m ON m.id = o.merchant_id GROUP BY m.category ORDER BY m.category")
r_arrow, s_arrow = run(sql, T3, {"o": big, "m": merchants})
r_inline, s_inline = run(sql, T3, {"o": big[:20], "m": merchants})
check("100 行走 Arrow、3 行商户内联", "VALUES" in s_arrow and s_arrow.count("VALUES") == 1)
F._INLINE_ROWS, keep = 0, F._INLINE_ROWS
r_inline2, s2 = run(sql, T3, {"o": big[:20], "m": merchants})
F._INLINE_ROWS = keep
check("同一份数据两条路径结果相同", r_inline == r_inline2 and "VALUES" not in s2, (r_inline, r_inline2))
lit_rows = [(1, "it's \\ 中文", Decimal("1.50"), datetime.datetime(2026, 1, 2, 3, 4, 5, 6), datetime.timedelta(hours=-1, seconds=3), b"\x00\xffA", None)]
cols = [F._Col("i", "BIGINT", None), F._Col("s", "VARCHAR", None), F._Col("d", "DECIMAL(10, 2)", None), F._Col("t", "DATETIME", None),
        F._Col("iv", "INTERVAL", None), F._Col("b", "BLOB", None), F._Col("n", "DOUBLE", None)]
import duckdb
got = duckdb.connect().execute(f'WITH {F._inline_cte("__x", cols, lit_rows)} SELECT * FROM "__x"').fetchall()
check("内联常量各类型往返不变", got == lit_rows, got)

print("== 8. 计算引擎沙箱 ==")
con = F._duck_db()
c = con.cursor()
check("禁止读文件", raises(lambda: c.execute("SELECT * FROM read_csv('/etc/passwd')").fetchall(), Exception, "disabled"))
check("禁止 COPY / ATTACH", raises(lambda: c.execute("ATTACH '/tmp/x.db'"), Exception, ""))
check("配置已锁定", raises(lambda: c.execute("SET enable_external_access = true"), Exception, "locked"))
c.close()
check("SQL 中的 DuckDB 专有文件函数被拒绝",
      raises(lambda: run("SELECT read_text('/etc/passwd') FROM shop.biz.orders o JOIN mall.mall.merchants m ON m.id = o.merchant_id",
                         T3, DATA), F.FederatedError, "不支持"))

print("== 9. 执行计划缓存 ==")


async def _cache_case():
    calls = []

    async def fake_build(sql, db, project_id, timeout):
        calls.append(sql)
        await asyncio.sleep(0.01)
        return F._Prepared(mode="single", ds=DS["shop"], native=sql)
    orig = F._build_prepared
    F._build_prepared = fake_build
    try:
        F.clear_plan_cache()
        await asyncio.gather(*[F._get_prepared("SELECT 1", None, 1, 5) for _ in range(20)])
        n1 = len(calls)
        await F._get_prepared("SELECT 1", None, 1, 5)
        n2 = len(calls)
        F.clear_plan_cache()
        await F._get_prepared("SELECT 1", None, 1, 5)
        return n1, n2, len(calls)
    finally:
        F._build_prepared = orig
        F.clear_plan_cache()

n1, n2, n3 = asyncio.run(_cache_case())
check("并发请求只生成一次执行计划", n1 == 1, n1)
check("缓存期内复用", n2 == 1, n2)
check("配置变更清空后重新生成", n3 == 2, n3)

print(f"\n========= 结果: {PASS} 通过, {FAIL} 失败 =========")
sys.exit(1 if FAIL else 0)
