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
"""
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text

from app.core.logging import get_logger
from app.models.models import ApiConfig, DataSource, Project, ProjectMember, ProjectVariable, User
from app.services.release import (ReleaseError, SYNC_STATES, apply_to_pre, build_snapshot, load_bases,
                                  read_env_project_snapshots, save_base, sync_state)

log = get_logger("env_sync")

CONFLICT_STATES = ("pre_ahead", "both", "diverged")
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


async def plan(db, overwrite: bool = False) -> Dict[str, Any]:
    """预览：逐项目、逐 API 列出会怎么处理。"""
    from app.services.api_lifecycle import STATUS_PENDING, STATUS_APPROVED
    prod_projects = await _prod_rows(db, "SELECT id, code, name FROM src_dop_projects ORDER BY id")
    pre_projects = {p.code: p for p in (await db.execute(select(Project))).scalars().all()}
    prod_ds = {r["name"] for r in await _prod_rows(db, "SELECT name FROM src_dop_datasources")}
    pre_ds = set((await db.execute(select(DataSource.name))).scalars().all())

    counts = {k: 0 for k in ("project_create", "project_update", "ds_create",
                             "create", "update", "same", "conflict", "skip_review", "keep")}
    counts["ds_create"] = len(prod_ds - pre_ds)
    projects: List[Dict[str, Any]] = []
    for pp in prod_projects:
        code = pp["code"]
        pre_p = pre_projects.get(code)
        counts["project_update" if pre_p else "project_create"] += 1
        prod_apis = await read_env_project_snapshots(db, "", code) or {}
        bases = await load_bases(db, code)
        pre_apis = {}
        if pre_p:
            for a in (await db.execute(select(ApiConfig).where(ApiConfig.project_id == pre_p.id))).scalars().all():
                pre_apis[(a.method, a.url_path)] = a
        items = []
        for key in list(prod_apis.keys()) + [k for k in pre_apis if k not in prod_apis]:
            prod_snap, prod_meta = prod_apis.get(key, (None, None))
            api = pre_apis.get(key)
            base = bases.get(key)
            state = sync_state(prod_snap, await build_snapshot(db, api) if api else None,
                               base["snapshot"] if base else None)
            act = _action(state, bool(api and api.status in (STATUS_PENDING, STATUS_APPROVED)), overwrite)
            counts[act] += 1
            items.append({"method": key[0], "url_path": key[1],
                          "name": (api.name if api else (prod_meta or {}).get("name")) or "",
                          "state": state, "state_label": SYNC_STATES[state], "action": act})
        projects.append({"code": code, "name": pp["name"], "exists_in_pre": pre_p is not None, "apis": items})
    counts["conflict_total"] = sum(1 for p in projects for i in p["apis"] if i["state"] in CONFLICT_STATES
                                   and i["action"] in ("conflict", "update"))
    return {"projects": projects, "counts": counts}


async def run(db, user: User, overwrite: bool = False) -> Dict[str, Any]:
    """执行同步。调用方负责 commit。返回各类数量和失败明细。"""
    from app.services.api_lifecycle import STATUS_PENDING, STATUS_APPROVED
    result = {k: 0 for k in ("project_create", "project_update", "ds_create",
                             "create", "update", "same", "conflict", "skip_review", "keep")}
    errors: List[Dict[str, str]] = []

    # 1. 数据源：只补预发没有的
    pre_ds = set((await db.execute(select(DataSource.name))).scalars().all())
    for r in await _prod_rows(db, f"SELECT {', '.join(_DS_COLS)} FROM src_dop_datasources"):
        if r["name"] in pre_ds:
            continue
        db.add(DataSource(**{c: r[c] for c in _DS_COLS}))
        pre_ds.add(r["name"])
        result["ds_create"] += 1
    await db.flush()

    user_ids = set((await db.execute(select(User.id))).scalars().all())
    for pp in await _prod_rows(db, "SELECT id, code, name, description, api_key, is_active FROM src_dop_projects ORDER BY id"):
        code = pp["code"]
        # 2. 项目
        project = (await db.execute(select(Project).where(Project.code == code))).scalar_one_or_none()
        if project is None:
            project = Project(code=code)
            db.add(project)
            result["project_create"] += 1
        else:
            result["project_update"] += 1
        project.name = pp["name"]
        project.description = pp["description"] or ""
        project.api_key = pp["api_key"] or ""
        project.is_active = bool(pp["is_active"]) if pp["is_active"] is not None else True
        await db.flush()

        have = set((await db.execute(select(ProjectMember.user_id).where(ProjectMember.project_id == project.id))).scalars().all())
        for m in await _prod_rows(db, "SELECT user_id, project_role FROM src_dop_project_members WHERE project_id = :p", {"p": pp["id"]}):
            if m["user_id"] not in have and m["user_id"] in user_ids:
                db.add(ProjectMember(project_id=project.id, user_id=m["user_id"], project_role=m["project_role"] or "developer"))
                have.add(m["user_id"])
        try:
            prod_vars = await _prod_rows(db, f"SELECT name, {', '.join(_VAR_COLS)} FROM src_dop_project_variables WHERE project_id = :p", {"p": pp["id"]})
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

        # 3. API
        prod_apis = await read_env_project_snapshots(db, "", code) or {}
        bases = await load_bases(db, code)
        pre_apis = {(a.method, a.url_path): a for a in
                    (await db.execute(select(ApiConfig).where(ApiConfig.project_id == project.id))).scalars().all()}
        result["keep"] += sum(1 for k in pre_apis if k not in prod_apis)
        for key, (prod_snap, prod_meta) in prod_apis.items():
            api = pre_apis.get(key)
            base = bases.get(key)
            state = sync_state(prod_snap, await build_snapshot(db, api) if api else None,
                               base["snapshot"] if base else None)
            act = _action(state, bool(api and api.status in (STATUS_PENDING, STATUS_APPROVED)), overwrite)
            try:
                async with db.begin_nested():
                    if act in ("create", "update"):
                        await apply_to_pre(db, prod_snap, project, api, prod_meta.get("status"), user, ignore_lock=True)
                    if act in ("create", "update", "same"):
                        await save_base(db, code, key[0], key[1], prod_snap, prod_meta.get("version"), "pull", user.username)
                result[act] += 1
            except ReleaseError as e:
                errors.append({"project": code, "method": key[0], "url_path": key[1], "msg": str(e)})
    log.info(f"从生产同步全部到预发 | by={user.username} | {result} | 失败 {len(errors)}")
    return {"counts": result, "errors": errors}
