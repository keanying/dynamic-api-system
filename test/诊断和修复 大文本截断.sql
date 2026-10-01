-- ============================================================
-- 静态页面 HTML 被截断 —— 诊断 + 修复 SQL
-- 原因:src_dop_api_configs 的 html_content/css_content/js_content
--       如果是 MySQL 的 TEXT 类型,上限 64KB,超过会被静默截断。
-- 用法:在你线上的 MySQL 里执行。先跑【第1步】看类型,是 text 就跑【第2步】升级。
-- ============================================================

-- 【第1步】查这三列当前的数据类型
SELECT column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'src_dop_api_configs'
  AND column_name IN ('html_content','css_content','js_content');
-- 如果 data_type 显示 'text'  -> 上限 64KB,需要升级(执行第2步)
-- 如果显示 'longtext'         -> 已经是 4GB,不用改(截断在别处,告诉我)

-- 【第2步】把三列升级为 LONGTEXT(4GB),历史数据保留不变
--   注意:已经被 TEXT 截断过的旧记录,丢掉的部分找不回来,需重新编辑保存那些大页面。
ALTER TABLE `src_dop_api_configs` MODIFY COLUMN `html_content` LONGTEXT;
ALTER TABLE `src_dop_api_configs` MODIFY COLUMN `css_content`  LONGTEXT;
ALTER TABLE `src_dop_api_configs` MODIFY COLUMN `js_content`   LONGTEXT;

-- 如果你在跑 pre 环境,pre 表也要一起升级:
-- ALTER TABLE `src_dop_api_configs_pre` MODIFY COLUMN `html_content` LONGTEXT;
-- ALTER TABLE `src_dop_api_configs_pre` MODIFY COLUMN `css_content`  LONGTEXT;
-- ALTER TABLE `src_dop_api_configs_pre` MODIFY COLUMN `js_content`   LONGTEXT;

-- 【第3步】升级后再查一次确认变成 longtext
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'src_dop_api_configs'
  AND column_name IN ('html_content','css_content','js_content');