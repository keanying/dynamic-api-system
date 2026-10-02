# -*- coding: utf-8 -*-
"""
预发用户并入共用用户表 (v2.23)
==============================

v2.23 起预发和生产共用一张用户表 src_dop_users（单点登录的前提）。
之前预发有自己的 src_dop_users_pre，且预发里的项目成员、API 责任人、审批记录等
保存的是 src_dop_users_pre 的用户 id。预发首次以新版本启动时自动做一次合并：

1. 按「账号(username)」把预发用户对应到共用表里的用户；共用表里没有的账号，原样插入
   （密码哈希、全局角色等一并带过去）。同名账号以共用表（生产）为准。
2. 预发各表里保存用户 id 的列，按对应关系改成共用表的 id。
3. 把 src_dop_users_pre 改名为 src_dop_users_pre_bak_时间，作为备份，同时标记已合并
   （下次启动看不到 src_dop_users_pre 就不会重复执行）。

改 id 时先写成负数再取反，避免「5→9、9→12」这类链式对应在唯一索引上撞车。
"""
import time

from sqlalchemy import text

from app.core.database import engine
from app.core.logging import get_logger
from app.core.runtime_env import IS_PRE

log = get_logger("user_merge")

OLD_TABLE = "src_dop_users_pre"
SHARED_TABLE = "src_dop_users"

# 预发表里保存用户 id 的列（表名不含 _pre 后缀）
USER_REF_COLUMNS = [
    ("src_dop_project_members", "user_id"),
    ("src_dop_datasources", "created_by"),
    ("src_dop_api_configs", "created_by"),
    ("src_dop_api_configs", "owner_id"),
    ("src_dop_api_approvals", "submitter_id"),
    ("src_dop_api_approvals", "reviewer_id"),
    ("src_dop_api_owner_approvals", "requester_id"),
    ("src_dop_api_owner_approvals", "owner_id_snapshot"),
    ("src_dop_api_owner_approvals", "decider_id"),
    ("src_dop_audit_logs", "user_id"),
    ("src_dop_plugin_libraries", "created_by"),
    ("src_dop_project_deletions", "requester_id"),
    ("src_dop_project_deletions", "approver_id"),
    ("src_dop_datasource_deletions", "requester_id"),
    ("src_dop_datasource_deletions", "approver_id"),
]

_USER_COLS = ["nickname", "username", "password_hash", "is_active", "global_role", "token_epoch",
              "created_at", "updated_at"]


def _is_mysql(conn) -> bool:
    return conn.dialect.name == "mysql"


async def _columns(conn, table: str):
    from sqlalchemy import inspect
    try:
        return {c["name"] for c in await conn.run_sync(lambda c: inspect(c).get_columns(table))}
    except Exception:  # noqa: BLE001  表不存在
        return set()


async def merge_pre_users() -> None:
    if not IS_PRE:
        return
    async with engine.begin() as conn:
        old_cols = await _columns(conn, OLD_TABLE)
        if not old_cols:
            return   # 已合并过（或从未有过预发用户表）
        shared_cols = await _columns(conn, SHARED_TABLE)
        if not shared_cols:
            log.warning("共用用户表不存在，跳过预发用户合并")
            return
        mysql = _is_mysql(conn)
        cols = [c for c in _USER_COLS if c in old_cols and c in shared_cols]
        col_sql = ", ".join(cols)

        rows = (await conn.execute(text(f"SELECT id, {col_sql} FROM {OLD_TABLE}"))).mappings().all()
        mapping, created, same = {}, [], 0
        for r in rows:
            sid = (await conn.execute(text(f"SELECT id FROM {SHARED_TABLE} WHERE username = :u"),
                                      {"u": r["username"]})).scalar()
            if sid is None:
                await conn.execute(text(
                    f"INSERT INTO {SHARED_TABLE} ({col_sql}) VALUES ({', '.join(':' + c for c in cols)})"
                ), {c: r[c] for c in cols})
                sid = (await conn.execute(text(f"SELECT id FROM {SHARED_TABLE} WHERE username = :u"),
                                          {"u": r["username"]})).scalar()
                created.append(r["username"])
            if sid != r["id"]:
                mapping[r["id"]] = sid
            else:
                same += 1

        if mysql:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            if mysql:
                # 成员表的外键原先指向 src_dop_users_pre，改名后会跟着指向备份表，先删掉
                fks = (await conn.execute(text(
                    "SELECT CONSTRAINT_NAME, TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = :t"
                ), {"t": OLD_TABLE})).all()
                for name, table in fks:
                    await conn.execute(text(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`"))
                    log.info(f"预发用户合并：删除外键 {table}.{name}（原指向 {OLD_TABLE}）")

            updated = []
            if mapping:
                case = " ".join(f"WHEN {o} THEN {-n}" for o, n in mapping.items())
                ids = ", ".join(str(o) for o in mapping)
                for base, col in USER_REF_COLUMNS:
                    table = base + "_pre"
                    if col not in await _columns(conn, table):
                        continue
                    res = await conn.execute(text(
                        f"UPDATE {table} SET {col} = CASE {col} {case} ELSE {col} END WHERE {col} IN ({ids})"
                    ))
                    await conn.execute(text(f"UPDATE {table} SET {col} = -{col} WHERE {col} < 0"))
                    if res.rowcount:
                        updated.append(f"{table}.{col}({res.rowcount})")

            bak = f"{OLD_TABLE}_bak_{time.strftime('%Y%m%d%H%M%S')}"
            if mysql:
                await conn.execute(text(f"RENAME TABLE `{OLD_TABLE}` TO `{bak}`"))
            else:
                await conn.execute(text(f'ALTER TABLE "{OLD_TABLE}" RENAME TO "{bak}"'))
        finally:
            if mysql:
                await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    log.info(f"预发用户已并入共用用户表 | 预发用户 {len(rows)} 个：id 不变 {same}、id 改写 {len(mapping)}、"
             f"新增到共用表 {len(created)}{('（' + '、'.join(created) + '）') if created else ''} | "
             f"改写引用: {', '.join(updated) if updated else '无'} | 原表备份为 {bak}")
