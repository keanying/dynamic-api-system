"""
权限体系核心 (v2.0+)
====================

角色模型：
  全局角色 (User.global_role):
    super_admin  超级管理员 —— 全平台最高权限，可管所有项目/用户/数据源
    user         普通用户   —— 注册默认；无项目权限，需被加入项目

  项目角色 (ProjectMember.project_role):
    manager      项目管理员 —— 管成员、审批上线、发起删项目
    developer    研发       —— 创建/编辑草稿、提交上线、参与审核
    viewer       只读       —— 仅查看

权限判定优先级：super_admin 一律放行；否则按项目成员关系与项目角色判定。

本模块提供：
  - 纯判定函数 (can_xxx)，便于单测与复用
  - FastAPI 依赖 (require_xxx)，用于路由保护
"""

from fastapi import Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.errors import ErrCode
from app.models.models import User, ProjectMember

# ---- 角色常量 ----
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"        # 管理员：可管理用户（除超管外），但无超管特权
ROLE_DEVELOPER = "developer"
ROLE_USER = "user"

PROJ_MANAGER = "manager"
PROJ_DEVELOPER = "developer"
PROJ_VIEWER = "viewer"


def is_super_admin(user: User) -> bool:
    return getattr(user, "global_role", "user") == ROLE_SUPER_ADMIN


def is_admin_or_above(user: User) -> bool:
    """管理员或超级管理员（拥有用户管理权限）。"""
    return getattr(user, "global_role", "user") in (ROLE_SUPER_ADMIN, ROLE_ADMIN)


async def get_project_role(db: AsyncSession, user: User, project_id: int):
    """返回用户在某项目的角色字符串；不是成员返回 None。超管视为 manager。"""
    if is_super_admin(user):
        return PROJ_MANAGER
    r = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    m = r.scalar_one_or_none()
    return m.project_role if m else None


async def is_project_member(db: AsyncSession, user: User, project_id: int) -> bool:
    return (await get_project_role(db, user, project_id)) is not None


async def is_project_manager(db: AsyncSession, user: User, project_id: int) -> bool:
    role = await get_project_role(db, user, project_id)
    return role == PROJ_MANAGER


async def can_edit_project_resources(db: AsyncSession, user: User, project_id: int) -> bool:
    """能否编辑项目内资源（建/改 API、草稿等）：manager 或 developer。"""
    role = await get_project_role(db, user, project_id)
    return role in (PROJ_MANAGER, PROJ_DEVELOPER)


# ============================================================
# FastAPI 依赖
# ============================================================
def _forbid(msg: str):
    raise HTTPException(status_code=403, detail={"code": ErrCode.AUTH_PERMISSION_DENIED, "msg": msg})


def require_super_admin():
    """依赖：要求当前用户是超级管理员。"""
    from app.api.auth import get_current_user

    async def _dep(user: User = Depends(get_current_user)) -> User:
        if not is_super_admin(user):
            _forbid("需要超级管理员权限")
        return user
    return _dep


def require_project_member():
    """依赖：要求当前用户是 {project_id} 项目成员（或超管）。"""
    from app.api.auth import get_current_user

    async def _dep(
        project_id: int = Path(...),
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function"),
    ) -> User:
        if not await is_project_member(db, user, project_id):
            _forbid("你不是该项目成员，无权访问")
        return user
    return _dep


def require_project_editor():
    """依赖：要求当前用户可编辑项目资源（manager/developer/超管）。"""
    from app.api.auth import get_current_user

    async def _dep(
        project_id: int = Path(...),
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function"),
    ) -> User:
        if not await can_edit_project_resources(db, user, project_id):
            _forbid("无权编辑该项目资源")
        return user
    return _dep


def require_project_manager():
    """依赖：要求当前用户是该项目的项目管理员（或超管）。"""
    from app.api.auth import get_current_user

    async def _dep(
        project_id: int = Path(...),
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function"),
    ) -> User:
        if not await is_project_manager(db, user, project_id):
            _forbid("需要项目管理员权限")
        return user
    return _dep
