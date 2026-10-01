# -*- coding: utf-8 -*-
"""v2.0.2 回归 + 新功能测试（不依赖数据库，直接测模板引擎与参数解析核心）"""
import sys, types
sys.path.insert(0, ".")

from app.services.sql_template import (
    render_template, extract_placeholders, eval_expr, SqlTplError,
    split_literals, sub_outside_literals,
)
from app.services.engine import _parse_sql_params, _to_pyformat, _check_ddl, format_as_json, _coerce_param_types

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")

def ap(name, required=False, default_value="", param_type="string"):
    o = types.SimpleNamespace()
    o.name = name; o.required = required; o.default_value = default_value; o.param_type = param_type
    return o

print("== 1. 向后兼容：v1.4 全部旧语法 ==")
sql = """SELECT #{cols} FROM t WHERE 1=1
$if(defined(kw))$ AND name LIKE :kw|like $endif$
$if(ids)$ AND id IN :ids $endif$
$for(s in sorts)$#{s}$sep$, $endfor$"""
final, bound = _parse_sql_params(sql, {"kw": "50%off", "ids": [1, 2, 3], "sorts": ["a", "b"], "cols": "x, y"}, [])
check("旧 #{} 插值", "SELECT x, y FROM t" in final, final)
check("旧 like modifier 转义", bound.get("kw__like") == "%50\\%off%", str(bound))
check("旧 IN 展开", "(:ids__0, :ids__1, :ids__2)" in final and bound["ids__0"] == 1, final)
check("旧 $for$sep$", "a, b" in final, final)

final2, bound2 = _parse_sql_params("SELECT * FROM t WHERE id IN :ids", {"ids": []}, [])
check("空数组 -> (NULL)", "(NULL)" in final2, final2)

final3, bound3 = _parse_sql_params("SELECT * FROM t WHERE a = :a", {}, [ap("a", default_value="7")])
check("默认值兜底", bound3["a"] == "7", str(bound3))
try:
    _parse_sql_params("SELECT :must", {}, [ap("must", required=True)])
    check("必填校验", False)
except ValueError as e:
    check("必填校验", "must" in str(e))

print("== 2. 用户案例：channels 嵌套参数 -> union all ==")
tpl = """$for(ch in channels)$
select sum(case when spu_id in (:{ch.prdId}) then pay_count else 0 end) as pay_count,
       :{ch.channelName} as channel
from report.r_report_dwm_common
WHERE merchant_id = :merchant_id
  AND operated_at >= :start_time
  AND operated_at < :end_time
$sep$
union all
$endfor$"""
params = {
    "channels": [
        {"channelName": "抖音", "prdId": ["476855"]},
        {"channelName": "美团", "prdId": ["476897", "476898"]},
    ],
    "merchant_id": 37950206,
    "start_time": "2024-12-25 00:00:00",
    "end_time": "2024-12-26 00:00:00",
}
final, bound = _parse_sql_params(tpl, params, [])
print("---- 渲染后的 SQL ----")
print(final)
print("---- 绑定参数 ----")
print(bound)
check("生成两段 union all", final.count("union all") == 1 and final.count("select sum") == 2, final)
check("抖音 prdId 展开1个(括号吸收)", "in (:__tpl_b0__0)" in final and "((" not in final and bound["__tpl_b0__0"] == "476855")
check("美团 prdId 展开2个", ":__tpl_b2__0, :__tpl_b2__1" in final and bound["__tpl_b2__1"] == "476898")
check("channelName 参数化绑定", bound["__tpl_b1"] == "抖音" and bound["__tpl_b3"] == "美团")
check("外层参数照常绑定", bound["merchant_id"] == 37950206)
py = _to_pyformat(final)
check("最终 pyformat 转换", "%(__tpl_b1)s as channel" in py and "%(merchant_id)s" in py, py)

print("== 3. 冒号冲突：字符串/注释内的 : 不再当占位符 ==")
sql = "SELECT DATE_FORMAT(t,'%H:%i') h, ':not_a_param' s, `a:b` c, :real FROM x -- 注释里的 :fake\nWHERE j = '{\"a\":{\"b\":1}}'"
final, bound = _parse_sql_params(sql, {"real": 1, "not_a_param": 9, "fake": 8}, [])
check("字符串内 :not_a_param 不提取不改写", "':not_a_param'" in final and "not_a_param" not in bound, final)
check(":real 正常绑定", bound.get("real") == 1)
check("JSON 字面量原样", '\'{"a":{"b":1}}\'' in final, final)
py = _to_pyformat(final)
check("pyformat: 字符串内冒号保留", "':not_a_param'" in py, py)
check("pyformat: %H 转义为 %%H", "'%%H:%%i'" in py, py)
check("pyformat: 占位符转换", "%(real)s" in py, py)
check("pyformat: 反引号内冒号保留", "`a:b`" in py, py)

check("已有 %% 不再重复转义", _to_pyformat("LIKE 'a%%b'") == "LIKE 'a%%b'")
check("裸 % 自动转义", _to_pyformat("SELECT 10 % 3, :x") == "SELECT 10 %% 3, %(x)s")

print("== 4. 反斜杠冒号转义 ==")
final, bound = _parse_sql_params(r"SELECT \:literal, :x FROM t", {"x": 5, "literal": 6}, [])
check(r"\:literal 不作为占位符", "literal" not in bound and bound["x"] == 5, str(bound))
py = _to_pyformat(final)
check(r"\: 还原为字面 :", ":literal" in py and "\\" not in py, py)

print("== 5. 中文全角冒号历史用法不受影响 ==")
sql = "SELECT CONCAT(a, '：', b), :x FROM t"
final, bound = _parse_sql_params(sql, {"x": 1}, [])
py = _to_pyformat(final)
check("全角冒号原样保留", "'：'" in py and "%(x)s" in py, py)

print("== 6. 点路径表达式 ==")
p = {"ch": {"name": "n1", "ids": [10, 20], "meta": {"lvl": 2}}, "arr": [{"v": 1}]}
check("$if 点路径", eval_expr("ch.meta.lvl == 2", p) is True)
check("len(嵌套)", eval_expr("len(ch.ids) > 1", p) is True)
check("列表索引", eval_expr("ch.ids.1", p) == 20)
check("defined(点路径) 真", eval_expr("defined(ch.meta.lvl)", p) is True)
check("defined(点路径) 假", eval_expr("defined(ch.meta.none)", p) is False)
check("缺失路径 -> None", eval_expr("ch.nope.deep", p) is None)
check("in 嵌套列表", eval_expr("10 in ch.ids", p) is True)

r = render_template("$for(x in arr)$#{x.v}$endfor$", p)
check("#{} 点路径", r == "1", r)
r = render_template("#{ch.ids}", p)
check("#{} 列表拼接", r == "10, 20", r)

print("== 7. #{} 安全校验仍然生效 ==")
try:
    render_template("#{bad}", {"bad": "x'; DROP TABLE t;--"})
    check("#{} 注入拦截", False)
except SqlTplError:
    check("#{} 注入拦截", True)
try:
    binds = {}
    render_template(":{bad}", {"bad": "x'; DROP--"}, collect_binds=binds)
    check(":{} 危险值走参数化(不报错)", binds["__tpl_b0"] == "x'; DROP--")
except SqlTplError:
    check(":{} 危险值走参数化(不报错)", False)

print("== 8. :{} 配合 modifier ==")
final, bound = _parse_sql_params("SELECT * FROM t WHERE n LIKE :{ch.name}|like", {"ch": {"name": "苏州_园"}}, [])
check(":{}|like 生成转义模糊值", bound.get("__tpl_b0__like") == "%苏州\\_园%", str(bound))

print("== 9. render_template 无收集器时 :{ 原样(旧行为) ==")
r = render_template("$if(true)$a :{x} b$endif$", {"x": 1})
check("无收集器 :{ 保留", r == "a :{x} b", r)

print("== 10. DDL 检查 ==")
check("SELECT 放行", _check_ddl("SELECT 1") is False)
check("前导注释绕过拦截", _check_ddl("/*x*/ DROP TABLE t") is True)
check("行注释绕过拦截", _check_ddl("-- c\nDELETE FROM t") is True)
check("列名含 update 不误伤", _check_ddl("SELECT update_time FROM t") is False)

print("== 10.5 IN 括号两种写法 ==")
f1, _ = _parse_sql_params("SELECT * FROM t WHERE id IN (:ids)", {"ids": [1, 2]}, [])
f2, _ = _parse_sql_params("SELECT * FROM t WHERE id IN :ids", {"ids": [1, 2]}, [])
check("IN (:ids) 吸收括号", "IN (:ids__0, :ids__1)" in f1 and "((" not in f1, f1)
check("IN :ids 补括号", "IN (:ids__0, :ids__1)" in f2, f2)
f3, _ = _parse_sql_params("SELECT * FROM t WHERE id IN (:ids)", {"ids": []}, [])
check("空数组括号形式 -> (NULL)", "IN (NULL)" in f3 and "((" not in f3, f3)

print("== 11. IN 展开上限 ==")
try:
    _parse_sql_params("SELECT * FROM t WHERE id IN :ids", {"ids": list(range(10001))}, [])
    check("超限报错", False)
except ValueError as e:
    check("超限报错", "上限" in str(e))

print("== 12. format_as_json 行为保持 ==")
import datetime
check("dict 直返", format_as_json({"a": 1}) == {"a": 1})
check("JSON 字符串解析", format_as_json('{"a":1}') == {"a": 1})
check("普通字符串原样", format_as_json("abc") == "abc")
check("数字字符串仍解析(历史行为)", format_as_json("123") == 123)
check("datetime 直通", isinstance(format_as_json(datetime.datetime.now()), datetime.datetime))

print("== 13. 参数类型宽松转换 ==")
params = {"n": "42", "f": "3.5", "b1": "false", "b2": "true", "arr": '["a","b"]', "keep": "abc", "obj": '{"channels":[{"prdId":["1"]}]}'}
_coerce_param_types(params, [ap("n", param_type="number"), ap("f", param_type="number"),
                             ap("b1", param_type="boolean"), ap("b2", param_type="boolean"),
                             ap("arr", param_type="array"), ap("keep", param_type="number"),
                             ap("obj", param_type="object")])
check("number 转 int", params["n"] == 42 and isinstance(params["n"], int))
check("number 转 float", params["f"] == 3.5)
check("boolean 'false' -> False (修复原 bool('false')=True 的bug)", params["b1"] is False)
check("boolean 'true' -> True", params["b2"] is True)
check("array JSON 解析", params["arr"] == ["a", "b"])
check("object 嵌套 JSON 解析", params["obj"]["channels"][0]["prdId"] == ["1"])
check("转换失败保留原值", params["keep"] == "abc")

print("== 14. $if 内使用嵌套条件 + :{} ==")
tpl = "SELECT 1 $for(c in chs)$ $if(len(c.ids) > 0)$ OR x IN (:{c.ids}) $endif$ $endfor$"
final, bound = _parse_sql_params(tpl, {"chs": [{"ids": [1]}, {"ids": []}]}, [])
check("空 ids 分支跳过", final.count("OR x IN") == 1, final)

print("== 15. 嵌套参数结构校验 (v2.1 item_schema) ==")
import json as _json
from app.services.engine import _validate_nested_params
_schema = _json.dumps({"item_type": "object", "children": [
    {"name": "channelName", "param_type": "string", "required": True},
    {"name": "prdId", "param_type": "array", "required": True,
     "item_schema": {"item_type": "string"}},
]})
_ap = ap("channels", param_type="array"); _ap.item_schema = _schema
_validate_nested_params({"channels": [{"channelName": "抖音", "prdId": ["1"]}]}, [_ap])
check("合法嵌套通过", True)
try:
    _validate_nested_params({"channels": [{"channelName": "抖音"}]}, [_ap])
    check("缺必填子字段报错", False)
except ValueError as e:
    check("缺必填子字段报错", "channels[0].prdId" in str(e), str(e))
try:
    _validate_nested_params({"channels": [{"channelName": "x", "prdId": "1"}]}, [_ap])
    check("子字段容器类型报错", False)
except ValueError as e:
    check("子字段容器类型报错", "应为数组" in str(e), str(e))
_old = ap("legacy"); _old.item_schema = ""
_validate_nested_params({"legacy": "whatever"}, [_old])
check("存量无schema参数零影响", True)
_bad = ap("b", param_type="array"); _bad.item_schema = "{not json"
_validate_nested_params({"b": [1]}, [_bad])
check("坏schema静默跳过", True)

print(f"\n========= 结果: {PASS} 通过, {FAIL} 失败 =========")
sys.exit(1 if FAIL else 0)
