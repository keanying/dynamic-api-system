# -*- coding: utf-8 -*-
"""v2.20 代码审查回归测试（不依赖数据库）：python tests_v220_review.py"""
import sys
sys.path.insert(0, ".")

from app.services.sql_template import eval_expr, render_template, SqlTplError
from app.services.engine import _parse_sql_params, _readonly_violation, _build_cache_key
from app.services.pipeline_ref import eval_arithmetic, RefError
from app.core.security import encrypt_value, decrypt_value

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


print("== 1. and / or 短路时右侧仍需解析 ==")
for expr, params, want in [
    ("a == 1 or b == 2", {"a": 1, "b": 2}, True),
    ("a == 1 and b == 2", {"a": 0, "b": 2}, False),
    ("!empty(kw) and len(kw) > 1", {"kw": ""}, False),
    ("!empty(kw) and len(kw) > 1", {"kw": "ab"}, True),
    ("defined(x) && x > 0", {}, False),
    ("a or b or c", {"a": 1}, True),
    ("(a == 1 or b == 2) and c", {"a": 1, "c": 1}, True),
    ("x == null or len(x) > 0", {"x": None}, True),
    ("a and b in [1,2]", {"a": 0, "b": 1}, False),
]:
    try:
        got = eval_expr(expr, params)
    except Exception as e:  # noqa: BLE001
        got = f"异常: {e}"
    check(f"{expr} {params}", got == want, got)
check("短路右侧的语法错误仍报错", raises(lambda: eval_expr("a or zz(b)", {"a": 1}), SqlTplError))
check("$if$ 中 and 左侧为假时整段渲染正常",
      render_template("SELECT 1 $if(!empty(kw) and len(kw) > 1)$AND name = :kw$endif$", {"kw": ""}).strip() == "SELECT 1")

print("== 2. #{} 与 :{} 同一行 ==")
sql, binds = _parse_sql_params("SELECT #{col}, :{ch.name} AS c FROM t", {"col": "amount", "ch": {"name": "抖音"}}, [])
check(":{} 在 #{} 之后也被替换", ":{" not in sql and binds == {"__tpl_b0": "抖音"}, (sql, binds))

print("== 3. #{} 拒绝 SQL 关键字 ==")
for v in ["created_at desc", "a.b, c", "update_time", "from_date", "*"]:
    check(f"允许 {v!r}", not raises(lambda: render_template("ORDER BY #{s}", {"s": v})))
for v in ["id UNION SELECT pwd FROM users", "1 or sleep 5", "x INTO OUTFILE y"]:
    check(f"拒绝 {v!r}", raises(lambda: render_template("ORDER BY #{s}", {"s": v}), SqlTplError))

print("== 4. 只读防护（检查最终执行的 SQL）==")
for sql in ["SELECT 1", "SELECT 1;", "SELECT 'a;b' FROM t -- x;y", "WITH x AS (SELECT 1) SELECT * FROM x",
            "SELECT update_time FROM t", "(SELECT 1) UNION (SELECT 2)", "SELECT * FROM t FOR UPDATE"]:
    check(f"放行 {sql!r}", _readonly_violation(sql) is None, _readonly_violation(sql))
for sql in ["SELECT 1; DELETE FROM t", "DELETE FROM t", "/*x*/ DROP TABLE t", "REPLACE INTO t VALUES (1)",
            "LOAD DATA INFILE 'x' INTO TABLE t", "SELECT * FROM t INTO OUTFILE '/tmp/x'",
            "WITH x AS (SELECT 1) DELETE FROM t", "LOCK TABLES t WRITE"]:
    check(f"拦截 {sql!r}", _readonly_violation(sql) is not None)

print("== 5. 其它 ==")
for pw in ["abc123", "密码P@ss", "x" * 70 + "中"]:
    check(f"含非 ASCII 的密码加解密往返 {pw[:8]!r}", decrypt_value(encrypt_value(pw)) == pw)
def _old_encrypt(value):   # v2.19 及之前的实现（按字符数生成密钥流）
    import base64
    from app.core.config import settings
    key = settings.security.encryption_key.encode()[:32].ljust(32, b"\0")
    enc = bytes(a ^ b for a, b in zip(value.encode(), (key * ((len(value) // 32) + 1))[:len(value)]))
    return base64.urlsafe_b64encode(enc).decode()


check("纯 ASCII 密码加密结果与旧实现逐字节一致（已存数据不受影响）",
      all(encrypt_value(p) == _old_encrypt(p) for p in ["abc", "P@ssw0rd!", "x" * 100]))
check("旧实现加密的 ASCII 密码可被解密", decrypt_value(_old_encrypt("Tr5Apzvat")) == "Tr5Apzvat")
check("缓存 key 随 API 版本变化", _build_cache_key(1, {"a": 1}, 1) != _build_cache_key(1, {"a": 1}, 2))
check("缓存 key 前缀不变（清除缓存按前缀删除）", _build_cache_key(7, {}, 3).endswith(_build_cache_key(7, {}, 3).split(":")[-1])
      and ":api:7:" in _build_cache_key(7, {}, 3))
check("乘方正常使用", eval_arithmetic("2 ** 10", {}) == 1024)
check("超大乘方被拒绝（不卡死事件循环）", raises(lambda: eval_arithmetic("9 ** 9 ** 9", {}), RefError))

print("== 6. v2.21：审查遗留项 ==")
from types import SimpleNamespace as N
from app.services.engine import apply_param_defaults
from app.services.sql_template import render_template as _rt
ap = [N(name="status", default_value="1", param_type="number"), N(name="kw", default_value="", param_type="string")]
eff = apply_param_defaults({"kw": "x"}, ap)
check("默认值补入并按类型转换", eff == {"kw": "x", "status": 1}, eff)
check("已传的参数不被默认值覆盖", apply_param_defaults({"status": 5}, ap)["status"] == 5)
check("开启后默认值对 $if$ 可见", "status" in _rt("SELECT 1 $if(defined(status))$AND status = :status$endif$",
                                              apply_param_defaults({}, ap)))

from app.core.client_ip import resolve_client_ip as rip
check("公网直连伪造 X-Forwarded-For 被忽略", rip("1.2.3.4", "9.9.9.9") == "1.2.3.4")
check("本机代理转发取真实 IP", rip("127.0.0.1", "8.8.8.8") == "8.8.8.8")
check("内网 LB 后客户端伪造的最左值被跳过", rip("10.0.0.5", "6.6.6.6, 8.8.8.8") == "8.8.8.8")

from app.services import ds_scope
check("数据源范围为空 = 所有项目可用", ds_scope.is_allowed(N(project_scope=""), "any"))
check("数据源范围限定项目", ds_scope.is_allowed(N(project_scope="a,b"), "b") and not ds_scope.is_allowed(N(project_scope="a,b"), "c"))
check("范围规范化（中文逗号/空格/去重）", ds_scope.normalize_scope(" b，a  a ") == "a,b")
check("不允许切换到 mysql 系统库", raises(lambda: ds_scope.check_database_override("mysql")))
_biz = N(name="biz", database_name="biz", project_scope="")
check("拦截 SQL 中引用 mysql 系统库", ds_scope.schema_violation("SELECT * FROM `mysql`.`user`", _biz) is not None)
check("字符串里出现系统库名不拦截", ds_scope.schema_violation("SELECT 'mysql.user' AS a", _biz) is None)

from app.api.auth import token_revoked
check("修改密码前签发的凭证作废", token_revoked({"iat": 100}, N(token_epoch=200)))
check("修改密码后签发的凭证有效", not token_revoked({"iat": 200}, N(token_epoch=200)))
check("从未改过密码的用户不受影响", not token_revoked({"iat": 1}, N(token_epoch=0)))

from app.core.permissions import role_at_least
check("角色门槛：developer >= developer", role_at_least(N(global_role="developer"), "developer"))
check("角色门槛：user < developer", not role_at_least(N(global_role="user"), "developer"))
check("角色门槛：super_admin >= admin", role_at_least(N(global_role="super_admin"), "admin"))

import asyncio
import app.api.approvals as _ap
from app.core.config import settings as _st


def _decide(mode, mgr, rev, reviewer_id=7):
    _st.approval.online_mode = mode
    appr = N(manager_decision=mgr, reviewer_decision=rev, reviewer_id=reviewer_id, overall_status="pending")
    api = N(status="pending")
    return asyncio.run(_ap._finalize_if_done(None, appr, api))


check("any：任一方通过即通过", _decide("any", "pending", "approved") == "approved")
check("both：只有会审通过时仍待审", _decide("both", "pending", "approved") == "pending")
check("both：双方通过才通过", _decide("both", "approved", "approved") == "approved")
check("both：未指定会审人时管理员通过即可", _decide("both", "approved", "pending", reviewer_id=None) == "approved")
check("任一方驳回即驳回", _decide("both", "approved", "rejected") == "rejected")
_st.approval.online_mode = "any"

print(f"\n========= 结果: {PASS} 通过, {FAIL} 失败 =========")
sys.exit(1 if FAIL else 0)
