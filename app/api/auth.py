"""
认证路由：登录、登出、当前用户
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import hash_password, verify_password, create_jwt_token, decode_jwt_token
from app.core.config import settings
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.models.models import User
from app.schemas.schemas import LoginRequest, LoginResponse

log = get_logger("auth")

router = APIRouter(prefix="/api/auth", tags=["认证"])


def token_revoked(payload: dict, user) -> bool:
    """签发时间早于用户 token_epoch（修改密码时更新）的凭证作废 (v2.21)。
    原来 JWT 无状态，改密码后旧 token 在过期前（默认 24 小时）仍然有效。"""
    return int(payload.get("iat", 0) or 0) < int(getattr(user, "token_epoch", 0) or 0)


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db, scope="function")) -> User:
    """从请求中提取并验证当前用户"""
    token = request.cookies.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        log.warning(f"认证失败: 缺少 Token | path={request.url.path}")
        raise HTTPException(status_code=401, detail={"code": ErrCode.AUTH_TOKEN_MISSING, "msg": "未登录"})

    payload = decode_jwt_token(token)
    if not payload:
        log.warning(f"认证失败: Token 无效或已过期 | path={request.url.path}")
        raise HTTPException(status_code=401, detail={"code": ErrCode.AUTH_TOKEN_INVALID, "msg": "Token 无效或已过期"})

    user_id = payload.get("user_id")
    log.debug(f"Token 解析成功 | user_id={user_id} | username={payload.get('username')}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        log.warning(f"认证失败: 用户不存在 | user_id={user_id}")
        raise HTTPException(status_code=401, detail={"code": ErrCode.USER_NOT_FOUND, "msg": "用户不存在"})
    if not user.is_active:
        log.warning(f"认证失败: 用户已禁用 | user_id={user_id} | username={user.username}")
        raise HTTPException(status_code=401, detail={"code": ErrCode.AUTH_USER_DISABLED, "msg": "用户已被禁用"})
    # v2.23 用户表合并后，同一个 id 可能换了人：凭证里的账号对不上就作废
    if payload.get("username") and payload.get("username") != user.username:
        log.warning(f"认证失败: 凭证账号与用户不一致 | user_id={user_id} | token={payload.get('username')} | user={user.username}")
        raise HTTPException(status_code=401, detail={"code": ErrCode.AUTH_TOKEN_INVALID, "msg": "登录已失效，请重新登录"})
    if token_revoked(payload, user):
        log.warning(f"认证失败: 密码已修改，旧凭证作废 | user_id={user_id} | username={user.username}")
        raise HTTPException(status_code=401, detail={"code": ErrCode.AUTH_TOKEN_INVALID, "msg": "密码已修改，请重新登录"})

    log.debug(f"认证成功 | user_id={user.id} | username={user.username}")
    return user


@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db, scope="function")):
    """用户登录"""
    log.info(f"登录请求 | username={req.username}")

    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()

    if not user:
        log.warning(f"登录失败: 用户不存在 | username={req.username}")
        return R_fail(ErrCode.AUTH_LOGIN_FAILED, msg="用户名或密码错误")

    if not verify_password(req.password, user.password_hash):
        log.warning(f"登录失败: 密码错误 | username={req.username}")
        return R_fail(ErrCode.AUTH_LOGIN_FAILED, msg="用户名或密码错误")

    if not user.is_active:
        log.warning(f"登录失败: 账号已禁用 | username={req.username}")
        return R_fail(ErrCode.AUTH_USER_DISABLED, msg="账号已禁用")

    token = create_jwt_token({"user_id": user.id, "username": user.username, "nickname": user.nickname or user.username})
    log.info(f"登录成功 | user_id={user.id} | username={user.username} | nickname={user.nickname}")

    return R_ok(
        data=LoginResponse(token=token, username=user.username, nickname=user.nickname or user.username).model_dump(),
        msg="登录成功",
    )


@router.get("/me")
async def get_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db, scope="function")):
    """获取当前用户信息（含全局角色 + 所在项目及项目角色）"""
    from app.models.models import ProjectMember, Project
    log.debug(f"获取当前用户信息 | user_id={user.id} | username={user.username}")

    global_role = getattr(user, "global_role", "user")
    # 所在项目及角色
    memberships = []
    if global_role == "super_admin":
        # 超管视为所有项目的管理员
        pr = await db.execute(select(Project))
        for p in pr.scalars().all():
            memberships.append({"project_id": p.id, "project_name": p.name, "project_role": "manager"})
    else:
        mr = await db.execute(select(ProjectMember).where(ProjectMember.user_id == user.id))
        members = mr.scalars().all()
        pid_map = {}
        if members:
            pr = await db.execute(select(Project).where(Project.id.in_([m.project_id for m in members])))
            pid_map = {p.id: p.name for p in pr.scalars().all()}
        for m in members:
            memberships.append({
                "project_id": m.project_id,
                "project_name": pid_map.get(m.project_id, ""),
                "project_role": m.project_role,
            })

    return R_ok(data={
        "id": user.id,
        "nickname": user.nickname or user.username,
        "username": user.username,
        "is_active": user.is_active,
        "global_role": global_role,
        "is_super_admin": global_role == "super_admin",
        "memberships": memberships,
    })


@router.post("/logout")
async def logout():
    """登出（前端清除 token 即可）"""
    log.info("用户登出")
    return R_ok(msg="登出成功")


# ============================================================
# 单点登录 (v2.23)：预发、生产共用用户表，用一次性票据在两个环境间免登录跳转
#   1. 已登录的环境 A：POST /api/auth/sso/ticket {target_env} → 票据（60 秒内有效，只能用一次）
#   2. 浏览器打开 B 环境 /sso?ticket=...&next=...
#   3. B 环境：POST /api/auth/sso/exchange {ticket} → B 环境自己的登录凭证
# 票据存在两个环境共用的 src_dop_sso_tickets 表里（只存哈希），两个环境的 JWT 密钥不必相同。
# ============================================================
import hashlib
import secrets
import time

from pydantic import BaseModel
from sqlalchemy import delete, update

SSO_TICKET_TTL = 60


class SsoTicketRequest(BaseModel):
    target_env: str


class SsoExchangeRequest(BaseModel):
    ticket: str


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def _login_payload(user: User) -> dict:
    token = create_jwt_token({"user_id": user.id, "username": user.username, "nickname": user.nickname or user.username})
    return LoginResponse(token=token, username=user.username, nickname=user.nickname or user.username).model_dump()


@router.post("/sso/ticket")
async def sso_ticket(req: SsoTicketRequest, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db, scope="function")):
    """为当前登录用户签发去另一个环境的一次性票据。"""
    from app.core.runtime_env import CURRENT_ENV, ENVIRONMENTS
    from app.models.models import SsoTicket
    target = (req.target_env or "").strip().lower()
    known = set(ENVIRONMENTS) or {"prod", "pre"}
    if target not in known or target == CURRENT_ENV:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="目标环境不正确")
    now = int(time.time())
    await db.execute(delete(SsoTicket).where(SsoTicket.expires_at < now - 3600))
    ticket = secrets.token_urlsafe(32)
    db.add(SsoTicket(ticket_hash=_ticket_hash(ticket), user_id=user.id, source_env=CURRENT_ENV,
                     target_env=target, expires_at=now + SSO_TICKET_TTL))
    await db.flush()
    log.info(f"签发单点登录票据 | user={user.username} | {CURRENT_ENV} -> {target}")
    return R_ok(data={"ticket": ticket, "expires_in": SSO_TICKET_TTL})


@router.post("/sso/exchange")
async def sso_exchange(req: SsoExchangeRequest, db: AsyncSession = Depends(get_db, scope="function")):
    """用票据换取当前环境的登录凭证（票据只能用一次）。"""
    from app.core.runtime_env import CURRENT_ENV
    from app.models.models import SsoTicket
    h = _ticket_hash((req.ticket or "").strip())
    now = int(time.time())
    # 原子地「占用」票据：已用过、过期、目标环境不对都占用不到
    res = await db.execute(
        update(SsoTicket)
        .where(SsoTicket.ticket_hash == h, SsoTicket.used_at.is_(None),
               SsoTicket.expires_at >= now, SsoTicket.target_env == CURRENT_ENV)
        .values(used_at=now)
    )
    if res.rowcount != 1:
        log.warning("单点登录失败: 票据无效、已使用或已过期")
        return R_fail(ErrCode.AUTH_TOKEN_INVALID, msg="单点登录已失效，请重新登录")
    user_id = (await db.execute(select(SsoTicket.user_id).where(SsoTicket.ticket_hash == h))).scalar()
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or not user.is_active:
        return R_fail(ErrCode.AUTH_USER_DISABLED, msg="账号不存在或已禁用")
    log.info(f"单点登录成功 | user={user.username} | env={CURRENT_ENV}")
    return R_ok(data=_login_payload(user), msg="登录成功")
