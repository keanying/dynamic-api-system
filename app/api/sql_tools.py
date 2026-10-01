"""
SQL 模板辅助接口
- POST /api/sql/preview: 用给定参数渲染 SQL 模板，返回最终 SQL + 条件块求值情况
- POST /api/sql/parse-params: 从 SQL 模板里提取所有 :param 占位符
"""
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.errors import R_ok, R_fail, ErrCode
from app.core.logging import get_logger
from app.services.sql_template import (
    render_template, extract_placeholders, eval_expr,
    SqlTplError, collect_expressions,
)
from app.api.auth import get_current_user

log = get_logger("sql_tools")

router = APIRouter(prefix="/api/sql", tags=["SQL工具"])


class PreviewRequest(BaseModel):
    sql_template: str
    params: dict = {}


def _sql_literal(v):
    """把一个 Python 值转成 SQL 字面量（仅用于预览展示，不用于真实执行）。"""
    import datetime
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple, set)):
        # IN (:list) 场景：展开成 (a, b, c)
        return "(" + ", ".join(_sql_literal(x) for x in v) + ")"
    if isinstance(v, (datetime.datetime, datetime.date)):
        return "'" + str(v) + "'"
    # 字符串：转义单引号
    return "'" + str(v).replace("'", "''") + "'"


def _fill_params_for_preview(sql: str, params: dict) -> str:
    """把 SQL 里的 :param 占位符替换成参数字面值，供预览查看。
    处理几种形态：
      IN :name / IN (:name)   -> IN (v1, v2, ...)
      ':name' / ":name"       -> 字面量（占位符被引号包着时，连同外层引号一起替换，
                                  避免出现 ''val'' 双引号；真实执行请不要给占位符加引号）
      = :name  等标量          -> = 'val'
    仅做展示用途；真实执行仍是参数化绑定。"""
    import re
    if not params:
        return sql
    # 先按参数名长度倒序，避免 :ab 误伤 :abc
    names = sorted(params.keys(), key=len, reverse=True)
    out = sql
    for name in names:
        val = params[name]
        lit = _sql_literal(val)
        if isinstance(val, (list, tuple, set)):
            # IN (:name) -> IN (...)：去掉多余括号避免双层
            out = re.sub(r"\(\s*:" + re.escape(name) + r"\s*\)", lit, out)
        else:
            # 占位符被单/双引号包着（写成 ':name' 或 ":name"）：连引号一起替换成字面量，
            # 否则会得到 ''val'' 这种双引号。
            out = re.sub(r"'\s*:" + re.escape(name) + r"\s*'", lit, out)
            out = re.sub(r'"\s*:' + re.escape(name) + r'\s*"', lit, out)
        # 常规 :name -> 字面量（用词边界避免误替换 :name2）
        out = re.sub(r":" + re.escape(name) + r"\b", lit, out)
    return out


@router.post("/preview")
async def preview(req: PreviewRequest, _user=Depends(get_current_user)):
    """根据当前参数渲染模板，预览最终 SQL。

    返回:
      rendered_sql: 渲染完成的 SQL
      placeholders: 渲染后仍存在的 :param 占位符列表
      expressions:  每个 $if/$elseif/$for 块的表达式 + 求值结果，便于看每条规则的命中情况
    """
    try:
        # 先收集所有控制块表达式做"求值快照"
        expressions = []
        for kind, expr in collect_expressions(req.sql_template):
            try:
                value = eval_expr(expr, req.params)
                # for 循环表达式：把集合长度显示出来更直观
                if kind == "for":
                    # expr 形如 "item in items" —— 取右侧 in 之后部分再单独 eval
                    rhs = expr.split(" in ", 1)[1] if " in " in expr else expr
                    rhs_val = eval_expr(rhs, req.params)
                    cnt = len(rhs_val) if isinstance(rhs_val, (list, tuple, set, str)) else (1 if rhs_val else 0)
                    expressions.append({"kind": kind, "expr": expr, "value": True, "iter_count": cnt})
                else:
                    expressions.append({"kind": kind, "expr": expr, "value": bool(value)})
            except SqlTplError as e:
                return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"表达式错误 [{expr}]: {e}")

        # v1.5: 收集 :{expr} 生成的绑定值，随预览一起返回
        tpl_binds: dict = {}
        rendered = render_template(req.sql_template, req.params, collect_binds=tpl_binds)
        placeholders = extract_placeholders(rendered)

        # 参数字面填充预览：把 :param 用实际值替换成字面量，方便直接看"填了参数后的完整 SQL"。
        # 注意：这只是预览展示，真实执行仍走参数化绑定（防 SQL 注入），二者行为一致但形式不同。
        preview_sql = _fill_params_for_preview(rendered, {**req.params, **tpl_binds})

        return R_ok(data={
            "rendered_sql": rendered,
            "preview_sql": preview_sql,     # 参数值已填入的完整 SQL（仅供查看/调试）
            "placeholders": placeholders,
            "expressions": expressions,
            "generated_binds": tpl_binds,   # :{expr} 自动生成的参数化绑定值
        })
    except SqlTplError as e:
        log.warning(f"SQL 模板渲染失败: {e}")
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"模板错误: {e}")
    except Exception as e:
        log.error(f"SQL 预览异常: {e}", exc_info=True)
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))


class ParseRequest(BaseModel):
    sql_template: str


@router.post("/parse-params")
async def parse_params(req: ParseRequest, _user=Depends(get_current_user)):
    """从 SQL 模板里抓出所有 :param 占位符（不渲染条件块，所以 $if$ 内的占位符也会包含）。"""
    # 模板里所有 :param —— 但需要先去掉字符串字面量 & 注释 防止误抓
    s = req.sql_template
    s = re.sub(r"'(?:[^'\\]|\\.)*'", "", s)
    s = re.sub(r'"(?:[^"\\]|\\.)*"', "", s)
    s = re.sub(r"--[^\n]*", "", s)
    s = re.sub(r"#[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    names = []
    seen = set()
    for m in re.finditer(r":([a-zA-Z_][a-zA-Z0-9_]*)", s):
        n = m.group(1)
        if n not in seen:
            seen.add(n); names.append(n)
    return R_ok(data={"params": names})
