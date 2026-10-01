"""
用户管理路由：CRUD
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from app.core.database import get_db
from app.core.security import hash_password
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.models.models import User
from app.schemas.schemas import UserCreate, UserUpdate, UserOut
from app.api.auth import get_current_user

log = get_logger("users")

router = APIRouter(prefix="/api/users", tags=["用户管理"])


@router.get("")
async def list_users(
    keyword: str = Query("", description="搜索关键字"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """获取用户列表"""
    log.debug(f"查询用户列表 | keyword={keyword} | page={page} | page_size={page_size}")

    query = select(User)
    if keyword:
        query = query.where(or_(User.username.contains(keyword), User.nickname.contains(keyword)))
    query = query.order_by(User.created_at.desc())

    # 总数
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # 分页
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    users = result.scalars().all()

    items = [
        UserOut(
            id=u.id,
            nickname=u.nickname or u.username,
            username=u.username,
            is_active=u.is_active,
            global_role=getattr(u, "global_role", "user"),
            created_at=u.created_at,
            updated_at=u.updated_at,
        ).model_dump()
        for u in users
    ]

    log.debug(f"用户列表查询完成 | total={total} | 返回={len(items)}条")
    return R_ok(data={
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.post("")
async def create_user(
    req: UserCreate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """创建用户"""
    from app.core.permissions import is_super_admin, is_admin_or_above
    log.info(f"创建用户请求 | nickname={req.nickname} | username={req.username}")

    # 管理员或超管可创建用户
    if not is_admin_or_above(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅管理员或超级管理员可创建用户")

    role = req.global_role if req.global_role in ("super_admin", "admin", "developer", "user") else "user"
    # 管理员不能创建超级管理员
    if role == "super_admin" and not is_super_admin(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有超级管理员才能设置超级管理员角色")

    # 检查账户是否已存在
    existing = await db.execute(select(User).where(User.username == req.username))
    if existing.scalar_one_or_none():
        log.warning(f"创建用户失败: 账户已存在 | username={req.username}")
        return R_fail(ErrCode.USER_ACCOUNT_EXISTS, msg="账户已存在")

    user = User(
        nickname=req.nickname,
        username=req.username,
        password_hash=hash_password(req.password),
        is_active=req.is_active,
        global_role=role,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    log.info(f"用户创建成功 | id={user.id} | username={user.username} | role={role}")
    return R_ok(data={"id": user.id}, msg="用户创建成功")


@router.get("/{user_id}")
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """获取单个用户详情"""
    log.debug(f"查询用户详情 | user_id={user_id}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        log.warning(f"用户不存在 | user_id={user_id}")
        return R_fail(ErrCode.USER_NOT_FOUND)

    return R_ok(data=UserOut(
        id=user.id,
        nickname=user.nickname or user.username,
        username=user.username,
        is_active=user.is_active,
        global_role=getattr(user, "global_role", "user"),
        created_at=user.created_at,
        updated_at=user.updated_at,
    ).model_dump())


@router.put("/{user_id}")
async def update_user(
    user_id: int,
    req: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """更新用户"""
    from app.core.permissions import is_super_admin, is_admin_or_above
    log.info(f"更新用户请求 | user_id={user_id}")

    if not is_admin_or_above(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅管理员或超级管理员可编辑用户")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        log.warning(f"更新用户失败: 用户不存在 | user_id={user_id}")
        return R_fail(ErrCode.USER_NOT_FOUND)

    # 管理员不能编辑超级管理员账户
    if user.global_role == "super_admin" and not is_super_admin(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="管理员不能编辑超级管理员账户")

    if req.nickname is not None:
        user.nickname = req.nickname

    if req.username is not None:
        # 检查新账户是否与其他用户冲突
        existing = await db.execute(
            select(User).where(User.username == req.username, User.id != user_id)
        )
        if existing.scalar_one_or_none():
            log.warning(f"更新用户失败: 账户已存在 | username={req.username}")
            return R_fail(ErrCode.USER_ACCOUNT_EXISTS, msg="账户已存在")
        user.username = req.username

    if req.password is not None:
        user.password_hash = hash_password(req.password)
        log.debug(f"用户密码已更新 | user_id={user_id}")

    if req.is_active is not None:
        user.is_active = req.is_active

    # 全局角色变更
    if req.global_role is not None and req.global_role in ("super_admin", "admin", "developer", "user"):
        # 管理员不能把任何人设为超管；只有超管能设/改超管
        if req.global_role == "super_admin" and not is_super_admin(_user):
            return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有超级管理员才能授予超级管理员角色")
        # 若把一个超管降级，确保系统还有其他超管（且只有超管能操作超管）
        if user.global_role == "super_admin" and req.global_role != "super_admin":
            if not is_super_admin(_user):
                return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="管理员不能变更超级管理员的角色")
            cnt = len((await db.execute(
                select(User).where(User.global_role == "super_admin")
            )).scalars().all())
            if cnt <= 1:
                return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="不能取消最后一个超级管理员")
        user.global_role = req.global_role

    await db.flush()
    log.info(f"用户更新成功 | user_id={user_id} | username={user.username}")
    return R_ok(msg="用户更新成功")


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """删除用户"""
    from app.core.permissions import is_super_admin, is_admin_or_above
    log.info(f"删除用户请求 | user_id={user_id} | 操作人={current_user.username}")

    if not is_admin_or_above(current_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅管理员或超级管理员可删除用户")

    if current_user.id == user_id:
        log.warning(f"删除用户失败: 不能删除自己 | user_id={user_id}")
        return R_fail(ErrCode.USER_CANNOT_DELETE_SELF)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        log.warning(f"删除用户失败: 用户不存在 | user_id={user_id}")
        return R_fail(ErrCode.USER_NOT_FOUND)

    # 管理员不能删除超级管理员
    if user.global_role == "super_admin" and not is_super_admin(current_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="管理员不能删除超级管理员账户")

    await db.delete(user)
    await db.flush()
    log.info(f"用户删除成功 | user_id={user_id} | username={user.username}")
    return R_ok(msg="用户删除成功")


@router.put("/{user_id}/toggle")
async def toggle_user_status(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """切换用户启用/禁用状态"""
    from app.core.permissions import is_super_admin, is_admin_or_above
    log.info(f"切换用户状态请求 | user_id={user_id} | 操作人={current_user.username}")

    if not is_admin_or_above(current_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅管理员或超级管理员可禁用/启用用户")

    if current_user.id == user_id:
        log.warning(f"切换状态失败: 不能禁用自己 | user_id={user_id}")
        return R_fail(ErrCode.USER_CANNOT_DISABLE_SELF)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        log.warning(f"切换状态失败: 用户不存在 | user_id={user_id}")
        return R_fail(ErrCode.USER_NOT_FOUND)

    # 管理员不能禁用超级管理员
    if user.global_role == "super_admin" and not is_super_admin(current_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="管理员不能禁用超级管理员账户")

    user.is_active = not user.is_active
    await db.flush()

    status_text = "启用" if user.is_active else "禁用"
    log.info(f"用户状态切换成功 | user_id={user_id} | username={user.username} | 新状态={status_text}")
    return R_ok(msg=f"用户已{status_text}")
