-- ============================================================
-- 诊断:以前的静态页面 API 访问报 5003 "未找到匹配的 API"
--       (删除重建就正常 -> 旧记录某字段值和新记录不同)
-- 在你线上 MySQL 执行。如果跑 pre 环境,把表名改成 src_dop_api_configs_pre
-- ============================================================

-- 【核心诊断】用特殊符号包裹 url_path,暴露隐藏的首尾空格/换行/斜杠
-- 网关匹配要求 5 项全中:project一致 / url_path精确相等 / method相等 /
--                        is_enabled=1 / status≠offline
SELECT
    ac.id,
    ac.name,
    CONCAT('[', ac.url_path, ']')          AS url_path_带方括号,  -- 方括号内若有空格/斜杠就是元凶
    LENGTH(ac.url_path)                     AS url_path_字节长度,
    ac.method,
    ac.is_enabled,                          -- 必须=1;若为0或NULL则匹配不上
    ac.status,                              -- 不能是offline
    ac.api_type,
    p.code                                  AS project_code,        -- 必须=quickBiData
    ac.datasource_id
FROM src_dop_api_configs ac
JOIN src_dop_projects p ON p.id = ac.project_id
WHERE ac.url_path LIKE '%shouxiHuTest%'
   OR ac.name LIKE '%shouxiHuTest%';

-- 对照:你请求的是  GET /quickBiData/tencentLbs/hotMap/shouxiHuTest
-- 期望这条记录:
--   url_path_带方括号 = [/tencentLbs/hotMap/shouxiHuTest]   <- 方括号紧贴,无空格无多余斜杠
--   method = GET
--   is_enabled = 1
--   status ≠ offline
--   project_code = quickBiData
-- 任何一项对不上,就是 5003 的原因。重点看 url_path 方括号里有没有藏空格/斜杠。

-- 【顺带排查重复】同一 project 下同 url_path+method 是否有多条(会导致查询报错->当成未找到)
SELECT ac.url_path, ac.method, COUNT(*) AS 条数
FROM src_dop_api_configs ac
JOIN src_dop_projects p ON p.id = ac.project_id
WHERE p.code = 'quickBiData'
GROUP BY ac.url_path, ac.method
HAVING COUNT(*) > 1;
-- 若有结果,说明有重复记录,网关 scalar_one_or_none() 会异常 -> 报未找到