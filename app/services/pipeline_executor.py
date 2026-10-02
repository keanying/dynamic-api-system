"""
多步骤 API 执行器 (Pipeline Executor)

一个 API 内部可以串联多个步骤，每步可以查询不同数据源；后续步骤通过 ${prev_step.x}
引用前面步骤的结果，最后选一个步骤的输出作为最终返回。

Step 配置 (dict)::

    {
      "name": "users",                  # 步骤名（在引用中使用）
      "type": "sql",                    # 当前支持 sql / transform
      "datasource": "biz_db",           # 数据源名（type=sql 时必填）
      "sql": "SELECT ...",              # SQL 模板（type=sql 时必填）
      "inputs": {                       # 覆盖给本步的参数（可引用前序步骤）
        "user_ids": "${users.*.id}",
        "limit": 100
      },
      "single_row": false               # 结果是否取首行
    }

Transform 步骤（内存数据处理）::

    {
      "name": "merged",
      "type": "transform",
      "op": "join",                     # join / map / filter / pick
      "left":  "${users}",
      "right": "${orders}",
      "on": {"left": "id", "right": "user_id"},
      "select": ["id", "name", {"total": "${right.total or 0}"}]
    }

API 配置增加 return 字段指定最终返回的 step 名。如果不指定，默认使用最后一步。
"""
from __future__ import annotations

import json
import time
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import DataSource
from app.services.pipeline_ref import resolve, RefError
from app.services.sql_template import SqlTplError
# _parse_sql_params / _execute_mysql 等从原 engine 模块复用

log = logging.getLogger(__name__)


class PipelineError(Exception):
    """管线执行错误，调用方会把 msg 返回给 API 调用者。"""

    def __init__(self, step_name: str, msg: str):
        self.step_name = step_name
        self.msg = msg
        super().__init__(f"步骤 [{step_name}] {msg}")


def parse_pipeline(raw: str | list) -> list[dict]:
    """把数据库存的 pipeline_steps JSON 文本解析成 list[dict]。"""
    if not raw:
        return []
    if isinstance(raw, list):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PipelineError("(parse)", f"pipeline_steps 不是合法 JSON: {e}")
    if not isinstance(data, list):
        raise PipelineError("(parse)", "pipeline_steps 顶层必须是数组")
    # 基础字段校验
    seen_names = set()
    for i, step in enumerate(data):
        if not isinstance(step, dict):
            raise PipelineError(f"#{i}", "step 必须是 object")
        name = step.get("name")
        if not name:
            raise PipelineError(f"#{i}", "缺少 name 字段")
        if name in seen_names:
            raise PipelineError(name, "步骤名重复")
        seen_names.add(name)
        if step.get("type", "sql") not in ("sql", "transform"):
            raise PipelineError(name, f"未知 type: {step.get('type')}")
    return data


async def execute_pipeline(
    steps: list[dict],
    params: dict,
    *,
    datasource_resolver,    # async (name_or_id) -> DataSource
    sql_runner,             # async (ds, sql, bound, timeout, max_rows) -> list[dict]
    timeout: int = 30,
    max_rows: int = 10000,
    return_step: str | None = None,
) -> tuple[Any, list[dict]]:
    """
    串行执行管线。

    返回 (data, step_results) 元组:
      data:         最终返回的数据（默认取最后一步）
      step_results: 每步的元数据，用于 preview/debug:
                    [{"name": str, "type": str, "elapsed_ms": int, "row_count": int}, ...]

    参数:
      datasource_resolver: 注入函数，按数据源 name 或 id 取出 DataSource ORM 行；
                           保持本模块对数据库会话的解耦
      sql_runner:          注入函数，把 SQL 实际发去数据库执行
                           保持本模块对具体驱动（aiomysql / asyncpg）的解耦
    """
    ctx: dict[str, Any] = {"params": params}   # 引用解析的初始上下文，含原始 params
    step_results: list[dict] = []

    for i, step in enumerate(steps):
        step_name = step["name"]
        step_type = step.get("type", "sql")
        t0 = time.perf_counter()

        try:
            if step_type == "sql":
                rows = await _exec_sql_step(
                    step, ctx, datasource_resolver, sql_runner, timeout, max_rows
                )
                result_data: Any = rows
                if step.get("single_row"):
                    result_data = rows[0] if rows else None
            elif step_type == "transform":
                result_data = _exec_transform_step(step, ctx)
            else:
                raise PipelineError(step_name, f"未知 step type: {step_type}")

        except PipelineError:
            raise
        except (SqlTplError, RefError, ValueError) as e:
            raise PipelineError(step_name, str(e))
        except Exception as e:
            log.error(f"步骤 [{step_name}] 执行异常: {e}", exc_info=True)
            raise PipelineError(step_name, f"{type(e).__name__}: {e}")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        row_count = len(result_data) if isinstance(result_data, list) else (1 if result_data is not None else 0)

        ctx[step_name] = result_data
        step_results.append({
            "name": step_name,
            "type": step_type,
            "elapsed_ms": elapsed_ms,
            "row_count": row_count,
        })

    # 选择最终返回
    if return_step:
        if return_step not in ctx:
            raise PipelineError(return_step, f"return_step 引用的步骤不存在")
        final_data = ctx[return_step]
    elif step_results:
        final_data = ctx[step_results[-1]["name"]]
    else:
        final_data = None

    return final_data, step_results


# ============================================================
# SQL 步骤
# ============================================================

async def _exec_sql_step(step: dict, ctx: dict, datasource_resolver, sql_runner, timeout: int, max_rows: int):
    sql = step.get("sql")
    if not sql:
        raise PipelineError(step["name"], "缺少 sql 字段")
    ds_ref = step.get("datasource")
    if not ds_ref:
        raise PipelineError(step["name"], "缺少 datasource 字段")

    # 解析 inputs（值里可以引用前序步骤）
    raw_inputs = step.get("inputs") or {}
    try:
        resolved_inputs = resolve(raw_inputs, ctx)
    except RefError as e:
        raise PipelineError(step["name"], f"inputs 引用错误: {e}")
    if not isinstance(resolved_inputs, dict):
        raise PipelineError(step["name"], "inputs 必须是 dict")

    # 把原始 params 也喂进去（用户 SQL 里可能引用 :outer_param）
    merged_params = {**ctx.get("params", {}), **resolved_inputs}

    # 拿数据源
    ds = await datasource_resolver(ds_ref)
    if ds is None:
        raise PipelineError(step["name"], f"数据源 {ds_ref!r} 不存在")

    # 同源不同库：step 可用 database 字段覆盖数据源默认库。
    # 复用同一份连接信息（host/port/账号/密码/类型），只切换 db。
    override_db = step.get("database")
    if override_db:
        from app.services.ds_scope import check_database_override
        try:
            check_database_override(override_db)   # v2.21：不允许切到平台系统库 / mysql 等系统库
        except Exception as e:
            raise PipelineError(step["name"], str(e))
        ds = _clone_ds_with_db(ds, override_db)

    # 复用 engine 的 _parse_sql_params 处理模板渲染 + IN 展开 + LIKE 后缀
    from app.services.engine import _parse_sql_params
    final_sql, bound = _parse_sql_params(sql, merged_params, [])  # 不强制 api_params 定义

    # 实际执行
    rows = await sql_runner(ds, final_sql, bound, timeout, max_rows)
    return rows


class _DsProxy:
    """轻量代理：除 database_name 外，所有属性透传给原 DataSource。

    用于「同源不同库」——同一连接信息换库查询，不污染原 ORM 对象。
    """
    __slots__ = ("_ds", "_db")

    def __init__(self, ds, db):
        object.__setattr__(self, "_ds", ds)
        object.__setattr__(self, "_db", db)

    def __getattr__(self, item):
        if item == "database_name":
            return object.__getattribute__(self, "_db")
        return getattr(object.__getattribute__(self, "_ds"), item)


def _clone_ds_with_db(ds, db_name: str):
    """返回一个 database_name 被替换的数据源视图。"""
    return _DsProxy(ds, db_name)


# ============================================================
# Transform 步骤（内存数据处理）
# ============================================================

def _exec_transform_step(step: dict, ctx: dict) -> Any:
    """支持几种常用变换:
        op=join       左右两表按字段合并
        op=aggregate  分组聚合 sum/count/avg/min/max
        op=compute    标量算术（跨步骤结果做 + - * / 等运算）
        op=combine    把多个引用/计算组装成一行多字段对象（返回多字段用这个）
        op=map        对每行做投影
        op=filter     按条件过滤
        op=pick       取首行（同 single_row）
    """
    op = step.get("op")
    _SUPPORTED_OPS = ("join", "aggregate", "compute", "combine", "mergerows",
                      "unpivot", "map", "filter", "pick")
    if op == "join":
        return _op_join(step, ctx)
    if op == "aggregate":
        return _op_aggregate(step, ctx)
    if op == "compute":
        return _op_compute(step, ctx)
    if op == "combine":
        return _op_combine(step, ctx)
    if op == "mergerows":
        return _op_mergerows(step, ctx)
    if op == "unpivot":
        return _op_unpivot(step, ctx)
    if op == "map":
        return _op_map(step, ctx)
    if op == "filter":
        return _op_filter(step, ctx)
    if op == "pick":
        src = resolve(step.get("from"), ctx)
        if isinstance(src, list):
            return src[0] if src else None
        return src
    raise PipelineError(
        step["name"],
        f"未知 transform op: {op!r}。当前版本支持: {', '.join(_SUPPORTED_OPS)}。"
        f"（若确认配置无误，可能是后端代码未更新到最新版，请重新部署并重启服务）"
    )


def _op_combine(step: dict, ctx: dict):
    """把多个步骤结果/引用/算术组装成一行多字段对象（或多行）。

    配置::
        {
          "name": "result",
          "op": "combine",
          "fields": {
            "income":  "${q1.0.dailyTicketIncome}",   纯引用
            "orders":  "${q2.0.cnt}",
            "total":   "${q1.0.cnt} + ${q2.0.cnt}",    支持算术
            "label":   "今日汇总"                        常量
          },
          "is_return": true
        }
    返回: [{"income":.., "orders":.., "total":.., "label":..}]

    fields 的值规则:
      - 含 ${...} 且整体是一个算术式 → 走安全算术求值（同 compute）
      - 单个 ${ref} → 取引用值（保留类型）
      - 其它 → 当常量原样放入
    """
    fields = step.get("fields")
    if not fields or not isinstance(fields, dict):
        raise PipelineError(step["name"], "combine 需要 fields（对象：字段名→引用/算术/常量）")

    from app.services.pipeline_ref import resolve as _resolve, eval_arithmetic

    row = {}
    for key, expr in fields.items():
        if isinstance(expr, str) and "${" in expr:
            # 判断是不是纯单引用 ${...}（首尾就是一个引用，无额外运算符）
            s = expr.strip()
            is_single_ref = (
                s.startswith("${") and s.endswith("}")
                and s.count("${") == 1 and s.count("}") == 1
            )
            if is_single_ref:
                row[key] = _resolve(s, ctx)
            else:
                # 含运算符的算术式，复用安全算术求值
                try:
                    row[key] = eval_arithmetic(expr, ctx)
                except Exception as e:
                    raise PipelineError(step["name"], f"字段 {key} 计算失败: {e}")
        else:
            # 常量（数字/字符串/布尔等）
            row[key] = expr
    return [row]


def _op_compute(step: dict, ctx: dict):
    """标量算术计算。

    配置::
        {
          "name": "total",
          "type": "transform",
          "op": "compute",
          "expr": "${q1.0.cnt} + ${q2.0.cnt}",   # 引用前序步骤的值做算术
          "as": "total",                          # 可选：包成 [{as: 结果}] 返回
          "is_return": true
        }

    expr 里用 ${步骤.路径} 引用值，支持 + - * / // % ** 和括号。
    - 不写 as：直接返回标量结果（如 6）
    - 写 as：返回 [{"<as>": 结果}]，方便前端按表格渲染
    """
    expr = step.get("expr")
    if not expr:
        raise PipelineError(step["name"], "compute 需要 expr 字段")
    from app.services.pipeline_ref import eval_arithmetic
    try:
        result = eval_arithmetic(expr, ctx)
    except Exception as e:
        raise PipelineError(step["name"], f"compute 求值失败: {e}")
    as_name = step.get("as")
    if as_name:
        return [{as_name: result}]
    return result


def _op_mergerows(step: dict, ctx: dict) -> list[dict]:
    """多个结果集按 key 字段横向合并成多行（情况B：按维度分多行）。

    场景：q1=各部门收入 [{dept:A,income:100},{dept:B,income:80}]
          q2=各部门订单 [{dept:A,cnt:10},{dept:B,cnt:7}]
      想合成: [{dept:A,income:100,cnt:10},{dept:B,income:80,cnt:7}]

    配置::
        {
          "op": "mergerows",
          "key": "dept",                       # 按哪个字段对齐（也可 ["d1","d2"] 多字段联合键）
          "sources": ["${q1}", "${q2}", ...],  # 要合并的多个结果集
          "fill": 0,                           # 某源缺这行时的填充值（默认 null）
          "is_return": true
        }
    输出：所有源里出现过的 key 各一行，字段是各源字段的并集。
    """
    key = step.get("key")
    if not key:
        raise PipelineError(step["name"], "mergerows 需要 key（对齐字段名）")
    keys = key if isinstance(key, list) else [key]
    src_refs = step.get("sources")
    if not src_refs or not isinstance(src_refs, list):
        raise PipelineError(step["name"], "mergerows 需要 sources（结果集引用数组）")
    fill = step.get("fill", None)

    def _kt(row):
        return tuple(row.get(k) for k in keys)

    order = []          # 保持首次出现顺序
    merged: dict = {}
    all_fields: list = []
    for ref in src_refs:
        rows = resolve(ref, ctx)
        if rows is None:
            continue
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise PipelineError(step["name"], f"mergerows 源 {ref!r} 不是列表/对象")
        for r in rows:
            if not isinstance(r, dict):
                continue
            kt = _kt(r)
            if kt not in merged:
                merged[kt] = {}
                order.append(kt)
            merged[kt].update(r)
            for f in r:
                if f not in all_fields:
                    all_fields.append(f)

    out = []
    for kt in order:
        row = merged[kt]
        # 补齐所有源的字段，缺的用 fill
        full = {}
        for f in all_fields:
            full[f] = row.get(f, fill)
        out.append(full)
    return out


def _op_unpivot(step: dict, ctx: dict) -> list[dict]:
    """把多个标量值/字段转成「名称-值」多行（情况C：指标转行）。

    场景：q1 算出收入100，q2 算出订单50，q3 算出退款20
      想要: [{指标:"收入",值:100},{指标:"订单",值:50},{指标:"退款",值:20}]

    配置::
        {
          "op": "unpivot",
          "name_field": "指标",      # 名称列叫什么（默认 "name"）
          "value_field": "值",       # 值列叫什么（默认 "value"）
          "items": {                 # 每项 = 一行
            "收入": "${q1.0.v}",     # 值可引用、可算术、可常量
            "订单": "${q2.0.cnt}",
            "退款": "${q3.0.amt}",
            "净额": "${q1.0.v} - ${q3.0.amt}"
          },
          "is_return": true
        }
    输出：items 每个条目一行，共 N 行。
    """
    items = step.get("items")
    if not items or not isinstance(items, dict):
        raise PipelineError(step["name"], "unpivot 需要 items（名称→引用/算术/常量）")
    nf = step.get("name_field", "name")
    vf = step.get("value_field", "value")

    from app.services.pipeline_ref import resolve as _resolve, eval_arithmetic
    out = []
    for label, expr in items.items():
        if isinstance(expr, str) and "${" in expr:
            s = expr.strip()
            is_single = (s.startswith("${") and s.endswith("}")
                         and s.count("${") == 1 and s.count("}") == 1)
            if is_single:
                val = _resolve(s, ctx)
            else:
                try:
                    val = eval_arithmetic(expr, ctx)
                except Exception as e:
                    raise PipelineError(step["name"], f"项 {label} 计算失败: {e}")
        else:
            val = expr
        out.append({nf: label, vf: val})
    return out


def _op_aggregate(step: dict, ctx: dict) -> list[dict]:
    """分组聚合，类似 SQL GROUP BY。

    配置::
        {
          "op": "aggregate",
          "from": "${orders}",
          "group_by": ["dept_id"],           # 不填则全表聚成 1 行
          "aggregations": {
              "order_count":  {"fn": "count"},
              "total_amount": {"fn": "sum", "field": "amount"},
              "avg_amount":   {"fn": "avg", "field": "amount"},
              "max_amount":   {"fn": "max", "field": "amount"},
              "min_amount":   {"fn": "min", "field": "amount"}
          }
        }

    输出：每个分组一行，含 group_by 字段 + 各聚合结果。
    """
    src = resolve(step.get("from"), ctx) or []
    if not isinstance(src, list):
        raise PipelineError(step["name"], "aggregate 的 from 必须是列表")

    group_by = step.get("group_by") or []
    if isinstance(group_by, str):
        group_by = [group_by]
    aggs = step.get("aggregations") or {}
    if not aggs:
        raise PipelineError(step["name"], "aggregate 需要 aggregations 定义")

    # 校验聚合函数
    valid_fns = {"count", "sum", "avg", "min", "max"}
    for out_name, spec in aggs.items():
        fn = (spec or {}).get("fn")
        if fn not in valid_fns:
            raise PipelineError(step["name"], f"聚合 {out_name} 的 fn={fn!r} 非法（支持 {valid_fns}）")
        if fn != "count" and not spec.get("field"):
            raise PipelineError(step["name"], f"聚合 {out_name} 的 fn={fn} 需要指定 field")

    # 分组
    groups: dict = {}
    group_order: list = []
    for row in src:
        if not isinstance(row, dict):
            continue
        key = tuple(row.get(g) for g in group_by) if group_by else ("__all__",)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(row)

    def _nums(rows, field):
        out = []
        for r in rows:
            v = r.get(field)
            if v is None:
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    result: list[dict] = []
    for key in group_order:
        rows = groups[key]
        out_row: dict = {}
        # 还原分组字段
        for i, g in enumerate(group_by):
            out_row[g] = key[i]
        # 算聚合
        for out_name, spec in aggs.items():
            fn = spec["fn"]
            if fn == "count":
                out_row[out_name] = len(rows)
                continue
            nums = _nums(rows, spec["field"])
            if not nums:
                out_row[out_name] = 0 if fn == "sum" else None
                continue
            if fn == "sum":
                out_row[out_name] = round(sum(nums), 10)
            elif fn == "avg":
                out_row[out_name] = round(sum(nums) / len(nums), 10)
            elif fn == "min":
                out_row[out_name] = min(nums)
            elif fn == "max":
                out_row[out_name] = max(nums)
        result.append(out_row)
    return result


def _op_join(step: dict, ctx: dict) -> list[dict]:
    left = resolve(step.get("left"), ctx) or []
    right = resolve(step.get("right"), ctx) or []
    on = step.get("on") or {}
    lk = on.get("left"); rk = on.get("right")
    if not lk or not rk:
        raise PipelineError(step["name"], "join 需要 on.left 和 on.right")
    join_type = step.get("join_type", "left")  # left / inner

    # 建右表索引
    right_index: dict[Any, list[dict]] = {}
    for r in right:
        key = r.get(rk) if isinstance(r, dict) else None
        right_index.setdefault(key, []).append(r)

    select = step.get("select")  # 可选投影
    out: list[dict] = []
    for l in left:
        if not isinstance(l, dict):
            continue
        key = l.get(lk)
        matches = right_index.get(key, [])
        if not matches:
            if join_type == "inner":
                continue
            matches = [None]
        for r in matches:
            merged = _project_row(l, r, select, ctx)
            out.append(merged)
    return out


def _project_row(left: dict, right: dict | None, select, ctx: dict) -> dict:
    """按 select 规则投影 (left, right) 两行。

    select 可以是:
      None         -> {**left, **right or {}}（右表覆盖左表同名字段）
      list[str]    -> 只保留这些字段（先从 left 取，否则从 right 取）
      list[dict]   -> 每个 dict 描述一个字段名: 表达式
    """
    if select is None:
        if right is None:
            return dict(left)
        return {**left, **right}

    out: dict[str, Any] = {}
    for item in select:
        if isinstance(item, str):
            if item in left:
                out[item] = left[item]
            elif right and item in right:
                out[item] = right[item]
            else:
                out[item] = None
        elif isinstance(item, dict):
            # 投影上下文：把 left/right 暴露出来供 ${left.x} / ${right.x} 引用
            scope = {**ctx, "left": left, "right": right or {}}
            for k, v in item.items():
                out[k] = resolve(v, scope)
        else:
            raise RefError(f"select 项必须是 str 或 dict: {item!r}")
    return out


def _op_map(step: dict, ctx: dict) -> list[dict]:
    src = resolve(step.get("from"), ctx) or []
    if not isinstance(src, list):
        raise PipelineError(step["name"], "map 的 from 必须是列表")
    select = step.get("select") or []
    out = []
    for row in src:
        if not isinstance(row, dict):
            continue
        scope = {**ctx, "row": row}
        new_row: dict[str, Any] = {}
        for item in select:
            if isinstance(item, str):
                new_row[item] = row.get(item)
            elif isinstance(item, dict):
                for k, v in item.items():
                    new_row[k] = resolve(v, scope)
        out.append(new_row)
    return out


def _op_filter(step: dict, ctx: dict) -> list[dict]:
    src = resolve(step.get("from"), ctx) or []
    if not isinstance(src, list):
        raise PipelineError(step["name"], "filter 的 from 必须是列表")
    # 简单 filter: 给一组字段相等条件
    where = step.get("where") or {}
    resolved_where = {k: resolve(v, ctx) for k, v in where.items()}
    return [row for row in src if isinstance(row, dict)
            and all(row.get(k) == v for k, v in resolved_where.items())]
