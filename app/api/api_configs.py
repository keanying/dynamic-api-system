"""
API 配置管理路由：CRUD、复制、启用/禁用
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.models.models import ApiConfig, ApiParameter, CallLog, DataSource
from app.schemas.schemas import (
    ApiConfigCreate, ApiConfigUpdate, ApiConfigOut, ApiParameterOut,
)
from app.api.auth import get_current_user

log = get_logger("api_configs")

router = APIRouter(prefix="/api/projects/{project_id}/apis", tags=["API配置"])


async def datasource_scope_error(db, project_id: int, datasource_id):
    """所选数据源未对本项目开放时返回错误信息（v2.21，保存时提前提示，执行时还会再校验）。"""
    if not datasource_id:
        return None
    from app.services import ds_scope
    ds = (await db.execute(select(DataSource).where(DataSource.id == datasource_id))).scalar_one_or_none()
    if ds is not None and not ds_scope.is_allowed(ds, await ds_scope.project_code(db, project_id)):
        return f"数据源「{ds.name}」未对本项目开放"
    return None


def plugin_edit_denied(user) -> bool:
    """插件代码是在服务进程里直接执行的 Python（可读配置里的数据库密码、执行系统命令），
    v2.20 起默认仅超级管理员可创建/修改插件类 API（与插件库、数据同步类 API 的权限一致）。
    config: security.plugin_editor = super_admin（默认）/ developer（恢复旧行为：项目研发即可）。"""
    from app.core.config import settings
    from app.core.permissions import is_super_admin
    if (getattr(settings.security, "plugin_editor", "super_admin") or "super_admin") == "developer":
        return False
    return not is_super_admin(user)


@router.get("")
async def list_apis(
    project_id: int,
    keyword: str = Query("", description="搜索关键字"),
    status: str = Query("all", description="状态筛选: all/enabled/disabled"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """获取项目下的 API 列表（非成员无权）"""
    from app.core.permissions import is_super_admin, is_project_member
    log.debug(f"查询 API 列表 | project_id={project_id} | keyword={keyword} | status={status} | page={page}")

    if not is_super_admin(_user) and not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权访问")

    query = select(ApiConfig).where(ApiConfig.project_id == project_id)
    if keyword:
        query = query.where(
            (ApiConfig.name.contains(keyword)) | (ApiConfig.url_path.contains(keyword))
        )
    if status == "enabled":
        query = query.where(ApiConfig.is_enabled == True)
    elif status == "disabled":
        query = query.where(ApiConfig.is_enabled == False)

    query = query.order_by(ApiConfig.updated_at.desc())

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    apis = result.scalars().all()

    # 批量取责任人名称（避免逐个查询）
    from app.models.models import User as _U
    owner_ids = list({a.owner_id for a in apis if getattr(a, "owner_id", None)})
    owner_name_map = {}
    if owner_ids:
        ur = await db.execute(select(_U).where(_U.id.in_(owner_ids)))
        owner_name_map = {u.id: (u.nickname or u.username) for u in ur.scalars().all()}

    # v2.19: 参数 / 数据源名 / 调用统计改为整页批量查询（原来每个 API 各查 4 次，
    # 一页 20 个就是 80 次查询，其中 40 次是对 call_logs 的聚合）
    api_ids = [a.id for a in apis]
    params_map = {i: [] for i in api_ids}
    ds_name_map = {}
    stats_map = {}
    if api_ids:
        pr = await db.execute(
            select(ApiParameter).where(ApiParameter.api_id.in_(api_ids))
            .order_by(ApiParameter.api_id, ApiParameter.sort_order)
        )
        for p in pr.scalars().all():
            params_map[p.api_id].append(p)

        ds_ids = list({a.datasource_id for a in apis if a.datasource_id})
        if ds_ids:
            dr = await db.execute(select(DataSource.id, DataSource.name).where(DataSource.id.in_(ds_ids)))
            ds_name_map = {r.id: r.name for r in dr.all()}

        sr = await db.execute(
            select(CallLog.api_id, func.count().label("cnt"), func.avg(CallLog.response_time_ms).label("avg_ms"))
            .where(CallLog.api_id.in_(api_ids))
            .group_by(CallLog.api_id)
        )
        stats_map = {r.api_id: (r.cnt, r.avg_ms) for r in sr.all()}

    # 待上线状态：查出该 API 已通过审批单的提交者，供前端判断"上线"按钮
    appr_map = {}
    approved_ids = [a.id for a in apis if getattr(a, "status", "") == "approved"]
    if approved_ids:
        from app.models.models import ApiApproval
        ar = await db.execute(
            select(ApiApproval).where(
                ApiApproval.api_id.in_(approved_ids),
                ApiApproval.overall_status == "approved",
            ).order_by(ApiApproval.created_at.desc())
        )
        for ap in ar.scalars().all():
            appr_map.setdefault(ap.api_id, ap.submitter_id)   # 每个 API 取最新一张

    items = []
    for api in apis:
        params = params_map.get(api.id, [])
        ds_name = ds_name_map.get(api.datasource_id, "") if api.datasource_id else ""
        total_calls, avg_time = stats_map.get(api.id, (0, 0))
        total_calls = total_calls or 0
        avg_time = avg_time or 0
        appr_submitter = appr_map.get(api.id)

        items.append(ApiConfigOut(
            id=api.id, project_id=api.project_id, datasource_id=api.datasource_id,
            owner_id=getattr(api, "owner_id", None),
            owner_name=owner_name_map.get(getattr(api, "owner_id", None), ""),
            name=api.name, description=api.description,
            url_path=api.url_path, method=api.method,
            sql_template=api.sql_template,
            pipeline_steps=api.pipeline_steps or "",
            api_type=api.api_type or "sql",
            html_content=api.html_content or "",
            css_content=api.css_content or "",
            js_content=api.js_content or "",
            plugin_code=getattr(api, "plugin_code", "") or "",
            status=getattr(api, "status", "draft") or "draft",
            is_locked=bool(getattr(api, "is_locked", False)),
            approval_submitter_id=appr_submitter,
            is_enabled=api.is_enabled,
            api_key=api.api_key,
            require_api_key=bool(api.require_api_key) if api.require_api_key is not None else True,
            cache_enabled=api.cache_enabled,
            cache_prewarm=bool(getattr(api, 'cache_prewarm', False)),
            prewarm_param_overrides=getattr(api, 'prewarm_param_overrides', '') or '',
            prewarm_stop_daily=bool(getattr(api, 'prewarm_stop_daily', False)),
            sync_tables=getattr(api, 'sync_tables', '') or '',
            cache_ttl=api.cache_ttl, timeout=api.timeout,
            rate_limit_enabled=api.rate_limit_enabled,
            rate_limit_qps=api.rate_limit_qps,
            max_rows=api.max_rows, version=api.version,
            parameters=[ApiParameterOut.model_validate(p) for p in params],
            datasource_name=ds_name,
            total_calls=total_calls,
            avg_time_ms=round(avg_time, 2),
            created_at=api.created_at, updated_at=api.updated_at,
        ))

    log.debug(f"API 列表查询完成 | project_id={project_id} | total={total} | 返回={len(items)}条")
    return R_ok(data={
        "items": [i.model_dump() for i in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get("/{api_id}")
async def get_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """获取 API 详情（非项目成员无权查看：含 SQL 与 API Key）"""
    from app.core.permissions import is_project_member
    log.debug(f"查询 API 详情 | project_id={project_id} | api_id={api_id}")
    if not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权访问")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        log.warning(f"API 不存在 | project_id={project_id} | api_id={api_id}")
        return R_fail(ErrCode.API_NOT_FOUND)

    params_r = await db.execute(
        select(ApiParameter).where(ApiParameter.api_id == api.id).order_by(ApiParameter.sort_order)
    )
    params = params_r.scalars().all()

    ds_name = ""
    if api.datasource_id:
        ds_r = await db.execute(select(DataSource.name).where(DataSource.id == api.datasource_id))
        ds_name = ds_r.scalar() or ""

    # 责任人名称
    owner_name = ""
    if getattr(api, "owner_id", None):
        from app.models.models import User as _U
        ow = await db.execute(select(_U).where(_U.id == api.owner_id))
        owu = ow.scalar_one_or_none()
        owner_name = (owu.nickname or owu.username) if owu else ""

    total_calls = (await db.execute(
        select(func.count()).where(CallLog.api_id == api.id)
    )).scalar() or 0

    avg_time = (await db.execute(
        select(func.avg(CallLog.response_time_ms)).where(CallLog.api_id == api.id)
    )).scalar() or 0

    out = ApiConfigOut(
        id=api.id, project_id=api.project_id, datasource_id=api.datasource_id,
        owner_id=getattr(api, "owner_id", None), owner_name=owner_name,
        name=api.name, description=api.description,
        url_path=api.url_path, method=api.method,
        sql_template=api.sql_template,
        pipeline_steps=api.pipeline_steps or "",
        api_type=api.api_type or "sql",
        html_content=api.html_content or "",
        css_content=api.css_content or "",
        js_content=api.js_content or "",
        plugin_code=getattr(api, "plugin_code", "") or "",
        status=getattr(api, "status", "draft") or "draft",
        is_locked=bool(getattr(api, "is_locked", False)),
        is_enabled=api.is_enabled,
        api_key=api.api_key,
        require_api_key=bool(api.require_api_key) if api.require_api_key is not None else True,
        cache_enabled=api.cache_enabled,
        cache_prewarm=bool(getattr(api, 'cache_prewarm', False)),
        prewarm_param_overrides=getattr(api, 'prewarm_param_overrides', '') or '',
        prewarm_stop_daily=bool(getattr(api, 'prewarm_stop_daily', False)),
        sync_tables=getattr(api, 'sync_tables', '') or '',
        cache_ttl=api.cache_ttl, timeout=api.timeout,
        rate_limit_enabled=api.rate_limit_enabled,
        rate_limit_qps=api.rate_limit_qps,
        max_rows=api.max_rows, version=api.version,
        parameters=[ApiParameterOut.model_validate(p) for p in params],
        datasource_name=ds_name,
        total_calls=total_calls,
        avg_time_ms=round(avg_time, 2),
        created_at=api.created_at, updated_at=api.updated_at,
    )

    log.debug(f"API 详情查询完成 | api_id={api_id} | name={api.name} | url_path={api.url_path}")
    return R_ok(data=out.model_dump())


@router.post("")
async def create_api(
    project_id: int,
    req: ApiConfigCreate,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """创建 API"""
    from app.core.permissions import can_edit_project_resources
    log.info(f"创建 API 请求 | project_id={project_id} | name={req.name} | url_path={req.url_path} | method={req.method}")

    # 权限：项目管理员/研发/超管（v2.20：原来任何登录用户都能往任意项目里建 API）
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权在该项目中创建 API")
    if ((req.api_type or "sql").lower() == "plugin" or (getattr(req, "plugin_code", "") or "").strip()) \
            and plugin_edit_denied(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="插件类 API 只能由超级管理员创建")

    # 数据同步 API (v2.17+)：写入类接口，只有超级管理员能创建
    if (getattr(req, "api_type", "sql") or "sql").lower() == "sync":
        from app.core.permissions import is_super_admin
        if not is_super_admin(_user):
            return R_fail(ErrCode.AUTH_PERMISSION_DENIED,
                          msg="远端同步类 API 只能由超级管理员创建")
        from app.services.data_sync import parse_whitelist, SyncError
        try:
            parse_whitelist(getattr(req, "sync_tables", "") or "")
        except SyncError as e:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"同步表白名单配置有误：{e}")

    # 自动预热依赖缓存：预热的本质是「提前把结果写进缓存」，没开缓存就无处可写
    if getattr(req, "cache_prewarm", False) and not req.cache_enabled:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID,
                      msg="开启「自动预热」需要先开启「缓存」——预热是把结果提前写入缓存，未启用缓存时不会生效")

    scope_err = await datasource_scope_error(db, project_id, req.datasource_id)
    if scope_err:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=scope_err)

    # 检查路径冲突
    existing = await db.execute(
        select(ApiConfig).where(
            ApiConfig.project_id == project_id,
            ApiConfig.url_path == req.url_path,
            ApiConfig.method == req.method,
        )
    )
    if existing.scalar_one_or_none():
        log.warning(f"创建 API 失败: 路径冲突 | {req.method} {req.url_path}")
        return R_fail(ErrCode.API_PATH_CONFLICT, msg=f"路径冲突: {req.method} {req.url_path} 已存在")

    api = ApiConfig(
        project_id=project_id,
        name=req.name, description=req.description,
        url_path=req.url_path, method=req.method,
        datasource_id=req.datasource_id,
        sql_template=req.sql_template,
        pipeline_steps=req.pipeline_steps or "",
        api_type=(req.api_type or "sql").lower(),
        html_content=req.html_content or "",
        css_content=req.css_content or "",
        js_content=req.js_content or "",
        plugin_code=getattr(req, "plugin_code", "") or "",
        is_enabled=req.is_enabled,
        api_key=req.api_key,
        require_api_key=bool(req.require_api_key) if req.require_api_key is not None else True,
        cache_enabled=req.cache_enabled,
        cache_prewarm=bool(getattr(req, 'cache_prewarm', False)),
        prewarm_param_overrides=getattr(req, 'prewarm_param_overrides', '') or '',
        prewarm_stop_daily=bool(getattr(req, 'prewarm_stop_daily', False)),
        sync_tables=getattr(req, 'sync_tables', '') or '',
        cache_ttl=req.cache_ttl, timeout=req.timeout,
        rate_limit_enabled=req.rate_limit_enabled,
        rate_limit_qps=req.rate_limit_qps,
        max_rows=req.max_rows,
        created_by=_user.id,
        owner_id=_user.id,   # v2.10: 创建人即默认责任人
    )
    db.add(api)
    await db.flush()

    # 添加参数
    for idx, p in enumerate(req.parameters):
        param = ApiParameter(
            api_id=api.id, name=p.name, param_type=p.param_type,
            required=p.required, default_value=p.default_value,
            description=p.description, sort_order=p.sort_order or idx,
            item_schema=getattr(p, "item_schema", "") or "",
        )
        db.add(param)
        log.debug(f"添加参数 | api_id={api.id} | param={p.name} | type={p.param_type} | required={p.required}")

    await db.flush()
    await db.refresh(api)

    log.info(f"API 创建成功 | id={api.id} | name={api.name} | url_path={api.url_path} | 参数数={len(req.parameters)}")
    return R_ok(data={"id": api.id}, msg="API 创建成功")


@router.put("/{api_id}")
async def update_api(
    project_id: int,
    api_id: int,
    req: ApiConfigUpdate,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """更新 API"""
    from app.core.permissions import can_edit_project_resources
    from app.services.api_lifecycle import can_edit, STATUS_LABELS
    log.info(f"更新 API 请求 | project_id={project_id} | api_id={api_id}")

    # 权限：必须是项目可编辑成员（manager/developer/超管）
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权编辑该项目的 API")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        log.warning(f"更新 API 失败: API 不存在 | api_id={api_id}")
        return R_fail(ErrCode.API_NOT_FOUND)

    # 锁定守卫：锁定的 API 不可编辑，需先解锁
    if getattr(api, "is_locked", False):
        return R_fail(ErrCode.API_UPDATE_FAILED, msg="该 API 已锁定，请先解锁后再编辑")

    # 状态守卫：已上线 / 待上线 不可编辑（需先下线回到草稿）
    cur_status = getattr(api, "status", "draft")
    if not can_edit(cur_status):
        label = STATUS_LABELS.get(cur_status, cur_status)
        return R_fail(
            ErrCode.API_UPDATE_FAILED,
            msg=f"当前状态「{label}」不可编辑。已上线 API 请先下线回到草稿后再修改。",
        )

    # 检查路径冲突
    if req.url_path is not None or req.method is not None:
        new_path = req.url_path or api.url_path
        new_method = req.method or api.method
        existing = await db.execute(
            select(ApiConfig).where(
                ApiConfig.project_id == project_id,
                ApiConfig.url_path == new_path,
                ApiConfig.method == new_method,
                ApiConfig.id != api_id,
            )
        )
        if existing.scalar_one_or_none():
            log.warning(f"更新 API 失败: 路径冲突 | {new_method} {new_path}")
            return R_fail(ErrCode.API_PATH_CONFLICT, msg=f"路径冲突: {new_method} {new_path} 已存在")

    # 更新字段（禁止通过普通更新接口篡改生命周期 status，须走专用状态流转接口）
    update_fields = req.model_dump(exclude_unset=True, exclude={"parameters", "status"})

    if update_fields.get("datasource_id") and update_fields["datasource_id"] != api.datasource_id:
        scope_err = await datasource_scope_error(db, project_id, update_fields["datasource_id"])
        if scope_err:
            return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=scope_err)

    # 数据同步 API (v2.17+)：涉及写库，只有超管能改。
    # 要拦的是三种「真的和 sync 有关」的情况：
    #   1. 这个 API 本来就是 sync
    #   2. 本次想把它改成 sync
    #   3. 本次真的填了非空白名单（想给某个 API 配写库表）
    # 注意不能用 `"sync_tables" in update_fields` 来判断 —— 前端保存时对所有
    # API 都会提交这个字段（普通 API 是空串），那样会把普通 API 的编辑也拦掉。
    _old_type = (getattr(api, "api_type", "sql") or "sql").lower()
    _new_type = (update_fields.get("api_type") or "").lower()
    _final_type = _new_type or _old_type
    _wl_submitted = (update_fields.get("sync_tables") or "").strip()
    _touch_sync = (_old_type == "sync") or (_new_type == "sync") or bool(_wl_submitted)
    from app.core.permissions import is_super_admin as _is_sa
    if _touch_sync and not _is_sa(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED,
                      msg="远端同步类 API 只能由超级管理员修改")
    # 插件类 API (v2.20)：判定方式同上面的 sync
    _plugin_submitted = (update_fields.get("plugin_code") or "").strip()
    _touch_plugin = (_old_type == "plugin") or (_new_type == "plugin") or bool(_plugin_submitted)
    if _touch_plugin and plugin_edit_denied(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="插件类 API 只能由超级管理员修改")
    if _final_type == "sync":
        from app.services.data_sync import parse_whitelist, SyncError
        _wl = update_fields.get("sync_tables", getattr(api, "sync_tables", "") or "")
        try:
            parse_whitelist(_wl)
        except SyncError as e:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"同步表白名单配置有误：{e}")

    # 自动预热依赖缓存：用「本次提交的值 + 未提交则沿用原值」算出最终状态再校验，
    # 避免只改其中一项时误判（比如单独关掉缓存但预热还开着）
    final_prewarm = update_fields.get("cache_prewarm",
                                      getattr(api, "cache_prewarm", False))
    final_cache = update_fields.get("cache_enabled", api.cache_enabled)
    if final_prewarm and not final_cache:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID,
                      msg="开启「自动预热」需要先开启「缓存」——预热是把结果提前写入缓存，未启用缓存时不会生效")

    for key, value in update_fields.items():
        if value is not None:
            setattr(api, key, value)
            log.debug(f"更新字段 | api_id={api_id} | {key}={value}")

    api.version += 1

    # 更新参数
    if req.parameters is not None:
        await db.execute(delete(ApiParameter).where(ApiParameter.api_id == api_id))
        for idx, p in enumerate(req.parameters):
            param = ApiParameter(
                api_id=api.id, name=p.name, param_type=p.param_type,
                required=p.required, default_value=p.default_value,
                description=p.description, sort_order=p.sort_order or idx,
                item_schema=getattr(p, "item_schema", "") or "",
            )
            db.add(param)
        log.debug(f"参数已更新 | api_id={api_id} | 新参数数={len(req.parameters)}")

    log.info(f"API 更新成功 | api_id={api_id} | name={api.name} | version={api.version}")
    return R_ok(msg="API 更新成功")


@router.delete("/{api_id}")
async def delete_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """删除 API"""
    from app.core.permissions import is_project_member
    from app.services.api_lifecycle import can_delete, STATUS_LABELS
    log.info(f"删除 API 请求 | project_id={project_id} | api_id={api_id}")

    # 责任人制：项目成员即可发起（能否直接删由责任人判定决定；非成员无关人员拒绝）
    if not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权操作该项目的 API")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        log.warning(f"删除 API 失败: API 不存在 | api_id={api_id}")
        return R_fail(ErrCode.API_NOT_FOUND)

    # 状态守卫：已上线 / 待上线 不可删除
    cur_status = getattr(api, "status", "draft")
    if not can_delete(cur_status):
        label = STATUS_LABELS.get(cur_status, cur_status)
        return R_fail(
            ErrCode.API_DELETE_FAILED,
            msg=f"当前状态「{label}」不可删除。已上线 API 请先下线后再删除。",
        )

    # 责任人制 (v2.10)：责任人本人/管理员可直接删；其他人发起申请由责任人或管理员审批
    from app.services.owner_approval import can_act_directly, create_request
    if not await can_act_directly(db, _user, api):
        appr = await create_request(db, _user, api, "delete")
        return R_ok(
            data={"pending_approval": True, "approval_id": appr.id},
            msg="你不是该 API 的责任人，删除申请已提交，待责任人或管理员审批",
        )

    await db.delete(api)
    log.info(f"API 删除成功 | api_id={api_id} | name={api.name}")
    return R_ok(msg="API 删除成功")


@router.post("/{api_id}/copy")
async def copy_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """复制 API（副本为草稿、不带独立 API Key，需重新走上线审批）"""
    from app.core.permissions import can_edit_project_resources, is_super_admin
    log.info(f"复制 API 请求 | project_id={project_id} | api_id={api_id}")

    # v2.20：原来无权限校验，且副本照抄状态（复制一个已上线 API 得到的副本直接是「已上线」，
    # 绕过审批）、照抄 API Key、重复复制时路径冲突
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权复制该项目的 API")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        log.warning(f"复制 API 失败: API 不存在 | api_id={api_id}")
        return R_fail(ErrCode.API_NOT_FOUND)

    _src_type = (api.api_type or "sql").lower()
    if _src_type == "sync" and not is_super_admin(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="远端同步类 API 只能由超级管理员复制")
    if (_src_type == "plugin" or (getattr(api, "plugin_code", "") or "").strip()) and plugin_edit_denied(_user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="插件类 API 只能由超级管理员复制")

    # 生成不冲突的路径：/x_copy、/x_copy2、/x_copy3 ...
    new_path, n = f"{api.url_path}_copy", 1
    while (await db.execute(select(ApiConfig.id).where(
            ApiConfig.project_id == project_id, ApiConfig.url_path == new_path,
            ApiConfig.method == api.method))).first():
        n += 1
        new_path = f"{api.url_path}_copy{n}"

    new_api = ApiConfig(
        project_id=project_id,
        name=f"{api.name} (副本)",
        description=api.description,
        url_path=new_path,
        method=api.method,
        datasource_id=api.datasource_id,
        sql_template=api.sql_template,
        pipeline_steps=api.pipeline_steps or "",
        api_type=api.api_type or "sql",
        html_content=api.html_content or "",
        css_content=api.css_content or "",
        js_content=api.js_content or "",
        plugin_code=getattr(api, "plugin_code", "") or "",
        status="draft",
        is_enabled=False,
        api_key="",
        created_by=_user.id,
        owner_id=_user.id,
        require_api_key=bool(api.require_api_key) if api.require_api_key is not None else True,
        cache_enabled=api.cache_enabled,
        cache_prewarm=bool(getattr(api, 'cache_prewarm', False)),
        prewarm_param_overrides=getattr(api, 'prewarm_param_overrides', '') or '',
        prewarm_stop_daily=bool(getattr(api, 'prewarm_stop_daily', False)),
        sync_tables=getattr(api, 'sync_tables', '') or '',
        cache_ttl=api.cache_ttl,
        timeout=api.timeout,
        rate_limit_enabled=api.rate_limit_enabled,
        rate_limit_qps=api.rate_limit_qps,
        max_rows=api.max_rows,
    )
    db.add(new_api)
    await db.flush()

    # 复制参数
    params_r = await db.execute(
        select(ApiParameter).where(ApiParameter.api_id == api_id)
    )
    for p in params_r.scalars().all():
        new_param = ApiParameter(
            api_id=new_api.id, name=p.name, param_type=p.param_type,
            required=p.required, default_value=p.default_value,
            description=p.description, sort_order=p.sort_order,
            item_schema=getattr(p, "item_schema", "") or "",
        )
        db.add(new_param)

    await db.flush()
    log.info(f"API 复制成功 | 原 api_id={api_id} | 新 api_id={new_api.id} | name={new_api.name}")
    return R_ok(data={"id": new_api.id}, msg="API 复制成功")


@router.post("/{api_id}/generate-key")
async def generate_api_key(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """生成独立 API Key（始终生成全新的独立 Key）"""
    import secrets
    from app.core.permissions import can_edit_project_resources
    # v2.20：原来无权限校验，任何登录用户都能操作任意项目的 API
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权操作该项目的 API")

    log.info(f"生成独立 API Key | project_id={project_id} | api_id={api_id}")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        log.warning(f"生成 Key 失败: API 不存在 | api_id={api_id}")
        return R_fail(ErrCode.API_NOT_FOUND)

    api.api_key = f"pfk_{secrets.token_hex(24)}"
    log.info(f"独立 API Key 已生成 | api_id={api_id} | key={api.api_key[:20]}...")
    return R_ok(data={"api_key": api.api_key}, msg="独立 API Key 已生成")


@router.put("/{api_id}/toggle")
async def toggle_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """启用/禁用 API"""
    from app.core.permissions import can_edit_project_resources
    # v2.20：原来无权限校验，任何登录用户都能操作任意项目的 API
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="无权操作该项目的 API")
    log.info(f"切换 API 状态 | project_id={project_id} | api_id={api_id}")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        log.warning(f"切换状态失败: API 不存在 | api_id={api_id}")
        return R_fail(ErrCode.API_NOT_FOUND)

    api.is_enabled = not api.is_enabled
    status_text = "启用" if api.is_enabled else "禁用"
    log.info(f"API 状态切换成功 | api_id={api_id} | name={api.name} | 新状态={status_text}")
    return R_ok(msg=f"API 已{status_text}")


@router.post("/{api_id}/lock")
async def lock_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """锁定 API（锁定后不可编辑/提交上线）。创建者本人 / 项目管理员 / 超管可操作。"""
    return await _set_lock(project_id, api_id, True, db, _user)


@router.post("/{api_id}/unlock")
async def unlock_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """解锁 API。创建者本人 / 项目管理员 / 超管可操作。"""
    return await _set_lock(project_id, api_id, False, db, _user)


async def _set_lock(project_id, api_id, locked, db, _user):
    from app.core.permissions import is_super_admin, is_project_manager
    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)

    # 权限：创建者本人 / 项目管理员 / 超管
    is_creator = (getattr(api, "created_by", None) == _user.id)
    allowed = is_creator or is_super_admin(_user) or await is_project_manager(db, _user, project_id)
    if not allowed:
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅创建者本人、项目管理员或超管可锁定/解锁")

    api.is_locked = bool(locked)
    await db.flush()
    log.info(f"API {'锁定' if locked else '解锁'} | api_id={api_id} | by={_user.username}")
    return R_ok(data={"is_locked": api.is_locked}, msg="已锁定" if locked else "已解锁")


@router.post("/{api_id}/publish")
async def publish_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """提交者确认上线（approved -> online）。

    审批两方都通过后，API 进入「待上线」(approved)，由该上线申请的提交者
    （或超管）点此正式上线。
    """
    from app.core.permissions import is_super_admin
    from app.services.api_lifecycle import next_status, STATUS_LABELS, STATUS_APPROVED
    from app.models.models import ApiApproval
    log.info(f"上线 API 请求 | project_id={project_id} | api_id={api_id} | by={_user.username}")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)

    if getattr(api, "status", "draft") != STATUS_APPROVED:
        label = STATUS_LABELS.get(getattr(api, "status", "draft"), api.status)
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=f"当前状态「{label}」不可上线，仅「待上线」状态可上线")

    # 找到最近一张已通过的审批单，校验提交者
    appr = (await db.execute(
        select(ApiApproval).where(
            ApiApproval.api_id == api_id,
            ApiApproval.overall_status == "approved",
        ).order_by(ApiApproval.created_at.desc())
    )).scalars().first()

    # 仅提交者或超管可上线
    if not is_super_admin(_user) and (not appr or appr.submitter_id != _user.id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅该上线申请的提交者可执行上线")

    try:
        api.status = next_status("publish", api.status)
    except ValueError as e:
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=str(e))

    await db.commit()   # 显式提交,确保上线状态在响应返回前已落库(避免前端刷新读到旧状态)
    log.info(f"API 已上线 | api_id={api_id} | name={api.name} | by={_user.username}")
    return R_ok(data={"status": api.status}, msg="API 已上线")


@router.post("/{api_id}/offline")
async def offline_api(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """下线 API（online -> offline，回到可编辑状态）。责任人/管理员可直接下线，其他人走申请。"""
    from app.services.api_lifecycle import next_status, STATUS_LABELS
    from app.services.owner_approval import can_act_directly, create_request
    log.info(f"下线 API 请求 | project_id={project_id} | api_id={api_id}")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)

    # 责任人制 (v2.10)：非责任人非管理员 -> 提交下线申请（须是项目成员）
    from app.core.permissions import is_project_member
    if not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权操作该项目的 API")
    if not await can_act_directly(db, _user, api):
        appr = await create_request(db, _user, api, "offline")
        return R_ok(data={"pending_approval": True, "approval_id": appr.id},
                    msg="你不是该 API 的责任人，下线申请已提交，待责任人或管理员审批")

    try:
        api.status = next_status("offline", getattr(api, "status", "draft"))
    except ValueError as e:
        return R_fail(ErrCode.API_UPDATE_FAILED, msg=str(e))

    await db.commit()   # 显式提交,确保下线状态在响应返回前已落库
    log.info(f"API 已下线 | api_id={api_id} | name={api.name} | 新状态={api.status}")
    return R_ok(data={"status": api.status}, msg="API 已下线，已回到可编辑状态")


@router.post("/{api_id}/clear-cache")
async def clear_api_cache(
    project_id: int,
    api_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """清除该 API 的所有缓存 (v2.13+)。

    同时清掉「自动预热」记录的历史请求参数 —— 否则下一轮预热又会把刚清掉的
    缓存按旧参数重新灌回去，用户会觉得"清了跟没清一样"。
    """
    from app.services.engine import _clear_api_cache, _clear_prewarm_params
    from app.core.permissions import is_project_member

    if not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员，无权操作")

    result = await db.execute(
        select(ApiConfig).where(ApiConfig.id == api_id, ApiConfig.project_id == project_id)
    )
    api = result.scalar_one_or_none()
    if not api:
        return R_fail(ErrCode.API_NOT_FOUND)

    await _clear_api_cache(api_id)
    await _clear_prewarm_params(api_id)
    log.info(f"API 缓存已清除 | api_id={api_id} | name={api.name} | by={_user.username}")
    return R_ok(msg="缓存已清除")