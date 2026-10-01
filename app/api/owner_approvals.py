# -*- coding: utf-8 -*-
"""
API 责任人操作审批流 (v2.10)

端点（挂在 /api/owner-approvals 下）：
  GET    /pending           待我审批的申请（我是责任人或管理员的）
  GET    /mine              我发起的申请
  POST   /{approval_id}/approve   审批通过并执行对应操作(删除/上线/下线)
  POST   /{approval_id}/reject    驳回
  POST   /{approval_id}/cancel    申请人撤回
  POST   /transfer          转交责任人  body: {api_id, new_owner_id}
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.core.timezone import now as cst_now
from app.models.models import ApiConfig, ApiOwnerApproval, ProjectMember, User
from app.api.auth import get_current_user
from app.services.owner_approval import (
    can_approve, transfer_owner, is_project_manager, is_global_admin, ACTION_LABELS,
)

log = get_logger("owner_approvals")
router = APIRouter(prefix="/api/owner-approvals", tags=["责任人审批"])


class DecisionBody(BaseModel):
    comment: str = ""


class TransferBody(BaseModel):
    api_id: int
    new_owner_id: int


async def _user_name_map(db, ids):
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    r = await db.execute(select(User).where(User.id.in_(ids)))
    return {u.id: (u.nickname or u.username) for u in r.scalars().all()}


@router.get("/pending")
async def pending_for_me(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """待我审批：我是责任人的 API 的申请，或我是管理员的项目的申请。"""
    # 我作为责任人的 API
    owned = await db.execute(select(ApiConfig.id).where(ApiConfig.owner_id == user.id))
    owned_ids = [row[0] for row in owned.all()]
    # 我作为管理员的项目
    mgr = await db.execute(select(ProjectMember.project_id).where(
        ProjectMember.user_id == user.id, ProjectMember.project_role == "manager"))
    mgr_pids = [row[0] for row in mgr.all()]

    conds = []
    if owned_ids:
        conds.append(ApiOwnerApproval.api_id.in_(owned_ids))
    if mgr_pids:
        conds.append(ApiOwnerApproval.project_id.in_(mgr_pids))
    if await is_global_admin(user):
        conds = [ApiOwnerApproval.status == "pending"]  # 超管看全部待审
    if not conds:
        return R_ok(data={"items": []})

    q = select(ApiOwnerApproval).where(ApiOwnerApproval.status == "pending", or_(*conds)) \
        if not await is_global_admin(user) else select(ApiOwnerApproval).where(ApiOwnerApproval.status == "pending")
    r = await db.execute(q.order_by(ApiOwnerApproval.created_at.desc()))
    rows = r.scalars().all()

    # 附带 API 名称、申请人名称
    api_ids = [a.api_id for a in rows]
    name_map = {}
    if api_ids:
        ar = await db.execute(select(ApiConfig).where(ApiConfig.id.in_(api_ids)))
        name_map = {a.id: a.name for a in ar.scalars().all()}
    umap = await _user_name_map(db, [a.requester_id for a in rows])

    items = [{
        "id": a.id, "api_id": a.api_id, "api_name": name_map.get(a.api_id, f"#{a.api_id}"),
        "project_id": a.project_id, "action": a.action, "action_label": ACTION_LABELS.get(a.action, a.action),
        "requester_id": a.requester_id, "requester_name": umap.get(a.requester_id, f"#{a.requester_id}"),
        "reason": a.reason, "status": a.status,
        "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
    } for a in rows]
    return R_ok(data={"items": items})


@router.get("/mine")
async def my_requests(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    r = await db.execute(select(ApiOwnerApproval).where(
        ApiOwnerApproval.requester_id == user.id).order_by(ApiOwnerApproval.created_at.desc()))
    rows = r.scalars().all()
    api_ids = [a.api_id for a in rows]
    name_map = {}
    if api_ids:
        ar = await db.execute(select(ApiConfig).where(ApiConfig.id.in_(api_ids)))
        name_map = {a.id: a.name for a in ar.scalars().all()}
    items = [{
        "id": a.id, "api_id": a.api_id, "api_name": name_map.get(a.api_id, f"#{a.api_id}"),
        "action": a.action, "action_label": ACTION_LABELS.get(a.action, a.action),
        "status": a.status, "reason": a.reason,
        "decide_comment": a.decide_comment,
        "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
    } for a in rows]
    return R_ok(data={"items": items})


async def _load_pending(db, approval_id):
    r = await db.execute(select(ApiOwnerApproval).where(ApiOwnerApproval.id == approval_id))
    return r.scalar_one_or_none()


@router.post("/{approval_id}/approve")
async def approve(approval_id: int, body: DecisionBody, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """审批通过 -> 执行对应操作（删除/上线/下线）。"""
    appr = await _load_pending(db, approval_id)
    if not appr:
        return R_fail(ErrCode.API_NOT_FOUND, msg="申请单不存在")
    if appr.status != "pending":
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"该申请已处理（{appr.status}）")

    ar = await db.execute(select(ApiConfig).where(ApiConfig.id == appr.api_id))
    api = ar.scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND, msg="目标 API 已不存在")

    if not await can_approve(db, user, api):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有责任人或管理员可审批")

    # 执行动作
    action = appr.action
    if action == "delete":
        await db.delete(api)
    elif action == "online":
        api.status = "online"
        api.updated_at = cst_now()
    elif action == "offline":
        api.status = "offline"
        api.updated_at = cst_now()
    else:
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"未知操作 {action}")

    appr.status = "approved"
    appr.decider_id = user.id
    appr.decide_comment = (body.comment or "")[:512]
    appr.decided_at = cst_now()
    log.info(f"责任人申请已通过并执行 | approval_id={approval_id} action={action} by={user.username}")
    return R_ok(msg=f"已通过并执行{ACTION_LABELS.get(action, action)}")


@router.post("/{approval_id}/reject")
async def reject(approval_id: int, body: DecisionBody, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    appr = await _load_pending(db, approval_id)
    if not appr:
        return R_fail(ErrCode.API_NOT_FOUND, msg="申请单不存在")
    if appr.status != "pending":
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"该申请已处理（{appr.status}）")
    ar = await db.execute(select(ApiConfig).where(ApiConfig.id == appr.api_id))
    api = ar.scalar_one_or_none()
    if api and not await can_approve(db, user, api):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有责任人或管理员可审批")
    appr.status = "rejected"
    appr.decider_id = user.id
    appr.decide_comment = (body.comment or "")[:512]
    appr.decided_at = cst_now()
    log.info(f"责任人申请已驳回 | approval_id={approval_id} by={user.username}")
    return R_ok(msg="已驳回")


@router.post("/{approval_id}/cancel")
async def cancel(approval_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    appr = await _load_pending(db, approval_id)
    if not appr:
        return R_fail(ErrCode.API_NOT_FOUND, msg="申请单不存在")
    if appr.requester_id != user.id:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只能撤回自己发起的申请")
    if appr.status != "pending":
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"该申请已处理（{appr.status}）")
    appr.status = "cancelled"
    appr.updated_at = cst_now()
    return R_ok(msg="已撤回")


@router.post("/transfer")
async def transfer(body: TransferBody, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    ar = await db.execute(select(ApiConfig).where(ApiConfig.id == body.api_id))
    api = ar.scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND, msg="API 不存在")
    ok, msg = await transfer_owner(db, user, api, body.new_owner_id)
    if not ok:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=msg)
    return R_ok(msg=msg)


# ========== 审批中心聚合（上线单 ApiApproval + 责任人操作单 ApiOwnerApproval）==========
from app.models.models import ApiApproval


async def _project_name_map(db, pids):
    from app.models.models import Project
    pids = [p for p in set(pids) if p]
    if not pids:
        return {}
    r = await db.execute(select(Project).where(Project.id.in_(pids)))
    return {p.id: p.name for p in r.scalars().all()}


@router.get("/center/pending")
async def center_pending(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """待我审批：合并「上线单(我是会审研发/管理员/超管)」+「责任人操作单(我是责任人/管理员)」。"""
    is_super = await is_global_admin(user)
    # 我管理的项目
    mgr = await db.execute(select(ProjectMember.project_id).where(
        ProjectMember.user_id == user.id, ProjectMember.project_role == "manager"))
    mgr_pids = [row[0] for row in mgr.all()]
    # 我作为责任人的 API
    owned = await db.execute(select(ApiConfig.id).where(ApiConfig.owner_id == user.id))
    owned_ids = [row[0] for row in owned.all()]

    result = []

    # 1) 上线单：待我处理（我是指定会审研发，或我是该项目管理员/超管）
    q1 = select(ApiApproval).where(ApiApproval.overall_status == "pending")
    a_rows = (await db.execute(q1.order_by(ApiApproval.created_at.desc()))).scalars().all()
    api_ids = [a.api_id for a in a_rows]
    pids = [a.project_id for a in a_rows]
    api_name = {}
    if api_ids:
        ar = await db.execute(select(ApiConfig).where(ApiConfig.id.in_(api_ids)))
        api_name = {a.id: a.name for a in ar.scalars().all()}
    pname = await _project_name_map(db, pids)
    umap = await _user_name_map(db, [a.submitter_id for a in a_rows])
    for a in a_rows:
        mine = is_super or (a.reviewer_id == user.id) or (a.project_id in mgr_pids)
        if not mine:
            continue
        result.append({
            "kind": "online", "id": a.id, "api_id": a.api_id,
            "api_name": api_name.get(a.api_id, f"#{a.api_id}"),
            "project_id": a.project_id, "project_name": pname.get(a.project_id, ""),
            "action": "online", "action_label": "上线",
            "requester_name": umap.get(a.submitter_id, f"#{a.submitter_id}"),
            "reason": a.remark or "",
            "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
            "can_reviewer": (a.reviewer_id == user.id or is_super),
            "can_manager": (a.project_id in mgr_pids or is_super),
        })

    # 2) 责任人操作单：待我处理（我是责任人 或 我是该项目管理员/超管）
    conds = []
    if owned_ids:
        conds.append(ApiOwnerApproval.api_id.in_(owned_ids))
    if mgr_pids:
        conds.append(ApiOwnerApproval.project_id.in_(mgr_pids))
    if is_super:
        q2 = select(ApiOwnerApproval).where(ApiOwnerApproval.status == "pending")
    elif conds:
        q2 = select(ApiOwnerApproval).where(ApiOwnerApproval.status == "pending", or_(*conds))
    else:
        q2 = None
    if q2 is not None:
        o_rows = (await db.execute(q2.order_by(ApiOwnerApproval.created_at.desc()))).scalars().all()
        oapi_ids = [o.api_id for o in o_rows]
        oapi_name = {}
        if oapi_ids:
            ar = await db.execute(select(ApiConfig).where(ApiConfig.id.in_(oapi_ids)))
            oapi_name = {a.id: a.name for a in ar.scalars().all()}
        opname = await _project_name_map(db, [o.project_id for o in o_rows])
        oumap = await _user_name_map(db, [o.requester_id for o in o_rows])
        for o in o_rows:
            result.append({
                "kind": "owner", "id": o.id, "api_id": o.api_id,
                "api_name": oapi_name.get(o.api_id, f"#{o.api_id}"),
                "project_id": o.project_id, "project_name": opname.get(o.project_id, ""),
                "action": o.action, "action_label": ACTION_LABELS.get(o.action, o.action),
                "requester_name": oumap.get(o.requester_id, f"#{o.requester_id}"),
                "reason": o.reason or "",
                "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
            })

    return R_ok(data={"items": result})


@router.get("/center/mine")
async def center_mine(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """我发起的：合并「我提交的上线单」+「我发起的责任人操作单」。"""
    result = []
    # 上线单
    a_rows = (await db.execute(select(ApiApproval).where(
        ApiApproval.submitter_id == user.id).order_by(ApiApproval.created_at.desc()))).scalars().all()
    api_ids = [a.api_id for a in a_rows]
    api_name = {}
    if api_ids:
        ar = await db.execute(select(ApiConfig).where(ApiConfig.id.in_(api_ids)))
        api_name = {a.id: a.name for a in ar.scalars().all()}
    pname = await _project_name_map(db, [a.project_id for a in a_rows])
    for a in a_rows:
        result.append({
            "kind": "online", "id": a.id, "api_id": a.api_id,
            "api_name": api_name.get(a.api_id, f"#{a.api_id}"),
            "project_id": a.project_id, "project_name": pname.get(a.project_id, ""),
            "action_label": "上线", "status": a.overall_status,
            "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
        })
    # 责任人操作单
    o_rows = (await db.execute(select(ApiOwnerApproval).where(
        ApiOwnerApproval.requester_id == user.id).order_by(ApiOwnerApproval.created_at.desc()))).scalars().all()
    oapi_ids = [o.api_id for o in o_rows]
    oapi_name = {}
    if oapi_ids:
        ar = await db.execute(select(ApiConfig).where(ApiConfig.id.in_(oapi_ids)))
        oapi_name = {a.id: a.name for a in ar.scalars().all()}
    opname = await _project_name_map(db, [o.project_id for o in o_rows])
    for o in o_rows:
        result.append({
            "kind": "owner", "id": o.id, "api_id": o.api_id,
            "api_name": oapi_name.get(o.api_id, f"#{o.api_id}"),
            "project_id": o.project_id, "project_name": opname.get(o.project_id, ""),
            "action_label": ACTION_LABELS.get(o.action, o.action), "status": o.status,
            "decide_comment": o.decide_comment or "",
            "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
        })
    return R_ok(data={"items": result})
