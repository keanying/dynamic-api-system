# -*- coding: utf-8 -*-
"""
启动备份 & pre 环境数据同步 (v2.8)

两件事，都在应用启动时执行：

1. 备份（每次启动都做）
   把「正式表」的 用户 / 项目 / API / 数据源 四类核心数据导出为可直接回灌的
   SQL(INSERT) 文件，存到项目根目录的 backup/ 下。文件按启动时间命名；
   同名（同一时刻）直接覆盖。

   备份的始终是正式表（无 _pre 后缀），无论当前跑 pre 还是 prod —— 因为要保护的
   是正式数据。

2. pre 同步（仅 pre 环境）
   pre 环境下，若某张 _pre 表「为空」，就从对应正式表把数据整体同步过去；
   若 _pre 表已有数据，则保留不动（避免冲掉正在测试的改动）。
   这样 pre 环境首次启动会得到一份正式数据副本，之后你在 pre 里怎么改都不影响正式。

实现说明：用 SQLAlchemy 的 text() 走原生 SQL，兼容 SQLite / MySQL；
表名统一从 runtime_env.t() / base_table_name() 推导，避免硬编码。
"""
import os
import datetime
from typing import List

from sqlalchemy import text
from app.core.database import engine
from app.core.logging import get_logger
from app.core.runtime_env import IS_PRE, TABLE_SUFFIX, t as _t, base_table_name
from app.core.config import BASE_DIR

log = get_logger("backup")

# 需要备份 / 同步的核心业务表（正式表名，无后缀）
CORE_TABLES = [
    "src_dop_users",        # 用户（被 projects/api_configs.created_by 等引用，放最前）
    "src_dop_datasources",  # 数据源（被 api_configs.datasource_id 引用）
    "src_dop_projects",     # 项目（被 api_configs.project_id 引用）
    "src_dop_api_configs",  # API（依赖 datasources + projects）
    "src_dop_api_parameters",  # API 参数（依赖 api_configs）
]

# 预发与生产共用、不加 _pre 后缀的表 (v2.23)
SHARED_TABLES = {"src_dop_users"}

BACKUP_DIR = os.path.join(str(BASE_DIR), "backup")


def _is_sqlite() -> bool:
    return "sqlite" in str(engine.url)


def _qi(name: str) -> str:
    """跨库标识符引用：MySQL 用反引号，SQLite/其它用双引号。
    避免 MySQL 把双引号当成字符串字面量而报 1064 语法错误。"""
    if _is_sqlite():
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def _sql_quote(val) -> str:
    """把一个 Python 值转成 SQL 字面量。"""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, (datetime.datetime, datetime.date)):
        return "'" + str(val) + "'"
    # 字符串：转义单引号
    s = str(val).replace("'", "''")
    return "'" + s + "'"


async def _table_exists(conn, table: str) -> bool:
    if _is_sqlite():
        r = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"
        ), {"n": table})
    else:
        r = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :n"
        ), {"n": table})
    return r.first() is not None


async def _table_columns(conn, table: str) -> List[str]:
    if _is_sqlite():
        r = await conn.execute(text(f'PRAGMA table_info("{table}")'))
        return [row[1] for row in r.fetchall()]
    else:
        r = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :n ORDER BY ordinal_position"
        ), {"n": table})
        return [row[0] for row in r.fetchall()]


async def _row_count(conn, table: str) -> int:
    r = await conn.execute(text(f'SELECT COUNT(*) FROM {_qi(table)}'))
    return int(r.scalar() or 0)


async def backup_core_tables():
    """把正式表核心数据导出为 SQL(INSERT) 文件到 backup/。每次启动都执行。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # 按启动时间命名；同名（同一时刻再次触发）直接覆盖
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(BACKUP_DIR, f"backup_{ts}.sql")

    lines: List[str] = []
    lines.append(f"-- OneData Portal 正式数据备份")
    lines.append(f"-- 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"-- 表: {', '.join(CORE_TABLES)}")
    lines.append(f"-- 说明: 正式表数据快照，可直接执行以回灌（按需先清空目标表）")
    lines.append("")

    total_rows = 0
    async with engine.connect() as conn:
        for table in CORE_TABLES:  # 备份始终针对正式表（无后缀）
            if not await _table_exists(conn, table):
                lines.append(f"-- [跳过] 表不存在: {table}")
                lines.append("")
                continue
            cols = await _table_columns(conn, table)
            r = await conn.execute(text(f'SELECT * FROM {_qi(table)}'))
            rows = r.fetchall()
            lines.append(f"-- 表 {table}: {len(rows)} 行")
            if rows:
                col_list = ", ".join(f"`{c}`" for c in cols)
                for row in rows:
                    vals = ", ".join(_sql_quote(v) for v in row)
                    lines.append(f"INSERT INTO `{table}` ({col_list}) VALUES ({vals});")
                total_rows += len(rows)
            lines.append("")

    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info(f"启动备份完成 | 文件={fname} | 表数={len(CORE_TABLES)} | 总行数={total_rows}")
    return fname


async def sync_prod_to_pre():
    """pre 环境专用：把正式表数据同步到「为空的」_pre 表。
    _pre 表已有数据则保留（不冲掉测试中的改动）。"""
    if not IS_PRE:
        return
    synced, skipped = [], []
    is_mysql = not _is_sqlite()
    async with engine.begin() as conn:
        # MySQL：整段同步期间临时关闭外键检查。
        # 原因：api_configs_pre 有外键指向 datasources_pre/projects_pre，逐表 INSERT..SELECT
        # 时若父表尚未同步，会触发 1216/1452 外键错误，导致整个事务回滚、pre 表全空、
        # 进而登录时"用户不存在"。CORE_TABLES 已按依赖排序，这里再关一层 FK 检查双保险。
        if is_mysql:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for base in CORE_TABLES:
                if base in SHARED_TABLES:   # 两个环境共用的表（如用户表）不需要同步
                    continue
                pre_table = _t(base)   # 加 _pre 后缀
                # _pre 表应已由 create_all 建好；正式表存在才能同步
                if not await _table_exists(conn, pre_table):
                    log.warning(f"pre 同步跳过：目标表不存在 {pre_table}")
                    continue
                if not await _table_exists(conn, base):
                    log.warning(f"pre 同步跳过：源正式表不存在 {base}")
                    continue

                pre_cnt = await _row_count(conn, pre_table)
                if pre_cnt > 0:
                    skipped.append(f"{pre_table}(已有{pre_cnt}行)")
                    continue

                # 只同步两边都有的列（pre 表由当前模型建，列可能比老正式表多）
                base_cols = await _table_columns(conn, base)
                pre_cols = set(await _table_columns(conn, pre_table))
                common = [c for c in base_cols if c in pre_cols]
                if not common:
                    skipped.append(f"{pre_table}(无公共列)")
                    continue

                col_list = ", ".join(_qi(c) for c in common)
                # 用 INSERT ... SELECT 直接在库内复制，效率高、避免大数据搬运
                await conn.execute(text(
                    f'INSERT INTO {_qi(pre_table)} ({col_list}) SELECT {col_list} FROM {_qi(base)}'
                ))
                cnt = await _row_count(conn, pre_table)
                synced.append(f"{pre_table}({cnt}行)")
        finally:
            if is_mysql:
                await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

    if synced:
        log.info(f"pre 同步完成（正式→pre）| 已同步: {', '.join(synced)}")
    if skipped:
        log.info(f"pre 同步跳过（已有数据/无源）| {', '.join(skipped)}")


async def run_startup_tasks():
    """启动时统一调用：先备份正式数据，再（若 pre 环境）同步到 pre。"""
    try:
        await backup_core_tables()
    except Exception as e:
        log.warning(f"启动备份失败（不阻断启动）| error={str(e)}")
    try:
        await sync_prod_to_pre()
    except Exception as e:
        log.warning(f"pre 同步失败（不阻断启动）| error={str(e)}")
