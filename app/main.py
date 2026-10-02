"""
OneData Portal - 主应用入口
"""

import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings, BASE_DIR
from app.core.database import engine, Base
from app.core.errors import ErrCode, R_fail
from app.core.logging import setup_logging, get_logger, set_trace_id, get_trace_id, is_debug
from starlette.datastructures import Headers, MutableHeaders

# 初始化日志系统（必须在最早期调用）
setup_logging(
    log_dir=settings.log.log_dir,
    level=settings.log.level,
    retention_days=settings.log.retention_days,
    file_level=settings.log.file_level,
)

log = get_logger("main")

# uvicorn 访问日志（log.access_log，默认关）：与网关日志、call_logs 重复，高并发下白白消耗 CPU。
# 在这里关而不是靠启动参数，`python main.py` 和直接 `uvicorn ...` 启动都生效
if not settings.log.access_log:
    import logging as _std_logging
    _std_logging.getLogger("uvicorn.access").disabled = True


async def _warmup_federated():
    """有多源 SQL API 时，后台预热其计算引擎（没有则不加载，不占内存）。"""
    import asyncio
    try:
        from sqlalchemy import func, select
        from app.core.database import async_session
        from app.models.models import ApiConfig
        async with async_session() as db:
            n = (await db.execute(select(func.count()).select_from(ApiConfig).where(ApiConfig.api_type == "federated"))).scalar()
        if n:
            from app.services import federated
            await asyncio.to_thread(federated.warmup)
    except Exception as e:  # noqa: BLE001 - 预热失败不影响启动，首个请求时再初始化
        log.warning(f"多源 SQL 预热失败（不影响使用）| {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    log.info("========== OneData Portal 启动中 ==========")
    log.info(f"配置: host={settings.app.host}, port={settings.app.port}, debug={settings.app.debug}")
    log.info(f"本地: http://localhost:{settings.app.port}")
    log.info(f"数据库: {settings.database.url[:50]}...")
    log.info(f"日志目录: {settings.log.log_dir}, 级别: {settings.log.level}, 保留: {settings.log.retention_days} 天")
    log.info(f"网关前缀: {settings.gateway.prefix}")

    # 启动时创建数据库表 + 自动迁移
    from app.core.database import init_db
    from app.core.runtime_env import CURRENT_ENV, IS_PRE, TABLE_SUFFIX
    log.info(f"运行环境: {CURRENT_ENV}" + (f"（表名后缀 {TABLE_SUFFIX}）" if IS_PRE else "（正式环境，无表名后缀）"))
    from app.core.runtime_env import env_port, startup_port
    _other = "prod" if IS_PRE else "pre"
    _port = startup_port() or settings.app.port
    if env_port(_other) and env_port(_other) == _port:
        log.warning(f"注意：当前以 {CURRENT_ENV} 环境运行，但端口 {_port} 在配置中属于 {_other} 环境，请确认启动参数")
    log.info("正在初始化数据库...")
    await init_db()
    log.info("数据库初始化完成（含自动迁移）")

    # 调用日志统计索引：后台补建，不阻塞启动（v2.19+）
    import asyncio
    from app.core.database import ensure_perf_indexes
    _index_task = asyncio.create_task(ensure_perf_indexes())
    from app.core.database import check_connection_budget
    await check_connection_budget(1 if settings.app.debug else settings.app.workers)
    _warmup_task = asyncio.create_task(_warmup_federated())  # noqa: F841 - 持有引用，避免任务被回收

    # 启动任务：备份正式核心数据 +（pre 环境）从正式表同步到空的 pre 表
    from app.core.backup import run_startup_tasks
    await run_startup_tasks()

    # 创建默认管理员用户
    from app.core.database import async_session
    from app.models.models import User
    from app.core.security import hash_password
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(select(User).where(User.username == settings.auth.default_admin))
        existing = result.scalar_one_or_none()
        if not existing:
            admin = User(
                nickname="管理员",
                username=settings.auth.default_admin,
                password_hash=hash_password(settings.auth.default_password),
                is_active=True,
                global_role="super_admin",
            )
            db.add(admin)
            await db.commit()
            log.info(f"默认超级管理员已创建: {settings.auth.default_admin}")
        else:
            # 兜底：老库升级后，确保默认管理员是超级管理员
            if getattr(existing, "global_role", "user") != "super_admin":
                existing.global_role = "super_admin"
                await db.commit()
                log.info(f"默认管理员已提升为超级管理员: {settings.auth.default_admin}")
            else:
                log.debug(f"默认超级管理员已存在: {settings.auth.default_admin}")

    # 调用日志批量写入（v2.19+）
    from app.services import call_log_writer
    call_log_writer.start(async_session)
    call_log_writer.start_retention(settings.monitor.call_log_retention_days)

    # 启动缓存自动预热调度器（仅 Redis 模式下生效）
    from app.services import cache_prewarm
    from app.core.database import async_session as _async_session
    cache_prewarm.start(_async_session)

    log.info(f"========== OneData Portal 启动成功 - {settings.app.host}:{settings.app.port} ==========")
    yield
    # 关闭
    log.info("========== OneData Portal 正在关闭 ==========")
    await cache_prewarm.stop()
    await call_log_writer.stop_retention()
    await call_log_writer.stop()
    # 释放业务数据源连接池（与系统库 engine 是两套，需分别关闭）
    try:
        from app.services.engine import close_all_mysql_pools
        await close_all_mysql_pools()
    except Exception as e:
        log.warning(f"关闭业务连接池异常 | error={str(e)}")
    await engine.dispose()
    log.info("数据库连接已关闭")


app = FastAPI(
    title=settings.app.title,
    version=settings.app.version,
    lifespan=lifespan,
)


# ========== CORS 跨域中间件 ==========
# 必须在其它中间件之前注册（Starlette 里先注册的中间件在最外层），
# 这样跨域预检请求(OPTIONS)才能在最外层被正确处理，不受下游中间件/路由影响。
# 默认允许所有来源跨域调用（本系统鉴权走 Authorization: Bearer token / API Key 头，
# 不依赖 cookie，所以 allow_credentials 保持 False 即可，不需要额外配置）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== 管理后台写请求：生产写保护 + 配置缓存失效 ==========
# 后注册的中间件在外层；请求日志中间件在它外面，被拦截的请求也有日志和 trace_id
from app.core.env_guard import AdminWriteMiddleware
app.add_middleware(AdminWriteMiddleware)


# ========== 请求日志中间件 ==========
class RequestLoggingMiddleware:
    """记录每个 HTTP 请求的日志，并在整个链路上传递 trace_id

    trace_id 来源优先级：
      1. 请求头 X-Trace-Id（前端/上游可主动透传，用于端到端追踪）
      2. 请求头 X-Request-Id（兼容常见网关）
      3. 服务端自动生成（uuid4 无连字符）

    v2.19: 由 @app.middleware("http")(BaseHTTPMiddleware) 改为纯 ASGI 实现，
    行为不变（响应头 X-Trace-Id、按状态码分级日志），每请求少一层任务调度和流包装。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        req_headers = Headers(scope=scope)
        incoming_trace = (req_headers.get("x-trace-id") or req_headers.get("x-request-id") or "").strip()
        trace_id = incoming_trace or uuid.uuid4().hex
        # 挂到 request.state（Starlette 的 request.state 即 scope["state"]），业务侧可直接读取
        scope.setdefault("state", {})["trace_id"] = trace_id
        # 写入 contextvar，loguru patcher 会把 trace_id 注入到本请求所有日志
        set_trace_id(trace_id)

        request_id = trace_id[:8]  # 控制台短显示（向后兼容）
        method = scope.get("method", "")
        path = scope.get("path", "")
        start_time = time.time()
        if is_debug():
            from app.core.client_ip import resolve_client_ip
            client_ip = resolve_client_ip(scope["client"][0] if scope.get("client") else "",
                                          req_headers.get("x-forwarded-for", ""))
            log.debug(
                f"[{request_id}] --> {method} {path} | IP={client_ip} | "
                f"Query={scope.get('query_string', b'').decode('latin-1')} | "
                f"UA={req_headers.get('user-agent', 'N/A')[:80]}"
            )

        status_code = 0

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # 把 trace_id 写到响应头，前端/调用方可直接拿到
                MutableHeaders(scope=message)["X-Trace-Id"] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            log.exception(f"[{request_id}] <-- EXCEPTION | {elapsed:.1f}ms | {method} {path} | {str(e)}")
            raise

        elapsed = (time.time() - start_time) * 1000
        # 根据状态码选择日志级别
        if status_code >= 500:
            log.error(f"[{request_id}] <-- {status_code} | {elapsed:.1f}ms | {method} {path}")
        elif status_code >= 400:
            log.warning(f"[{request_id}] <-- {status_code} | {elapsed:.1f}ms | {method} {path}")
        else:
            log.debug(f"[{request_id}] <-- {status_code} | {elapsed:.1f}ms | {method} {path}")


app.add_middleware(RequestLoggingMiddleware)


# ========== 全局异常处理：统一响应格式（带 code） ==========
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """将 HTTPException 转为统一格式 {status, code, msg, data}"""
    # 根据 HTTP 状态码映射到错误码
    code_map = {
        401: ErrCode.AUTH_TOKEN_INVALID,
        403: ErrCode.AUTH_PERMISSION_DENIED,
        404: ErrCode.SYSTEM_NOT_FOUND,
        422: ErrCode.SYSTEM_PARAM_INVALID,
        429: ErrCode.GW_RATE_LIMITED,
    }
    err_code = code_map.get(exc.status_code, ErrCode.SYSTEM_ERROR)

    # 如果 detail 是 dict 且包含 code，使用它
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        err_code = exc.detail["code"]
        detail_msg = exc.detail.get("msg", str(exc.detail))
    else:
        detail_msg = str(exc.detail)

    trace_id = getattr(request.state, "trace_id", get_trace_id())
    log.warning(f"HTTP异常 | status={exc.status_code} | code={err_code} | msg={detail_msg} | path={request.url.path}")

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": False,
            "code": int(err_code),
            "msg": detail_msg,
            "data": None,
            "trace_id": trace_id,
        },
        headers={"X-Trace-Id": trace_id},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """捕获未处理异常，统一返回格式"""
    trace_id = getattr(request.state, "trace_id", get_trace_id())
    log.exception(f"未处理异常 | path={request.url.path} | error={str(exc)}")
    body = R_fail(ErrCode.SYSTEM_ERROR, msg=str(exc) or "服务器内部错误")
    # 在统一响应里附带 trace_id，便于前端展示和上报
    try:
        body["trace_id"] = trace_id
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content=body,
        headers={"X-Trace-Id": trace_id},
    )


# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# 注册 API 路由
from app.api import auth, projects, api_configs, datasources, monitor, test_api, gateway, views, users, sql_tools, \
    system, members, approvals, plugin_libraries, owner_approvals, project_variables, releases, catalog

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(api_configs.router)
app.include_router(datasources.router)
app.include_router(monitor.router)
app.include_router(test_api.router)
app.include_router(gateway.router)
app.include_router(users.router)
app.include_router(sql_tools.router)
app.include_router(system.router)
app.include_router(views.router)
app.include_router(members.router)
app.include_router(approvals.router)
app.include_router(owner_approvals.router)
app.include_router(project_variables.router)
app.include_router(plugin_libraries.router)
app.include_router(releases.router)
app.include_router(catalog.router)
