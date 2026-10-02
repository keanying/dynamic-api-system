"""
跨环境发布路由 (v2.18+)：预发 → 生产

预发环境：
    POST /api/releases                 对一个已上线 API 发起「发布到生产」
    POST /api/releases/{id}/cancel     撤回待审核的发布单
生产环境：
    GET  /api/releases/{id}            查看发布单 + 与生产当前配置的差异
    POST /api/releases/{id}/approve    管理员审核通过：写入生产并上线
    POST /api/releases/{id}/reject     管理员驳回
两边通用：
    GET  /api/releases/env             当前环境信息（页面横幅、跨环境跳转用，无需登录）
    GET  /api/releases                 发布单列表
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.core.permissions import is_admin_or_above, is_super_admin, can_edit_project_resources
from app.core.runtime_env import CURRENT_ENV, ENV_LABEL, IS_PRE, IS_PROD, env_port, env_url
from app.core.timezone import now as _cst_now
from app.models.models import ApiConfig, Project, ReleaseRequest
from app.services.audit import audit
from app.services.release import ReleaseError, apply_release, build_snapshot, diff_snapshots, find_target

log = get_logger("releases")

router = APIRouter(prefix="/api/releases", tags=["跨环境发布"])

STATUS_LABELS = {"pending": "待审核", "approved": "已发布", "rejected": "已驳回", "cancelled": "已撤回"}


class ReleaseCreate(BaseModel):
    project_id: int
    api_id: int
    remark: str = Field("", max_length=512)


class ReviewBody(BaseModel):
    comment: str = Field("", max_length=512)
    # 审核时看到的生产版本号：提交时生产若已被改动，拒绝执行，避免「看的是 A、发的是 B」
    expected_prod_version: Optional[int] = None


def env_info() -> dict:
    return {
        "env": CURRENT_ENV,
        "label": ENV_LABEL,
        "is_prod": IS_PROD,
        "is_pre": IS_PRE,
        "prod_port": env_port("prod"),
        "pre_port": env_port("pre"),
        "prod_url": env_url("prod"),
        "pre_url": env_url("pre"),
    }


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _row(r: ReleaseRequest) -> dict:
    return {
        "id": r.id,
        "source_env": r.source_env, "target_env": r.target_env,
        "project_code": r.project_code, "project_name": r.project_name,
        "source_api_id": r.source_api_id, "target_api_id": r.target_api_id,
        "api_name": r.api_name, "method": r.method, "url_path": r.url_path,
        "remark": r.remark,
        "status": r.status, "status_label": STATUS_LABELS.get(r.status, r.status),
        "submitter_username": r.submitter_username, "submitter_name": r.submitter_name,
        "reviewer_username": r.reviewer_username, "reviewer_name": r.reviewer_name,
        "review_comment": r.review_comment,
        "reviewed_at": _fmt(r.reviewed_at), "created_at": _fmt(r.created_at),
    }


@router.get("/env")
async def get_env():
    return R_ok(data=env_info())


@router.get("")
async def list_releases(
    status: str = Query("all", description="all/pending/approved/rejected/cancelled"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db, scope="function"),
    user=Depends(get_current_user),
):
    """管理员看全部；其他人只看自己发起的。"""
    q = select(ReleaseRequest)
    if status != "all":
        q = q.where(ReleaseRequest.status == status)
    if not is_admin_or_above(user):
        q = q.where(ReleaseRequest.submitter_username == user.username)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(
        q.order_by(ReleaseRequest.created_at.desc(), ReleaseRequest.id.desc())
         .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return R_ok(data={
        "items": [_row(r) for r in rows], "total": total, "page": page, "page_size": page_size,
        "can_review": IS_PROD and is_admin_or_above(user),
        "env": env_info(),
    })


@router.post("")
async def create_release(req: ReleaseCreate, db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    """预发环境：把一个已上线的 API 冻结快照，提交到生产待审核。"""
    from app.services.api_lifecycle import STATUS_ONLINE, STATUS_LABELS as API_STATUS_LABELS

    if not IS_PRE:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只能在预发环境发起「发布到生产」")
    if not await can_edit_project_resources(db, user, req.project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权发布该项目的 API")

    api = (await db.execute(
        select(ApiConfig).where(ApiConfig.id == req.api_id, ApiConfig.project_id == req.project_id)
    )).scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)
    if api.status != STATUS_ONLINE:
        label = API_STATUS_LABELS.get(api.status, api.status)
        return R_fail(ErrCode.API_UPDATE_FAILED,
                      msg=f"当前状态「{label}」不能发布到生产：请先在预发走完审批并上线验证")
    if (api.api_type or "sql") == "sync" and not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="远端同步类 API 只能由超级管理员发布")

    project = (await db.execute(select(Project).where(Project.id == req.project_id))).scalar_one_or_none()
    if not project:
        return R_fail(ErrCode.PROJECT_NOT_FOUND)

    dup = (await db.execute(select(ReleaseRequest.id).where(
        ReleaseRequest.status == "pending",
        ReleaseRequest.project_code == project.code,
        ReleaseRequest.method == api.method,
        ReleaseRequest.url_path == api.url_path,
    ))).scalars().first()
    if dup:
        return R_fail(ErrCode.API_UPDATE_FAILED,
                      msg=f"该 API 已有待审核的发布单 #{dup}，请等待审核或先撤回")

    snapshot = await build_snapshot(db, api)
    rr = ReleaseRequest(
        source_env=CURRENT_ENV, target_env="prod",
        project_code=project.code, project_name=project.name,
        source_api_id=api.id, api_name=api.name,
        method=api.method, url_path=api.url_path,
        snapshot=json.dumps(snapshot, ensure_ascii=False),
        remark=req.remark or "",
        status="pending",
        submitter_username=user.username, submitter_name=user.nickname or user.username,
    )
    db.add(rr)
    await db.flush()
    await audit(db, user, "release.submit", "api", api.id,
                f"发布单#{rr.id} {project.code} {api.method} {api.url_path}")
    log.info(f"发起发布到生产 | release_id={rr.id} | {project.code} {api.method} {api.url_path} | by={user.username}")
    return R_ok(data={"id": rr.id}, msg="已提交发布单，请到生产环境由管理员核对差异后审核")


async def _load(db, release_id: int, lock: bool = False) -> Optional[ReleaseRequest]:
    q = select(ReleaseRequest).where(ReleaseRequest.id == release_id)
    if lock:
        q = q.with_for_update()
    return (await db.execute(q)).scalar_one_or_none()


@router.get("/{release_id}")
async def get_release(release_id: int, db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    """发布单详情。生产环境实时对比「待发布快照 vs 生产当前配置」；
    预发环境读不到生产表，已处理的单子展示发布当时的差异，待审核的提示去生产查看。"""
    rr = await _load(db, release_id)
    if not rr:
        return R_fail(ErrCode.SYSTEM_NOT_FOUND, msg="发布单不存在")
    if not is_admin_or_above(user) and rr.submitter_username != user.username:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权查看该发布单")

    snapshot = json.loads(rr.snapshot or "{}")
    data = _row(rr)
    data["env"] = env_info()
    data["can_review"] = IS_PROD and rr.status == "pending" and is_admin_or_above(user)
    data["can_cancel"] = IS_PRE and rr.status == "pending" and (
        rr.submitter_username == user.username or is_super_admin(user))

    if IS_PROD and rr.status == "pending":
        project, prod_api = await find_target(db, rr.project_code, rr.method, rr.url_path)
        before = await build_snapshot(db, prod_api) if prod_api else None
        data["diff"] = diff_snapshots(before, snapshot)
        data["prod_project_exists"] = project is not None
        data["prod_api_id"] = prod_api.id if prod_api else None
        data["prod_version"] = prod_api.version if prod_api else None
        data["prod_status"] = prod_api.status if prod_api else None
        data["diff_basis"] = "live"
    elif rr.status == "approved":
        before = json.loads(rr.prod_before) if rr.prod_before else None
        data["diff"] = diff_snapshots(before, snapshot)
        data["diff_basis"] = "at_release"
    else:
        data["diff"] = None
        data["diff_basis"] = "none"
    return R_ok(data=data)


@router.post("/{release_id}/approve")
async def approve_release(release_id: int, body: ReviewBody,
                          db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    if not IS_PROD:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="发布单只能在生产环境审核")
    if not is_admin_or_above(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有管理员可以审核发布到生产")

    rr = await _load(db, release_id, lock=True)
    if not rr:
        return R_fail(ErrCode.SYSTEM_NOT_FOUND, msg="发布单不存在")
    if rr.status != "pending":
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"该发布单已处理（{STATUS_LABELS.get(rr.status, rr.status)}）")

    # 版本必须和查看差异时一致（生产原先没有该 API 时两边都是 None）
    _, prod_api = await find_target(db, rr.project_code, rr.method, rr.url_path)
    cur_version = prod_api.version if prod_api else None
    if cur_version != body.expected_prod_version:
        return R_fail(ErrCode.API_UPDATE_FAILED,
                      msg="生产环境的该 API 在你查看差异后发生了变化，请刷新后重新核对差异")

    try:
        api, before = await apply_release(
            db, json.loads(rr.snapshot or "{}"), rr.project_code, rr.method, rr.url_path,
            rr.submitter_username, user,
        )
    except ReleaseError as e:
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=str(e))

    rr.status = "approved"
    rr.reviewer_username = user.username
    rr.reviewer_name = user.nickname or user.username
    rr.review_comment = body.comment or ""
    rr.reviewed_at = _cst_now()
    rr.target_api_id = api.id
    rr.prod_before = json.dumps(before, ensure_ascii=False) if before else ""
    await audit(db, user, "release.approve", "api", api.id,
                f"发布单#{rr.id} {rr.project_code} {rr.method} {rr.url_path} -> v{api.version}")
    log.info(f"发布到生产完成 | release_id={rr.id} | api_id={api.id} | version={api.version} | by={user.username}")
    return R_ok(data={"api_id": api.id, "version": api.version}, msg="已发布到生产并上线")


@router.post("/{release_id}/reject")
async def reject_release(release_id: int, body: ReviewBody,
                         db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    if not IS_PROD:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="发布单只能在生产环境审核")
    if not is_admin_or_above(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有管理员可以审核发布到生产")
    rr = await _load(db, release_id, lock=True)
    if not rr:
        return R_fail(ErrCode.SYSTEM_NOT_FOUND, msg="发布单不存在")
    if rr.status != "pending":
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"该发布单已处理（{STATUS_LABELS.get(rr.status, rr.status)}）")
    rr.status = "rejected"
    rr.reviewer_username = user.username
    rr.reviewer_name = user.nickname or user.username
    rr.review_comment = body.comment or ""
    rr.reviewed_at = _cst_now()
    await audit(db, user, "release.reject", "release", rr.id, body.comment or "")
    return R_ok(msg="已驳回")


@router.post("/{release_id}/cancel")
async def cancel_release(release_id: int, db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    if not IS_PRE:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="请在预发环境撤回发布单")
    rr = await _load(db, release_id, lock=True)
    if not rr:
        return R_fail(ErrCode.SYSTEM_NOT_FOUND, msg="发布单不存在")
    if rr.submitter_username != user.username and not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有发起人可以撤回")
    if rr.status != "pending":
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"该发布单已处理（{STATUS_LABELS.get(rr.status, rr.status)}）")
    rr.status = "cancelled"
    await audit(db, user, "release.cancel", "release", rr.id, "")
    return R_ok(msg="已撤回")
