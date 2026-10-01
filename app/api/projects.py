"""
项目管理路由：CRUD、搜索、导入导出
"""

import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.models.models import Project, ApiConfig, CallLog, ApiParameter
from app.schemas.schemas import ProjectCreate, ProjectUpdate, ProjectOut
from app.api.auth import get_current_user

log = get_logger("projects")

router = APIRouter(prefix="/api/projects", tags=["项目管理"])


@router.get("")
async def list_projects(
    keyword: str = Query("", description="搜索关键字"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """获取项目列表（非超管仅返回自己所属的项目）"""
    from app.core.permissions import is_super_admin
    from app.models.models import ProjectMember
    log.debug(f"查询项目列表 | keyword={keyword} | page={page} | by={_user.username}")

    query = select(Project).where(Project.is_active == True)
    # 归属过滤：非超级管理员只能看到自己是成员的项目
    if not is_super_admin(_user):
        member_pids = (await db.execute(
            select(ProjectMember.project_id).where(ProjectMember.user_id == _user.id)
        )).scalars().all()
        if not member_pids:
            # 不属于任何项目 -> 空列表
            return R_ok(data={"items": [], "total": 0, "page": page, "page_size": page_size})
        query = query.where(Project.id.in_(member_pids))
    if keyword:
        query = query.where(Project.name.contains(keyword))
    query = query.order_by(Project.updated_at.desc())

    # 总数
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # 分页
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    projects = result.scalars().all()

    # v2.19: API 数 / 调用数按整页批量分组统计（原来每个项目各查 2 次）
    pids = [p.id for p in projects]
    api_count_map, call_count_map = {}, {}
    if pids:
        r = await db.execute(
            select(ApiConfig.project_id, func.count()).where(ApiConfig.project_id.in_(pids))
            .group_by(ApiConfig.project_id)
        )
        api_count_map = dict(r.all())
        r = await db.execute(
            select(CallLog.project_id, func.count()).where(CallLog.project_id.in_(pids))
            .group_by(CallLog.project_id)
        )
        call_count_map = dict(r.all())

    items = []
    for p in projects:
        api_count = api_count_map.get(p.id, 0)
        total_calls = call_count_map.get(p.id, 0)

        items.append(ProjectOut(
            id=p.id, code=p.code, name=p.name, description=p.description,
            api_key=p.api_key, is_active=p.is_active,
            api_count=api_count, total_calls=total_calls,
            created_at=p.created_at, updated_at=p.updated_at,
        ))

    log.debug(f"项目列表查询完成 | total={total} | 返回={len(items)}条")
    return R_ok(data={
        "items": [i.model_dump() for i in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get("/{project_id}")
async def get_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """获取项目详情（非成员无权访问）"""
    from app.core.permissions import is_super_admin, is_project_member
    log.debug(f"查询项目详情 | project_id={project_id}")

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        log.warning(f"项目不存在 | project_id={project_id}")
        return R_fail(ErrCode.PROJECT_NOT_FOUND)

    # 归属校验：非超管且非本项目成员 -> 拒绝
    if not is_super_admin(_user) and not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权访问")

    api_count = (await db.execute(
        select(func.count()).where(ApiConfig.project_id == project_id)
    )).scalar() or 0

    # v2.19: 调用数和平均耗时一次扫描算出（原来两次）
    total_calls, avg_time = (await db.execute(
        select(func.count(), func.avg(CallLog.response_time_ms)).where(CallLog.project_id == project_id)
    )).one()
    total_calls = total_calls or 0
    avg_time = avg_time or 0

    log.debug(f"项目详情查询完成 | project_id={project_id} | code={project.code} | api_count={api_count}")
    return R_ok(data={
        **ProjectOut(
            id=project.id, code=project.code, name=project.name, description=project.description,
            api_key=project.api_key, is_active=project.is_active,
            api_count=api_count, total_calls=total_calls,
            created_at=project.created_at, updated_at=project.updated_at,
        ).model_dump(),
        "avg_response_time": round(avg_time, 2),
    })


@router.post("")
async def create_project(
    req: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """创建项目"""
    log.info(f"创建项目请求 | code={req.code} | name={req.name}")

    # 检查编码唯一性
    existing = await db.execute(select(Project).where(Project.code == req.code))
    if existing.scalar_one_or_none():
        log.warning(f"创建项目失败: 编码已存在 | code={req.code}")
        return R_fail(ErrCode.PROJECT_CODE_EXISTS, msg=f"项目编码 '{req.code}' 已存在，请使用其他编码")

    project = Project(code=req.code, name=req.name, description=req.description, api_key=req.api_key)
    db.add(project)
    await db.flush()
    await db.refresh(project)

    # 创建者自动成为该项目的管理员（超管创建也加，便于其直接管理）
    from app.models.models import ProjectMember
    db.add(ProjectMember(project_id=project.id, user_id=_user.id, project_role="manager"))
    await db.flush()

    log.info(f"项目创建成功 | id={project.id} | code={project.code} | name={project.name} | 创建者={_user.username}(项目管理员)")
    return R_ok(data={"id": project.id, "code": project.code}, msg="项目创建成功")


@router.put("/{project_id}")
async def update_project(
    project_id: int,
    req: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """更新项目（超管或本项目管理员可改）"""
    from app.core.permissions import is_super_admin, is_project_manager
    log.info(f"更新项目请求 | project_id={project_id}")

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        log.warning(f"更新项目失败: 项目不存在 | project_id={project_id}")
        return R_fail(ErrCode.PROJECT_NOT_FOUND)

    if not is_super_admin(_user) and not await is_project_manager(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅超管或项目管理员可修改项目信息")

    if req.code is not None and req.code != project.code:
        existing = await db.execute(select(Project).where(Project.code == req.code, Project.id != project_id))
        if existing.scalar_one_or_none():
            log.warning(f"更新项目失败: 编码已存在 | code={req.code}")
            return R_fail(ErrCode.PROJECT_CODE_EXISTS, msg=f"项目编码 '{req.code}' 已存在，请使用其他编码")
        project.code = req.code
    if req.name is not None:
        project.name = req.name
    if req.description is not None:
        project.description = req.description
    if req.api_key is not None:
        project.api_key = req.api_key

    log.info(f"项目更新成功 | project_id={project_id} | code={project.code}")
    return R_ok(msg="项目更新成功")


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """删除项目。

    超级管理员：直接删除（级联 API/日志/成员）。
    项目管理员：发起删除申请，需超管或另一名项目管理员审批后才删除。
    其他人：无权。
    """
    from app.core.permissions import is_super_admin, is_project_manager
    from app.models.models import ProjectDeletionRequest
    from app.services.audit import audit
    log.info(f"删除项目请求 | project_id={project_id} | by={_user.username}")

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return R_fail(ErrCode.PROJECT_NOT_FOUND)

    # 超管：直接删
    if is_super_admin(_user):
        await audit(db, _user, "project.delete", "project", project_id, f"超管直接删除项目 {project.name}")
        await db.delete(project)
        await db.commit()
        log.info(f"项目删除成功(超管) | project_id={project_id} | name={project.name}")
        return R_ok(msg="项目已删除")

    # 项目管理员：发起删除申请
    if not await is_project_manager(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅项目管理员可发起删除项目")

    # 已有进行中的申请则不重复发起
    exist = (await db.execute(
        select(ProjectDeletionRequest).where(
            ProjectDeletionRequest.project_id == project_id,
            ProjectDeletionRequest.status == "pending",
        )
    )).scalar_one_or_none()
    if exist:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="该项目已有待审批的删除申请")

    req = ProjectDeletionRequest(
        project_id=project_id, project_name=project.name,
        requester_id=_user.id, status="pending",
    )
    db.add(req)
    await audit(db, _user, "project.delete.request", "project", project_id, f"发起删除项目申请 {project.name}")
    await db.commit()
    log.info(f"已发起删除项目申请 | project_id={project_id} | by={_user.username}")
    return R_ok(msg="已发起删除申请，等待超管或另一名项目管理员审批")


@router.get("/{project_id}/export")
async def export_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """导出项目配置为 JSON（非成员无权）"""
    from app.core.permissions import is_super_admin, is_project_member
    log.info(f"导出项目请求 | project_id={project_id}")

    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        log.warning(f"导出项目失败: 项目不存在 | project_id={project_id}")
        return R_fail(ErrCode.PROJECT_NOT_FOUND)

    if not is_super_admin(_user) and not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权导出")

    apis_result = await db.execute(
        select(ApiConfig).where(ApiConfig.project_id == project_id)
    )
    apis = apis_result.scalars().all()

    export_data = {
        "project": {
            "code": project.code,
            "name": project.name,
            "description": project.description,
            "api_key": project.api_key,
        },
        "apis": []
    }

    for api in apis:
        params_result = await db.execute(
            select(ApiParameter).where(ApiParameter.api_id == api.id).order_by(ApiParameter.sort_order)
        )
        params = params_result.scalars().all()

        export_data["apis"].append({
            "name": api.name,
            "description": api.description,
            "url_path": api.url_path,
            "method": api.method,
            "sql_template": api.sql_template,
            "is_enabled": api.is_enabled,
            "cache_enabled": api.cache_enabled,
            "cache_ttl": api.cache_ttl,
            "timeout": api.timeout,
            "rate_limit_enabled": api.rate_limit_enabled,
            "rate_limit_qps": api.rate_limit_qps,
            "max_rows": api.max_rows,
            "parameters": [
                {
                    "name": p.name,
                    "param_type": p.param_type,
                    "required": p.required,
                    "default_value": p.default_value,
                    "description": p.description,
                    "sort_order": p.sort_order,
                }
                for p in params
            ],
        })

    log.info(f"项目导出成功 | project_id={project_id} | api_count={len(export_data['apis'])}")
    return R_ok(data=export_data)


# ============ 删除项目审批 (v2.4+) ============
from pydantic import BaseModel as _BaseModel


class _DeletionDecisionReq(_BaseModel):
    decision: str          # approve / reject
    comment: str = ""


@router.get("/deletion-requests/list")
async def list_deletion_requests(
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """列出删除项目申请。

    超管：看全部；项目管理员：看自己是管理员的项目的申请。
    """
    from app.core.permissions import is_super_admin, get_project_role, PROJ_MANAGER
    from app.models.models import ProjectDeletionRequest, User

    q = select(ProjectDeletionRequest)
    if status and status != "all":
        q = q.where(ProjectDeletionRequest.status == status)
    q = q.order_by(ProjectDeletionRequest.created_at.desc())
    reqs = (await db.execute(q)).scalars().all()

    items = []
    for r in reqs:
        # 权限过滤：非超管只能看自己是该项目管理员的
        if not is_super_admin(_user):
            role = await get_project_role(db, _user, r.project_id)
            if role != PROJ_MANAGER:
                continue
        requester = (await db.execute(select(User).where(User.id == r.requester_id))).scalar_one_or_none()
        items.append({
            "id": r.id, "project_id": r.project_id, "project_name": r.project_name,
            "requester": (requester.nickname or requester.username) if requester else f"#{r.requester_id}",
            "requester_id": r.requester_id,
            "reason": r.reason, "status": r.status,
            "approver_comment": r.approver_comment,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "can_approve": (is_super_admin(_user) or True) and r.requester_id != _user.id,
        })
    return R_ok(data={"items": items, "total": len(items)})


@router.post("/deletion-requests/{request_id}/decide")
async def decide_deletion_request(
    request_id: int,
    req: _DeletionDecisionReq,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """审批删除项目申请。审批人：超管 或 该项目的另一名管理员（不能是发起人）。"""
    from app.core.permissions import is_super_admin, is_project_manager
    from app.models.models import ProjectDeletionRequest
    from app.services.audit import audit

    dr = (await db.execute(
        select(ProjectDeletionRequest).where(ProjectDeletionRequest.id == request_id)
    )).scalar_one_or_none()
    if not dr or dr.status != "pending":
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="申请不存在或已处理")

    # 审批权限：超管，或该项目的项目管理员
    allowed = is_super_admin(_user) or await is_project_manager(db, _user, dr.project_id)
    if not allowed:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权审批该删除申请")
    # 发起人不能审批自己
    if dr.requester_id == _user.id:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="不能审批自己发起的删除申请")

    if req.decision not in ("approve", "reject"):
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="decision 必须是 approve / reject")

    dr.approver_id = _user.id
    dr.approver_comment = req.comment or ""

    if req.decision == "reject":
        dr.status = "rejected"
        await audit(db, _user, "project.delete.reject", "project", dr.project_id, f"驳回删除申请 {dr.project_name}")
        await db.commit()
        return R_ok(msg="已驳回删除申请")

    # 通过 -> 真正删除项目
    project = (await db.execute(select(Project).where(Project.id == dr.project_id))).scalar_one_or_none()
    if project:
        await db.delete(project)
    dr.status = "approved"
    await audit(db, _user, "project.delete.approve", "project", dr.project_id, f"批准并删除项目 {dr.project_name}")
    await db.commit()
    log.info(f"删除项目申请通过，项目已删除 | project_id={dr.project_id} | approver={_user.username}")
    return R_ok(msg="已通过，项目已删除")


@router.post("/deletion-requests/{request_id}/cancel")
async def cancel_deletion_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """发起人撤销自己的删除申请。"""
    from app.core.permissions import is_super_admin
    from app.models.models import ProjectDeletionRequest

    dr = (await db.execute(
        select(ProjectDeletionRequest).where(ProjectDeletionRequest.id == request_id)
    )).scalar_one_or_none()
    if not dr or dr.status != "pending":
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="申请不存在或已处理")
    if dr.requester_id != _user.id and not is_super_admin(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有发起人可撤销")
    dr.status = "canceled"
    await db.commit()
    return R_ok(msg="已撤销删除申请")
