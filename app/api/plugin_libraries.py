"""
插件库 (v2.3+)：可复用 Python 函数集，供 API 插件通过 `# require: name` 引用。

管理权限：增删改限超级管理员（库代码全局生效，影响面大）；列表/详情登录可见。
"""
import re
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.core.database import get_db
from app.core.errors import ErrCode, R_ok, R_fail
from app.core.logging import get_logger
from app.core.permissions import is_super_admin
from app.models.models import User, PluginLibrary
from app.api.auth import get_current_user

log = get_logger("plugin_lib")
router = APIRouter(prefix="/api/plugin-libraries", tags=["插件库"])

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class LibCreate(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    code: str = ""
    is_enabled: bool = True


class LibUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    code: str | None = None
    is_enabled: bool | None = None


def _dict(l: PluginLibrary):
    return {
        "id": l.id, "name": l.name, "title": l.title, "description": l.description,
        "code": l.code, "is_enabled": l.is_enabled,
        "created_at": l.created_at.isoformat() if l.created_at else None,
        "updated_at": l.updated_at.isoformat() if l.updated_at else None,
    }


@router.get("")
async def list_libraries(db: AsyncSession = Depends(get_db, scope="function"), _user: User = Depends(get_current_user)):
    r = await db.execute(select(PluginLibrary).order_by(PluginLibrary.name))
    items = [_dict(l) for l in r.scalars().all()]
    return R_ok(data={"items": items, "total": len(items)})


@router.get("/{lib_id}")
async def get_library(lib_id: int, db: AsyncSession = Depends(get_db, scope="function"), _user: User = Depends(get_current_user)):
    l = (await db.execute(select(PluginLibrary).where(PluginLibrary.id == lib_id))).scalar_one_or_none()
    if not l:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="插件库不存在")
    return R_ok(data=_dict(l))


@router.post("")
async def create_library(req: LibCreate, db: AsyncSession = Depends(get_db, scope="function"), user: User = Depends(get_current_user)):
    if not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅超级管理员可管理插件库")
    if not _NAME_RE.match(req.name or ""):
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="库名只能是字母/数字/下划线，且不以数字开头")
    # 语法预检
    if req.code:
        try:
            compile(req.code, "<plugin_library>", "exec")
        except SyntaxError as e:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"代码语法错误 (第 {e.lineno} 行): {e.msg}")
    exist = (await db.execute(select(PluginLibrary).where(PluginLibrary.name == req.name))).scalar_one_or_none()
    if exist:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"库名已存在: {req.name}")
    lib = PluginLibrary(
        name=req.name, title=req.title or req.name, description=req.description or "",
        code=req.code or "", is_enabled=req.is_enabled, created_by=user.id,
    )
    db.add(lib)
    await db.commit()
    await db.refresh(lib)
    log.info(f"创建插件库 | name={req.name} | by={user.username}")
    return R_ok(data=_dict(lib), msg="插件库已创建")


@router.put("/{lib_id}")
async def update_library(lib_id: int, req: LibUpdate, db: AsyncSession = Depends(get_db, scope="function"), user: User = Depends(get_current_user)):
    if not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅超级管理员可管理插件库")
    l = (await db.execute(select(PluginLibrary).where(PluginLibrary.id == lib_id))).scalar_one_or_none()
    if not l:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="插件库不存在")
    if req.code is not None:
        try:
            compile(req.code, "<plugin_library>", "exec")
        except SyntaxError as e:
            return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"代码语法错误 (第 {e.lineno} 行): {e.msg}")
        l.code = req.code
    if req.title is not None: l.title = req.title
    if req.description is not None: l.description = req.description
    if req.is_enabled is not None: l.is_enabled = req.is_enabled
    await db.commit()
    log.info(f"更新插件库 | id={lib_id} | by={user.username}")
    return R_ok(msg="插件库已更新")


@router.delete("/{lib_id}")
async def delete_library(lib_id: int, db: AsyncSession = Depends(get_db, scope="function"), user: User = Depends(get_current_user)):
    if not is_super_admin(user):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="仅超级管理员可管理插件库")
    l = (await db.execute(select(PluginLibrary).where(PluginLibrary.id == lib_id))).scalar_one_or_none()
    if not l:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg="插件库不存在")
    await db.delete(l)
    await db.commit()
    log.info(f"删除插件库 | id={lib_id} | name={l.name} | by={user.username}")
    return R_ok(msg="插件库已删除")
