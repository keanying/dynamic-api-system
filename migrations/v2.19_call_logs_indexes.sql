-- v2.19 调用日志统计索引（性能优化）
--
-- 作用：API 列表 / 项目详情的调用数与平均耗时、仪表盘统计、今日调用分布、
--       按 API 筛选的调用日志，都可以只读索引完成，不再回表读整行。
--
-- 说明：
--   * 新部署无需执行（启动建表时会自动创建）；已有数据的库需要手动执行一次。
--   * MySQL 5.6+ / 8.0 的 ADD INDEX 是在线 DDL，执行期间不阻塞写入，但大表耗时较长
--     （千万行级别可能要几分钟），建议在低峰期执行。
--   * 生产表和预发(_pre)表各执行一次；某张表不存在就跳过对应语句。

ALTER TABLE `src_dop_call_logs`
    ADD INDEX `ix_call_logs_api_created_rt` (`api_id`, `created_at`, `response_time_ms`),
    ADD INDEX `ix_call_logs_project_created_rt` (`project_id`, `created_at`, `response_time_ms`),
    ADD INDEX `ix_call_logs_created_status_rt` (`created_at`, `response_status`, `response_time_ms`);

ALTER TABLE `src_dop_call_logs_pre`
    ADD INDEX `ix_call_logs_api_created_rt_pre` (`api_id`, `created_at`, `response_time_ms`),
    ADD INDEX `ix_call_logs_project_created_rt_pre` (`project_id`, `created_at`, `response_time_ms`),
    ADD INDEX `ix_call_logs_created_status_rt_pre` (`created_at`, `response_status`, `response_time_ms`);
