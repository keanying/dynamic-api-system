#!/usr/bin/env python3
"""
OneData 部署自检脚本
用法: 在项目根目录执行  python3 diagnose.py
作用: 一眼看出当前服务实际跑的是不是最新代码、模板引擎是否生效。
"""
import sys, os, importlib.util

print("=" * 64)
print("OneData 部署自检")
print("=" * 64)

# 1. 版本号
try:
    sys.path.insert(0, os.getcwd())
    from app.api.system import BACKEND_VERSION
    print(f"[1] 代码内版本号 BACKEND_VERSION = {BACKEND_VERSION}")
except Exception as e:
    print(f"[1] 读不到版本号: {e}  (可能是很旧的代码，没有 system.py)")

# 2. engine.py 的实际路径和关键行号
try:
    import app.services.engine as eng
    path = eng.__file__
    print(f"[2] 实际加载的 engine.py 路径:\n    {path}")
    src = open(path, encoding="utf-8").read()
    has_render = "render_template" in src and "rendered = render_template" in src
    print(f"[3] engine.py 是否调用模板引擎 render_template: {'是 ✓' if has_render else '否 ✗ —— 这是旧代码！'}")
except Exception as e:
    print(f"[2] 检查 engine.py 失败: {e}")

# 3. 实测模板渲染
try:
    from app.services.sql_template import render_template
    out = render_template("A $if(len(x)>0)$ B $endif$", {})
    ok = out.strip() == "A"
    print(f"[4] 模板渲染实测: 输入 'A $if(len(x)>0)$ B $endif$' (不传x)")
    print(f"    输出: {out!r}")
    print(f"    结果: {'正常 ✓ ($if$ 被正确移除)' if ok else '异常 ✗'}")
except Exception as e:
    print(f"[4] 模板引擎不可用: {e}  —— 旧代码没有模板引擎")

# 4. _parse_sql_params 是否先渲染再转占位符
try:
    from app.services.engine import _parse_sql_params
    class _P:
        def __init__(s,n): s.name=n; s.required=False; s.default_value=None
    sql,_b = _parse_sql_params("WHERE 1=1 $if(len(prdId)>0)$ AND id IN :prdId $endif$", {}, [_P("prdId")])
    has_if = "$if" in sql
    print(f"[5] _parse_sql_params 实测 (不传 prdId):")
    print(f"    输出: {sql!r}")
    if has_if:
        print(f"    结果: 异常 ✗ —— $if$ 没被渲染，你的报错就是这里！部署的是旧代码")
    else:
        print(f"    结果: 正常 ✓ —— 当前代码没问题")
except Exception as e:
    print(f"[5] _parse_sql_params 测试失败: {e}")

print("=" * 64)
print("如果 [3]/[4]/[5] 任意一项是 ✗：")
print("  → 服务跑的是旧代码。检查:")
print("    a) 解压目录是否就是服务启动目录 (看 [2] 的路径)")
print("    b) ps aux | grep -E 'uvicorn|gunicorn|python' 看进程的工作目录")
print("    c) 杀掉所有相关进程后重新启动 (不要用 reload，直接重启)")
print("-" * 64)
print("如果报错含 \"Unknown column ... pipeline_steps\":")
print("  → 生产 MySQL 缺列。v1.7.6+ 启动时会自动补列，")
print("     只需用新代码重启服务即可；或手动执行:")
print("     ALTER TABLE src_dop_api_configs")
print("       ADD COLUMN pipeline_steps TEXT NULL;")
print("=" * 64)
