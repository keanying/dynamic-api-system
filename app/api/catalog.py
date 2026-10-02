# -*- coding: utf-8 -*-
"""
多源 SQL 编辑辅助接口 (v2.22+)
=============================

- GET  /api/sql/federated/catalogs   本项目可用的 catalog（MySQL 协议的数据源）
- GET  /api/sql/federated/databases  某个 catalog 下的库
- GET  /api/sql/federated/tables     某个库下的表
- GET  /api/sql/federated/columns    某张表的列
- POST /api/sql/federated/explain    执行计划：各数据源要执行的 SQL、关联计算的 SQL（不取数据）

都是只读操作；需要是项目成员。查询走数据源连接池，受只读防护和系统库保护约束。
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import get_current_user
from app.core.database import get_db
from app.core.errors import ErrCode, R_fail, R_ok
from app.core.logging import get_logger
from app.models.models import DataSource

log = get_logger("catalog")

router = APIRouter(prefix="/api/sql/federated", tags=["多源SQL"])


async def _member_or_fail(db, user, project_id: int):
    from app.core.permissions import is_project_member
    if not await is_project_member(db, user, project_id):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg="不是该项目成员")
    return None


async def _catalog(db, project_id: int, name: str):
    """按名称取本项目可用的 MySQL 协议数据源，不可用时返回 (None, 错误响应)。"""
    from app.services import ds_scope
    from app.services.datasource_types import MYSQL_PROTOCOL_TYPES
    rows = (await db.execute(select(DataSource).where(DataSource.name == name))).scalars().all()
    if len(rows) != 1:
        return None, R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"数据源「{name}」不存在" if not rows else f"存在多个名为「{name}」的数据源")
    ds = rows[0]
    if (ds.type or "mysql").lower() not in MYSQL_PROTOCOL_TYPES:
        return None, R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=f"数据源「{name}」不是 MySQL 协议的数据源")
    if not ds_scope.is_allowed(ds, await ds_scope.project_code(db, project_id)):
        return None, R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=f"数据源「{name}」未对本项目开放")
    return ds, None


async def _show(ds, sql: str, max_rows: int = 5000):
    from app.core.security import decrypt_value
    from app.services.engine import _query_mysql_raw
    password = decrypt_value(ds.password_encrypted) if ds.password_encrypted else ""
    fields, rows, _ = await _query_mysql_raw(ds, password, sql, {}, timeout=10, max_rows=max_rows)
    return fields, rows


def _protected(ds, database: str) -> bool:
    """系统库 / 平台库（数据源本身登记为该库时除外），与 SQL 执行时的系统库保护一致。"""
    from app.services import ds_scope
    name = (database or "").strip().strip("`").lower()
    return name in ds_scope.protected_schemas() and name != (ds.database_name or "").lower()


def _quote(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


@router.get("/catalogs")
async def list_catalogs(project_id: int, db: AsyncSession = Depends(get_db, scope="function"), _user=Depends(get_current_user)):
    denied = await _member_or_fail(db, _user, project_id)
    if denied:
        return denied
    from app.services import ds_scope
    from app.services.datasource_types import DATASOURCE_DISPLAY_NAMES, MYSQL_PROTOCOL_TYPES
    code = await ds_scope.project_code(db, project_id)
    out = []
    for ds in (await db.execute(select(DataSource).order_by(DataSource.name))).scalars():
        if (ds.type or "mysql").lower() in MYSQL_PROTOCOL_TYPES and ds_scope.is_allowed(ds, code):
            out.append({"id": ds.id, "name": ds.name, "type": ds.type,
                        "type_name": DATASOURCE_DISPLAY_NAMES.get(ds.type, ds.type),
                        "database": ds.database_name or ""})
    return R_ok(data=out)


@router.get("/databases")
async def list_databases(project_id: int, catalog: str, db: AsyncSession = Depends(get_db, scope="function"),
                         _user=Depends(get_current_user)):
    denied = await _member_or_fail(db, _user, project_id)
    if denied:
        return denied
    ds, err = await _catalog(db, project_id, catalog)
    if err:
        return err
    try:
        _, rows = await _show(ds, "SHOW DATABASES")
    except Exception as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=f"读取库列表失败：{e}")
    return R_ok(data=[r[0] for r in rows if str(r[0]).lower() != "information_schema" and not _protected(ds, str(r[0]))])


@router.get("/tables")
async def list_tables(project_id: int, catalog: str, database: str,
                      db: AsyncSession = Depends(get_db, scope="function"), _user=Depends(get_current_user)):
    denied = await _member_or_fail(db, _user, project_id)
    if denied:
        return denied
    ds, err = await _catalog(db, project_id, catalog)
    if err:
        return err
    if _protected(ds, database):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=f"不允许访问系统库「{database}」")
    try:
        _, rows = await _show(ds, f"SHOW TABLES FROM {_quote(database)}")
    except Exception as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=f"读取表列表失败：{e}")
    return R_ok(data=[r[0] for r in rows])


@router.get("/columns")
async def list_columns(project_id: int, catalog: str, database: str, table: str,
                       db: AsyncSession = Depends(get_db, scope="function"), _user=Depends(get_current_user)):
    denied = await _member_or_fail(db, _user, project_id)
    if denied:
        return denied
    ds, err = await _catalog(db, project_id, catalog)
    if err:
        return err
    if _protected(ds, database):
        return R_fail(ErrCode.AUTH_PERMISSION_DENIED, msg=f"不允许访问系统库「{database}」")
    try:
        fields, rows = await _show(ds, f"SHOW COLUMNS FROM {_quote(database)}.{_quote(table)}")
    except Exception as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=f"读取列失败：{e}")
    names = [f.name.lower() for f in fields]
    fi, ti = names.index("field") if "field" in names else 0, names.index("type") if "type" in names else 1
    return R_ok(data=[{"name": r[fi], "type": r[ti]} for r in rows])


class ExplainRequest(BaseModel):
    project_id: int
    sql_template: str
    params: dict = {}
    timeout: Optional[int] = 30


@router.post("/explain")
async def explain(req: ExplainRequest, db: AsyncSession = Depends(get_db, scope="function"),
                  _user=Depends(get_current_user)):
    """执行计划：模板按给定参数渲染后，各数据源要执行的 SQL 与关联计算的 SQL（不取数据）。"""
    denied = await _member_or_fail(db, _user, req.project_id)
    if denied:
        return denied
    try:
        from app.services import federated
    except ImportError as e:
        return R_fail(ErrCode.SYSTEM_ERROR, msg=f"多源 SQL 依赖未安装（sqlglot / duckdb / pyarrow）：{e}")
    from app.services.engine import _parse_sql_params
    try:
        sql, _bound = _parse_sql_params(req.sql_template or "", dict(req.params or {}), [])
        plan = await federated.explain(sql, db=db, project_id=req.project_id, timeout=min(max(req.timeout or 30, 1), 60))
    except federated.FederatedError as e:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=str(e))
    except ValueError as e:
        return R_fail(ErrCode.SYSTEM_PARAM_INVALID, msg=str(e))
    except Exception as e:
        log.warning(f"多源 SQL 执行计划失败 | project_id={req.project_id} | {e}")
        return R_fail(ErrCode.SYSTEM_ERROR, msg=str(e))
    return R_ok(data={"plan": plan, "text": federated.plan_text(plan)})
