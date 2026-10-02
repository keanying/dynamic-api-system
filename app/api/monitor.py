"""
监控统计与日志管理路由
"""

import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, delete, extract

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.models.models import ApiConfig, CallLog
from app.schemas.schemas import DashboardStats, CallLogOut
from app.api.auth import get_current_user
from app.core.timezone import now as cst_now, today_start as cst_today_start, days_ago as cst_days_ago

log = get_logger("monitor")

router = APIRouter(prefix="/api/monitor", tags=["监控统计"])


@router.get("/dashboard")
async def get_dashboard(
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """获取仪表盘统计数据"""
    log.debug("查询仪表盘统计数据")

    total_apis = (await db.execute(
        select(func.count()).select_from(ApiConfig)
    )).scalar() or 0

    # v2.19: 总数 / 平均耗时 / 失败数 / 今日数 合并成一次扫描（原来对 call_logs 扫 4 遍）
    today_start = cst_today_start()
    total_calls, avg_time, error_count, today_calls = (await db.execute(
        select(
            func.count(),
            func.avg(CallLog.response_time_ms),
            func.sum(case((CallLog.response_status == "error", 1), else_=0)),
            func.sum(case((CallLog.created_at >= today_start, 1), else_=0)),
        ).select_from(CallLog)
    )).one()
    total_calls = total_calls or 0
    avg_time = avg_time or 0
    error_count = int(error_count or 0)
    today_calls = int(today_calls or 0)
    failure_rate = round(error_count / total_calls * 100, 2) if total_calls > 0 else 0

    stats = DashboardStats(
        total_apis=total_apis,
        total_calls=total_calls,
        avg_response_time=round(avg_time, 2),
        failure_rate=failure_rate,
        today_calls=today_calls,
    )

    log.debug(f"仪表盘统计完成 | APIs={total_apis} | 总调用={total_calls} | 今日={today_calls} | 失败率={failure_rate}%")
    return R_ok(data=stats.model_dump())


@router.get("/api-latency-rank")
async def get_api_latency_rank(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """API 耗时排行榜"""
    log.debug(f"查询 API 耗时排行 | limit={limit}")

    result = await db.execute(
        select(
            CallLog.api_id,
            CallLog.api_name,
            CallLog.url_path,
            func.avg(CallLog.response_time_ms).label("avg_time"),
            func.count().label("call_count"),
        )
        .group_by(CallLog.api_id, CallLog.api_name, CallLog.url_path)
        .order_by(func.avg(CallLog.response_time_ms).desc())
        .limit(limit)
    )
    rows = result.all()

    items = [
        {
            "api_id": r.api_id,
            "api_name": r.api_name,
            "url_path": r.url_path,
            "value": round(r.avg_time, 2),
            "call_count": r.call_count,
        }
        for r in rows
    ]
    log.debug(f"耗时排行查询完成 | 返回={len(items)}条")
    return R_ok(data=items)


@router.get("/api-call-rank")
async def get_api_call_rank(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """API 调用次数排行榜"""
    log.debug(f"查询 API 调用排行 | limit={limit}")

    result = await db.execute(
        select(
            CallLog.api_id,
            CallLog.api_name,
            CallLog.url_path,
            func.count().label("call_count"),
            func.avg(CallLog.response_time_ms).label("avg_time"),
        )
        .group_by(CallLog.api_id, CallLog.api_name, CallLog.url_path)
        .order_by(func.count().desc())
        .limit(limit)
    )
    rows = result.all()

    items = [
        {
            "api_id": r.api_id,
            "api_name": r.api_name,
            "url_path": r.url_path,
            "value": r.call_count,
            "call_count": r.call_count,
            "avg_time": round(r.avg_time, 2),
        }
        for r in rows
    ]
    log.debug(f"调用排行查询完成 | 返回={len(items)}条")
    return R_ok(data=items)


@router.get("/call-trend")
async def get_call_trend(
    period: str = Query("hour", description="统计周期: hour/day"),
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """调用趋势图数据"""
    log.debug(f"查询调用趋势 | period={period} | days={days}")

    since = cst_days_ago(days)

    # 按小时 / 按天分桶。原来固定用 SQLite 的 strftime，系统库是 MySQL 时报 FUNCTION strftime does not exist
    fmt = "%Y-%m-%d %H:00" if period == "hour" else "%Y-%m-%d"
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        bucket = func.strftime(fmt, CallLog.created_at)
    elif dialect == "postgresql":
        bucket = func.to_char(CallLog.created_at, "YYYY-MM-DD HH24:00" if period == "hour" else "YYYY-MM-DD")
    else:
        bucket = func.date_format(CallLog.created_at, fmt)
    result = await db.execute(
        select(
            bucket.label("time_bucket"),
            func.count().label("call_count"),
            func.sum(case((CallLog.response_status == "error", 1), else_=0)).label("error_count"),
            func.avg(CallLog.response_time_ms).label("avg_time"),
        )
        .where(CallLog.created_at >= since)
        .group_by(bucket)
        .order_by(bucket)
    )

    rows = result.all()
    items = [
        {
            "time": r.time_bucket,
            "call_count": r.call_count,
            "error_count": r.error_count or 0,
            "avg_time": round(r.avg_time, 2) if r.avg_time else 0,
        }
        for r in rows
    ]
    log.debug(f"调用趋势查询完成 | 返回={len(items)}个时间点")
    return R_ok(data=items)


@router.get("/today-hourly")
async def get_today_hourly(
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """今日调用分布（按小时，0-23 点补齐，没有调用的整点为 0）。"""
    now = cst_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # v2.19: 在数据库里按小时分组聚合（原来把今天的每一条日志都拉回 Python 再归桶，
    # 日调用量百万级时要传输百万行）。extract('hour') 在 MySQL/SQLite 上都可用。
    hour_col = extract("hour", CallLog.created_at)
    result = await db.execute(
        select(
            hour_col.label("h"),
            func.count().label("cnt"),
            func.sum(case((CallLog.response_status == "error", 1), else_=0)).label("err"),
            func.sum(func.coalesce(CallLog.response_time_ms, 0)).label("rt_sum"),
        )
        .where(CallLog.created_at >= today_start)
        .group_by(hour_col)
    )

    buckets = [{"hour": h, "call_count": 0, "error_count": 0, "_sum": 0.0} for h in range(24)]
    for h, cnt, err, rt_sum in result.all():
        if h is None:
            continue
        h = int(h)
        if 0 <= h <= 23:
            b = buckets[h]
            b["call_count"] += int(cnt or 0)
            b["error_count"] += int(err or 0)
            b["_sum"] += float(rt_sum or 0)

    items = [{
        "hour": b["hour"],
        "label": f"{b['hour']:02d}:00",
        "call_count": b["call_count"],
        "error_count": b["error_count"],
        "success_count": b["call_count"] - b["error_count"],
        "avg_time": round(b["_sum"] / b["call_count"], 1) if b["call_count"] else 0,
    } for b in buckets]

    total = sum(b["call_count"] for b in buckets)
    peak = max(buckets, key=lambda x: x["call_count"]) if total else None
    log.debug(f"今日调用分布查询完成 | total={total}")
    return R_ok(data={
        "items": items,
        "total": total,
        "peak_hour": peak["hour"] if peak and peak["call_count"] else None,
        "peak_count": peak["call_count"] if peak else 0,
    })


@router.get("/slow-queries")
async def get_slow_queries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """慢查询列表"""
    log.debug(f"查询慢查询列表 | page={page}")

    query = select(CallLog).where(CallLog.is_slow_query == True).order_by(CallLog.created_at.desc())

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    logs = result.scalars().all()

    items = [CallLogOut.model_validate(l).model_dump() for l in logs]
    log.debug(f"慢查询列表查询完成 | total={total} | 返回={len(items)}条")
    return R_ok(data={"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/logs")
async def get_call_logs(
    api_id: int = Query(0, description="按 API 筛选"),
    status: str = Query("all", description="状态筛选: all/success/error"),
    call_source: str = Query("all", description="来源筛选: all/gateway/test"),
    start_date: str = Query("", description="开始日期 YYYY-MM-DD"),
    end_date: str = Query("", description="结束日期 YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """调用日志列表"""
    log.debug(f"查询调用日志 | api_id={api_id} | status={status} | call_source={call_source} | page={page}")

    query = select(CallLog)

    if api_id > 0:
        query = query.where(CallLog.api_id == api_id)
    if status != "all":
        query = query.where(CallLog.response_status == status)
    if call_source != "all":
        query = query.where(CallLog.call_source == call_source)
    if start_date:
        try:
            sd = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            query = query.where(CallLog.created_at >= sd)
        except ValueError:
            log.warning(f"无效的开始日期格式 | start_date={start_date}")
    if end_date:
        try:
            ed = datetime.datetime.strptime(end_date, "%Y-%m-%d") + datetime.timedelta(days=1)
            query = query.where(CallLog.created_at < ed)
        except ValueError:
            log.warning(f"无效的结束日期格式 | end_date={end_date}")

    query = query.order_by(CallLog.created_at.desc())

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    logs = result.scalars().all()

    items = [CallLogOut.model_validate(l).model_dump() for l in logs]
    log.debug(f"调用日志查询完成 | total={total} | 返回={len(items)}条")
    return R_ok(data={"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/logs/{log_id}")
async def get_log_detail(
    log_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """日志详情"""
    log.debug(f"查询日志详情 | log_id={log_id}")

    result = await db.execute(select(CallLog).where(CallLog.id == log_id))
    call_log = result.scalar_one_or_none()
    if not call_log:
        log.warning(f"日志不存在 | log_id={log_id}")
        return R_fail(ErrCode.LOG_NOT_FOUND)

    return R_ok(data=CallLogOut.model_validate(call_log).model_dump())


@router.delete("/logs/cleanup")
async def cleanup_logs(
    days: int = Query(30, ge=1, description="清理多少天前的日志"),
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """清理过期日志"""
    log.info(f"清理过期日志请求 | days={days}")

    cutoff = cst_days_ago(days)
    result = await db.execute(
        delete(CallLog).where(CallLog.created_at < cutoff)
    )
    count = result.rowcount
    log.info(f"过期日志清理完成 | 清理了 {count} 条 | 截止日期={cutoff}")
    return R_ok(msg=f"已清理 {count} 条日志")
