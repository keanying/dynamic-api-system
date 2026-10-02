# -*- coding: utf-8 -*-
"""
项目环境变量管理 (v2.14+)

供缓存预热的参数模板引用，解决「预热参数里时间写死会过期」的问题。
端点挂在 /api/projects/{project_id}/variables 下。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.core.timezone import now as cst_now
from app.models.models import ProjectVariable, Project
from app.api.auth import get_current_user
from app.services.project_vars import VAR_TYPES, eval_variable

log = get_logger("project_vars_api")
router = APIRouter(prefix="/api/projects/{project_id}/variables", tags=["项目变量"])


class VariableIn(BaseModel):
    name: str
    var_type: str = "date"
    offset_days: int = 0
    date_format: str = ""
    const_value: str = ""
    description: str = ""


def _to_out(v: ProjectVariable) -> dict:
    d = {
        "id": v.id,
        "project_id": v.project_id,
        "name": v.name,
        "var_type": v.var_type,
        "offset_days": v.offset_days or 0,
        "date_format": v.date_format or "",
        "const_value": v.const_value or "",
        "description": v.description or "",
    }
    # 附带当前求值结果，方便前端直观看到这个变量现在等于什么
    try:
        d["preview"] = eval_variable(v)
    except Exception as e:  # noqa: BLE001
        d["preview"] = f"(求值失败: {e})"
    return d


@router.get("")
async def list_variables(
    project_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """列出项目的所有环境变量（带当前求值预览）。"""
    from app.core.permissions import is_project_member
    if not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员")

    r = await db.execute(
        select(ProjectVariable).where(ProjectVariable.project_id == project_id)
        .order_by(ProjectVariable.name)
    )
    items = [_to_out(v) for v in r.scalars().all()]
    return R_ok(data={"items": items, "var_types": VAR_TYPES})


@router.post("")
async def create_variable(
    project_id: int,
    req: VariableIn,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """新增环境变量。"""
    from app.core.permissions import can_edit_project_resources
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有项目管理员/研发可修改项目变量")

    name = (req.name or "").strip()
    if not name:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="变量名不能为空")
    # 变量名用于 ${NAME} 引用，限制为字母数字下划线，避免正则匹配不到
    if not name.replace("_", "").isalnum() or name[0].isdigit():
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID,
                      msg="变量名只能用字母、数字、下划线，且不能以数字开头")

    pr = await db.execute(select(Project).where(Project.id == project_id))
    if pr.scalar_one_or_none() is None:
        return R_fail(ErrCode.API_NOT_FOUND, msg="项目不存在")

    dup = await db.execute(
        select(ProjectVariable).where(
            ProjectVariable.project_id == project_id, ProjectVariable.name == name
        )
    )
    if dup.scalar_one_or_none():
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"变量名 {name} 已存在")

    v = ProjectVariable(
        project_id=project_id, name=name, var_type=req.var_type,
        offset_days=req.offset_days, date_format=req.date_format,
        const_value=req.const_value, description=req.description,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    log.info(f"项目变量已创建 | project_id={project_id} | name={name} | by={_user.username}")
    return R_ok(data=_to_out(v), msg="变量已创建")


@router.put("/{var_id}")
async def update_variable(
    project_id: int,
    var_id: int,
    req: VariableIn,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """修改环境变量。"""
    from app.core.permissions import can_edit_project_resources
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有项目管理员/研发可修改项目变量")

    r = await db.execute(
        select(ProjectVariable).where(
            ProjectVariable.id == var_id, ProjectVariable.project_id == project_id
        )
    )
    v = r.scalar_one_or_none()
    if not v:
        return R_fail(ErrCode.API_NOT_FOUND, msg="变量不存在")

    name = (req.name or "").strip()
    if name and name != v.name:
        if not name.replace("_", "").isalnum() or name[0].isdigit():
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID,
                          msg="变量名只能用字母、数字、下划线，且不能以数字开头")
        dup = await db.execute(
            select(ProjectVariable).where(
                ProjectVariable.project_id == project_id,
                ProjectVariable.name == name,
                ProjectVariable.id != var_id,
            )
        )
        if dup.scalar_one_or_none():
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"变量名 {name} 已存在")
        v.name = name

    v.var_type = req.var_type
    v.offset_days = req.offset_days
    v.date_format = req.date_format
    v.const_value = req.const_value
    v.description = req.description
    v.updated_at = cst_now()
    await db.commit()
    await db.refresh(v)
    log.info(f"项目变量已更新 | project_id={project_id} | name={v.name} | by={_user.username}")
    return R_ok(data=_to_out(v), msg="变量已更新")


@router.delete("/{var_id}")
async def delete_variable(
    project_id: int,
    var_id: int,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """删除环境变量。"""
    from app.core.permissions import can_edit_project_resources
    if not await can_edit_project_resources(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="只有项目管理员/研发可修改项目变量")

    r = await db.execute(
        select(ProjectVariable).where(
            ProjectVariable.id == var_id, ProjectVariable.project_id == project_id
        )
    )
    v = r.scalar_one_or_none()
    if not v:
        return R_fail(ErrCode.API_NOT_FOUND, msg="变量不存在")

    name = v.name
    await db.delete(v)
    await db.commit()
    log.info(f"项目变量已删除 | project_id={project_id} | name={name} | by={_user.username}")
    return R_ok(msg="变量已删除")


class PreviewIn(BaseModel):
    template: str


@router.post("/preview-template")
async def preview_template(
    project_id: int,
    req: PreviewIn,
    db: AsyncSession = Depends(get_db, scope="function"),
    _user=Depends(get_current_user),
):
    """预览预热参数模板的求值结果 —— 让用户保存前就能看到实际会用什么参数查询。"""
    from app.core.permissions import is_project_member
    from app.services.project_vars import render_template
    if not await is_project_member(db, _user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="你不是该项目成员")

    r = await db.execute(
        select(ProjectVariable).where(ProjectVariable.project_id == project_id)
    )
    variables = r.scalars().all()
    try:
        result = render_template(req.template or "", variables)
    except Exception as e:  # noqa: BLE001
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"模板解析失败: {e}")
    return R_ok(data={"params": result})
