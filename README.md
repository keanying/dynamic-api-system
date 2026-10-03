# OneData Portal - 数据开放平台

OneData Portal 是一个轻量级的数据 API 开放平台，支持通过可视化配置将数据库查询快速发布为 RESTful API，无需编写后端代码。

## 功能特性

- **项目管理**：多项目隔离，每个项目独立管理 API 和密钥
- **API 配置**：可视化配置 SQL 模板、参数、缓存、限流等；API 类型分 **SQL / HTML / 插件 / 远端同步** 四种
- **选表生成 SQL（v2.23）**：点选数据源、表、字段、关联、筛选条件即可生成 SQL 和参数，不用手写，见下文
- **数据源管理**：支持 MySQL / **StarRocks** / **SelectDB** / **Apache Doris** / PostgreSQL / Redis 多种数据源，密码加密存储
- **动态调用引擎**：SQL 参数化执行、DDL 拦截、行数限制、超时控制
- **多源 SQL（v2.22）**：一条 MySQL 语法的 SQL 关联多个数据源（MySQL / SelectDB / Doris / StarRocks）的表，见下文
- **SQL 动态模板**：`$if(条件)$ ... $endif$` 条件块，根据入参动态拼装 SQL，配合 `:param IN` 自动展开数组
- **API 网关**：统一入口 `/v1/data/{项目编码}/{路径}`（前缀由 `gateway.prefix` 配置），支持 API Key 认证和 IP 白名单
- **在线测试**：内置 API 测试工具，实时查看请求和响应
- **监控统计**：仪表盘展示调用量、耗时、失败率等指标
- **调用日志**：完整记录每次调用，支持慢查询分析和日志清理
- **缓存管理**：内存缓存 + 可选 Redis 缓存，支持 TTL 抖动防雪崩

## SQL 动态模板语法

在 SQL 模板里用控制块包裹动态片段，平台执行前会根据传入参数渲染出最终 SQL。

### 控制块

| 语法 | 用途 |
|------|------|
| `$if(条件)$ ... $endif$` | 条件包含 |
| `$if(...)$ ... $else$ ... $endif$` | 二选一 |
| `$if(...)$ ... $elseif(...)$ ... $else$ ... $endif$` | 多分支 |
| `$for(item in items)$ ... $endfor$` | 循环 |
| `$for(item in items)$ ... $sep$, $endfor$` | 循环 + 分隔符 |

### 两种插值

| 写法 | 用途 | 安全策略 |
|------|------|---------|
| `:name` | **参数化绑定**（用于数据值） | 走数据库 prepared statement，防 SQL 注入 |
| `:name|like` | LIKE 模糊匹配，值前后自动加 `%`，并转义内部 `% _ \` | 同上，安全 |
| `:name|like_left` | 后缀匹配，值前加 `%` | 同上 |
| `:name|like_right` | 前缀匹配，值后加 `%` | 同上 |
| `#{name}` | **文本插值**（用于字段名/排序方向），支持点路径 `#{ch.field}`，list 自动 ", " 拼接 | 仅允许 `[A-Za-z0-9_,.\s*]`，含其他字符直接拒绝 |
| `:{表达式}` | **安全绑定插值**（v2.0.2 新增）：在循环/条件内把任意表达式的值（含嵌套字段 `ch.channelName`）注册为参数化绑定；list 值自动 IN 展开 | 值走 prepared statement，任意字符（含中文/引号）均安全 |

### 表达式

- **比较**：`==` `!=` `>` `>=` `<` `<=`
- **布尔**：`and` `or` `not`（也支持 `&&` `||` `!`）
- **包含**：`x in [...]`、`x not in [...]`、`x in some_list`
- **函数**：`len(x)` 长度、`defined(x)` 是否传值（None 视为未传）、`empty(x)` 是否为空
- **点路径**（v2.0.2 新增）：表达式中可用 `a.b.0.c` 访问嵌套字段 / 列表索引，如 `len(ch.prdId) > 0`、`defined(ch.channelName)`；路径缺失返回 null 不报错
- **字面量**：数字、`'字符串'`、`true`、`false`、`null`、`[1, 2, 'three']`

### 典型用法

```sql
-- LIKE 模糊查询（自动加 % 并转义 % _ \）
$if(!empty(keyword))$
    AND (name LIKE :keyword|like OR email LIKE :keyword|like)
$endif$
-- 用户传 "50%off" 会被安全转义为 "%50\%off%"，不会被当成通配符

-- NULL 安全比较：用 if/else 显式区分
$if(defined(parent_id))$
    AND parent_id = :parent_id
$else$
    AND parent_id IS NULL
$endif$
-- SQL 标准里 col = NULL 永远为 false，所以必须用 IS NULL 处理可空字段

-- 时间段多分支
SELECT * FROM events WHERE 1=1
$if(period == 'day')$
    AND ts >= NOW() - INTERVAL 1 DAY
$elseif(period == 'week')$
    AND ts >= NOW() - INTERVAL 7 DAY
$else$
    AND ts >= '2000-01-01'
$endif$

-- 动态字段列表（注意是 #{} 不是 :）
SELECT $for(c in cols)$#{c}$sep$, $endfor$ FROM users WHERE id = :id

-- IN 自动展开
$if(len(tags) > 0)$
    AND tag IN :tags         -- :tags=['a','b'] 自动展开为 IN (:tags__0, :tags__1)
$endif$

-- 角色权限分组
$if(role in ['admin', 'super'])$
    AND scope = 'all'
$else$
    AND scope = 'self'
$endif$
```

### 预览渲染

在 API 编辑器右上角点 **预览渲染** 可以用当前测试参数预先看到最终 SQL，每个控制块的求值结果（keep/drop/loop×N）都会列出来，便于调试。
错误提示带行号、列号和上下文片段，例如 `[行 5:3] $if$ 缺少配对的 $endif$  ↳ $if(x > 0)$`。

## 多步骤数据管线（历史保留）

> **新需求请使用「多源 SQL（Catalog 模式）」**：跨库关联、多个查询结果相加、分组汇总，用一条 SQL 即可完成，
> 也可以用「选表生成 SQL」点选生成，学习成本低得多。多步骤管线仅为兼容线上已有接口保留，已有管线接口照常编辑和运行。
> 新建接口时点 SQL 卡片上的「多步骤管线」，会先提示改用多个数据源，确需管线时可选「仍使用多步骤管线」。

当一个 API 需要查询**多个数据源**，或者基于前一步结果继续查询、再做内存聚合时，可以用多步骤管线。
在 API 编辑器的「多步骤管线」卡片填入 JSON 数组，配置后会覆盖单 SQL 模板，按步骤串行执行。

### 步骤类型

| type | 说明 |
|------|------|
| `sql` | 查询数据源。`datasource` 用数据源 name 或 id 字符串；`sql` 支持全部模板语法（`$if$` / `$for$` / IN 展开 / LIKE 后缀） |
| `transform` | 内存数据处理。`op` 取值：`join`（双表合并）、`aggregate`（分组聚合 sum/count/avg/min/max）、`map`（行投影）、`filter`（行过滤）、`pick`（取首行） |

### 步骤间引用语法

在 `inputs` 或 transform 的值里用 `${...}` 引用前序步骤结果：

| 写法 | 含义 |
|------|------|
| `${step.field}` | 取单行的字段 |
| `${step.0.field}` | 取第 N 行的字段 |
| `${step.*.id}` | 所有行的 id 列（数组），常用于把 ID 列表传给下一步的 `IN :ids` |
| `${step.*}` | 整个结果列表 |
| `${x or 0}` | 默认值兜底 |
| `${params.status}` | 引用 API 的原始入参 |

给某一步加 `"is_return": true` 指定最终返回；不指定则返回最后一步结果。

### 示例：跨库用户订单聚合

```json
[
  {
    "name": "users",
    "type": "sql",
    "datasource": "biz_db",
    "sql": "SELECT id, name, dept_id FROM users WHERE status = :status"
  },
  {
    "name": "orders",
    "type": "sql",
    "datasource": "analytics_db",
    "sql": "SELECT user_id, SUM(amount) AS total FROM orders WHERE user_id IN :user_ids GROUP BY user_id",
    "inputs": { "user_ids": "${users.*.id}" }
  },
  {
    "name": "result",
    "type": "transform",
    "op": "join",
    "left": "${users}",
    "right": "${orders}",
    "on": { "left": "id", "right": "user_id" },
    "select": ["id", "name", "dept_id", { "total": "${right.total or 0}" }],
    "is_return": true
  }
]
```

执行流程：库 A 查出 active 用户 → 用户 ID 列表传给库 B 查订单聚合 → 内存按 user_id 关联，
没有订单的用户 total 兜底为 0。每步的耗时和行数都会记录在日志里。

### 示例：分组聚合（类似 SQL GROUP BY）

```json
[
  { "name": "orders", "type": "sql", "datasource": "biz_db",
    "sql": "SELECT user_id, dept_id, amount FROM orders WHERE created_at >= :since" },
  { "name": "summary", "type": "transform", "op": "aggregate",
    "from": "${orders}",
    "group_by": ["dept_id"],
    "aggregations": {
      "order_count":  { "fn": "count" },
      "total_amount": { "fn": "sum", "field": "amount" },
      "avg_amount":   { "fn": "avg", "field": "amount" },
      "max_amount":   { "fn": "max", "field": "amount" }
    },
    "is_return": true }
]
```

按 `dept_id` 分组，每组输出 order_count / total_amount / avg_amount / max_amount。
不写 `group_by` 则对全部数据聚合成一行。聚合函数支持 `count` / `sum` / `avg` / `min` / `max`。

> 编辑器里点 SQL 模板卡片右上角 **使用教程** 可以打开分标签页的完整教程弹窗。
> 多步骤管线默认关闭，需要时打开「多步骤管线」卡片的开关即可，关闭时走普通单 SQL。

> **向后兼容**：`pipeline_steps` 为空时走原单 SQL 模式，老 API 完全不受影响。
> **生产部署**：v1.6 给 `src_dop_api_configs` 新增了 `pipeline_steps` 列，生产库需执行
> `ALTER TABLE src_dop_api_configs ADD COLUMN pipeline_steps TEXT DEFAULT '';`（开发用 SQLite 自动建表无需处理）。

## 多源 SQL（catalog 模式，v2.22）

API 类型选「SQL」，在「数据源」卡片切到 **多个数据源**，用**一条标准 MySQL 语法的 SQL** 关联多个数据源的表，表名写成 `数据源名.库名.表名`
（数据源名即「数据源管理」中登记的名称；也可写 `数据源名.表名`，库取数据源配置的默认库）：

```sql
SELECT m.category, u.city_level, COUNT(*) AS orders, SUM(o.amount) AS gmv
  FROM 订单库.biz.orders o                                   -- MySQL 实例一
  JOIN selectdb.crm.members u ON u.user_name = o.user_name   -- SelectDB
  JOIN 商户库.mall.merchants m ON m.id = o.merchant_id       -- MySQL 实例二
 WHERE o.status = 'paid' AND o.created_at >= :start
 $if(vip != null)$ AND u.vip = :vip $endif$
 GROUP BY m.category, u.city_level ORDER BY gmv DESC
```

参数、`$if$` / `$for$` / `#{}` 等模板语法与单数据源 SQL 完全相同；缓存、预热、限流、审批、预发→生产发布照常使用。
（后端类型仍为 `federated`，单个数据源为 `sql`，已有接口不受影响。）

**执行方式**

| 情况 | 怎么执行 |
|------|---------|
| 只涉及一个数据源 | 只去掉 SQL 里的数据源名前缀，整句交给该数据源执行，结果与单 SQL 模式完全相同 |
| 跨数据源 | ① 整段都来自同一数据源、且不引用外层的子查询 / CTE，整段下推（聚合在源库完成）；② 其余的表各自只取用到的列，只涉及这张表的 WHERE / ON 条件下推；③ 先取「驱动表」，把它的关联键作为 `IN (...)` 下推到另一侧，只取能关联上的行（各表行数用 EXPLAIN 估算）；④ 取回的数据在服务进程内用嵌入式计算库 DuckDB 完成关联、聚合、排序 |

下推到各数据源的 SQL 照常经过只读防护、系统库保护、数据源项目范围检查、连接池与超时控制；请求参数始终以绑定参数传给数据源。
关联计算的 DuckDB 禁止访问文件系统和安装插件，配置锁定。

**编辑器**：「多个数据源」下可浏览本项目可用的数据源 → 库 → 表 → 列，点击插入到 SQL；「执行计划」按测试参数展示
各数据源实际执行的 SQL、取数顺序与关联计算 SQL（不取数据）。调用日志的「执行 SQL」记录每次的执行计划、各源行数与耗时。

**与 MySQL 的语义一致性**：跨数据源计算时，SQL 会按 MySQL 规则改写后再计算，已覆盖：字符串比较 / LIKE / REGEXP 不区分大小写
（`catalog.string_compare`）、升序 NULL 在前、整数与 DECIMAL 除法 / AVG 的结果小数位、字符串参与算术与比较时按 MySQL 转数字、
DATE 加减天/月/年仍为 DATE、`LENGTH` 为字节数、`DAYOFWEEK / WEEKDAY / WEEK / YEARWEEK`、`TIMESTAMPDIFF`（按满单位）、
`DATE_FORMAT` 全部格式符、`FIND_IN_SET / FIELD / ELT / SUBSTRING_INDEX / STRCMP / MID / MAKEDATE / TO_DAYS / UNIX_TIMESTAMP /
FROM_UNIXTIME / JSON_UNQUOTE / JSON_LENGTH / TIMEDIFF / ADDTIME`、`LOG / SQRT` 负数返回 NULL、`FORMAT` 四舍五入、
非严格 GROUP BY（未分组列取任意一行）、结果列名（未写别名时为表达式原文）。
200 个常用函数 / 表达式与 MySQL 原生结果逐个比对，187 个完全一致；其余为：`CONV / OCT / CRC32 / QUOTE / TIME_FORMAT /
JSON_CONTAINS` 等跨源时暂不支持（明确报错，单数据源时不受限制）、无符号整数溢出回绕、非法日期截断等极少见写法。

**使用建议与限制**

- 跨数据源时尽量带上过滤条件；单个源表需要取回的行数超过 `catalog.max_rows_per_source`（默认 20 万）时报错提示。
- `NOW()` / `CURDATE()` 等按北京时间计算（与平台其它时间一致）。
- 跨数据源查询之间没有统一的一致性快照（各源在各自的时间点读取），与「流水线」模式相同。
- 跨源列引用请写成「表别名.列名」；只在一张表中存在的列可以省略表别名。
- 依赖 `sqlglot`、`duckdb`、`pyarrow`（已加入 `pyproject.toml`，`uv sync` 安装；内网部署请提前准备离线 wheel）。
  没有多源 SQL API 时这些依赖不会被加载。

**参考性能**（单 worker、本机 MySQL）：三个数据源的点查关联（每源 1 行）约 5ms，单进程约 600 QPS；
订单 8000 行 × 会员 5 万行 × 商户的分组聚合约 120ms（同一 MySQL 实例原生关联约 80ms）。开启缓存后与普通 API 相同。

**配置**（`config.yaml` 的 `catalog` 段）

| 配置 | 默认 | 说明 |
|------|------|------|
| `max_rows_per_source` | 200000 | 单个源表最多取回的行数 |
| `dynamic_filter_max_keys` | 10000 | 关联键不超过这么多个时作为 IN 条件下推到另一侧 |
| `max_concurrency` | 8 | 每个 worker 同时进行的跨源计算数 |
| `memory_limit` / `threads` | 1GB / 4 | 关联计算的内存上限与线程数 |
| `string_compare` | nocase | `nocase` 不区分大小写；`nocase_noaccent` 再不区分重音（与 utf8mb4_general_ci 完全一致，字符串关联约慢一倍）；`binary` 区分大小写 |
| `schema_cache_ttl` | 300 | 源表结构缓存秒数 |

执行计划按「项目 + 渲染后的 SQL」缓存（时长同 `gateway.config_cache_ttl`，修改数据源 / 项目配置后立即清空）。
发布到生产时会检查 SQL 中引用的数据源在生产是否存在。

## 选表生成 SQL（v2.23）

不会写 SQL 也能开发接口：API 编辑器「执行逻辑」→ SQL 模板右上角 **选表生成 SQL**。

1. **选表和字段**：依次选数据源 → 库 → 表，点字段选中；「+ 关联另一张表」可关联其它表（可以是其它数据源），关联字段自动猜（同名字段、`xxx_id = id`）
2. **筛选条件**：等于 / 不等于 / 大于 / 小于 / 包含 / 开头是 / 在列表中 / 介于；值选「调用时传入」或「固定值」，非必填条件调用时不传就不筛选（自动生成 `$if$`）
3. **汇总与排序**：勾「分组汇总」后每个字段选 分组 / 计数 / 去重计数 / 求和 / 平均 / 最大 / 最小；排序；最多返回行数
4. **生成 SQL**：SQL 填入编辑器，参数定义自动补齐；用到多个数据源时自动切到「多个数据源」；名称和路径为空时按表名自动填写

另外：保存时 SQL 里用到但未定义的 `:参数` 会自动加入参数定义（字符串、非必填）。
向导支持 MySQL 协议的数据源（MySQL / SelectDB / Doris / StarRocks），PostgreSQL 等请手写 SQL。

## 多环境：预发 → 生产发布

同一个数据库里跑两套环境，**用端口区分**：

| 环境 | 默认端口 | 表名 | 写操作权限 |
|------|---------|------|-----------|
| 生产 prod | 3000 | 无后缀，如 `src_dop_api_configs` | API 的编辑 / 上线 / 下线 / 锁定等：**仅项目管理员和超级管理员**；其它写操作仅管理员 |
| 预发 pre | 3002 | `_pre` 后缀，如 `src_dop_api_configs_pre`（用户表两环境共用） | 按原有项目角色 |

端口在 `config.yaml` 的 `environments` 段配置。环境判定优先级：`--env` > `APP_ENV` > 启动端口匹配 > 默认 prod，
所以 `uvicorn app.main:app --port 3002` 会自动识别为预发；`python main.py --env pre` 会自动监听预发端口。
两套环境的 Redis 缓存键自动隔离（预发前缀追加 `pre:`）。

**发布流程**

1. 预发环境：修改 API → 走原有审批并上线 → 在预发验证
2. 预发环境：在 API 卡片点「发布到生产」，先看到 **生产当前 vs 本次发布** 的逐项差异，再填写发布说明提交发布单（冻结当前配置快照）
3. 生产环境：管理员在 **审批中心 →「发布到生产」页签** 打开发布单，再次核对差异（SQL 等文本为行级 diff）
4. 通过：写入生产，状态为 **上线 + 锁定**（生产原先没有该 API 则新建）；驳回：生产不做任何改动
   - 生产没有该项目时 **自动创建**：编码、名称、描述、项目 Key、成员及角色、环境变量按预发的项目带过去
   - API 自己的 Key 随发布一起带到生产；生产项目没设项目 Key 时补上预发的项目 Key（已有的不覆盖）

**生产环境权限（v2.24）**

- 生产里只有 **项目管理员和超级管理员** 能编辑、上线、下线、锁定 / 解锁、删除、复制、转交 API，其他人（含研发）只读，
  编辑页隐藏保存按钮
- 生产改动 API 的路径：下线 → 解锁 → 修改 → 「上线」。生产的「上线」是直接上线（不再走提交审批），上线后自动锁定

**拉取生产到预发（v2.24，类似 git）**

生产被直接改过（例如线上热修）后，可以在预发把生产的内容拉取过来。系统记录每个 API 最近一次两边一致时的配置作为「基线」
（发布到生产审核通过、拉取生产时更新，存于共用表 `src_dop_env_sync_bases`），据此判断差异来自哪边：

| 状态 | 含义 | 能做什么 |
|------|------|---------|
| 一致 | 两边相同 | — |
| 预发有新改动 | 生产没动过，预发改过 | 正常「发布到生产」 |
| 生产有变更 | 预发没动过，生产改过 | 「拉取生产」；此时不能发布（会覆盖生产的改动） |
| 两边都有改动 | 两边都改过 | 核对差异后：发布需勾选确认覆盖生产，或拉取并确认覆盖预发 |
| 仅生产有 | 生产里直接新建的 | 拉取到预发（新建） |
| 有差异 | 没有基线（本功能上线前的数据），分不清哪边改的 | 核对差异后决定 |

- 预发项目页「同步生产」按钮：按状态列出项目里所有 API，可勾选批量拉取；按钮上的数字是需要处理的个数
- 预发 API 卡片上显示「生产 vN」和同步状态；「对比生产」弹窗里可直接「拉取生产到预发」
- 拉取会覆盖预发中该 API 的配置（版本号 +1），锁定或审批中的 API 不能拉取
- 勾选多个 API 拉取时按每批 5 个分批执行，底栏显示进度

**从生产同步全部（v2.24，仅超级管理员，仅预发）**

预发「项目」页的「从生产同步全部」按钮（生产环境不显示，接口在生产也会拒绝）：把生产的内容一次性同步到预发，
**只有 生产 → 预发 一个方向**，不会改动生产。**分批进行、页面显示进度**：先逐个项目对比（每个项目一个请求），
确认后先同步数据源，再逐个项目同步（每个项目单独提交，可以中途「停止」，已完成的项目不受影响）：

- 数据源：生产有、预发没有（按名称）的复制到预发；预发已有的同名数据源不覆盖（预发可以连不同的库）
- 项目：预发没有的新建；已有的更新名称、描述、项目 Key；补上预发没有的成员；环境变量按名称覆盖
- API：生产独有的新建，生产有变更的覆盖（锁定的也覆盖，锁定状态不变），同步后记为新的同步基线
- 不删除预发独有的 API；预发正在上线审批的 API 跳过；预发有未发布改动的默认跳过，勾选「也用生产覆盖」才覆盖

**差异对比（v2.23，v2.24 起左右并排）**

- 统一 **左边生产、右边预发（待发布）**，SQL / HTML / 插件代码等多行内容逐行并排，行内只高亮改动的字符，大段相同内容自动折叠

- 两个环境同库，预发直接读取生产表，**预发里也能实时对比生产**：
  - 预发 API 卡片上，已在生产的显示「生产 vN」标签和「对比生产」按钮；编辑器顶部也有「对比生产」
  - 生产环境对应为「对比预发」
  - 预发查看待审核的发布单，也展示与生产当前配置的实时差异
- 旧地址 `/admin/releases` 自动跳到审批中心的「发布到生产」页签

**删除已发布到生产的 API（v2.23）**

- 预发里已发布到生产的 API **不能单独删除**：点删除会提示并提交「删除审核」（填写删除原因）
- 生产管理员在审批中心「发布到生产」页签同意后，**同时删除生产和预发** 中的该 API；驳回则两边都不动
- 只在预发存在（从未发布到生产）的 API 照常直接删除

**用户与单点登录（v2.23）**

- 预发和生产 **共用一张用户表** `src_dop_users`：账号、密码、全局角色两边一致；项目成员仍按环境分别管理
- **单点登录**：在一个环境登录后，点顶栏「前往预发 / 前往生产」直接以登录状态打开；直接打开另一个环境的登录页，
  若对方已登录也会自动登录。退出登录时两个环境一起退出
- 实现方式：一次性票据（60 秒有效、只能用一次，存于共用表 `src_dop_sso_tickets`，只保存哈希），两个环境的 JWT 密钥不必相同
- 两个环境部署在不同域名时，请在 `config.yaml` 的 `environments.<env>.url` 填写各自的访问地址（未填写时按「当前主机 + 对方端口」）

**升级到 v2.23 须知**

- 预发首次以新版本启动时，自动把原预发用户（`src_dop_users_pre`）按账号并入共用用户表：同名账号以生产为准，
  生产没有的账号原样加入；预发各表里的用户 id 随之改写。原表改名为 `src_dop_users_pre_bak_时间` 作为备份，不会重复执行
- 用户 id 有变化的账号，原来在预发的登录状态会失效，重新登录即可

说明：
- 发布单（含删除单）存放在两套环境共享的 `src_dop_release_requests` 表（不加 `_pre` 后缀），首次启动自动建表
- 跨环境按自然键对应：项目按编码、API 按「项目编码 + 方法 + 路径」、数据源按名称；生产缺少对应数据源时会拒绝发布并提示
- 新建 API 时 API Key 默认填入项目 Key；不想用项目 Key 时点「生成」换成独立 Key
- 审核时会校验生产版本号，查看差异后生产被改动过则拒绝执行，需刷新重新核对
- 生产环境非管理员的 `/api/**` 写请求统一被拦截（登录、在线测试、SQL 预览、连接测试除外；项目管理员操作自己项目的 API 除外），对外网关不受影响

## 性能与部署建议（v2.19）

**升级须知**

- 首次启动会在后台为 `call_logs` 补建 3 个统计索引（MySQL 在线 DDL，不阻塞读写；千万行级约需几分钟）。
  也可以提前在低峰期手动执行 `migrations/v2.19_call_logs_indexes.sql`，启动时会自动跳过已存在的索引。
- 调用日志改为后台批量写入，页面上最多晚 1 秒左右出现；进程被 `kill -9` 时最后约 1 秒的日志会丢失（正常停止会先写完）。
- 文件日志级别默认跟随 `log.level`（之前固定为 DEBUG），排查问题时可设 `log.file_level: "DEBUG"`。

**建议配置**

| 配置 | 建议 | 说明 |
|------|------|------|
| `server.workers` | CPU 核数 | 单进程只能用一个核，多进程吞吐近似线性增长（需 `redis.enabled: true`） |
| `monitor.call_log_retention_days` | 如 30 | 自动清理旧调用日志，避免表无限增长 |
| `gateway.config_cache_ttl` | 5（默认） | 网关接口配置缓存秒数，0 关闭 |

### v2.20 高并发与大结果

- **缓存命中不再解析/重新序列化**：缓存里存的是序列化好的 JSON（较大的结果预压缩），命中时直接拼进响应。
  2MB 的结果原来每次命中要 ~90ms CPU，现在 <1ms。升级前写入的旧格式缓存可以正常读取。
- **gzip**：客户端带 `Accept-Encoding: gzip` 且响应超过 `gateway.gzip_min_bytes` 时压缩返回，大结果传输量降到约 1/10。
- **进程内热点缓存**（`cache.local_ttl`）与**并发未命中合并**：同一 key 缓存失效瞬间只查一次库。
- **业务数据源驱动默认 asyncmy**（`query.mysql_driver`），返回值类型、参数转义、报错信息与 aiomysql 逐项比对一致。
- 极限吞吐可设 `log.gateway_info: false`（调用明细仍完整写入 call_logs）。
- 同一数据源上的大查询会占满该数据源的连接池、让小查询排队；报表类重查询建议单独建一个数据源（独立连接池）。

## 技术栈

| 组件 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 模板引擎 | Jinja2 |
| ORM | SQLAlchemy 2.0 (async) |
| 数据库 | SQLite (开发) / MySQL / PostgreSQL |
| 包管理 | uv |
| 配置管理 | YAML + dataclass |
| 前端 | HTML + CSS + JavaScript |

## 项目结构

```
onedata-portal-backend/
├── config.yaml              # 统一配置文件
├── main.py                  # 启动入口
├── pyproject.toml           # 项目依赖
├── app/
│   ├── main.py              # FastAPI 应用
│   ├── core/
│   │   ├── config.py        # 配置统一出口
│   │   ├── database.py      # 数据库引擎/会话
│   │   └── security.py      # JWT/密码/加密
│   ├── models/
│   │   └── models.py        # SQLAlchemy 数据模型
│   ├── schemas/
│   │   └── schemas.py       # Pydantic 请求/响应模型
│   ├── services/
│   │   └── engine.py        # 动态 SQL 执行引擎
│   ├── api/
│   │   ├── auth.py          # 认证路由
│   │   ├── projects.py      # 项目管理
│   │   ├── api_configs.py   # API 配置管理
│   │   ├── datasources.py   # 数据源管理
│   │   ├── gateway.py       # 动态调用网关
│   │   ├── test_api.py      # API 测试
│   │   ├── monitor.py       # 监控统计
│   │   └── views.py         # 页面视图路由
│   ├── templates/
│   │   ├── layouts/         # 布局模板
│   │   └── pages/           # 页面模板
│   └── static/
│       ├── css/style.css    # 样式
│       └── js/app.js        # 前端脚本
```

## 快速开始

### 1. 安装依赖

需要 **Python 3.11+** 和 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装 uv —— Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# 安装 uv —— Windows（PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# 在项目根目录安装依赖（三个系统相同）
uv sync
```

### 2. 配置

编辑 `config.yaml`，主要改数据库连接和两个环境的端口：

```yaml
server:
  host: "0.0.0.0"
  port: 3000          # 未指定环境时的默认端口
  debug: false
  workers: 1

environments:         # 预发 / 生产各自的端口（启动时按环境自动选端口）
  prod:
    port: 3000
  pre:
    port: 3002

database:
  url: "mysql+aiomysql://用户名:密码@主机:3306/库名"
  # 本地试用也可以用 SQLite：sqlite+aiosqlite:///./onedata.db
```

两个环境用同一个库，预发环境的表名统一加 `_pre` 后缀；预发首次启动会把生产数据复制一份到 `_pre` 表。

### 3. 本地启动

最简单、三个系统通用的写法是 `uv run main.py`，用 `--env` 指定环境，端口自动取 `environments` 里的配置：

| 环境 | 命令（Linux / macOS / Windows 通用） | 端口 |
|------|-----------------------------------|------|
| 生产 prod | `uv run main.py --env prod`（不写 `--env` 默认就是 prod） | 3000 |
| 预发 pre | `uv run main.py --env pre` | 3002 |

也可以直接用 uvicorn 启动，用环境变量 `APP_ENV` 指定环境（与线上脚本的写法一致）：

**Linux / macOS**

```bash
# 生产
APP_ENV=prod uv run uvicorn app.main:app --host 0.0.0.0 --port 3000
# 预发
APP_ENV=pre  uv run uvicorn app.main:app --host 0.0.0.0 --port 3002
# 本地开发热重载（改代码自动重启）
APP_ENV=pre  uv run uvicorn app.main:app --host 0.0.0.0 --port 3002 --reload
```

**Windows PowerShell**

```powershell
# 生产
$env:APP_ENV="prod"; uv run uvicorn app.main:app --host 0.0.0.0 --port 3000
# 预发
$env:APP_ENV="pre";  uv run uvicorn app.main:app --host 0.0.0.0 --port 3002
```

**Windows CMD**

```bat
:: 生产
set APP_ENV=prod
uv run uvicorn app.main:app --host 0.0.0.0 --port 3000

:: 预发（新开一个窗口）
set APP_ENV=pre
uv run uvicorn app.main:app --host 0.0.0.0 --port 3002
```

> PowerShell / CMD 里设置的 `APP_ENV` 只对当前窗口有效，同时跑两个环境请各开一个窗口。
> 不设 `APP_ENV` 时按端口判断环境（3002 → pre，3000 → prod），见下文「多环境」。
> 启动日志里的 `运行环境: pre（表名后缀 _pre）` / `运行环境: prod（正式环境，无表名后缀）` 可以确认当前环境。

### 4. 访问

| 环境 | 管理后台 | API 文档 |
|------|---------|---------|
| 生产 | http://localhost:3000/login | http://localhost:3000/docs |
| 预发 | http://localhost:3002/login | http://localhost:3002/docs |

默认账号：`admin / admin123`（`config.yaml` 的 `security` 段可改，首次登录后请修改密码）。

### 5. 线上部署（public_opinion_across.sh）

线上服务器（Linux）用项目根目录的 `public_opinion_across.sh` 后台启动和管理：

```bash
chmod +x public_opinion_across.sh      # 首次需要

./public_opinion_across.sh start       # 后台启动
./public_opinion_across.sh stop        # 停止
./public_opinion_across.sh restart     # 重启（发版后执行）
./public_opinion_across.sh status      # 查看是否在运行 + 最近 10 行日志
./public_opinion_across.sh logs        # 实时查看日志（Ctrl+C 退出）
```

- 进程号写在 `logs/public_opinion_across.pid`，日志在 `logs/public_opinion_across.log`
- 启动命令由脚本里的 `UV_COMMAND` 决定，当前为 **预发环境 3002 端口**：

  ```bash
  UV_COMMAND="env APP_ENV=pre uv run uvicorn app.main:app --host 0.0.0.0 --port 3002  --workers 1"
  ```

  部署生产时改为：

  ```bash
  UV_COMMAND="env APP_ENV=prod uv run uvicorn app.main:app --host 0.0.0.0 --port 3000 --workers 1"
  ```

  预发和生产同机部署时，请放在两个目录分别部署（各自一份脚本，PID 和日志互不影响）。
- `--workers` 可按 CPU 核数调大，需在 `config.yaml` 开启 Redis（缓存在多进程间共享）。
- 发版流程：拉取代码 → `uv sync`（依赖有变化时）→ `./public_opinion_across.sh restart` → `status` 确认已启动。

## 配置说明

所有配置统一在 `config.yaml` 中管理，通过 `app/core/config.py` 统一出口。支持环境变量覆盖：

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ONEDATA_DATABASE_URL` | 数据库连接 URL | sqlite+aiosqlite:///./onedata.db |
| `SERVER_HOST` | 监听地址 | 0.0.0.0 |
| `APP_ENV` | 运行环境 `prod` / `pre`（也可用启动参数 `--env`） | 按端口判断，默认 prod |
| `SERVER_PORT` | 监听端口 | `environments.<环境>.port`，再取 `server.port` |
| `ADMIN_USERNAME` | 管理员用户名 | admin |
| `ADMIN_PASSWORD` | 管理员密码 | admin123 |
| `JWT_SECRET` | JWT 密钥 | (config.yaml 中配置) |
| `LOG_LEVEL` | 日志级别 | INFO |

## API 接口

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录获取 Token |
| GET | `/api/auth/me` | 获取当前用户 |
| POST | `/api/auth/logout` | 登出 |

### 项目管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/projects` | 项目列表 |
| POST | `/api/projects` | 创建项目 |
| GET | `/api/projects/{id}` | 项目详情 |
| PUT | `/api/projects/{id}` | 更新项目 |
| DELETE | `/api/projects/{id}` | 删除项目 |
| GET | `/api/projects/{id}/export` | 导出项目 |

### API 配置

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/projects/{pid}/apis` | API 列表 |
| POST | `/api/projects/{pid}/apis` | 创建 API |
| GET | `/api/projects/{pid}/apis/{id}` | API 详情 |
| PUT | `/api/projects/{pid}/apis/{id}` | 更新 API |
| DELETE | `/api/projects/{pid}/apis/{id}` | 删除 API |
| POST | `/api/projects/{pid}/apis/{id}/copy` | 复制 API |
| POST | `/api/projects/{pid}/apis/{id}/toggle` | 启用/禁用 |

### 数据源

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/datasources` | 数据源列表 |
| POST | `/api/datasources` | 创建数据源 |
| GET | `/api/datasources/{id}` | 数据源详情 |
| PUT | `/api/datasources/{id}` | 更新数据源 |
| DELETE | `/api/datasources/{id}` | 删除数据源 |
| POST | `/api/datasources/{id}/test` | 测试连接 |

### 动态网关

| 方法 | 路径 | 说明 |
|------|------|------|
| ANY | `/v1/data/{项目编码}/{路径}` | 动态 API 调用（前缀见 `gateway.prefix`） |

### 监控

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/monitor/dashboard` | 仪表盘统计 |
| GET | `/api/monitor/latency-rank` | 耗时排行 |
| GET | `/api/monitor/call-rank` | 调用量排行 |
| GET | `/api/monitor/call-trend` | 调用趋势 |
| GET | `/api/monitor/slow-queries` | 慢查询列表 |
| GET | `/api/monitor/logs` | 调用日志 |
| DELETE | `/api/monitor/logs/cleanup` | 清理日志 |

### API 测试

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/test/{api_id}` | 测试 API 调用 |

### 多源 SQL 编辑辅助（v2.22）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sql/federated/catalogs?project_id=` | 本项目可用的数据源（MySQL 协议） |
| GET | `/api/sql/federated/databases?project_id=&catalog=` | 数据源下的库 |
| GET | `/api/sql/federated/tables?project_id=&catalog=&database=` | 库下的表 |
| GET | `/api/sql/federated/columns?project_id=&catalog=&database=&table=` | 表的列 |
| POST | `/api/sql/federated/explain` | 执行计划（各数据源执行的 SQL、关联计算 SQL，不取数据） |

## License

MIT
