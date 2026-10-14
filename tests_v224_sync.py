# -*- coding: utf-8 -*-
"""v2.24 预发 ↔ 生产同步回归测试（不依赖数据库）：python tests_v224_sync.py"""
import sys
sys.path.insert(0, ".")

from app.services.release import diff_snapshots, mask_key, split_rows, sync_state

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def snap(sql="SELECT 1", ttl=300, key=""):
    return {"name": "a", "method": "GET", "url_path": "/a", "sql_template": sql, "cache_ttl": ttl,
            "api_key": key, "parameters": []}


print("== 1. 三方同步状态 ==")
B = snap()
check("一致", sync_state(snap(), snap(), B) == "same")
check("预发有新改动", sync_state(snap(), snap(ttl=60), B) == "pre_ahead")
check("生产有变更", sync_state(snap(ttl=60), snap(), B) == "prod_ahead")
check("两边都有改动", sync_state(snap(ttl=60), snap(sql="SELECT 2"), B) == "both")
check("两边改成一样也算一致", sync_state(snap(ttl=60), snap(ttl=60), B) == "same")
check("没有基线 → 有差异", sync_state(snap(ttl=60), snap(), None) == "diverged")
check("仅生产有", sync_state(snap(), None, None) == "prod_only")
check("仅预发有", sync_state(None, snap(), None) == "pre_only")

print("== 2. 左右并排行 ==")
rows = split_rows(["a", "b", "c"], ["a", "B2", "c", "d"])
check("相同行左右都有行号", rows[0] == {"t": "eq", "ln": 1, "rn": 1, "l": [["a", False]], "r": [["a", False]]}, rows[0])
chg = [r for r in rows if r["t"] == "chg"]
check("改动行配对（左 b / 右 B2）", chg and chg[0]["ln"] == 2 and chg[0]["rn"] == 2, chg)
check("新增行只有右侧", rows[-1]["t"] == "add" and rows[-1]["l"] is None and rows[-1]["rn"] == 4, rows[-1])
rows = split_rows(["x"], [])
check("删除行只有左侧", rows == [{"t": "del", "ln": 1, "rn": None, "l": [["x", True]], "r": None}], rows)
long_a = [f"line{i}" for i in range(40)]
long_b = list(long_a)
long_b[20] = "changed"
rows = split_rows(long_a, long_b)
skips = [r for r in rows if r["t"] == "skip"]
check("大段相同行折叠", len(skips) == 2 and sum(s["n"] for s in skips) == 39 - 6, skips)
rows = split_rows(["SELECT id, name FROM t WHERE a = 1"], ["SELECT id, name FROM t WHERE a = 2"])
l = rows[0]["l"]
check("行内只高亮改动的字符", [t for t, h in l if h] == ["1"], l)

print("== 3. API Key 进入差异但脱敏 ==")
d = diff_snapshots(snap(key="pfk_abcdefghijklmnop"), snap(key="pfk_zzzzzzzzzzzzzzzz"))
k = [f for f in d["fields"] if f["field"] == "api_key"][0]
check("Key 不同算差异", k["changed"])
check("差异里只显示首尾", k["before"] == mask_key("pfk_abcdefghijklmnop") and "ghij" not in k["before"], k)
sql = [f for f in diff_snapshots(snap(), snap(sql="SELECT 2"))["fields"] if f["field"] == "sql_template"][0]
check("多行字段带 split", sql["split"][0]["t"] == "chg", sql)

print("== 4. 更新类 API 只允许带 WHERE 的单条 UPDATE ==")
from app.services.engine import _update_violation, _readonly_violation
for q in ["UPDATE t SET a = 1 WHERE id = 1", "/* c */ update t set a=1\nwhere b=2;", "UPDATE t SET nowhere = 1 WHERE x = 1"]:
    check(f"放行 {q[:40]!r}", _update_violation(q) is None, _update_violation(q))
for q in ["UPDATE t SET a = 1", "UPDATE t SET a = (SELECT x FROM y WHERE z = 1)", "UPDATE t SET a = 'where'",
          "DELETE FROM t WHERE id = 1", "INSERT INTO t VALUES (1)", "UPDATE t SET a=1 WHERE id=1; DELETE FROM t",
          "DROP TABLE t"]:
    check(f"拦截 {q[:40]!r}", _update_violation(q) is not None)
check("普通 SQL 仍然不能执行 UPDATE", _readonly_violation("UPDATE t SET a = 1 WHERE id = 1") is not None)

print("== 5. 参数占位符多加的引号自动去掉 ==")
from app.services.sql_template import unquote_param_literals
src = "WHERE scenic_id = ':scenicId'\nAND channel = \":channelId\" AND t = '10:30' AND c = 'a:b' -- 注释 ':x'\nAND g = ':timeTag'"
out, fixed = unquote_param_literals(src)
check("去掉引号", out == "WHERE scenic_id = :scenicId\nAND channel = :channelId AND t = '10:30' AND c = 'a:b' -- 注释 ':x'\nAND g = :timeTag", out)
check("返回修正的参数名", fixed == ["scenicId", "channelId", "timeTag"], fixed)
check("正常 SQL 原样返回", unquote_param_literals("SELECT ':' AS a, ':: x' AS b WHERE id = :id") == ("SELECT ':' AS a, ':: x' AS b WHERE id = :id", []))

print("== 6. 删除类 API：只允许带 WHERE 的单表 DELETE ==")
from app.services.engine import _delete_violation, _delete_count_sql
for q in ["DELETE FROM t WHERE a = 1", "delete from `db`.`t` where a = 1 limit 10;", "/* c */ DELETE FROM t\nWHERE a = 'x;y'"]:
    check(f"放行 {q[:40]!r}", _delete_violation(q) is None, _delete_violation(q))
for q in ["DELETE FROM t", "DELETE FROM t WHERE a=1; DROP TABLE t", "DELETE t1 FROM t1 JOIN t2 ON t1.id = t2.id WHERE t2.x = 1",
          "DELETE FROM t USING t, t2 WHERE t.id = t2.id", "UPDATE t SET a = 1 WHERE b = 1", "TRUNCATE TABLE t"]:
    check(f"拦截 {q[:40]!r}", _delete_violation(q) is not None)
check("删除前统计行数的 SQL", _delete_count_sql("DELETE FROM t WHERE a = 1 LIMIT 5;").strip() == "SELECT COUNT(*) AS n FROM t WHERE a = 1")
check("普通 SQL 仍不能执行 DELETE", _readonly_violation("DELETE FROM t WHERE a = 1") is not None)

print("== 7. 同步替换模式的请求校验 ==")
from app.services.data_sync import validate_replace, SyncError
wl = {"t": ["scenic_id", "stat_date", "amount"]}
def bad(p):
    try:
        validate_replace(p, wl); return False
    except SyncError:
        return True
ok = {"tableName": "t", "mode": "replace", "deleteWhere": {"scenic_id": "S1"}, "data": [{"scenic_id": "S1", "amount": 1}]}
check("合法请求通过", validate_replace(ok, wl)[1] == {"scenic_id": "S1"})
check("没有删除条件拒绝", bad(dict(ok, deleteWhere={})))
check("删除条件值为空拒绝", bad(dict(ok, deleteWhere={"scenic_id": ""})))
check("删除条件字段不在白名单拒绝", bad(dict(ok, deleteWhere={"x": 1})))
check("数据行超出删除范围拒绝", bad(dict(ok, data=[{"scenic_id": "S2", "amount": 1}])))
check("各行字段不一致拒绝", bad(dict(ok, data=[{"scenic_id": "S1", "amount": 1}, {"scenic_id": "S1"}])))
check("data 为空拒绝", bad(dict(ok, data=[])))

print("== 8. 数字字符串：前端会丢精度的保持字符串 ==")
from app.services.engine import _clean_value
from app.core.config import settings as _st
check("19 位 ID 字符串保持字符串", _clean_value("7461234567890123456") == "7461234567890123456")
check("2^53 以内仍按原规则转数字", _clean_value("9007199254740991") == 9007199254740991 and _clean_value("123") == 123)
check("2^53 + 1 保持字符串", _clean_value("9007199254740992") == "9007199254740992")
check("超过 15 位有效数字的小数保持字符串", _clean_value("12345678901234567.25") == "12345678901234567.25")
check("JSON 结构仍解析", _clean_value('["a","b"]') == ["a", "b"] and _clean_value('{"a":1}') == {"a": 1})
check("前导零不当数字", _clean_value("0012") == "0012")
_old = _st.query.parse_numeric_strings
_st.query.parse_numeric_strings = False
check("开关关闭：纯数字 / true 原样返回", _clean_value("123") == "123" and _clean_value("true") == "true")
check("开关关闭：JSON 结构仍解析", _clean_value("[1,2]") == [1, 2])
_st.query.parse_numeric_strings = _old

print(f"========= 结果: {PASS} 通过, {FAIL} 失败 =========")
sys.exit(1 if FAIL else 0)
