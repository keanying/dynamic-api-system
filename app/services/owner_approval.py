# -*- coding: utf-8 -*-
"""
API 责任人制 + 操作审批 (v2.10)

规则：
  - 每个 API 有责任人 owner_id（默认=创建人 created_by，历史数据已迁移回填）。
  - 「删除 / 上线 / 下线」三类操作：
      * 责任人本人 或 管理员（项目管理员/超管）：可直接执行。
      * 其他人：不能直接执行，需发起申请，由责任人或管理员审批通过后才执行。
  - 转交责任人：责任人本人可转交给任意人；项目管理员/超管可强制转交。

本模块只提供「判定」与「审批数据操作」，具体执行动作(真正删/上/下线)仍在各端点里做，
以复用其状态守卫等既有逻辑。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import ApiConfig, ApiOwnerApproval, ProjectMember, User
from app.core.timezone import now as cst_now
from app.core.logging import get_logger

log = get_logger("owner_approval")

# 需要责任人/审批的操作
OWNER_ACTIONS = ("delete", "online", "offline")
ACTION_LABELS = {"delete": "删除", "online": "上线", "offline": "下线"}


async def is_global_admin(user) -> bool:
    return getattr(user, "global_role", "user") == "super_admin"


async def is_project_manager(db: AsyncSession, user, project_id: int) -> bool:
    """是否该项目的管理员（或超管）。"""
    if await is_global_admin(user):
        return True
    r = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
            ProjectMember.project_role == "manager",
        )
    )
    return r.scalar_one_or_none() is not None


async def can_act_directly(db: AsyncSession, user, api: ApiConfig) -> bool:
    """该用户能否直接执行责任人级操作（删除/上下线）：责任人本人 或 管理员。"""
    if api.owner_id is not None and api.owner_id == user.id:
        return True
    return await is_project_manager(db, user, api.project_id)


async def can_approve(db: AsyncSession, user, api: ApiConfig) -> bool:
    """该用户能否审批某 API 的申请：责任人本人 或 管理员。"""
    return await can_act_directly(db, user, api)


async def create_request(db: AsyncSession, user, api: ApiConfig, action: str, reason: str = "") -> ApiOwnerApproval:
    """非责任人发起操作申请。若已有同 API 同 action 的 pending 单，直接返回该单避免重复。"""
    existing = await db.execute(
        select(ApiOwnerApproval).where(
            ApiOwnerApproval.api_id == api.id,
            ApiOwnerApproval.action == action,
            ApiOwnerApproval.status == "pending",
        )
    )
    dup = existing.scalar_one_or_none()
    if dup:
        return dup

    appr = ApiOwnerApproval(
        api_id=api.id,
        project_id=api.project_id,
        action=action,
        status="pending",
        requester_id=user.id,
        owner_id_snapshot=api.owner_id,
        reason=(reason or "")[:512],
    )
    db.add(appr)
    await db.flush()
    log.info(f"责任人操作申请已创建 | api_id={api.id} action={action} requester={user.username}")
    return appr


async def transfer_owner(db: AsyncSession, user, api: ApiConfig, new_owner_id: int) -> tuple[bool, str]:
    """转交责任人。责任人本人可转交；项目管理员/超管可强制转交。"""
    # 校验新责任人存在
    u = await db.execute(select(User).where(User.id == new_owner_id))
    if u.scalar_one_or_none() is None:
        return False, "目标用户不存在"

    is_owner = (api.owner_id is not None and api.owner_id == user.id)
    is_mgr = await is_project_manager(db, user, api.project_id)
    if not (is_owner or is_mgr):
        return False, "只有责任人本人或项目管理员可转交责任人"

    api.owner_id = new_owner_id
    api.updated_at = cst_now()
    log.info(f"责任人转交 | api_id={api.id} -> new_owner={new_owner_id} by={user.username} force={is_mgr and not is_owner}")
    return True, "转交成功"
