# -*- coding: utf-8 -*-
"""
执行链路 Trace 上下文 (v2.4)

设计目标：把「日志建档」与「关键节点回填」解耦。

  - 网关层（gateway）负责日志记录的整个生命周期：请求进入即建档，
    执行完成后统一落库。
  - 业务逻辑层（engine / 各执行器）不再直接 new CallLog / db.add，
    而是通过 contextvar 拿到当前请求的 TraceContext，往里写关键节点
    （渲染后 SQL、执行 SQL、各阶段耗时、行数、缓存命中、错误堆栈）。

这样逻辑层只关心「记录了什么」，不关心「记录到哪、何时提交」，
且天然按 trace_id 串联 —— 应用日志里的 trace=xxx 与调用日志表的
trace_id、响应头 X-Trace-Id 三者一致。

contextvar 使每个并发请求各自独立，不会串数据。
"""
import time
import contextvars
from typing import Optional, Any


class TraceContext:
    """单次 API 调用的关键节点收集器。"""

    __slots__ = (
        "trace_id", "started_at",
        "rendered_sql", "executed_sql",
        "render_time_ms", "query_time_ms",
        "row_count", "cache_hit", "error_stack",
        "_render_started",
    )

    def __init__(self, trace_id: str = ""):
        self.trace_id = trace_id or ""
        self.started_at = time.time()
        self.rendered_sql = ""
        self.executed_sql = ""
        self.render_time_ms = 0.0
        self.query_time_ms = 0.0
        self.row_count = 0
        self.cache_hit = False
        self.error_stack = ""
        self._render_started = None

    # ---- 关键节点写入 ----
    def set_rendered_sql(self, sql: str):
        if sql:
            self.rendered_sql = str(sql)[:8000]

    def set_executed_sql(self, sql: str, binds: Optional[dict] = None):
        if not sql:
            return
        text = str(sql)
        if binds:
            # 附带绑定值，便于排查（截断，避免超长）
            try:
                import json
                text += "\n/* binds: " + json.dumps(binds, default=str, ensure_ascii=False)[:1500] + " */"
            except Exception:
                pass
        self.executed_sql = text[:8000]

    def mark_render_start(self):
        self._render_started = time.time()

    def mark_render_end(self):
        if self._render_started is not None:
            self.render_time_ms = (time.time() - self._render_started) * 1000
            self._render_started = None

    def add_query_time(self, ms: float):
        self.query_time_ms += max(0.0, ms)

    def set_row_count(self, n: int):
        try:
            self.row_count = int(n)
        except Exception:
            pass

    def set_cache_hit(self, hit: bool = True):
        self.cache_hit = bool(hit)

    def set_error_stack(self, stack: str):
        if stack:
            self.error_stack = str(stack)[:4000]

    def as_log_fields(self) -> dict:
        """收集到的节点，按 CallLog 列名返回（供批量写入用）。"""
        return {
            "rendered_sql": self.rendered_sql,
            "executed_sql": self.executed_sql,
            "render_time_ms": self.render_time_ms,
            "query_time_ms": self.query_time_ms,
            "row_count": self.row_count,
            "cache_hit": self.cache_hit,
            "error_stack": self.error_stack,
        }

    def apply_to_log(self, log_entry) -> None:
        """把收集到的节点写入 CallLog 实例（字段不存在则跳过，兼容旧库）。"""
        for field in ("rendered_sql", "executed_sql", "render_time_ms",
                      "query_time_ms", "row_count", "cache_hit", "error_stack"):
            if hasattr(log_entry, field):
                setattr(log_entry, field, getattr(self, field))


# 当前请求的 trace 上下文（每个协程/请求独立）
_current_trace: contextvars.ContextVar[Optional[TraceContext]] = \
    contextvars.ContextVar("current_trace", default=None)


def set_trace(ctx: Optional[TraceContext]) -> contextvars.Token:
    return _current_trace.set(ctx)


def reset_trace(token: contextvars.Token) -> None:
    try:
        _current_trace.reset(token)
    except Exception:
        pass


def get_trace() -> Optional[TraceContext]:
    """取当前 trace 上下文；不在请求链路内时返回 None（调用方需容错）。"""
    return _current_trace.get()
