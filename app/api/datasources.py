"""
数据源管理路由：CRUD、连接测试
"""

import json
import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.core.timezone import now as _cst_now
from app.core.database import get_db
from app.core.security import encrypt_value, decrypt_value
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.models.models import DataSource, ApiConfig
from app.schemas.schemas import DataSourceCreate, DataSourceUpdate, DataSourceOut
from app.api.auth import get_current_user

log = get_logger("datasources")

router = APIRouter(prefix="/api/datasources", tags=["数据源管理"])


@router.get("")
async def list_datasources(
    keyword: str = Query("", description="搜索关键字"),
    ds_type: str = Query("all", description="类型筛选: all/mysql/redis/postgresql"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """获取数据源列表"""
    log.debug(f"查询数据源列表 | keyword={keyword} | type={ds_type} | page={page}")

    query = select(DataSource)
    if keyword:
        query = query.where(DataSource.name.contains(keyword))
    if ds_type != "all":
        query = query.where(DataSource.type == ds_type)
    query = query.order_by(DataSource.updated_at.desc())

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    datasources = result.scalars().all()

    # 批量取创建人名
    from app.models.models import User as _U
    creator_ids = list({ds.created_by for ds in datasources if getattr(ds, "created_by", None)})
    cname = {}
    if creator_ids:
        us = (await db.execute(select(_U).where(_U.id.in_(creator_ids)))).scalars().all()
        cname = {u.id: (u.nickname or u.username) for u in us}

    items = []
    for ds in datasources:
        api_count = (await db.execute(
            select(func.count()).where(ApiConfig.datasource_id == ds.id)
        )).scalar() or 0

        cb = getattr(ds, "created_by", None)
        items.append(DataSourceOut(
            id=ds.id, name=ds.name, type=ds.type,
            host=ds.host, port=ds.port, username=ds.username,
            database_name=ds.database_name, pool_size=ds.pool_size,
            extra_config=ds.extra_config, status=ds.status,
            last_test_at=ds.last_test_at, api_count=api_count,
            created_by=cb, created_by_name=cname.get(cb, "") if cb else "",
            created_at=ds.created_at, updated_at=ds.updated_at,
        ))

    log.debug(f"数据源列表查询完成 | total={total} | 返回={len(items)}条")
    return R_ok(data={
        "items": [i.model_dump() for i in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get("/{ds_id}")
async def get_datasource(
    ds_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """获取数据源详情"""
    log.debug(f"查询数据源详情 | ds_id={ds_id}")

    result = await db.execute(select(DataSource).where(DataSource.id == ds_id))
    ds = result.scalar_one_or_none()
    if not ds:
        log.warning(f"数据源不存在 | ds_id={ds_id}")
        return R_fail(ErrCode.DS_NOT_FOUND)

    api_count = (await db.execute(
        select(func.count()).where(ApiConfig.datasource_id == ds.id)
    )).scalar() or 0

    return R_ok(data=DataSourceOut(
        id=ds.id, name=ds.name, type=ds.type,
        host=ds.host, port=ds.port, username=ds.username,
        database_name=ds.database_name, pool_size=ds.pool_size,
        extra_config=ds.extra_config, status=ds.status,
        last_test_at=ds.last_test_at, api_count=api_count,
        created_at=ds.created_at, updated_at=ds.updated_at,
    ).model_dump())


@router.post("")
async def create_datasource(
    req: DataSourceCreate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """创建数据源"""
    log.info(f"创建数据源请求 | name={req.name} | type={req.type} | host={req.host}:{req.port} | db={req.database_name}")

    ds = DataSource(
        name=req.name, type=req.type,
        host=req.host, port=req.port,
        username=req.username,
        password_encrypted=encrypt_value(req.password) if req.password else "",
        database_name=req.database_name,
        pool_size=req.pool_size,
        extra_config=req.extra_config,
        created_by=_user.id,
    )
    db.add(ds)
    await db.flush()
    await db.refresh(ds)

    log.info(f"数据源创建成功 | id={ds.id} | name={ds.name} | type={ds.type}")
    return R_ok(data={"id": ds.id}, msg="数据源创建成功")


@router.put("/{ds_id}")
async def update_datasource(
    ds_id: int,
    req: DataSourceUpdate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """更新数据源"""
    log.info(f"更新数据源请求 | ds_id={ds_id} | data={req.model_dump(exclude_none=True, exclude={'password'})}")

    result = await db.execute(select(DataSource).where(DataSource.id == ds_id))
    ds = result.scalar_one_or_none()
    if not ds:
        log.warning(f"更新数据源失败: 数据源不存在 | ds_id={ds_id}")
        return R_fail(ErrCode.DS_NOT_FOUND)

    if req.name is not None:
        ds.name = req.name
    if req.type is not None:
        ds.type = req.type
    if req.host is not None:
        ds.host = req.host
    if req.port is not None:
        ds.port = req.port
    if req.username is not None:
        ds.username = req.username
    if req.password is not None and req.password:
        ds.password_encrypted = encrypt_value(req.password)
        log.debug(f"数据源密码已更新 | ds_id={ds_id}")
    if req.database_name is not None:
        ds.database_name = req.database_name
    if req.pool_size is not None:
        ds.pool_size = req.pool_size
    if req.extra_config is not None:
        ds.extra_config = req.extra_config

    log.info(f"数据源更新成功 | ds_id={ds_id} | name={ds.name}")
    return R_ok(msg="数据源更新成功")


@router.delete("/{ds_id}")
async def delete_datasource(
    ds_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """删除数据源。

    - 有 API 依赖 -> 一律不允许删除（无论谁）。
    - 超级管理员 -> 直接删除。
    - 管理员 -> 发起删除审批（需另一名管理员或超管审批）。
    - 其他用户 -> 无权。
    """
    from app.core.permissions import is_super_admin, is_admin_or_above
    from app.models.models import DataSourceDeletionRequest
    from app.services.audit import audit
    log.info(f"删除数据源请求 | ds_id={ds_id} | by={_user.username}")

    result = await db.execute(select(DataSource).where(DataSource.id == ds_id))
    ds = result.scalar_one_or_none()
    if not ds:
        return R_fail(ErrCode.DS_NOT_FOUND)

    # 依赖检查：有 API 在用则不允许删除
    api_count = (await db.execute(
        select(func.count()).where(ApiConfig.datasource_id == ds_id)
    )).scalar() or 0
    if api_count > 0:
        return R_fail(ErrCode.DS_IN_USE, msg=f"该数据源被 {api_count} 个 API 引用，不允许删除，请先解除关联")

    # 权限：仅管理员及以上
    if not is_admin_or_above(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅管理员或超级管理员可删除数据源")

    # 超管直接删
    if is_super_admin(_user):
        await audit(db, _user, "datasource.delete", "datasource", ds_id, f"超管直接删除数据源 {ds.name}")
        await db.delete(ds)
        await db.commit()
        log.info(f"数据源删除成功(超管) | ds_id={ds_id} | name={ds.name}")
        return R_ok(msg="数据源已删除")

    # 管理员发起审批
    exist = (await db.execute(
        select(DataSourceDeletionRequest).where(
            DataSourceDeletionRequest.datasource_id == ds_id,
            DataSourceDeletionRequest.status == "pending",
        )
    )).scalar_one_or_none()
    if exist:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="该数据源已有待审批的删除申请")

    dr = DataSourceDeletionRequest(
        datasource_id=ds_id, datasource_name=ds.name,
        requester_id=_user.id, status="pending",
    )
    db.add(dr)
    await audit(db, _user, "datasource.delete.request", "datasource", ds_id, f"发起删除数据源申请 {ds.name}")
    await db.commit()
    log.info(f"已发起删除数据源申请 | ds_id={ds_id} | by={_user.username}")
    return R_ok(msg="已发起删除申请，等待另一名管理员或超管审批")


@router.post("/{ds_id}/test")
async def test_datasource(
    ds_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """测试数据源连接"""
    log.info(f"测试数据源连接 | ds_id={ds_id}")

    result = await db.execute(select(DataSource).where(DataSource.id == ds_id))
    ds = result.scalar_one_or_none()
    if not ds:
        log.warning(f"测试连接失败: 数据源不存在 | ds_id={ds_id}")
        return R_fail(ErrCode.DS_NOT_FOUND)

    password = decrypt_value(ds.password_encrypted) if ds.password_encrypted else ""
    log.debug(f"测试连接参数 | type={ds.type} | host={ds.host}:{ds.port} | db={ds.database_name}")

    try:
        # MySQL 协议家族：MySQL / StarRocks / SelectDB / Doris 都用 aiomysql
        if ds.type in ("mysql", "starrocks", "selectdb", "doris"):
            import aiomysql
            log.debug(f"正在连接 {ds.type} | {ds.host}:{ds.port}/{ds.database_name}")
            conn = await aiomysql.connect(
                host=ds.host, port=ds.port,
                user=ds.username, password=password,
                db=ds.database_name,
                connect_timeout=5,
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
            conn.close()
            log.info(f"{ds.type} 连接测试成功 | ds_id={ds_id} | {ds.host}:{ds.port}/{ds.database_name}")

        elif ds.type == "redis":
            import redis.asyncio as aioredis
            log.debug(f"正在连接 Redis | {ds.host}:{ds.port}")
            r = aioredis.Redis(
                host=ds.host, port=ds.port,
                password=password or None,
                db=int(ds.database_name or 0),
                socket_connect_timeout=5,
            )
            await r.ping()
            await r.close()
            log.info(f"Redis 连接测试成功 | ds_id={ds_id} | {ds.host}:{ds.port}")

        elif ds.type == "postgresql":
            log.warning(f"PostgreSQL 连接测试暂未实现 | ds_id={ds_id}")
            return R_fail(ErrCode.DS_CONNECT_FAILED, msg="PostgreSQL 连接测试暂未实现")
        else:
            log.warning(f"不支持的数据源类型 | ds_id={ds_id} | type={ds.type}")
            return R_fail(ErrCode.DS_CONNECT_FAILED, msg=f"不支持的数据源类型: {ds.type}")

        ds.status = "connected"
        ds.last_test_at = _cst_now()
        return R_ok(msg="连接成功")

    except Exception as e:
        log.error(f"数据源连接测试失败 | ds_id={ds_id} | type={ds.type} | error={str(e)}")
        ds.status = "error"
        ds.last_test_at = _cst_now()
        return R_fail(ErrCode.DS_CONNECT_FAILED, msg=f"连接失败: {str(e)}")


# ============ 数据源使用情况 + 删除审批 (v2.7+) ============
from pydantic import BaseModel as _BaseModel
from app.models.models import Project as _Project, DataSourceDeletionRequest as _DSDel, User as _User
from app.api.auth import get_current_user as _gcu


class _DSDelDecisionReq(_BaseModel):
    decision: str
    comment: str = ""


@router.get("/{ds_id}/usage")
async def datasource_usage(
    ds_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(_gcu),
):
    """查看数据源被哪些项目 / API 引用。"""
    apis = (await db.execute(
        select(ApiConfig).where(ApiConfig.datasource_id == ds_id)
    )).scalars().all()
    # 按项目聚合
    proj_ids = list({a.project_id for a in apis})
    pname = {}
    if proj_ids:
        prs = (await db.execute(select(_Project).where(_Project.id.in_(proj_ids)))).scalars().all()
        pname = {p.id: p.name for p in prs}
    projects = []
    for pid in proj_ids:
        cnt = len([a for a in apis if a.project_id == pid])
        projects.append({"project_id": pid, "project_name": pname.get(pid, f"#{pid}"), "api_count": cnt})
    api_items = [{"id": a.id, "name": a.name, "project_id": a.project_id,
                  "project_name": pname.get(a.project_id, "")} for a in apis]
    return R_ok(data={
        "total_apis": len(apis),
        "projects": projects,
        "apis": api_items,
        "deletable": len(apis) == 0,
    })


@router.get("/deletion-requests/list")
async def list_ds_deletion_requests(
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
    _user=Depends(_gcu),
):
    """删除数据源申请列表（管理员/超管可见）。"""
    from app.core.permissions import is_admin_or_above
    if not is_admin_or_above(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权查看")
    q = select(_DSDel)
    if status and status != "all":
        q = q.where(_DSDel.status == status)
    q = q.order_by(_DSDel.created_at.desc())
    reqs = (await db.execute(q)).scalars().all()
    items = []
    for r in reqs:
        u = (await db.execute(select(_User).where(_User.id == r.requester_id))).scalar_one_or_none()
        items.append({
            "id": r.id, "datasource_id": r.datasource_id, "datasource_name": r.datasource_name,
            "requester": (u.nickname or u.username) if u else f"#{r.requester_id}",
            "requester_id": r.requester_id, "reason": r.reason,
            "status": r.status, "approver_comment": r.approver_comment,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return R_ok(data={"items": items, "total": len(items)})


@router.post("/deletion-requests/{request_id}/decide")
async def decide_ds_deletion(
    request_id: int,
    req: _DSDelDecisionReq,
    db: AsyncSession = Depends(get_db),
    _user=Depends(_gcu),
):
    """审批删除数据源申请。审批人：管理员/超管，且不能是发起人。"""
    from app.core.permissions import is_admin_or_above
    from app.services.audit import audit

    dr = (await db.execute(select(_DSDel).where(_DSDel.id == request_id))).scalar_one_or_none()
    if not dr or dr.status != "pending":
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="申请不存在或已处理")
    if not is_admin_or_above(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权审批")
    if dr.requester_id == _user.id:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="不能审批自己发起的申请")
    if req.decision not in ("approve", "reject"):
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="decision 必须是 approve / reject")

    dr.approver_id = _user.id
    dr.approver_comment = req.comment or ""

    if req.decision == "reject":
        dr.status = "rejected"
        await audit(db, _user, "datasource.delete.reject", "datasource", dr.datasource_id, f"驳回删除 {dr.datasource_name}")
        await db.commit()
        return R_ok(msg="已驳回删除申请")

    # 通过前再次确认无 API 依赖（防审批期间被引用）
    api_count = (await db.execute(
        select(func.count()).where(ApiConfig.datasource_id == dr.datasource_id)
    )).scalar() or 0
    if api_count > 0:
        return R_fail(ErrCode.DS_IN_USE, msg=f"该数据源现被 {api_count} 个 API 引用，无法删除")

    ds = (await db.execute(select(DataSource).where(DataSource.id == dr.datasource_id))).scalar_one_or_none()
    if ds:
        await db.delete(ds)
    dr.status = "approved"
    await audit(db, _user, "datasource.delete.approve", "datasource", dr.datasource_id, f"批准删除数据源 {dr.datasource_name}")
    await db.commit()
    log.info(f"删除数据源申请通过 | ds_id={dr.datasource_id} | approver={_user.username}")
    return R_ok(msg="已通过，数据源已删除")


@router.post("/deletion-requests/{request_id}/cancel")
async def cancel_ds_deletion(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    _user=Depends(_gcu),
):
    """发起人撤销删除申请。"""
    from app.core.permissions import is_super_admin
    dr = (await db.execute(select(_DSDel).where(_DSDel.id == request_id))).scalar_one_or_none()
    if not dr or dr.status != "pending":
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="申请不存在或已处理")
    if dr.requester_id != _user.id and not is_super_admin(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有发起人可撤销")
    dr.status = "canceled"
    await db.commit()
    return R_ok(msg="已撤销")
