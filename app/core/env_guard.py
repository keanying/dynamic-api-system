# -*- coding: utf-8 -*-
"""
生产环境写保护 (v2.18+)
======================

生产环境下，管理后台的写操作（/api/** 的 POST/PUT/PATCH/DELETE）只允许
管理员（超级管理员 / 管理员）执行；其他人只能查看。
正常的改动路径是：预发环境修改并验证 → 发布到生产 → 生产管理员核对差异后审核。

不受影响：
  - 预发环境（不做任何限制）
  - 对外网关 /v1/data/**（业务调用，不是管理操作）
  - 页面、静态资源、所有 GET 请求
  - 下面白名单里的「只读性质」的 POST（登录、在线测试、SQL 预览、连接测试等）
"""
import re
from typing import Optional

from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import ErrCode
from app.core.logging import get_logger
from app.core.runtime_env import IS_PROD

log = get_logger("env_guard")

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 生产环境里非管理员也可以调用的写方法接口（都不修改平台配置）
_ALLOW_PATTERNS = [
    re.compile(r"^/api/auth/"),                                    # 登录 / 登出
    re.compile(r"^/api/test/"),                                    # API 在线测试
    re.compile(r"^/api/sql/"),                                     # SQL 预览渲染 / 参数解析
    re.compile(r"^/api/projects/\d+/variables/preview-template$"), # 项目变量预览
    re.compile(r"^/api/datasources/\d+/test$"),                    # 数据源连接测试
]

READONLY_MSG = "生产环境仅管理员可操作。请在预发环境修改并验证后「发布到生产」，由生产管理员审核上线"


def _deny(status: int, code: ErrCode, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"status": False, "code": int(code), "msg": msg, "data": None})


def _needs_guard(method: str, path: str) -> bool:
    if not IS_PROD or method not in _WRITE_METHODS:
        return False
    if not path.startswith("/api/"):
        return False
    gw = settings.gateway.prefix.rstrip("/")
    if gw and path.startswith(gw + "/"):
        return False
    return not any(p.match(path) for p in _ALLOW_PATTERNS)


async def _check_admin(scope) -> Optional[JSONResponse]:
    """生产环境写请求的身份检查；放行返回 None，否则返回拒绝响应。"""
    from starlette.requests import Request

    from app.core.database import async_session
    from app.core.permissions import is_admin_or_above
    from app.core.security import decode_jwt_token
    from app.models.models import User

    request = Request(scope)
    token = request.cookies.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    payload = decode_jwt_token(token) if token else None
    if not payload:
        # 未登录/过期仍返回 401，前端据此跳登录页
        return _deny(401, ErrCode.AUTH_TOKEN_INVALID, "未登录或登录已过期")

    async with async_session() as db:
        user = (await db.execute(select(User).where(User.id == payload.get("user_id")))).scalar_one_or_none()

    if not user or not user.is_active:
        return _deny(401, ErrCode.AUTH_TOKEN_INVALID, "未登录或登录已过期")
    if not is_admin_or_above(user):
        log.warning(f"生产写保护拦截 | user={user.username} | {request.method} {request.url.path}")
        return _deny(403, ErrCode.AUTH_PERMISSION_DENIED, READONLY_MSG)
    return None


class AdminWriteMiddleware:
    """管理后台写请求的统一入口（纯 ASGI 中间件，比 @app.middleware 开销小）：
      1. 生产环境写保护（见模块说明）
      2. 写请求处理完后清空网关接口配置缓存，改完配置立即生效
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope["method"].upper()
        path = scope["path"]
        if method not in _WRITE_METHODS or not path.startswith("/api/"):
            return await self.app(scope, receive, send)

        if _needs_guard(method, path):
            denied = await _check_admin(scope)
            if denied is not None:
                return await denied(scope, receive, send)
        from app.services import gateway_cache
        try:
            await self.app(scope, receive, send)
        finally:
            # 写操作提交后清一次；处理期间并发的网关请求可能按旧配置回填缓存，
            # 那种情况最多持续 config_cache_ttl 秒
            gateway_cache.invalidate()
