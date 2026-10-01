# -*- coding: utf-8 -*-
"""
项目环境变量求值 (v2.14+)
========================

用途：缓存预热时，参数里的时间不能写死。写死会导致「明天还在预热昨天的数据」——
缓存是热的，但内容已经过期，用户明天查今天照样是冷查询。

解决办法：参数模板里用 ${变量名} 引用项目环境变量，预热时实时求值。
例如模板：
    {"supplierId": [1757026], "startTime": "${TODAY} 00:00:00", "endTime": "${NOW}"}
每天预热求值出来的都是「当天」的时间范围，永远不会过期。

支持的变量类型见 ProjectVariable 模型的 docstring。
"""
import datetime
import json
import re
from typing import Any, Dict, List

from app.core.logging import get_logger

log = get_logger("project_vars")

# 各类型的默认输出格式
_DEFAULT_FORMATS = {
    "date": "%Y-%m-%d",
    "datetime": "%Y-%m-%d %H:%M:%S",
    "week_start": "%Y-%m-%d",
    "week_end": "%Y-%m-%d",
    "month_start": "%Y-%m-%d",
    "month_end": "%Y-%m-%d",
    "now": "%Y-%m-%d %H:%M:%S",
    "day_start": "%Y-%m-%d %H:%M:%S",
    "day_end": "%Y-%m-%d %H:%M:%S",
}

# 变量类型清单（供前端下拉展示）
VAR_TYPES = [
    {"value": "date",        "label": "日期",          "hint": "按天偏移，如 offset=-1 表示昨天"},
    {"value": "datetime",    "label": "日期时间",       "hint": "精确到秒，按天偏移"},
    {"value": "now",         "label": "当前时刻",       "hint": "求值那一刻的时间，忽略偏移"},
    {"value": "day_start",   "label": "某天 00:00:00",  "hint": "按天偏移，offset=0 今天零点"},
    {"value": "day_end",     "label": "某天 23:59:59",  "hint": "按天偏移"},
    {"value": "week_start",  "label": "周一",           "hint": "按周偏移，offset=-1 上周一"},
    {"value": "week_end",    "label": "周日",           "hint": "按周偏移"},
    {"value": "month_start", "label": "月初(1号)",      "hint": "按月偏移，offset=-1 上月一号"},
    {"value": "month_end",   "label": "月末(最后一天)",  "hint": "按月偏移"},
    {"value": "const",       "label": "固定值",         "hint": "直接使用填写的固定字符串"},
]


def _add_months(d: datetime.date, months: int) -> datetime.date:
    """按月偏移，自动处理跨年和月末日期不存在的情况（如 1/31 减一月）。"""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # 该月最后一天
    if m == 12:
        last = 31
    else:
        last = (datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)).day
    return datetime.date(y, m, min(d.day, last))


def eval_variable(var, now: datetime.datetime = None) -> str:
    """求值单个变量，返回字符串。"""
    now = now or datetime.datetime.now()
    vtype = (var.var_type or "date").strip()
    offset = int(var.offset_days or 0)
    fmt = (var.date_format or "").strip() or _DEFAULT_FORMATS.get(vtype, "%Y-%m-%d")

    if vtype == "const":
        return var.const_value or ""

    if vtype == "now":
        return now.strftime(fmt)

    today = now.date()

    if vtype in ("date", "datetime"):
        target = now + datetime.timedelta(days=offset)
        return target.strftime(fmt)

    if vtype == "day_start":
        d = today + datetime.timedelta(days=offset)
        return datetime.datetime.combine(d, datetime.time(0, 0, 0)).strftime(fmt)

    if vtype == "day_end":
        d = today + datetime.timedelta(days=offset)
        return datetime.datetime.combine(d, datetime.time(23, 59, 59)).strftime(fmt)

    if vtype == "week_start":
        monday = today - datetime.timedelta(days=today.weekday())
        monday = monday + datetime.timedelta(weeks=offset)
        return monday.strftime(fmt)

    if vtype == "week_end":
        monday = today - datetime.timedelta(days=today.weekday())
        sunday = monday + datetime.timedelta(days=6) + datetime.timedelta(weeks=offset)
        return sunday.strftime(fmt)

    if vtype == "month_start":
        first = _add_months(today.replace(day=1), offset)
        return first.strftime(fmt)

    if vtype == "month_end":
        first = _add_months(today.replace(day=1), offset)
        if first.month == 12:
            nxt = datetime.date(first.year + 1, 1, 1)
        else:
            nxt = datetime.date(first.year, first.month + 1, 1)
        last = nxt - datetime.timedelta(days=1)
        return last.strftime(fmt)

    log.warning(f"未知变量类型 {vtype}，按日期处理 | var={var.name}")
    return (now + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")


def build_var_map(variables: List[Any], now: datetime.datetime = None) -> Dict[str, str]:
    """把项目变量列表求值成 {变量名: 值} 映射。"""
    now = now or datetime.datetime.now()
    out = {}
    for v in variables:
        try:
            out[v.name] = eval_variable(v, now)
        except Exception as e:
            log.warning(f"变量求值失败 | name={getattr(v,'name','?')} | error={str(e)}")
    return out


_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute_in_str(s: str, var_map: Dict[str, str]) -> str:
    """替换字符串里的 ${VAR}。未定义的变量原样保留，便于排查。"""
    def _rep(m):
        name = m.group(1)
        if name in var_map:
            return var_map[name]
        log.warning(f"模板引用了未定义的变量 ${{{name}}}，已原样保留")
        return m.group(0)
    return _VAR_PATTERN.sub(_rep, s)


def substitute(obj: Any, var_map: Dict[str, str]) -> Any:
    """递归替换任意结构（dict/list/str）里的 ${VAR}。

    只替换字符串值；数字、布尔等原样保留。
    如果整个字符串就是一个变量引用（如 "${TODAY}"），仍返回字符串
    ——因为时间类变量本来就是字符串，不做类型猜测以免出错。
    """
    if isinstance(obj, str):
        return _substitute_in_str(obj, var_map)
    if isinstance(obj, dict):
        return {k: substitute(v, var_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, var_map) for v in obj]
    return obj


def render_template(template_text: str, variables: List[Any],
                    now: datetime.datetime = None):
    """把预热参数覆盖(JSON 文本) + 项目变量 求值成最终结果。

    支持两种写法，与预热调度器保持一致：
      单组：{"startTime": "${LAST_7D}"}          -> 返回 dict
      多组：[{"startTime": "${LAST_1D}"}, ...]   -> 返回 list[dict]

    解析失败时抛异常，由调用方决定怎么处理。
    """
    if not template_text or not template_text.strip():
        return {}
    var_map = build_var_map(variables, now)
    data = json.loads(template_text)

    if isinstance(data, dict):
        return substitute(data, var_map)

    if isinstance(data, list):
        groups = [g for g in data if isinstance(g, dict)]
        if not groups:
            raise ValueError("参数覆盖数组里没有有效的对象元素")
        return [substitute(g, var_map) for g in groups]

    raise ValueError(
        '参数覆盖须是 JSON 对象或对象数组，'
        '形如 {"startTime":"${LAST_7D}"} 或 [{"startTime":"${LAST_1D}"},{"startTime":"${LAST_7D}"}]'
    )
