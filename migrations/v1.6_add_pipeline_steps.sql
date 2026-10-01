-- ============================================================
-- OneData Portal v1.6 数据库迁移
-- 新增多步骤管线字段
-- ============================================================
-- 开发环境用 SQLite 且依赖 Base.metadata.create_all 自动建表，
-- 无需执行本脚本。
-- 生产环境（MySQL）已有 src_dop_api_configs 表的，执行下面这条：

ALTER TABLE src_dop_api_configs
    ADD COLUMN pipeline_steps TEXT NULL COMMENT '多步骤管线 JSON 配置，空则走单 SQL 模式';

-- 回滚:
-- ALTER TABLE src_dop_api_configs DROP COLUMN pipeline_steps;
