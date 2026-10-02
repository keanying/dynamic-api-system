"""
API 上线审批流 (v2.2+)
======================

流程：
  研发/管理员 提交上线 (submit)         -> API: draft/offline -> pending，建审批单
  指定研发 会审 (reviewer approve/reject)
  项目管理员 审批 (manager approve/reject)
  满足通过条件   -> API -> approved（待上线），审批单 approved；再由提交人点「上线」-> online
  任一 reject     -> API -> draft，  审批单 rejected
  提交人 撤回 (withdraw) -> API -> draft，审批单 withdrawn

规则：由 config.yaml approval.online_mode 决定（v2.21）——
  any（默认，v2.10 起的行为）：项目管理员或会审研发任一方通过即可；
  both：两方都要通过（管理员提交且未指定会审人时，管理员通过即可）。
提交人不能审批自己（既不能当管理员审，也不能当会审研发）。
"""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel
import datetime

from app.core.timezone import now as _cst_now
from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.core.permissions import (
    is_super_admin, is_project_manager, get_project_role,
    can_edit_project_resources, PROJ_MANAGER, PROJ_DEVELOPER,
)
from app.models.models import User, ApiConfig, ApiApproval, ProjectMember
from app.api.auth import get_current_user
from app.services.api_lifecycle import next_status, STATUS_ONLINE, STATUS_DRAFT
from app.services.audit import audit

log = get_logger("approvals")
router = APIRouter(prefix="/api/projects/{project_id}", tags=["上线审批"])


class SubmitReq(BaseModel):
    reviewer_id: int | None = None   # 指定会审研发（管理员/超管直接上线时可不填）
    remark: str = ""


class DecisionReq(BaseModel):
    decision: str             # approve / reject
    comment: str = ""


async def _user_brief(db, uid):
    u = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    return {"id": uid, "nickname": (u.nickname or u.username) if u else f"#{uid}", "username": u.username if u else ""}


def _appr_dict(a: ApiApproval, api_name="", submitter=None, reviewer=None):
    return {
        "id": a.id, "api_id": a.api_id, "api_name": api_name, "project_id": a.project_id,
        "submitter": submitter, "reviewer": reviewer,
        "overall_status": a.overall_status,
        "manager_decision": a.manager_decision, "manager_comment": a.manager_comment,
        "reviewer_decision": a.reviewer_decision, "reviewer_comment": a.reviewer_comment,
        "remark": a.remark,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.post("/apis/{api_id}/submit")
async def submit_for_approval(
    req: SubmitReq,
    project_id: int = Path(...),
    api_id: int = Path(...),
    db: AsyncSession = Depends(get_db, scope="function"),
    user: User = Depends(get_current_user),
):
    """提交 API 上线审批。研发/管理员可提交。"""
    if not await can_edit_project_resources(db, user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权提交该项目的 API")

    api = (await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )).scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)

    # 锁定守卫：锁定的 API 不可提交上线
    if getattr(api, "is_locked", False):
        return R_fail(ErrCode.API_UPDATE_FAILED, msg="该 API 已锁定，请先解锁后再提交上线")

    # 状态必须可提交（draft/offline）
    try:
        new_status = next_status("submit", getattr(api, "status", "draft"))
    except ValueError as e:
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=str(e))

    # v2.10: 上线一律生成审批单（留痕）。
    #   - 管理员/超管：可不指定会审研发（reviewer 可空），且有权自己审批通过该单。
    #   - 普通研发/非管理员责任人：必须指定一名会审研发（通过条件见 online_rule）。
    is_mgr = is_super_admin(user) or await is_project_manager(db, user, project_id)

    if not is_mgr:
        # 非管理员：必须指定会审研发，且不能是自己；会审人须为本项目成员
        if req.reviewer_id is None:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="请指定一名会审研发")
        if req.reviewer_id == user.id:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="不能指定自己作为会审研发")
        reviewer = await _get_user(db, req.reviewer_id)
        if not reviewer:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="会审人不存在")
        reviewer_role = await get_project_role(db, reviewer, project_id)
        if reviewer_role is None:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="会审人必须是本项目成员")
    else:
        # 管理员/超管：会审研发可选（可不填）。若填了则做基本校验。
        if req.reviewer_id is not None and req.reviewer_id != user.id:
            reviewer = await _get_user(db, req.reviewer_id)
            if reviewer:
                rr = await get_project_role(db, reviewer, project_id)
                if rr is None:
                    return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="会审人必须是本项目成员")

    # 关闭该 API 其它进行中的审批单
    olds = (await db.execute(
        select(ApiApproval).where(ApiApproval.api_id == api_id, ApiApproval.overall_status == "pending")
    )).scalars().all()
    for o in olds:
        o.overall_status = "withdrawn"

    appr = ApiApproval(
        api_id=api_id, project_id=project_id,
        submitter_id=user.id, reviewer_id=req.reviewer_id,
        overall_status="pending", manager_decision="pending", reviewer_decision="pending",
        remark=req.remark or "",
    )
    db.add(appr)
    api.status = new_status   # -> pending
    await audit(db, user, "api.submit", "api", api_id, f"提交上线，会审研发={req.reviewer_id}")
    await db.commit()
    log.info(f"提交上线 | api_id={api_id} | submitter={user.username} | reviewer_id={req.reviewer_id} | is_mgr={is_mgr}")
    if is_mgr:
        return R_ok(data={"approval_id": appr.id}, msg="上线申请已提交，你可直接审批通过")
    return R_ok(data={"approval_id": appr.id}, msg="已提交上线审批，等待会审研发审核")


async def _get_user(db, uid):
    return (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()


async def _finalize_if_done(db, appr: ApiApproval, api: ApiConfig):
    """结单逻辑。

    通过条件见 online_rule()（默认任一方通过）-> 审批单 approved，API 进入「待上线」(STATUS_APPROVED)，
      由发起者再点「上线」确认才真正 online。
    任一「驳回」-> 审批单 rejected，API 回草稿。
    （管理员/超管提交的上线单，可由管理员自己审批通过，见 manager_decide 放行逻辑。）
    """
    from app.services.api_lifecycle import STATUS_APPROVED
    if appr.reviewer_decision == "rejected" or appr.manager_decision == "rejected":
        appr.overall_status = "rejected"
        api.status = STATUS_DRAFT
        return "rejected"
    if online_rule() == "both":
        # 双方都要通过；未指定会审人（管理员提交）时管理员通过即可
        passed = appr.manager_decision == "approved" and (
            appr.reviewer_id is None or appr.reviewer_decision == "approved")
    else:
        passed = appr.reviewer_decision == "approved" or appr.manager_decision == "approved"
    if passed:
        appr.overall_status = "approved"
        api.status = STATUS_APPROVED
        return "approved"
    return "pending"


def online_rule() -> str:
    """上线审批通过条件：any（任一方通过）/ both（两方都通过），见 config.yaml approval.online_mode。"""
    from app.core.config import settings
    mode = (getattr(settings.approval, "online_mode", "any") or "any").lower()
    return "both" if mode == "both" else "any"


@router.post("/approvals/{approval_id}/manager")
async def manager_decide(
    req: DecisionReq,
    project_id: int = Path(...),
    approval_id: int = Path(...),
    db: AsyncSession = Depends(get_db, scope="function"),
    user: User = Depends(get_current_user),
):
    """项目管理员审批。"""
    if not await is_project_manager(db, user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅项目管理员可审批")
    if req.decision not in ("approve", "reject"):
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="decision 必须是 approve / reject")

    appr = (await db.execute(
        select(ApiApproval).where(ApiApproval.id == approval_id, ApiApproval.project_id == project_id)
    )).scalar_one_or_none()
    if not appr or appr.overall_status != "pending":
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="审批单不存在或已结束")
    # v2.10: 管理员/超管可审批自己提交的上线单（管理员上线也走单，但可自审通过）。
    #   非管理员提交的单，管理员审批时仍不能是提交人自己。
    if appr.submitter_id == user.id and not (is_super_admin(user) or await is_project_manager(db, user, project_id)):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="不能审批自己提交的上线单")

    api = (await db.execute(select(ApiConfig).where(ApiConfig.id == appr.api_id))).scalar_one_or_none()
    appr.manager_decision = "approved" if req.decision == "approve" else "rejected"
    appr.manager_id = user.id
    appr.manager_comment = req.comment or ""
    appr.manager_at = _cst_now()
    outcome = await _finalize_if_done(db, appr, api)
    await audit(db, user, "api.approve.manager", "api", appr.api_id, f"管理员{req.decision} -> {outcome}")
    await db.commit()
    return R_ok(data={"overall_status": appr.overall_status, "api_status": api.status},
                msg=f"已{'通过' if req.decision=='approve' else '驳回'}（管理员）")


@router.post("/approvals/{approval_id}/reviewer")
async def reviewer_decide(
    req: DecisionReq,
    project_id: int = Path(...),
    approval_id: int = Path(...),
    db: AsyncSession = Depends(get_db, scope="function"),
    user: User = Depends(get_current_user),
):
    """指定会审研发审核。"""
    if req.decision not in ("approve", "reject"):
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="decision 必须是 approve / reject")
    appr = (await db.execute(
        select(ApiApproval).where(ApiApproval.id == approval_id, ApiApproval.project_id == project_id)
    )).scalar_one_or_none()
    if not appr or appr.overall_status != "pending":
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="审批单不存在或已结束")
    # 仅被指定的会审人（或超管）可操作
    if appr.reviewer_id != user.id and not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是本单的会审研发")

    api = (await db.execute(select(ApiConfig).where(ApiConfig.id == appr.api_id))).scalar_one_or_none()
    appr.reviewer_decision = "approved" if req.decision == "approve" else "rejected"
    appr.reviewer_comment = req.comment or ""
    appr.reviewer_at = _cst_now()
    outcome = await _finalize_if_done(db, appr, api)
    await audit(db, user, "api.approve.reviewer", "api", appr.api_id, f"研发{req.decision} -> {outcome}")
    await db.commit()
    return R_ok(data={"overall_status": appr.overall_status, "api_status": api.status},
                msg=f"已{'通过' if req.decision=='approve' else '驳回'}（会审研发）")


@router.post("/approvals/{approval_id}/withdraw")
async def withdraw_approval(
    project_id: int = Path(...),
    approval_id: int = Path(...),
    db: AsyncSession = Depends(get_db, scope="function"),
    user: User = Depends(get_current_user),
):
    """提交人撤回审批，API 回到草稿。"""
    appr = (await db.execute(
        select(ApiApproval).where(ApiApproval.id == approval_id, ApiApproval.project_id == project_id)
    )).scalar_one_or_none()
    if not appr or appr.overall_status != "pending":
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="审批单不存在或已结束")
    if appr.submitter_id != user.id and not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有提交人可撤回")

    api = (await db.execute(select(ApiConfig).where(ApiConfig.id == appr.api_id))).scalar_one_or_none()
    appr.overall_status = "withdrawn"
    if api:
        api.status = STATUS_DRAFT
    await audit(db, user, "api.withdraw", "api", appr.api_id, "撤回上线申请")
    await db.commit()
    return R_ok(msg="已撤回，API 回到草稿")


@router.get("/approvals")
async def list_approvals(
    project_id: int = Path(...),
    status: str = "pending",
    db: AsyncSession = Depends(get_db, scope="function"),
    user: User = Depends(get_current_user),
):
    """列出项目的审批单（默认进行中）。项目成员可见。"""
    if not is_super_admin(user):
        role = await get_project_role(db, user, project_id)
        if role is None:
            return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员")

    q = select(ApiApproval).where(ApiApproval.project_id == project_id)
    if status and status != "all":
        q = q.where(ApiApproval.overall_status == status)
    q = q.order_by(ApiApproval.created_at.desc())
    apprs = (await db.execute(q)).scalars().all()

    items = []
    for a in apprs:
        api = (await db.execute(select(ApiConfig).where(ApiConfig.id == a.api_id))).scalar_one_or_none()
        items.append(_appr_dict(
            a, api_name=api.name if api else f"#{a.api_id}",
            submitter=await _user_brief(db, a.submitter_id),
            reviewer=await _user_brief(db, a.reviewer_id),
        ))
    return R_ok(data={"items": items, "total": len(items)})
