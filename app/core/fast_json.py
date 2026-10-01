# -*- coding: utf-8 -*-
"""
快速 JSON 序列化 (v2.19+)
========================

FastAPI 端点直接 return dict 时，会先用 jsonable_encoder 把整个结构递归走一遍
（纯 Python，逐个值判断类型），再交给 JSONResponse 做 json.dumps。
网关返回成千上万行数据时，这一遍递归是最大的 CPU 开销之一。

这里改为直接用 C 实现的 json.dumps 序列化，只有遇到 json 不认识的类型
（Decimal / timedelta / set 等）才回退到 jsonable_encoder 处理那一个值。
输出与「return dict」完全一致（同样的 separators / ensure_ascii / allow_nan），
调用方无感知。
"""
import json
from decimal import Decimal
from typing import Any

from fastapi.encoders import decimal_encoder, jsonable_encoder
from fastapi.responses import JSONResponse


def json_default(obj: Any) -> Any:
    """json.dumps 的 default 钩子：与 FastAPI 的类型转换规则保持一致
    （Decimal -> int/float、timedelta -> 秒数、set -> list ...）。"""
    if type(obj) is Decimal:
        # 数据库 DECIMAL 列最常见，直接走 FastAPI 同一个转换函数，省掉 jsonable_encoder 的层层类型判断
        return decimal_encoder(obj)
    return jsonable_encoder(obj)


def dumps_compat(content: Any) -> str:
    """与 FastAPI JSONResponse 渲染结果一致的序列化。"""
    return json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
        default=json_default,
    )


class FastJSONResponse(JSONResponse):
    """跳过 jsonable_encoder 预处理的 JSONResponse。端点需直接 return 本类实例。"""

    def render(self, content: Any) -> bytes:
        return dumps_compat(content).encode("utf-8")


def dumps_preview(data: Any, limit: int) -> str:
    """等价于 json.dumps(data, default=str, ensure_ascii=False)[:limit]，
    但对列表只序列化够用的前若干项，避免为了截取几千个字符把上万行结果整体序列化。"""
    if not isinstance(data, list):
        return json.dumps(data, default=str, ensure_ascii=False)[:limit]
    parts = []
    length = 1  # 已拼出的 "[" + ", ".join(parts) 的长度
    for item in data:
        piece = json.dumps(item, default=str, ensure_ascii=False)
        length += len(piece) + (2 if parts else 0)
        parts.append(piece)
        if length >= limit:
            # 完整结果以这段为前缀，截到 limit 即与整体序列化后截断一致
            return ("[" + ", ".join(parts))[:limit]
    return ("[" + ", ".join(parts) + "]")[:limit]
