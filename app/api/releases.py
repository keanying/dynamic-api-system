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
from app.services.release import (ReleaseError, SYNC_STATES, apply_release, apply_to_pre, build_snapshot,
                                  delete_everywhere, diff_snapshots, find_target, load_bases, prod_presence,
                                  read_env_project_snapshots, read_env_snapshot, save_base, sync_state)

log = get_logger("releases")

router = APIRouter(prefix="/api/releases", tags=["跨环境发布"])

STATUS_LABELS = {"pending": "待审核", "approved": "已发布", "rejected": "已驳回", "cancelled": "已撤回"}
ACTION_LABELS = {"publish": "发布", "delete": "删除"}


class ReleaseCreate(BaseModel):
    project_id: int
    api_id: int
    remark: str = Field("", max_length=512)
    # 生产和预发都改过时，发布会覆盖生产上的改动，需明确确认 (v2.24)
    force: bool = False


class DeleteRequestBody(BaseModel):
    project_id: int
    api_id: int
    remark: str = Field("", max_length=512)


class PullItem(BaseModel):
    method: str
    url_path: str


class PullBody(BaseModel):
    project_id: int
    items: list[PullItem] = Field(default_factory=list)
    # 预发也改过的 API，拉取会覆盖预发的改动，需明确确认
    overwrite: bool = False


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
        "action": r.action or "publish", "action_label": ACTION_LABELS.get(r.action or "publish", r.action),
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
    if (api.api_type or "sql") in ("sync", "update") and not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="远端同步 / 更新类 API 只能由超级管理员发布")

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

    # 同步检查 (v2.24)：生产在上次同步后改过，预发却没拉取，直接发布会把生产的改动覆盖掉
    try:
        _, prod_snap, _ = await read_env_snapshot(db, "", project.code, api.method, api.url_path)
    except ReleaseError:
        prod_snap = None
    if prod_snap is not None:
        base = (await load_bases(db, project.code)).get((api.method, api.url_path))
        state = sync_state(prod_snap, snapshot, base["snapshot"] if base else None)
        if state == "same":
            return R_fail(ErrCode.API_UPDATE_FAILED, msg="与生产完全一致，不需要发布")
        if state == "prod_ahead":
            return R_fail(ErrCode.API_UPDATE_FAILED, data={"sync_state": state},
                          msg="生产在上次同步后有新的改动，预发还是旧内容，发布会覆盖生产。请先「拉取生产」")
        if state == "both" and not req.force:
            return R_fail(ErrCode.API_UPDATE_FAILED, data={"sync_state": state, "need_force": True},
                          msg="生产和预发都有改动，发布会覆盖生产上的改动，请核对差异后确认")

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


@router.get("/prod-presence")
async def get_prod_presence(project_id: int, db: AsyncSession = Depends(get_db, scope="function"),
                            user=Depends(get_current_user)):
    """预发：本项目哪些 API 已经在生产里（按 方法 + 路径 对应），返回 {api_id: {version, status}}。"""
    if not IS_PRE:
        return R_ok(data={})
    from app.core.permissions import is_project_member
    if not (is_admin_or_above(user) or await is_project_member(db, user, project_id)):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="不是该项目成员")
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not project:
        return R_fail(ErrCode.PROJECT_NOT_FOUND)
    try:
        prod = await prod_presence(db, project.code)
    except Exception as e:  # noqa: BLE001  生产表不存在等
        log.warning(f"读取生产 API 列表失败 | project={project.code} | error={e}")
        return R_ok(data={})
    rows = (await db.execute(
        select(ApiConfig.id, ApiConfig.method, ApiConfig.url_path).where(ApiConfig.project_id == project_id)
    )).all()
    return R_ok(data={str(i): prod[(m, u)] for i, m, u in rows if (m, u) in prod})


@router.post("/delete")
async def create_delete_request(req: DeleteRequestBody, db: AsyncSession = Depends(get_db, scope="function"),
                                user=Depends(get_current_user)):
    """预发：已发布到生产的 API 不能在预发单独删除，提交删除审核；生产管理员同意后两边一起删除。"""
    if not IS_PRE:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="请在预发环境提交删除审核")
    if not await can_edit_project_resources(db, user, req.project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权删除该项目的 API")
    api = (await db.execute(
        select(ApiConfig).where(ApiConfig.id == req.api_id, ApiConfig.project_id == req.project_id)
    )).scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)
    if (api.api_type or "sql") in ("sync", "update") and not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="远端同步类 API 只能由超级管理员删除")
    project = (await db.execute(select(Project).where(Project.id == req.project_id))).scalar_one_or_none()
    if not project:
        return R_fail(ErrCode.PROJECT_NOT_FOUND)
    try:
        _, _, meta = await read_env_snapshot(db, "", project.code, api.method, api.url_path)
    except ReleaseError as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))
    if not meta:
        return R_fail(ErrCode.API_DELETE_FAILED, msg="生产环境没有该 API，直接在预发删除即可")
    dup = (await db.execute(select(ReleaseRequest.id).where(
        ReleaseRequest.status == "pending",
        ReleaseRequest.project_code == project.code,
        ReleaseRequest.method == api.method,
        ReleaseRequest.url_path == api.url_path,
    ))).scalars().first()
    if dup:
        return R_fail(ErrCode.API_DELETE_FAILED, msg=f"该 API 已有待审核的发布单 #{dup}，请等待审核或先撤回")

    snapshot = await build_snapshot(db, api)

    # 同步检查 (v2.24)：生产在上次同步后改过，预发却没拉取，直接发布会把生产的改动覆盖掉
    try:
        _, prod_snap, _ = await read_env_snapshot(db, "", project.code, api.method, api.url_path)
    except ReleaseError:
        prod_snap = None
    if prod_snap is not None:
        base = (await load_bases(db, project.code)).get((api.method, api.url_path))
        state = sync_state(prod_snap, snapshot, base["snapshot"] if base else None)
        if state == "same":
            return R_fail(ErrCode.API_UPDATE_FAILED, msg="与生产完全一致，不需要发布")
        if state == "prod_ahead":
            return R_fail(ErrCode.API_UPDATE_FAILED, data={"sync_state": state},
                          msg="生产在上次同步后有新的改动，预发还是旧内容，发布会覆盖生产。请先「拉取生产」")
        if state == "both" and not req.force:
            return R_fail(ErrCode.API_UPDATE_FAILED, data={"sync_state": state, "need_force": True},
                          msg="生产和预发都有改动，发布会覆盖生产上的改动，请核对差异后确认")

    rr = ReleaseRequest(
        source_env=CURRENT_ENV, target_env="prod", action="delete",
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
    await audit(db, user, "release.delete_submit", "api", api.id,
                f"删除单#{rr.id} {project.code} {api.method} {api.url_path}")
    log.info(f"提交删除审核 | release_id={rr.id} | {project.code} {api.method} {api.url_path} | by={user.username}")
    return R_ok(data={"id": rr.id}, msg="已提交删除审核，生产管理员同意后会同时删除生产和预发中的该 API")


@router.get("/compare")
async def compare_with_other_env(project_id: int, api_id: int,
                                 db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    """当前环境的一个 API 与另一环境同名 API（按 项目编码 + 方法 + 路径 对应）的配置差异 (v2.23)。

    方向固定为「生产当前 → 预发当前」：预发里看就是「发布后生产会变成什么样」。
    """
    from app.core.permissions import is_project_member
    if not (is_admin_or_above(user) or await is_project_member(db, user, project_id)):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="不是该项目成员")
    api = (await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )).scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not project:
        return R_fail(ErrCode.PROJECT_NOT_FOUND)

    mine = await build_snapshot(db, api)
    try:
        exists, other, meta = await read_env_snapshot(db, "" if IS_PRE else "_pre", project.code, api.method, api.url_path)
    except ReleaseError as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))

    data = {
        "api_name": api.name, "method": api.method, "url_path": api.url_path,
        "project_code": project.code, "project_name": project.name,
        "env": env_info(),
        "other_env": "prod" if IS_PRE else "pre",
        "other_project_exists": exists,
        "other_api_exists": other is not None,
        "other_version": meta["version"] if meta else None,
        "other_status": meta["status"] if meta else None,
        "version": api.version, "status": api.status,
    }
    prod_snap, pre_snap = (other, mine) if IS_PRE else (mine, other)
    if exists:
        base = (await load_bases(db, project.code)).get((api.method, api.url_path))
        state = sync_state(prod_snap, pre_snap, base["snapshot"] if base else None)
        data["sync_state"] = state
        data["sync_state_label"] = SYNC_STATES.get(state, state)
        data["base_source"] = base["source"] if base else None
        data["base_at"] = _fmt(base["updated_at"]) if base else ""
        data["can_pull"] = IS_PRE and other is not None and state != "same" \
            and await can_edit_project_resources(db, user, project_id)
    if IS_PRE:
        data["diff"] = diff_snapshots(other, mine, "生产当前", "预发当前")
    elif other is not None:
        data["diff"] = diff_snapshots(mine, other, "生产当前", "预发当前")
    else:
        data["diff"] = None   # 预发没有该 API
    return R_ok(data=data)


@router.get("/sync-status")
async def get_sync_status(project_id: int, db: AsyncSession = Depends(get_db, scope="function"),
                          user=Depends(get_current_user)):
    """预发 (v2.24)：项目内每个 API 与生产的同步状态（类似 git status）。"""
    from app.core.permissions import is_project_member
    if not IS_PRE:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="请在预发环境查看与生产的同步状态")
    if not (is_admin_or_above(user) or await is_project_member(db, user, project_id)):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="不是该项目成员")
    project = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not project:
        return R_fail(ErrCode.PROJECT_NOT_FOUND)
    try:
        prod_all = await read_env_project_snapshots(db, "", project.code)
    except ReleaseError as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))
    if prod_all is None:
        return R_ok(data={"prod_project_exists": False, "items": [], "counts": {}})
    bases = await load_bases(db, project.code)
    pre_apis = (await db.execute(select(ApiConfig).where(ApiConfig.project_id == project_id))).scalars().all()

    items, seen = [], set()
    for api in pre_apis:
        key = (api.method, api.url_path)
        seen.add(key)
        prod_snap, prod_meta = prod_all.get(key, (None, None))
        base = bases.get(key)
        mine = await build_snapshot(db, api)
        state = sync_state(prod_snap, mine, base["snapshot"] if base else None)
        items.append({
            "method": api.method, "url_path": api.url_path, "name": api.name,
            "state": state, "state_label": SYNC_STATES[state],
            "pre_api_id": api.id, "pre_version": api.version, "pre_status": api.status,
            "pre_locked": bool(api.is_locked),
            "prod_api_id": prod_meta["id"] if prod_meta else None,
            "prod_version": prod_meta["version"] if prod_meta else None,
            "prod_status": prod_meta["status"] if prod_meta else None,
            "base_source": base["source"] if base else None,
            "base_at": _fmt(base["updated_at"]) if base else "",
        })
    for key, (snap, meta) in prod_all.items():
        if key in seen:
            continue
        items.append({
            "method": key[0], "url_path": key[1], "name": meta.get("name") or snap.get("name") or "",
            "state": "prod_only", "state_label": SYNC_STATES["prod_only"],
            "pre_api_id": None, "pre_version": None, "pre_status": None, "pre_locked": False,
            "prod_api_id": meta["id"], "prod_version": meta["version"], "prod_status": meta["status"],
            "base_source": None, "base_at": "",
        })
    order = {"prod_ahead": 0, "both": 1, "diverged": 2, "prod_only": 3, "pre_ahead": 4, "pre_only": 5, "same": 6}
    items.sort(key=lambda x: (order.get(x["state"], 9), x["url_path"]))
    counts = {}
    for it in items:
        counts[it["state"]] = counts.get(it["state"], 0) + 1
    return R_ok(data={"prod_project_exists": True, "items": items, "counts": counts,
                      "can_pull": await can_edit_project_resources(db, user, project_id)})


@router.post("/pull")
async def pull_from_prod(body: PullBody, db: AsyncSession = Depends(get_db, scope="function"),
                         user=Depends(get_current_user)):
    """预发 (v2.24)：把生产的配置拉取到预发（类似 git pull），逐个返回结果。"""
    if not IS_PRE:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只能在预发环境拉取生产")
    if not await can_edit_project_resources(db, user, body.project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权修改该项目的 API")
    if not body.items:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="请选择要拉取的 API")
    project = (await db.execute(select(Project).where(Project.id == body.project_id))).scalar_one_or_none()
    if not project:
        return R_fail(ErrCode.PROJECT_NOT_FOUND)
    bases = await load_bases(db, project.code)

    results, ok_count = [], 0
    for it in body.items:
        res = {"method": it.method, "url_path": it.url_path, "ok": False, "msg": ""}
        results.append(res)
        try:
            _, prod_snap, prod_meta = await read_env_snapshot(db, "", project.code, it.method, it.url_path)
            if prod_snap is None:
                res["msg"] = "生产没有该 API"
                continue
            api = (await db.execute(select(ApiConfig).where(
                ApiConfig.project_id == project.id, ApiConfig.method == it.method, ApiConfig.url_path == it.url_path,
            ))).scalar_one_or_none()
            base = bases.get((it.method, it.url_path))
            state = sync_state(prod_snap, await build_snapshot(db, api) if api else None,
                               base["snapshot"] if base else None)
            if state == "same":
                res.update(ok=True, msg="已是最新")
                continue
            if state in ("pre_ahead", "both", "diverged") and not body.overwrite:
                res["msg"] = "预发有未发布到生产的改动，拉取会覆盖，请确认后再拉取"
                res["need_overwrite"] = True
                continue
            async with db.begin_nested():
                api = await apply_to_pre(db, prod_snap, project, api, prod_meta.get("status"), user)
                await save_base(db, project.code, it.method, it.url_path, prod_snap,
                                prod_meta.get("version"), "pull", user.username)
            await audit(db, user, "release.pull", "api", api.id,
                        f"拉取生产 {project.code} {it.method} {it.url_path} 生产 v{prod_meta.get('version')}")
            res.update(ok=True, msg=f"已拉取生产 v{prod_meta.get('version')}", pre_api_id=api.id)
            ok_count += 1
        except ReleaseError as e:
            res["msg"] = str(e)
    await db.commit()
    log.info(f"拉取生产 | project={project.code} | 成功 {ok_count}/{len(body.items)} | by={user.username}")
    failed = [r for r in results if not r["ok"]]
    msg = f"已拉取 {ok_count} 个" + (f"，{len(failed)} 个未拉取" if failed else "")
    return R_ok(data={"results": results, "ok_count": ok_count}, msg=msg)


def _pull_all_denied(user) -> Optional[str]:
    if not IS_PRE:
        return "只能在预发环境执行：方向固定为 生产 → 预发"
    if not is_super_admin(user):
        return "只有超级管理员可以从生产同步全部"
    return None


class PullProjectBody(BaseModel):
    code: str
    overwrite: bool = False   # 预发有未发布改动的 API 也用生产覆盖


@router.get("/pull-all/overview")
async def pull_all_overview(db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    """预发 + 超管 (v2.24)：从生产同步全部 · 第一步，生产的项目列表和要新建的数据源（很快）。"""
    from app.services import env_sync
    denied = _pull_all_denied(user)
    if denied:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=denied)
    try:
        return R_ok(data=await env_sync.overview(db))
    except ReleaseError as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))


@router.get("/pull-all/preview")
async def pull_all_preview(code: str, overwrite: bool = False, db: AsyncSession = Depends(get_db, scope="function"),
                           user=Depends(get_current_user)):
    """预览一个项目：每个 API 会怎么处理（前端逐个项目调用，显示进度）。"""
    from app.services import env_sync
    denied = _pull_all_denied(user)
    if denied:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=denied)
    try:
        return R_ok(data=await env_sync.plan_project(db, code, overwrite))
    except ReleaseError as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))


@router.post("/pull-all/datasources")
async def pull_all_datasources(db: AsyncSession = Depends(get_db, scope="function"), user=Depends(get_current_user)):
    """同步第一批：把预发没有的数据源从生产复制过来。"""
    from app.services import env_sync
    denied = _pull_all_denied(user)
    if denied:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=denied)
    try:
        created = await env_sync.run_datasources(db)
    except ReleaseError as e:
        await db.rollback()
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))
    if created:
        await audit(db, user, "release.pull_all", "datasource", 0, f"从生产同步数据源：{'、'.join(created)}")
    await db.commit()
    return R_ok(data={"created": created}, msg=f"数据源新建 {len(created)} 个")


@router.post("/pull-all/project")
async def pull_all_project(body: PullProjectBody, db: AsyncSession = Depends(get_db, scope="function"),
                           user=Depends(get_current_user)):
    """同步一个项目（前端逐个项目调用；每个项目单独提交，中途停止或失败不影响已完成的项目）。"""
    from app.services import env_sync
    denied = _pull_all_denied(user)
    if denied:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=denied)
    try:
        res = await env_sync.run_project(db, user, body.code, body.overwrite)
    except ReleaseError as e:
        await db.rollback()
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))
    c = res["counts"]
    await audit(db, user, "release.pull_all", "project", 0,
                f"从生产同步项目 {body.code}：API 新建 {c['create']} 覆盖 {c['update']} 一致 {c['same']} "
                f"跳过 {c['conflict'] + c['skip_review']}，失败 {len(res['errors'])}")
    await db.commit()
    return R_ok(data=res)


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

    if (rr.action or "publish") == "delete":
        # 删除单：展示生产当前是否还有该 API、版本号（审核时校验没被改动）
        data["diff"] = None
        data["diff_basis"] = "delete"
        if rr.status == "pending":
            try:
                if IS_PROD:
                    _, prod_api = await find_target(db, rr.project_code, rr.method, rr.url_path)
                    data["prod_version"] = prod_api.version if prod_api else None
                    data["prod_status"] = prod_api.status if prod_api else None
                else:
                    _, _, meta = await read_env_snapshot(db, "", rr.project_code, rr.method, rr.url_path)
                    data["prod_version"] = meta["version"] if meta else None
                    data["prod_status"] = meta["status"] if meta else None
            except ReleaseError as e:
                data["diff_error"] = str(e)
        return R_ok(data=data)
    if rr.status == "approved":
        before = json.loads(rr.prod_before) if rr.prod_before else None
        data["diff"] = diff_snapshots(before, snapshot)
        data["diff_basis"] = "at_release"
    elif IS_PROD:
        project, prod_api = await find_target(db, rr.project_code, rr.method, rr.url_path)
        before = await build_snapshot(db, prod_api) if prod_api else None
        data["diff"] = diff_snapshots(before, snapshot)
        data["prod_project_exists"] = project is not None
        data["prod_api_id"] = prod_api.id if prod_api else None
        data["prod_version"] = prod_api.version if prod_api else None
        data["prod_status"] = prod_api.status if prod_api else None
        data["diff_basis"] = "live"
    else:
        # 预发 (v2.23)：同库直接读生产表，实时对比生产当前配置
        try:
            exists, before, meta = await read_env_snapshot(db, "", rr.project_code, rr.method, rr.url_path)
            data["diff"] = diff_snapshots(before, snapshot)
            data["prod_project_exists"] = exists
            data["prod_version"] = meta["version"] if meta else None
            data["diff_basis"] = "live"
        except ReleaseError as e:
            data["diff"] = None
            data["diff_basis"] = "none"
            data["diff_error"] = str(e)
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

    if (rr.action or "publish") == "delete":
        try:
            prod_id, pre_id = await delete_everywhere(db, rr.project_code, rr.method, rr.url_path)
        except ReleaseError as e:
            return R_fail(ErrCode.API_DELETE_FAILED, msg=str(e))
        rr.status = "approved"
        rr.reviewer_username = user.username
        rr.reviewer_name = user.nickname or user.username
        rr.review_comment = body.comment or ""
        rr.reviewed_at = _cst_now()
        rr.target_api_id = prod_id
        await audit(db, user, "release.delete", "api", prod_id or 0,
                    f"删除单#{rr.id} {rr.project_code} {rr.method} {rr.url_path} 生产 api_id={prod_id} 预发 api_id={pre_id}")
        log.info(f"删除审核通过 | release_id={rr.id} | prod_api={prod_id} | pre_api={pre_id} | by={user.username}")
        return R_ok(data={"prod_api_id": prod_id, "pre_api_id": pre_id}, msg="已删除生产和预发中的该 API")

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
    # 同步基线 (v2.24)：此刻生产 = 预发发布的内容
    await save_base(db, rr.project_code, rr.method, rr.url_path, await build_snapshot(db, api),
                    api.version, "release", user.username)
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
