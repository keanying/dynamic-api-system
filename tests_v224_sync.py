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

print(f"========= 结果: {PASS} 通过, {FAIL} 失败 =========")
sys.exit(1 if FAIL else 0)
