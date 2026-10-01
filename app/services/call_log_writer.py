# -*- coding: utf-8 -*-
"""
调用日志批量写入 (v2.19+)
========================

原来每个网关请求都在自己的事务里 INSERT 一条 call_logs 再 COMMIT：
一次提交就是一次刷盘（InnoDB 默认 innodb_flush_log_at_trx_commit=1），
并且整个过程占着系统库连接、算在请求耗时里。

现在网关只把日志放进内存队列，后台任务每隔约 1 秒（或攒够一批）
用一条多行 INSERT 写入。代价：
  - 日志在「调用日志」页面最多晚 1 秒左右出现；
  - 进程被 kill -9 时，队列里尚未落库的日志会丢失（正常停止会先写完）；
  - 系统库长时间不可用导致队列满时，丢弃新日志并告警，不影响接口本身。
"""
import asyncio
import time
from typing import List, Optional

from sqlalchemy import insert

from app.core.logging import get_logger
from app.core.timezone import now as _cst_now

log = get_logger("call_log_writer")

_QUEUE_MAX = 20000
_BATCH_MAX = 500
_FLUSH_INTERVAL = 1.0

_queue: Optional[asyncio.Queue] = None
_task: Optional[asyncio.Task] = None
_session_factory = None
_dropped = 0
_last_drop_warn = 0.0


def start(session_factory) -> None:
    global _queue, _task, _session_factory
    _session_factory = session_factory
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_run())
        log.info("调用日志批量写入已启动")


def submit(fields: dict) -> None:
    """登记一条调用日志（非阻塞）。fields 为 CallLog 的列名 -> 值。"""
    global _dropped, _last_drop_warn
    if _queue is None or _session_factory is None:
        # 未启动（例如脚本里直接调用）：由调用方自行决定，这里退回不记录
        log.warning("调用日志写入器未启动，日志未记录")
        return
    fields.setdefault("created_at", _cst_now())
    try:
        _queue.put_nowait(fields)
    except asyncio.QueueFull:
        _dropped += 1
        now = time.monotonic()
        if now - _last_drop_warn > 10:
            _last_drop_warn = now
            log.error(f"调用日志队列已满，已丢弃 {_dropped} 条（系统库写入跟不上或不可用）")


_write_lock = asyncio.Lock()
_inflight: Optional[asyncio.Future] = None


async def _write(batch: List[dict]) -> None:
    from app.models.models import CallLog
    async with _write_lock:
        try:
            async with _session_factory() as db:
                await db.execute(insert(CallLog), batch)
                await db.commit()
        except Exception as e:  # noqa: BLE001
            log.error(f"调用日志批量写入失败，本批 {len(batch)} 条丢弃 | error={e}")


def _drain(first: dict) -> List[dict]:
    batch = [first]
    while len(batch) < _BATCH_MAX:
        try:
            batch.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return batch


async def _run() -> None:
    while True:
        first = await _queue.get()
        try:
            # 等一小会儿让同一时段的日志攒成一批（已经攒够就不等）
            if _queue.qsize() < _BATCH_MAX:
                await asyncio.sleep(_FLUSH_INTERVAL)
        except asyncio.CancelledError:
            await _write(_drain(first))
            raise
        # shield：停止时正在写的这一批照样写完（stop 会等它结束）
        global _inflight
        _inflight = asyncio.ensure_future(_write(_drain(first)))
        await asyncio.shield(_inflight)


async def stop() -> None:
    """停止后台任务，并把队列里剩余日志全部写完。"""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    if _inflight is not None and not _inflight.done():   # 等正在进行的写入结束
        await _inflight
    if _queue is not None:
        while not _queue.empty():
            await _write(_drain(_queue.get_nowait()))
    log.info("调用日志批量写入已停止（队列已写完）")


# ========== 调用日志自动清理 (v2.19+) ==========
# call_logs 每次调用一行，带响应预览和两段 SQL 文本，单行可达十几 KB，且原来没有任何
# 自动清理，表会无限增长，拖慢所有统计查询和写入。配置 monitor.call_log_retention_days > 0
# 后，每小时删除一次超过保留天数的日志；分批删除，避免一次大事务长时间锁表。
_retention_task: Optional[asyncio.Task] = None
_RETENTION_BATCH = 5000


async def _purge_once(days: int) -> int:
    from sqlalchemy import select, delete
    from app.core.timezone import days_ago
    from app.models.models import CallLog

    cutoff = days_ago(days)
    total = 0
    while True:
        async with _session_factory() as db:
            ids = (await db.execute(
                select(CallLog.id).where(CallLog.created_at < cutoff).limit(_RETENTION_BATCH)
            )).scalars().all()
            if not ids:
                break
            await db.execute(delete(CallLog).where(CallLog.id.in_(ids)))
            await db.commit()
        total += len(ids)
        await asyncio.sleep(0.2)   # 给正常写入让路
    return total


async def _retention_loop(days: int) -> None:
    while True:
        try:
            n = await _purge_once(days)
            if n:
                log.info(f"调用日志自动清理 | 删除 {n} 条 {days} 天前的日志")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error(f"调用日志自动清理失败 | error={e}")
        await asyncio.sleep(3600)


def start_retention(days: int) -> None:
    global _retention_task
    if days <= 0 or _session_factory is None:
        return
    if _retention_task is None or _retention_task.done():
        _retention_task = asyncio.get_running_loop().create_task(_retention_loop(days))
        log.info(f"调用日志自动清理已启用 | 保留 {days} 天")


async def stop_retention() -> None:
    global _retention_task
    if _retention_task is not None:
        _retention_task.cancel()
        try:
            await _retention_task
        except asyncio.CancelledError:
            pass
        _retention_task = None
