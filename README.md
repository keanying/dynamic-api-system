# OneData Portal - 数据开放平台

OneData Portal 是一个轻量级的数据 API 开放平台，支持通过可视化配置将数据库查询快速发布为 RESTful API，无需编写后端代码。

## 功能特性

- **项目管理**：多项目隔离，每个项目独立管理 API 和密钥
- **API 配置**：可视化配置 SQL 模板、参数、缓存、限流等
- **数据源管理**：支持 MySQL / **StarRocks** / **SelectDB** / **Apache Doris** / PostgreSQL / Redis 多种数据源，密码加密存储
- **动态调用引擎**：SQL 参数化执行、DDL 拦截、行数限制、超时控制
- **SQL 动态模板**：`$if(条件)$ ... $endif$` 条件块，根据入参动态拼装 SQL，配合 `:param IN` 自动展开数组
- **API 网关**：统一入口 `/gw/{project_id}/...`，支持 API Key 认证和 IP 白名单
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

## 多步骤数据管线

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

## 多环境：预发 → 生产发布

同一个数据库里跑两套环境，**用端口区分**：

| 环境 | 默认端口 | 表名 | 写操作权限 |
|------|---------|------|-----------|
| 生产 prod | 3000 | 无后缀，如 `src_dop_api_configs` | **仅管理员**（超级管理员 / 管理员） |
| 预发 pre | 3002 | `_pre` 后缀，如 `src_dop_api_configs_pre` | 按原有项目角色 |

端口在 `config.yaml` 的 `environments` 段配置。环境判定优先级：`--env` > `APP_ENV` > 启动端口匹配 > 默认 prod，
所以 `uvicorn app.main:app --port 3002` 会自动识别为预发；`python main.py --env pre` 会自动监听预发端口。
两套环境的 Redis 缓存键自动隔离（预发前缀追加 `pre:`）。

**发布流程**

1. 预发环境：修改 API → 走原有审批并上线 → 在预发验证
2. 预发环境：在 API 卡片点「发布到生产」，填写发布说明，生成发布单（冻结当前配置快照）
3. 生产环境：管理员在「发布审核」页打开发布单，查看 **待发布内容 vs 生产当前配置** 的逐项差异（SQL 等文本为行级 diff）
4. 通过：写入生产并直接上线（生产原先没有该 API 则新建）；驳回：生产不做任何改动

说明：
- 发布单存放在两套环境共享的 `src_dop_release_requests` 表（不加 `_pre` 后缀），首次启动自动建表
- 跨环境按自然键对应：项目按编码、API 按「项目编码 + 方法 + 路径」、数据源按名称；生产缺少对应项目/数据源时会拒绝发布并提示
- API Key 按环境分别管理，不随发布同步
- 审核时会校验生产版本号，查看差异后生产被改动过则拒绝执行，需刷新重新核对
- 生产环境非管理员的 `/api/**` 写请求统一被拦截（登录、在线测试、SQL 预览、连接测试除外），对外网关不受影响

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

```bash
# 确保已安装 uv (https://docs.astral.sh/uv/)
uv sync
```

### 2. 配置

编辑 `config.yaml` 修改配置项：

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  debug: true

database:
  url: "sqlite+aiosqlite:///./onedata.db"  # 开发环境
  # url: "mysql+aiomysql://user:pass@host:3306/dbname"  # 生产环境

security:
  admin_username: "admin"
  admin_password: "admin123"
  jwt_secret: "your-secret-key"
```

### 3. 启动

```bash
uv run main.py
```

或者直接使用 uvicorn：

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 3000 --reload
```

### 4. 访问

- 管理后台: http://localhost:8000/login
- API 文档: http://localhost:8000/docs
- 默认账号: admin / admin123

## 配置说明

所有配置统一在 `config.yaml` 中管理，通过 `app/core/config.py` 统一出口。支持环境变量覆盖：

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `ONEDATA_DATABASE_URL` | 数据库连接 URL | sqlite+aiosqlite:///./onedata.db |
| `SERVER_HOST` | 监听地址 | 0.0.0.0 |
| `SERVER_PORT` | 监听端口 | 8000 |
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
| ANY | `/gw/{project_id}/{path}` | 动态 API 调用 |

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

## License

MIT
