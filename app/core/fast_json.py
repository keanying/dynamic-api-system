# -*- coding: utf-8 -*-
"""
JSON 序列化工具 (v2.19+)
=======================

与 FastAPI「端点 return dict」完全一致的类型转换和输出格式，供需要自行序列化的地方使用
（网关结果缓存 app/services/result_cache.py）。
"""
import json
from decimal import Decimal
from typing import Any

from fastapi.encoders import decimal_encoder, jsonable_encoder


def json_default(obj: Any) -> Any:
    """json.dumps / orjson.dumps 的 default 钩子：与 FastAPI 的类型转换规则保持一致
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
