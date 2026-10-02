"""
管线引用表达式解析器。

支持的引用语法（用在 step.inputs 的值里）::

    ${var}              -> ctx[var]               一级取值
    ${step.field}       -> ctx[step][field]       字典/对象属性
    ${step.0}           -> ctx[step][0]           列表索引
    ${step.0.name}      -> ctx[step][0]["name"]   嵌套
    ${step.*.id}        -> [row["id"] for row in ctx[step]]   广播取一列
    ${step.*}           -> ctx[step]              整个列表

  组合使用::
    ${users.*.id}                -> 上一步 users 步骤的 id 列表 (list)
    ${order.0.total or 0}        -> 默认值兜底
    ${user.name}                 -> 直接取字段

设计要点:
  - 不引入复杂表达式（保持比模板引擎更窄的子集），降低出错概率
  - "纯 ${...}" 模式直接返回原值（保持类型），可用于把 list 传给 :ids
  - "嵌入文本" 模式（如 "prefix-${x}"）转字符串拼接
"""
from __future__ import annotations

import re
from typing import Any, Optional


# 整段就是一个 ${...} 引用：保留原值类型（不转 str）
_FULL_REF = re.compile(r"^\$\{([^}]+)\}$")
# 文本里嵌入的 ${...}
_EMBED_REF = re.compile(r"\$\{([^}]+)\}")


class RefError(Exception):
    """引用解析错误。"""


def _walk_path(ctx: dict, path: str) -> Any:
    """按 a.b.0.* 形式的路径在 ctx 中取值。"""
    tokens = path.split(".")
    cur: Any = ctx
    for i, tok in enumerate(tokens):
        if tok == "*":
            # 广播：cur 必须是 list，剩余路径对每个元素再 walk
            if not isinstance(cur, list):
                raise RefError(f"路径 .{tok} 需要列表，得到 {type(cur).__name__}")
            rest = ".".join(tokens[i + 1:])
            if not rest:
                return cur
            return [_walk_path({"_": item}, "_." + rest) for item in cur]
        # 数字索引
        if tok.isdigit():
            idx = int(tok)
            if not isinstance(cur, (list, tuple)):
                raise RefError(f"路径 .{tok} 需要列表，得到 {type(cur).__name__}")
            if idx >= len(cur):
                return None
            cur = cur[idx]
            continue
        # 字典 / 对象属性
        if isinstance(cur, dict):
            if tok not in cur:
                return None
            cur = cur[tok]
        elif hasattr(cur, tok):
            cur = getattr(cur, tok)
        else:
            return None
    return cur


def _eval_ref(expr: str, ctx: dict) -> Any:
    """求值一个 ${...} 内容。支持 `path` 或 `path or default`。"""
    expr = expr.strip()
    # 处理 "path or default" 兜底
    or_match = re.match(r"^(.*?)\s+or\s+(.+)$", expr)
    if or_match:
        path = or_match.group(1).strip()
        fallback_raw = or_match.group(2).strip()
        val = _walk_path(ctx, path) if path else None
        if val not in (None, "", [], {}):
            return val
        # 解析 fallback 字面量
        return _parse_literal(fallback_raw)
    return _walk_path(ctx, expr)


def _parse_literal(s: str) -> Any:
    """简单字面量解析：数字、字符串、true/false/null/空列表。"""
    if s == "null":
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s == "[]":
        return []
    if s == "{}":
        return {}
    # 字符串
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    # 数字
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s  # 当成字符串


def resolve(value: Any, ctx: dict) -> Any:
    """解析一个 inputs 值。

    - 如果是 dict / list：递归解析
    - 如果是 string 且整段是 ${...}：返回原值类型
    - 如果是 string 含嵌入 ${...}：字符串替换
    - 其他类型：原样返回
    """
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if not isinstance(value, str):
        return value

    # 纯引用：保留原值类型
    m = _FULL_REF.match(value)
    if m:
        return _eval_ref(m.group(1), ctx)

    # 嵌入引用：字符串拼接
    if "${" not in value:
        return value

    def replace(m):
        v = _eval_ref(m.group(1), ctx)
        if v is None:
            return ""
        return str(v)

    return _EMBED_REF.sub(replace, value)


# ============================================================
# 安全算术求值（给 transform op=compute 用）
# ============================================================
# 先把表达式里的 ${...} 引用替换成它们的值（数字直接内联，字符串加引号），
# 再用 ast 白名单求值。只允许 + - * / // % ** 和括号、数字字面量。
# 绝不 eval()，杜绝任意代码执行。

import ast as _ast
import operator as _op

def _safe_pow(a, b):
    """乘方加上限 (v2.20)：原来不限制，9 ** 9 ** 9 这类式子会在事件循环里算很久，
    期间整个进程的所有请求都被卡住。业务算术用不到这么大的指数。"""
    if abs(b) > 100 or (abs(a) > 1e6 and abs(b) > 10):
        raise RefError(f"compute 乘方过大：{a!r} ** {b!r}")
    return _op.pow(a, b)


_ALLOWED_BINOPS = {
    _ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul,
    _ast.Div: _op.truediv, _ast.FloorDiv: _op.floordiv,
    _ast.Mod: _op.mod, _ast.Pow: _safe_pow,
}
_ALLOWED_UNARY = {_ast.UAdd: _op.pos, _ast.USub: _op.neg}


def _eval_ast(node):
    if isinstance(node, _ast.Expression):
        return _eval_ast(node.body)
    # Python 3.8+: 常量
    if isinstance(node, _ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise RefError(f"compute 表达式只支持数字，得到 {node.value!r}")
    if isinstance(node, _ast.BinOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_BINOPS:
            raise RefError(f"compute 不支持的运算符: {op_type.__name__}")
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        try:
            return _ALLOWED_BINOPS[op_type](left, right)
        except ZeroDivisionError:
            raise RefError("compute 表达式除以 0")
    if isinstance(node, _ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_UNARY:
            raise RefError(f"compute 不支持的一元运算符: {op_type.__name__}")
        return _ALLOWED_UNARY[op_type](_eval_ast(node.operand))
    if isinstance(node, _ast.Paren) if hasattr(_ast, "Paren") else False:
        return _eval_ast(node.value)  # 兼容性兜底（实际括号在 BinOp 树里）
    raise RefError(f"compute 表达式含不允许的语法: {type(node).__name__}")


def eval_arithmetic(expr: str, ctx: dict) -> Any:
    """对一个算术表达式求值，表达式里可以用 ${step.path} 引用步骤结果。

    例: "${q1.0.cnt} + ${q2.0.cnt}"  ->  两步 count 相加
        "${a.0.x} * 1.0 / ${b.0.y}"  ->  比率

    引用值必须能转成数字，否则报错。
    """
    if not isinstance(expr, str) or not expr.strip():
        raise RefError("compute 需要非空表达式字符串")

    # 把每个 ${...} 替换成其数字值的字面量
    def _sub(m):
        v = _eval_ref(m.group(1), ctx)
        if v is None:
            raise RefError(f"compute 引用 ${{{m.group(1)}}} 求值为空（None）")
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)):
            return repr(v)
        # 尝试把字符串数字转过来
        try:
            return repr(float(v) if "." in str(v) else int(v))
        except (ValueError, TypeError):
            raise RefError(f"compute 引用 ${{{m.group(1)}}}={v!r} 不是数字，无法参与计算")

    substituted = _EMBED_REF.sub(_sub, expr)

    # 解析 + 白名单求值
    try:
        tree = _ast.parse(substituted, mode="eval")
    except SyntaxError as e:
        raise RefError(f"compute 表达式语法错误: {e.msg}（替换引用后: {substituted!r}）")
    return _eval_ast(tree)
