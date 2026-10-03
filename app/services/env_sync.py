# -*- coding: utf-8 -*-
"""
从生产同步全部到预发 (v2.24)
============================

超级管理员在预发一键把生产的内容同步过来，**只有 生产 → 预发 一个方向**（这里只读生产表、只写预发表）。

同步范围：
  - 数据源：生产有、预发没有（按名称）的，原样复制到预发；预发已有的同名数据源不动（预发可以连不同的库）
  - 项目：生产有、预发没有的新建；已有的更新名称、描述、项目 Key；成员补上预发没有的；环境变量按名称覆盖
  - API：按「项目编码 + 方法 + 路径」对应，用生产的配置覆盖预发（锁定的也覆盖，锁定状态不变），生产独有的新建
不会做的：
  - 不删除预发独有的 API（还没发布到生产的开发中内容）
  - 预发有未发布到生产的改动（预发有新改动 / 两边都有改动 / 有差异）默认跳过，勾选「覆盖」才同步
  - 预发里正在上线审批的 API 跳过
同步完成的 API 记为新的同步基线（与生产一致）。

分批执行 (v2.24)：前端先取概览，再逐个项目预览 / 同步（每个项目一个请求、单独提交），页面显示进度；
不会因为项目多、API 内容大而一个请求卡很久或超时。
"""
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text

from app.core.logging import get_logger
from app.models.models import ApiConfig, DataSource, Project, ProjectMember, ProjectVariable, User
from app.services.release import (ReleaseError, SYNC_STATES, apply_to_pre, build_snapshot, load_bases,
                                  read_env_project_snapshots, save_base, sync_state)

log = get_logger("env_sync")

_DS_COLS = ["name", "type", "host", "port", "username", "password_encrypted", "database_name", "pool_size",
            "extra_config", "status", "created_by", "project_scope"]
_VAR_COLS = ["var_type", "offset_days", "date_format", "const_value", "description"]


async def _prod_rows(db, sql: str, params: Optional[dict] = None):
    try:
        return (await db.execute(text(sql), params or {})).mappings().all()
    except Exception as e:  # noqa: BLE001
        raise ReleaseError(f"读取生产环境失败：{e}")


def _action(state: str, in_review: bool, overwrite: bool) -> str:
    """create / update / same / conflict / skip_review / keep（预发独有，保留）"""
    if state == "pre_only":
        return "keep"
    if in_review and state != "same":
        return "skip_review"
    if state == "same":
        return "same"
    if state == "prod_only":
        return "create"
    if state == "prod_ahead":
        return "update"
    return "update" if overwrite else "conflict"


async def overview(db) -> Dict[str, Any]:
    """第一步（很快）：生产有哪些项目、预发要新建哪些数据源。逐项目的对比 / 同步由前端分批调用。"""
    prod_projects = await _prod_rows(db, "SELECT p.id, p.code, p.name, (SELECT COUNT(*) FROM src_dop_api_configs a "
                                         "WHERE a.project_id = p.id) AS api_count FROM src_dop_projects p ORDER BY p.id")
    pre_codes = set((await db.execute(select(Project.code))).scalars().all())
    prod_ds = [r["name"] for r in await _prod_rows(db, "SELECT name FROM src_dop_datasources ORDER BY id")]
    pre_ds = set((await db.execute(select(DataSource.name))).scalars().all())
    return {
        "projects": [{"code": p["code"], "name": p["name"], "api_count": int(p["api_count"] or 0),
                      "exists_in_pre": p["code"] in pre_codes} for p in prod_projects],
        "datasources_to_create": [n for n in prod_ds if n not in pre_ds],
    }


async def _project_states(db, code: str, pre_p: Optional[Project], overwrite: bool):
    """一个项目里每个 API 的 (key, 生产快照, 生产 meta, 预发 API, 状态, 处理方式)。"""
    from app.services.api_lifecycle import STATUS_PENDING, STATUS_APPROVED
    prod_apis = await read_env_project_snapshots(db, "", code) or {}
    bases = await load_bases(db, code)
    pre_apis = {}
    if pre_p:
        for a in (await db.execute(select(ApiConfig).where(ApiConfig.project_id == pre_p.id))).scalars().all():
            pre_apis[(a.method, a.url_path)] = a
    out = []
    for key in list(prod_apis.keys()) + [k for k in pre_apis if k not in prod_apis]:
        prod_snap, prod_meta = prod_apis.get(key, (None, None))
        api = pre_apis.get(key)
        base = bases.get(key)
        state = sync_state(prod_snap, await build_snapshot(db, api) if api else None,
                           base["snapshot"] if base else None)
        act = _action(state, bool(api and api.status in (STATUS_PENDING, STATUS_APPROVED)), overwrite)
        out.append((key, prod_snap, prod_meta, api, state, act))
    return out


def _empty_counts() -> Dict[str, int]:
    return {k: 0 for k in ("create", "update", "same", "conflict", "skip_review", "keep")}


async def plan_project(db, code: str, overwrite: bool = False) -> Dict[str, Any]:
    """预览一个项目：每个 API 会怎么处理。"""
    pre_p = (await db.execute(select(Project).where(Project.code == code))).scalar_one_or_none()
    counts, items = _empty_counts(), []
    for key, prod_snap, prod_meta, api, state, act in await _project_states(db, code, pre_p, overwrite):
        counts[act] += 1
        items.append({"method": key[0], "url_path": key[1],
                      "name": (api.name if api else (prod_meta or {}).get("name")) or "",
                      "state": state, "state_label": SYNC_STATES[state], "action": act})
    return {"code": code, "exists_in_pre": pre_p is not None, "counts": counts, "apis": items}


async def run_datasources(db) -> List[str]:
    """数据源：生产有、预发没有（按名称）的复制过来；预发已有的不动。返回新建的名称。"""
    pre_ds = set((await db.execute(select(DataSource.name))).scalars().all())
    created = []
    for r in await _prod_rows(db, f"SELECT {', '.join(_DS_COLS)} FROM src_dop_datasources ORDER BY id"):
        if r["name"] in pre_ds:
            continue
        db.add(DataSource(**{c: r[c] for c in _DS_COLS}))
        pre_ds.add(r["name"])
        created.append(r["name"])
    await db.flush()
    return created


async def run_project(db, user: User, code: str, overwrite: bool = False) -> Dict[str, Any]:
    """同步一个项目（项目信息、成员、环境变量、API）。调用方负责 commit。"""
    rows = await _prod_rows(db, "SELECT id, code, name, description, api_key, is_active FROM src_dop_projects WHERE code = :c",
                            {"c": code})
    if not rows:
        raise ReleaseError(f"生产没有项目「{code}」")
    pp = rows[0]
    project = (await db.execute(select(Project).where(Project.code == code))).scalar_one_or_none()
    created_project = project is None
    if project is None:
        project = Project(code=code)
        db.add(project)
    project.name = pp["name"]
    project.description = pp["description"] or ""
    project.api_key = pp["api_key"] or ""
    project.is_active = bool(pp["is_active"]) if pp["is_active"] is not None else True
    await db.flush()

    user_ids = set((await db.execute(select(User.id))).scalars().all())
    have = set((await db.execute(select(ProjectMember.user_id).where(ProjectMember.project_id == project.id))).scalars().all())
    for m in await _prod_rows(db, "SELECT user_id, project_role FROM src_dop_project_members WHERE project_id = :p", {"p": pp["id"]}):
        if m["user_id"] not in have and m["user_id"] in user_ids:
            db.add(ProjectMember(project_id=project.id, user_id=m["user_id"], project_role=m["project_role"] or "developer"))
            have.add(m["user_id"])
    try:
        prod_vars = await _prod_rows(db, f"SELECT name, {', '.join(_VAR_COLS)} FROM src_dop_project_variables WHERE project_id = :p",
                                     {"p": pp["id"]})
    except ReleaseError:
        prod_vars = []   # 老版本没有环境变量表
    pre_vars = {v.name: v for v in (await db.execute(select(ProjectVariable).where(ProjectVariable.project_id == project.id))).scalars().all()}
    for v in prod_vars:
        row = pre_vars.get(v["name"]) or ProjectVariable(project_id=project.id, name=v["name"])
        for c in _VAR_COLS:
            setattr(row, c, v[c])
        if v["name"] not in pre_vars:
            db.add(row)
    await db.flush()

    counts, errors = _empty_counts(), []
    for key, prod_snap, prod_meta, api, state, act in await _project_states(db, code, project, overwrite):
        if act == "keep":
            counts["keep"] += 1
            continue
        try:
            async with db.begin_nested():
                if act in ("create", "update"):
                    await apply_to_pre(db, prod_snap, project, api, prod_meta.get("status"), user, ignore_lock=True)
                if act in ("create", "update", "same"):
                    await save_base(db, code, key[0], key[1], prod_snap, prod_meta.get("version"), "pull", user.username)
            counts[act] += 1
        except ReleaseError as e:
            errors.append({"method": key[0], "url_path": key[1], "msg": str(e)})
    log.info(f"从生产同步项目 | project={code} | by={user.username} | {counts} | 失败 {len(errors)}")
    return {"code": code, "created_project": created_project, "counts": counts, "errors": errors}
