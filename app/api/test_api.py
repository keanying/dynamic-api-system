"""
API 在线测试路由
"""

import time
import json
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.config import settings
from app.core.logging import get_logger
from app.models.models import ApiConfig, CallLog
from app.schemas.schemas import TestApiRequest
from app.services.engine import execute_api
from app.services.trace_context import TraceContext, set_trace, reset_trace
from app.api.auth import get_current_user

log = get_logger("test_api")

router = APIRouter(prefix="/api/test", tags=["API测试"])


@router.post("/{api_id}")
async def test_api_execution(
    api_id: int,
    req: TestApiRequest,
    request: Request,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """在线测试 API 执行"""
    log.info(f"在线测试 API | api_id={api_id} | params={req.params}")

    result = await db.execute(select(ApiConfig).where(ApiConfig.id == api_id))
    api_config = result.scalar_one_or_none()
    if not api_config:
        log.warning(f"在线测试失败: API 不存在 | api_id={api_id}")
        return R_fail(ErrCode.API_NOT_FOUND)

    # v2.20：原来没有任何权限校验——任何登录用户都能执行任意项目的 API，
    # 包括数据同步（写库）类 API
    from app.core.permissions import is_project_member, is_super_admin
    if not await is_project_member(db, _user, api_config.project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权测试该 API")
    if (api_config.api_type or "sql").lower() == "sync" and not is_super_admin(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="远端同步类 API 只能由超级管理员测试")

    log.debug(f"在线测试 API 详情 | name={api_config.name} | url_path={api_config.url_path} | method={api_config.method}")

    client_ip = request.client.host if request.client else "test"
    trace_id = getattr(request.state, "trace_id", "") or ""

    # v2.4: 与网关一致的「建档 + 回填」模式，call_source=test
    start_time = time.time()
    tctx = TraceContext(trace_id=trace_id)
    _tok = set_trace(tctx)
    try:
        exec_result = await execute_api(api_config, req.params, client_ip, db, call_source="test", trace_id=trace_id)
    finally:
        reset_trace(_tok)

    elapsed = (time.time() - start_time) * 1000
    try:
        ok = bool(exec_result["status"])
        log_entry = CallLog(
            api_id=api_config.id,
            project_id=api_config.project_id,
            api_name=api_config.name,
            url_path=api_config.url_path,
            method=api_config.method,
            request_params=json.dumps(req.params, default=str, ensure_ascii=False)[:5000],
            response_status="success" if ok else "error",
            response_time_ms=elapsed,
            status_code=200 if ok else 500,
            error_message="" if ok else str(exec_result.get("msg", ""))[:2000],
            client_ip=client_ip,
            response_data=json.dumps(exec_result.get("data"), default=str, ensure_ascii=False)[:5000] if ok else "",
            is_slow_query=elapsed >= settings.query.slow_query_threshold,
            call_source="test",
            trace_id=trace_id,
        )
        tctx.apply_to_log(log_entry)
        db.add(log_entry)
    except Exception as log_err:
        log.warning(f"记录测试调用日志失败 | trace_id={trace_id} | error={str(log_err)}")

    if exec_result["status"]:
        log.info(f"在线测试成功 | api_id={api_id} | name={api_config.name}")
    else:
        log.warning(f"在线测试失败 | api_id={api_id} | name={api_config.name} | msg={exec_result['msg']}")

    return {
        "status": exec_result["status"],
        "code": 0 if exec_result["status"] else ErrCode.GW_SQL_EXECUTE_FAILED.value,
        "msg": exec_result["msg"],
        "data": exec_result["data"],
        "trace_id": trace_id,
    }
