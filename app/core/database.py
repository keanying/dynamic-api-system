"""
数据库引擎与会话管理
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("database")

# 根据数据库类型选择不同的引擎参数
engine_kwargs = {
    "echo": settings.database.echo,
}

# SQLite 不支持 pool_size 等参数
if "sqlite" not in settings.database.url:
    engine_kwargs.update({
        "pool_size": settings.database.pool_size,
        "pool_recycle": settings.database.pool_recycle,
        "max_overflow": settings.database.max_overflow,
    })

log.debug(f"数据库引擎配置 | url={settings.database.url} | kwargs={engine_kwargs}")

engine = create_async_engine(settings.database.url, **engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI 依赖注入：获取数据库会话"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            log.error(f"数据库会话异常，已回滚 | error={str(e)}")
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# 自动迁移：检测并补全缺失字段（兼容 SQLite / MySQL）
# ---------------------------------------------------------------------------

async def _get_existing_columns(conn, table_name: str, is_mysql: bool) -> dict:
    """
    返回 {列名小写: {nullable: bool, col_type: str}} 字典
    """
    cols: dict = {}
    if is_mysql:
        result = await conn.execute(text(
            "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tbl"
        ), {"tbl": table_name})
        for row in result.fetchall():
            cols[row[0].lower()] = {
                "nullable": row[1] == "YES",
                "col_type": row[2],
            }
    else:
        # SQLite
        result = await conn.execute(text(f"PRAGMA table_info(`{table_name}`)"))
        for row in result.fetchall():
            # row: (cid, name, type, notnull, dflt_value, pk)
            cols[row[1].lower()] = {
                "nullable": row[3] == 0,  # notnull=0 means nullable
                "col_type": row[2],
            }
    return cols


async def _get_foreign_keys(conn, table_name: str, column_name: str) -> list:
    """获取 MySQL 表某列上的所有外键约束名"""
    result = await conn.execute(text(
        "SELECT CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :tbl "
        "AND COLUMN_NAME = :col AND REFERENCED_TABLE_NAME IS NOT NULL"
    ), {"tbl": table_name, "col": column_name})
    return [row[0] for row in result.fetchall()]


async def _run_migration(conn, table_suffix: str = ""):
    """在已有数据库上补全新增字段、修改 nullable 属性。
    table_suffix: 表名后缀（正式表传 ""，pre 环境表传 "_pre"），
    使同一套迁移逻辑能同时修补正式表和 pre 表，避免 pre 表因
    「假设 create_all 已建齐全字段」而漏掉后续新增的列（如 owner_id）。
    """
    """在已有数据库上补全新增字段、修改 nullable 属性"""
    db_url = settings.database.url
    is_mysql = "mysql" in db_url

    log.info(f"开始自动迁移检测 | is_mysql={is_mysql}")

    # ---- 1. src_dop_users: 补 nickname ----
    try:
        cols = await _get_existing_columns(conn, "src_dop_users" + table_suffix, is_mysql)
        if cols and "nickname" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_users{table_suffix}` ADD COLUMN `nickname` VARCHAR(64) NOT NULL DEFAULT '' AFTER `username`"
                ))
            else:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_users{table_suffix}` ADD COLUMN `nickname` VARCHAR(64) NOT NULL DEFAULT ''"
                ))
            log.info("迁移: src_dop_users 添加 nickname 列")
        if cols and "global_role" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_users{table_suffix}` ADD COLUMN `global_role` VARCHAR(16) NOT NULL DEFAULT 'user'"
                ))
            else:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_users{table_suffix}` ADD COLUMN `global_role` VARCHAR(16) NOT NULL DEFAULT 'user'"
                ))
            log.info("迁移: src_dop_users 添加 global_role 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_users 失败 | error={str(e)}")

    # ---- 2. src_dop_projects: 补 code ----
    try:
        cols = await _get_existing_columns(conn, "src_dop_projects" + table_suffix, is_mysql)
        if cols and "code" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_projects{table_suffix}` ADD COLUMN `code` VARCHAR(64) NOT NULL DEFAULT '' AFTER `name`"
                ))
            else:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_projects{table_suffix}` ADD COLUMN `code` VARCHAR(64) NOT NULL DEFAULT ''"
                ))
            log.info("迁移: src_dop_projects 添加 code 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_projects.code 失败 | error={str(e)}")

    # ---- 3. src_dop_call_logs: 补 response_data + 修改 nullable ----
    try:
        cols = await _get_existing_columns(conn, "src_dop_call_logs" + table_suffix, is_mysql)
        if not cols:
            log.debug("src_dop_call_logs 表不存在，跳过迁移")
        else:
            # 3a. 补 response_data 列
            if "response_data" not in cols:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_call_logs{table_suffix}` ADD COLUMN `response_data` TEXT DEFAULT NULL"
                ))
                log.info("迁移: src_dop_call_logs 添加 response_data 列")

            # 3a2. 补 call_source 列（区分 gateway / test 来源）
            if "call_source" not in cols:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_call_logs{table_suffix}` ADD COLUMN `call_source` VARCHAR(32) NOT NULL DEFAULT 'gateway'"
                ))
                log.info("迁移: src_dop_call_logs 添加 call_source 列")

            # 3a3. v2.4 关键节点列（网关建档 + trace 回填）
            _v24_cols = [
                ("rendered_sql", "TEXT DEFAULT NULL", "TEXT"),
                ("executed_sql", "TEXT DEFAULT NULL", "TEXT"),
                ("render_time_ms", "DOUBLE DEFAULT 0", "REAL DEFAULT 0"),
                ("query_time_ms", "DOUBLE DEFAULT 0", "REAL DEFAULT 0"),
                ("row_count", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
                ("cache_hit", "TINYINT(1) DEFAULT 0", "BOOLEAN DEFAULT 0"),
                ("error_stack", "TEXT DEFAULT NULL", "TEXT"),
            ]
            for cname, mysql_type, sqlite_type in _v24_cols:
                if cname not in cols:
                    coltype = mysql_type if is_mysql else sqlite_type
                    await conn.execute(text(
                        f"ALTER TABLE `src_dop_call_logs{table_suffix}` ADD COLUMN `{cname}` {coltype}"
                    ))
                    log.info(f"迁移: src_dop_call_logs 添加 {cname} 列")

            if is_mysql:
                # 3b. api_id: 删除旧外键 -> 改为 nullable -> 重建外键 ON DELETE SET NULL
                if "api_id" in cols and not cols["api_id"]["nullable"]:
                    # 删除所有 api_id 上的外键约束
                    fk_names = await _get_foreign_keys(conn, "src_dop_call_logs" + table_suffix, "api_id")
                    for fk_name in fk_names:
                        try:
                            await conn.execute(text(
                                f"ALTER TABLE `src_dop_call_logs{table_suffix}` DROP FOREIGN KEY `{fk_name}`"
                            ))
                            log.info(f"迁移: 删除外键 {fk_name}")
                        except Exception as e:
                            log.warning(f"删除外键 {fk_name} 失败（可能已不存在） | error={str(e)}")

                    # 修改列为 nullable
                    await conn.execute(text(
                        f"ALTER TABLE `src_dop_call_logs{table_suffix}` MODIFY COLUMN `api_id` INT NULL DEFAULT NULL"
                    ))
                    log.info("迁移: src_dop_call_logs.api_id 改为 nullable")

                    # 重建外键 ON DELETE SET NULL
                    try:
                        await conn.execute(text(
                            f"ALTER TABLE `src_dop_call_logs{table_suffix}` ADD CONSTRAINT `fk_call_logs_api_id` "
                            f"FOREIGN KEY (`api_id`) REFERENCES `src_dop_api_configs{table_suffix}`(`id`) ON DELETE SET NULL"
                        ))
                        log.info("迁移: 重建外键 fk_call_logs_api_id (ON DELETE SET NULL)")
                    except Exception as e:
                        log.warning(f"重建外键 fk_call_logs_api_id 失败（可忽略） | error={str(e)}")

                # 3c. project_id: 改为 nullable
                if "project_id" in cols and not cols["project_id"]["nullable"]:
                    await conn.execute(text(
                        f"ALTER TABLE `src_dop_call_logs{table_suffix}` MODIFY COLUMN `project_id` INT NULL DEFAULT NULL"
                    ))
                    log.info("迁移: src_dop_call_logs.project_id 改为 nullable")

    except Exception as e:
        log.warning(f"迁移 src_dop_call_logs 失败 | error={str(e)}")

    # ---- 4. src_dop_api_configs: 补 pipeline_steps（v1.6+ 多步骤管线）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "pipeline_steps" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `pipeline_steps` TEXT NULL "
                    "COMMENT '多步骤管线JSON配置，空则走单SQL模式'"
                ))
            else:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `pipeline_steps` TEXT"
                ))
            log.info("迁移: src_dop_api_configs 添加 pipeline_steps 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.pipeline_steps 失败 | error={str(e)}")

    # ---- 5. src_dop_api_configs: 补 api_type / html_content / css_content / js_content
    #         (v1.8+ HTML 静态页面 API) ----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols:
            if "api_type" not in cols:
                if is_mysql:
                    await conn.execute(text(
                        f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `api_type` VARCHAR(16) "
                        "NOT NULL DEFAULT 'sql' COMMENT 'API 类型: sql / html'"
                    ))
                else:
                    await conn.execute(text(
                        f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `api_type` VARCHAR(16) "
                        "NOT NULL DEFAULT 'sql'"
                    ))
                log.info("迁移: src_dop_api_configs 添加 api_type 列")
            if "html_content" not in cols:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `html_content` TEXT"
                ))
                log.info("迁移: src_dop_api_configs 添加 html_content 列")
            if "css_content" not in cols:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `css_content` TEXT"
                ))
                log.info("迁移: src_dop_api_configs 添加 css_content 列")
            if "js_content" not in cols:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `js_content` TEXT"
                ))
                log.info("迁移: src_dop_api_configs 添加 js_content 列")
            if "plugin_code" not in cols:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `plugin_code` TEXT"
                ))
                log.info("迁移: src_dop_api_configs 添加 plugin_code 列")
            if "status" not in cols:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `status` VARCHAR(16) NOT NULL DEFAULT 'draft'"
                ))
                # 已存在的老 API 视为已上线，保持对外可用（新建的才是 draft）
                await conn.execute(text(
                    f"UPDATE `src_dop_api_configs{table_suffix}` SET `status` = 'online' WHERE `status` = 'draft'"
                ))
                log.info("迁移: src_dop_api_configs 添加 status 列（存量 API 置为 online）")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs (html 类型字段) 失败 | error={str(e)}")

    # ---- 6. src_dop_api_configs: 补 require_api_key （v1.8+ API Key 校验开关）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "require_api_key" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `require_api_key` "
                    "TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否校验 API Key'"
                ))
            else:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `require_api_key` "
                    "BOOLEAN NOT NULL DEFAULT 1"
                ))
            log.info("迁移: src_dop_api_configs 添加 require_api_key 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.require_api_key 失败 | error={str(e)}")

    # ---- 6.5 src_dop_api_configs: html/css/js 列升级为 LONGTEXT（仅 MySQL）----
    #   普通 TEXT 上限 64KB，静态页面内联地图库/大量数据时会被截断。
    #   升级为 LONGTEXT（4GB）。用 information_schema 判断当前类型，已是 longtext 则跳过，避免每次启动都改。
    if is_mysql:
        try:
            for col in ("html_content", "css_content", "js_content"):
                r = await conn.execute(text(
                    "SELECT DATA_TYPE FROM information_schema.columns "
                    f"WHERE table_schema = DATABASE() AND table_name = 'src_dop_api_configs{table_suffix}' "
                    "AND column_name = :c"
                ), {"c": col})
                row = r.first()
                if row and str(row[0]).lower() != "longtext":
                    await conn.execute(text(
                        f"ALTER TABLE `src_dop_api_configs{table_suffix}` MODIFY COLUMN `{col}` LONGTEXT"
                    ))
                    log.info(f"迁移: src_dop_api_configs.{col} 升级为 LONGTEXT（支持大页面）")
        except Exception as e:
            log.warning(f"迁移 src_dop_api_configs html/css/js 升级 LONGTEXT 失败 | error={str(e)}")

    # ---- 6.6 src_dop_api_configs: 补 owner_id（v2.10+ 责任人制）----
    #   历史 API 无责任人，回填为创建人 created_by（创建人即默认责任人）。
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "owner_id" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `owner_id` INT NULL"
                ))
            else:
                await conn.execute(text(
                    f'ALTER TABLE "src_dop_api_configs{table_suffix}" ADD COLUMN owner_id INTEGER'
                ))
            # 历史数据：责任人 = 创建人
            await conn.execute(text(
                f"UPDATE `src_dop_api_configs{table_suffix}` SET `owner_id` = `created_by` "
                "WHERE `owner_id` IS NULL AND `created_by` IS NOT NULL"
            ) if is_mysql else text(
                f'UPDATE "src_dop_api_configs{table_suffix}" SET owner_id = created_by '
                'WHERE owner_id IS NULL AND created_by IS NOT NULL'
            ))
            log.info("迁移: src_dop_api_configs 添加 owner_id 列（历史 API 责任人回填为创建人）")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.owner_id 失败 | error={str(e)}")

    # ---- 6.6.1 src_dop_api_configs: 补 cache_prewarm（v2.13+ 缓存自动预热开关）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "cache_prewarm" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` "
                    "ADD COLUMN `cache_prewarm` TINYINT(1) NOT NULL DEFAULT 0"
                ))
            else:
                await conn.execute(text(
                    f'ALTER TABLE "src_dop_api_configs{table_suffix}" '
                    'ADD COLUMN cache_prewarm BOOLEAN NOT NULL DEFAULT 0'
                ))
            log.info("迁移: src_dop_api_configs 添加 cache_prewarm 列（缓存自动预热开关，默认关闭）")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.cache_prewarm 失败 | error={str(e)}")

    # ---- 6.6.2 src_dop_api_configs: 补 prewarm_param_overrides（v2.14+ 预热参数模板）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "prewarm_param_overrides" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` "
                    "ADD COLUMN `prewarm_param_overrides` TEXT"
                ))
            else:
                await conn.execute(text(
                    f'ALTER TABLE "src_dop_api_configs{table_suffix}" '
                    'ADD COLUMN prewarm_param_overrides TEXT'
                ))
            log.info("迁移: src_dop_api_configs 添加 prewarm_param_overrides 列（预热参数覆盖）")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.prewarm_param_overrides 失败 | error={str(e)}")

    # ---- 6.6.3 src_dop_api_configs: 补 prewarm_stop_daily（v2.16+ 预热跨日停止开关）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "prewarm_stop_daily" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` "
                    "ADD COLUMN `prewarm_stop_daily` TINYINT(1) NOT NULL DEFAULT 0"
                ))
            else:
                await conn.execute(text(
                    f'ALTER TABLE "src_dop_api_configs{table_suffix}" '
                    'ADD COLUMN prewarm_stop_daily BOOLEAN NOT NULL DEFAULT 0'
                ))
            log.info("迁移: src_dop_api_configs 添加 prewarm_stop_daily 列（预热跨日停止，默认关闭）")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.prewarm_stop_daily 失败 | error={str(e)}")

    # ---- 6.6.4 src_dop_api_configs: 补 sync_tables（v2.17+ 数据同步白名单）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "sync_tables" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `sync_tables` TEXT"
                ))
            else:
                await conn.execute(text(
                    f'ALTER TABLE "src_dop_api_configs{table_suffix}" ADD COLUMN sync_tables TEXT'
                ))
            log.info("迁移: src_dop_api_configs 添加 sync_tables 列（数据同步表白名单）")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.sync_tables 失败 | error={str(e)}")

    # ---- 6.7 src_dop_api_approvals: reviewer_id 改为可空（v2.10 管理员上线单可无会审）----
    if is_mysql:
        try:
            r = await conn.execute(text(
                "SELECT IS_NULLABLE FROM information_schema.columns "
                f"WHERE table_schema = DATABASE() AND table_name = 'src_dop_api_approvals{table_suffix}' "
                "AND column_name = 'reviewer_id'"
            ))
            row = r.first()
            if row and str(row[0]).upper() == "NO":
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_approvals{table_suffix}` MODIFY COLUMN `reviewer_id` INT NULL"
                ))
                log.info("迁移: src_dop_api_approvals.reviewer_id 改为可空（管理员上线单可无会审）")
        except Exception as e:
            log.warning(f"迁移 src_dop_api_approvals.reviewer_id 失败 | error={str(e)}")

    # ---- 7. src_dop_call_logs: 补 trace_id （v1.9+ 全链路 trace_id）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_call_logs" + table_suffix, is_mysql)
        if cols and "trace_id" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_call_logs{table_suffix}` ADD COLUMN `trace_id` "
                    "VARCHAR(64) NOT NULL DEFAULT '' COMMENT '全链路 trace_id'"
                ))
                # 顺手补索引（失败可忽略，可能已存在）
                try:
                    await conn.execute(text(
                        f"ALTER TABLE `src_dop_call_logs{table_suffix}` ADD INDEX `ix_call_logs_trace_id` (`trace_id`)"
                    ))
                except Exception as ie:
                    log.debug(f"trace_id 索引创建跳过 | {ie}")
            else:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_call_logs{table_suffix}` ADD COLUMN `trace_id` VARCHAR(64) NOT NULL DEFAULT ''"
                ))
                try:
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS `ix_call_logs_trace_id` "
                        f"ON `src_dop_call_logs{table_suffix}` (`trace_id`)"
                    ))
                except Exception as ie:
                    log.debug(f"trace_id 索引创建跳过 | {ie}")
            log.info("迁移: src_dop_call_logs 添加 trace_id 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_call_logs.trace_id 失败 | error={str(e)}")

    # ---- src_dop_datasources: 补 created_by (v2.7+) ----
    try:
        cols = await _get_existing_columns(conn, "src_dop_datasources" + table_suffix, is_mysql)
        if cols and "created_by" not in cols:
            await conn.execute(text(
                f"ALTER TABLE `src_dop_datasources{table_suffix}` ADD COLUMN `created_by` INTEGER NULL"
            ))
            log.info("迁移: src_dop_datasources 添加 created_by 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_datasources.created_by 失败 | error={str(e)}")

    # ---- src_dop_api_configs: 补 is_locked (v2.9+) ----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "is_locked" not in cols:
            default_false = "0"
            await conn.execute(text(
                f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `is_locked` BOOLEAN DEFAULT {default_false}"
            ))
            log.info("迁移: src_dop_api_configs 添加 is_locked 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.is_locked 失败 | error={str(e)}")

    # ---- src_dop_api_configs: 补 created_by (v2.9+) ----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_configs" + table_suffix, is_mysql)
        if cols and "created_by" not in cols:
            await conn.execute(text(
                f"ALTER TABLE `src_dop_api_configs{table_suffix}` ADD COLUMN `created_by` INTEGER NULL"
            ))
            log.info("迁移: src_dop_api_configs 添加 created_by 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_configs.created_by 失败 | error={str(e)}")

    # ---- v2.1: src_dop_api_parameters 补 item_schema（复杂/嵌套参数结构定义）----
    try:
        cols = await _get_existing_columns(conn, "src_dop_api_parameters" + table_suffix, is_mysql)
        if cols and "item_schema" not in cols:
            if is_mysql:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_parameters{table_suffix}` ADD COLUMN `item_schema` TEXT NULL "
                    "COMMENT '嵌套参数结构定义 JSON（array/object 类型使用）'"
                ))
            else:
                await conn.execute(text(
                    f"ALTER TABLE `src_dop_api_parameters{table_suffix}` ADD COLUMN `item_schema` TEXT"
                ))
            log.info("迁移: src_dop_api_parameters 添加 item_schema 列")
    except Exception as e:
        log.warning(f"迁移 src_dop_api_parameters.item_schema 失败 | error={str(e)}")

    log.info("自动迁移检测完成")



async def init_db():
    """初始化数据库表 + 自动迁移补全字段"""
    log.info("开始初始化数据库表...")
    # 确保所有模型已加载并注册到 Base.metadata（否则 create_all 不会建新表）
    import app.models.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("数据库表初始化完成")

    # 自动迁移：检测并补全缺失字段
    #   之前认为 _pre 表由 create_all 按当前模型新建、字段天然齐全，因此整体跳过迁移。
    #   但这个假设只在 _pre 表「第一次」被创建时成立——一旦 _pre 表在更早版本就已建好
    #   （比如新增 owner_id 之前部署过 pre），create_all 只会跳过已存在的表、不会给它
    #   补新列，导致 _pre 表长期缺列，一查询就报
    #   "Unknown column 'src_dop_api_configs_pre.owner_id'" 这类错误。
    #   所以 pre 环境也要跑迁移，只是操作对象换成 _pre 后缀的表（_run_migration 的
    #   table_suffix 参数控制）。
    from app.core.runtime_env import IS_PRE
    if IS_PRE:
        try:
            async with engine.begin() as conn:
                await _run_migration(conn, table_suffix="_pre")
        except Exception as e:
            log.warning(f"pre 环境自动迁移过程中出现异常（不影响首次建表） | error={str(e)}")
    else:
        try:
            async with engine.begin() as conn:
                await _run_migration(conn)
        except Exception as e:
            log.warning(f"自动迁移过程中出现异常（不影响首次建表） | error={str(e)}")


# ---------------------------------------------------------------------------
# 性能索引 (v2.19+)：调用日志统计用覆盖索引，启动后在后台补建
# ---------------------------------------------------------------------------
# 新建的表由 create_all 按模型建好索引；已有的表（尤其是数据量大的 call_logs）
# 需要补建。放在后台任务里做，不阻塞启动；MySQL 用在线 DDL（LOCK=NONE），
# 建索引期间照常读写。也可以提前在低峰期手动执行 migrations/v2.19_call_logs_indexes.sql，
# 已存在的索引这里会自动跳过。
_PERF_INDEXES = [
    ("ix_call_logs_api_created_rt", "api_id, created_at, response_time_ms"),
    ("ix_call_logs_project_created_rt", "project_id, created_at, response_time_ms"),
    ("ix_call_logs_created_status_rt", "created_at, response_status, response_time_ms"),
]


async def ensure_perf_indexes():
    from app.core.runtime_env import t as _t
    table = _t("src_dop_call_logs")
    is_mysql = "mysql" in settings.database.url
    for base_name, cols in _PERF_INDEXES:
        name = _t(base_name)
        try:
            async with engine.begin() as conn:
                if is_mysql:
                    exists = (await conn.execute(text(
                        "SELECT 1 FROM information_schema.statistics "
                        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i LIMIT 1"
                    ), {"t": table, "i": name})).first()
                    if exists:
                        continue
                    log.info(f"补建性能索引开始（在线 DDL，不阻塞读写）| {table}.{name}")
                    await conn.execute(text(
                        f"ALTER TABLE `{table}` ADD INDEX `{name}` ({cols}), ALGORITHM=INPLACE, LOCK=NONE"
                    ))
                else:
                    await conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({cols})'))
                    continue
            log.info(f"补建性能索引完成 | {table}.{name}")
        except Exception as e:  # noqa: BLE001
            # 多 worker 同时补建会有一个报「索引已存在」，忽略即可
            log.warning(f"补建性能索引跳过 | {table}.{name} | {e}")
