"""
项目成员管理路由 (v2.0+)
========================

项目管理员可以：把用户加入项目、调整其项目角色、移除成员。
超级管理员可对任意项目执行。

路由前缀：/api/projects/{project_id}/members
"""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.core.permissions import (
    is_super_admin, is_project_manager, get_project_role,
    PROJ_MANAGER, PROJ_DEVELOPER, PROJ_VIEWER,
)
from app.models.models import User, Project, ProjectMember
from app.api.auth import get_current_user
from pydantic import BaseModel

log = get_logger("members")

router = APIRouter(prefix="/api/projects/{project_id}/members", tags=["项目成员"])

_VALID_ROLES = {PROJ_MANAGER, PROJ_DEVELOPER, PROJ_VIEWER}
_ROLE_LABELS = {PROJ_MANAGER: "项目管理员", PROJ_DEVELOPER: "研发", PROJ_VIEWER: "只读"}


class AddMemberReq(BaseModel):
    user_id: int
    project_role: str = PROJ_DEVELOPER


class UpdateRoleReq(BaseModel):
    project_role: str


@router.get("")
async def list_members(
    project_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出项目成员。项目成员均可查看。"""
    # 任何项目成员（或超管）可查看
    if not is_super_admin(user):
        role = await get_project_role(db, user, project_id)
        if role is None:
            return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员")

    r = await db.execute(select(ProjectMember).where(ProjectMember.project_id == project_id))
    members = r.scalars().all()
    items = []
    if members:
        ur = await db.execute(select(User).where(User.id.in_([m.user_id for m in members])))
        umap = {u.id: u for u in ur.scalars().all()}
        for m in members:
            u = umap.get(m.user_id)
            if not u:
                continue
            items.append({
                "user_id": u.id,
                "username": u.username,
                "nickname": u.nickname or u.username,
                "project_role": m.project_role,
                "project_role_label": _ROLE_LABELS.get(m.project_role, m.project_role),
                "is_active": u.is_active,
                "joined_at": m.created_at.isoformat() if m.created_at else None,
            })
    return R_ok(data={"items": items, "total": len(items)})


@router.post("")
async def add_member(
    req: AddMemberReq,
    project_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """添加成员到项目。仅项目管理员/超管。"""
    if not await is_project_manager(db, user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅项目管理员可添加成员")

    if req.project_role not in _VALID_ROLES:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"无效角色: {req.project_role}")

    # 项目、用户存在性
    proj = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not proj:
        return R_fail(ErrCode.PROJECT_NOT_FOUND, msg="项目不存在")
    target = (await db.execute(select(User).where(User.id == req.user_id))).scalar_one_or_none()
    if not target:
        return R_fail(ErrCode.USER_NOT_FOUND, msg="用户不存在")

    # 是否已是成员
    exist = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == req.user_id,
        )
    )).scalar_one_or_none()
    if exist:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="该用户已是项目成员")

    db.add(ProjectMember(project_id=project_id, user_id=req.user_id, project_role=req.project_role))
    await db.commit()
    log.info(f"添加项目成员 | project_id={project_id} | user_id={req.user_id} | role={req.project_role} | by={user.username}")
    return R_ok(msg=f"已添加成员（{_ROLE_LABELS.get(req.project_role)}）")


@router.put("/{member_user_id}")
async def update_member_role(
    member_user_id: int,
    req: UpdateRoleReq,
    project_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """调整成员的项目角色。仅项目管理员/超管。"""
    if not await is_project_manager(db, user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅项目管理员可调整成员角色")
    if req.project_role not in _VALID_ROLES:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"无效角色: {req.project_role}")

    m = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == member_user_id,
        )
    )).scalar_one_or_none()
    if not m:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="该用户不是项目成员")

    # 防止把最后一个项目管理员降级，导致项目失去管理员
    if m.project_role == PROJ_MANAGER and req.project_role != PROJ_MANAGER:
        mgr_count = len((await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.project_role == PROJ_MANAGER,
            )
        )).scalars().all())
        if mgr_count <= 1:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="不能降级最后一个项目管理员")

    m.project_role = req.project_role
    await db.commit()
    log.info(f"调整成员角色 | project_id={project_id} | user_id={member_user_id} | role={req.project_role} | by={user.username}")
    return R_ok(msg=f"角色已更新为「{_ROLE_LABELS.get(req.project_role)}」")


@router.delete("/{member_user_id}")
async def remove_member(
    member_user_id: int,
    project_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """移除项目成员。仅项目管理员/超管。"""
    if not await is_project_manager(db, user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅项目管理员可移除成员")

    m = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == member_user_id,
        )
    )).scalar_one_or_none()
    if not m:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="该用户不是项目成员")

    # 不能移除最后一个项目管理员
    if m.project_role == PROJ_MANAGER:
        mgr_count = len((await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.project_role == PROJ_MANAGER,
            )
        )).scalars().all())
        if mgr_count <= 1:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="不能移除最后一个项目管理员")

    await db.delete(m)
    await db.commit()
    log.info(f"移除项目成员 | project_id={project_id} | user_id={member_user_id} | by={user.username}")
    return R_ok(msg="已移除成员")
