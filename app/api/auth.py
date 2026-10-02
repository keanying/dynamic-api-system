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
