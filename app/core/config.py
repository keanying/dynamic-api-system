"""
统一配置出口模块
所有配置项从 config.yaml 读取，通过本模块统一对外暴露。
支持环境变量覆盖 YAML 配置。
"""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.runtime_env import CURRENT_ENV, IS_PRE, env_port

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = BASE_DIR / "config.yaml"


def _load_yaml() -> dict:
    """加载 YAML 配置文件"""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_FILE}")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _env(key: str, default=None, cast=None):
    """从环境变量获取值，支持类型转换"""
    val = os.environ.get(key, default)
    if val is not None and cast is not None:
        if cast == bool:
            return str(val).lower() in ("true", "1", "yes")
        return cast(val)
    return val


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True
    workers: int = 1
    secret_key: str = "change-me"


@dataclass
class DatabaseConfig:
    url: str = "sqlite+aiosqlite:///./onedata.db"
    echo: bool = False
    pool_size: int = 10
    pool_recycle: int = 3600
    max_overflow: int = 20


@dataclass
class RedisConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6379
    password: str = ""
    db: int = 0
    max_connections: int = 20
    key_prefix: str = "onedata:"


@dataclass
class SecurityConfig:
    encryption_key: str = "onedata-encryption-key-32bytes!!"
    admin_username: str = "admin"
    admin_password: str = "admin123"
    jwt_secret: str = "change-me"
    jwt_expire_hours: int = 24
    ip_whitelist: List[str] = field(default_factory=list)
    max_request_body: int = 1048576
    max_array_length: int = 100
    # 谁可以创建/修改插件类 API (v2.20+)：super_admin（默认）/ developer（项目研发即可，旧行为）
    plugin_editor: str = "super_admin"
    # 可信代理 (v2.21+)：只有对端在这些地址/网段内时才采信 X-Forwarded-For。None = 本机 + 内网网段
    trusted_proxies: Optional[List[str]] = None
    # 创建项目 / 数据源所需的最低全局角色 (v2.21+)：user / developer（默认）/ admin / super_admin
    project_creator_role: str = "developer"
    datasource_creator_role: str = "developer"


@dataclass
class QueryConfig:
    default_max_rows: int = 10000
    default_timeout: int = 30
    slow_query_threshold: int = 500
    slow_query_alert_threshold: int = 1000
    # 业务数据源 MySQL 驱动 (v2.20+)：asyncmy（默认，C 扩展，快 4~6 倍）/ aiomysql
    mysql_driver: str = "asyncmy"
    # 单 SQL 模式下参数默认值是否对 $if$/$for$ 条件可见 (v2.21+)，与流水线模式一致
    defaults_visible_in_template: bool = False
    ddl_keywords: List[str] = field(default_factory=lambda: [
        "DROP", "CREATE", "ALTER", "TRUNCATE",
        "INSERT", "UPDATE", "DELETE", "GRANT", "REVOKE"
    ])


@dataclass
class CacheConfig:
    default_ttl: int = 300
    null_ttl: int = 60
    ttl_jitter: int = 30
    # --- 缓存自动预热 (v2.13+) ---
    # 后台调度器扫描间隔（秒）。每轮会检查开了预热的 API，
    # 对「快过期」的缓存重新查询回填。
    prewarm_interval: int = 60
    # 单个 API 最多记住多少个参数组合（超过按最后请求时间淘汰最旧的）。
    # 防止参数组合无限增长把 Redis 撑爆、预热任务打垮数据库。
    prewarm_max_params: int = 200
    # 缓存剩余寿命低于该比例时触发预热（0.2 = 剩余不足 20% 就提前刷新）
    prewarm_threshold_ratio: float = 0.2
    # 预热策略：always=每轮全量刷新(默认)，near_expiry=只在缓存快过期时刷
    prewarm_strategy: str = "always"
    # 进程内热点缓存 (v2.20+)：命中过的结果在本进程内存里再留几秒，热门 key 不必每次访问 Redis。
    # 不会超过该 key 在 Redis 里的剩余寿命；手动「清除缓存」后其它进程最多延迟 local_ttl 秒。0 = 关闭
    local_ttl: float = 5
    # 热点缓存每个进程最多占用的内存（MB，按存储大小计，大结果以压缩后大小计）
    local_max_mb: float = 256


@dataclass
class LogConfig:
    log_dir: str = "./logs"  # 日志目录，可配置
    retention_days: int = 3  # 日志保留天数，默认 3 天
    level: str = "DEBUG"  # 日志级别
    file_level: str = ""  # 全量/网关日志文件级别，留空同 level (v2.19+)
    # uvicorn 自带的访问日志（每个请求一行到 stdout）(v2.20+)。网关日志和 call_logs 表已完整记录，默认关闭
    access_log: bool = False
    # 网关每个请求一条 INFO 日志（网关响应 | ...）(v2.20+)。call_logs 表里有同样的明细；
    # 追求极限吞吐时可关闭，约省 15% CPU
    gateway_info: bool = True
    queue_size: int = 10000


@dataclass
class MonitorConfig:
    failure_rate_threshold: int = 10
    avg_latency_threshold: int = 2000
    single_latency_threshold: int = 5000
    collect_interval: int = 60
    # 调用日志保留天数 (v2.19+)，> 0 时每小时自动删除更早的日志；0 = 不自动清理
    call_log_retention_days: int = 0


@dataclass
class GatewayConfig:
    prefix: str = "/v1/data"
    # 网关接口配置进程内缓存秒数 (v2.19+)，0 = 关闭（每个请求都查系统库）
    config_cache_ttl: float = 5
    # 响应体超过这么多字节且客户端支持 gzip 时压缩返回 (v2.20+)，0 = 不压缩
    gzip_min_bytes: int = 8192


@dataclass
class ApprovalConfig:
    # 上线审批通过条件 (v2.21+)：any = 项目管理员或会审研发任一方通过（v2.10 起的行为，默认）；
    # both = 两方都要通过（管理员提交且未指定会审人时，管理员通过即可）
    online_mode: str = "any"


@dataclass
class CatalogConfig:
    """多源 SQL（catalog 模式，v2.22+）：一条 MySQL 语法的 SQL 关联多个数据源的表。"""
    # 单个源表最多拉取的行数，超过即报错（提示加过滤条件），防止误写成全表关联把内存打满
    max_rows_per_source: int = 200000
    # 关联键不超过这么多个时，把它们作为 IN 条件下推到另一侧的源库，只取能关联上的行
    dynamic_filter_max_keys: int = 10000
    # 同时进行的跨源计算数（每个 worker 进程），超出的请求排队
    max_concurrency: int = 8
    # 关联计算引擎(DuckDB)的内存上限与线程数
    memory_limit: str = "1GB"
    threads: int = 4
    # 跨源计算时字符串比较：nocase = 不区分大小写（与 MySQL 默认 *_ci 排序规则一致）；
    # nocase_noaccent = 再不区分重音（é = e，与 utf8mb4_general_ci 完全一致，字符串关联约慢一倍）；binary = 区分大小写
    string_compare: str = "nocase"
    # 源表结构（列名/类型）缓存秒数
    schema_cache_ttl: int = 300


@dataclass
class AuthConfig:
    default_admin: str = "admin"
    default_password: str = "admin123"


@dataclass
class AppServerConfig:
    """应用级配置（title, version, host, port 等）"""
    title: str = "OneData Portal"
    version: str = "2.9.0"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True
    workers: int = 1
    secret_key: str = "change-me"


@dataclass
class FullConfig:
    """应用总配置，聚合所有子配置"""
    app: AppServerConfig = field(default_factory=AppServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    log: LogConfig = field(default_factory=LogConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)


def load_config() -> FullConfig:
    """
    加载配置：先从 config.yaml 读取，再用环境变量覆盖。
    """
    raw = _load_yaml()

    server_raw = raw.get("server", {})
    db_raw = raw.get("database", {})
    redis_raw = raw.get("redis", {})
    sec_raw = raw.get("security", {})
    query_raw = raw.get("query", {})
    cache_raw = raw.get("cache", {})
    log_raw = raw.get("log", {})
    monitor_raw = raw.get("monitor", {})
    cat_raw = raw.get("catalog", {}) or {}

    # 监听端口 (v2.18+)：environments.<当前环境>.port 优先于 server.port，
    # 这样 `python main.py --env pre` 会自动监听预发端口。环境变量 SERVER_PORT 仍最高优先。
    listen_port = env_port(CURRENT_ENV) or server_raw.get("port", 8000)

    # Redis 键前缀按环境隔离 (v2.18+)：pre 表是正式表的副本，API id 会重合，
    # 若两套环境共用同一前缀，缓存键 api:{id}:... 会互相串数据。
    redis_prefix = _env("REDIS_KEY_PREFIX", redis_raw.get("key_prefix", "onedata:"))
    if IS_PRE:
        redis_prefix = f"{redis_prefix}pre:"

    # 处理 SQLite 相对路径，转为绝对路径
    db_url = _env("ONEDATA_DATABASE_URL", db_raw.get("url", "sqlite+aiosqlite:///./onedata.db"))
    if "sqlite" in db_url and "///./" in db_url:
        db_url = db_url.replace("///./", f"///{BASE_DIR}/")

    config = FullConfig(
        app=AppServerConfig(
            title=_env("APP_TITLE", raw.get("app", {}).get("title", "OneData Portal")),
            version=_env("APP_VERSION", raw.get("app", {}).get("version", "2.9.0")),
            host=_env("SERVER_HOST", server_raw.get("host", "0.0.0.0")),
            port=_env("SERVER_PORT", listen_port, int),
            debug=_env("SERVER_DEBUG", server_raw.get("debug", True), bool),
            workers=_env("SERVER_WORKERS", server_raw.get("workers", 1), int),
            secret_key=_env("SERVER_SECRET_KEY", server_raw.get("secret_key", "change-me")),
        ),
        auth=AuthConfig(
            default_admin=_env("ADMIN_USERNAME", sec_raw.get("admin_username", "admin")),
            default_password=_env("ADMIN_PASSWORD", sec_raw.get("admin_password", "admin123")),
        ),
        server=ServerConfig(
            host=_env("SERVER_HOST", server_raw.get("host", "0.0.0.0")),
            port=_env("SERVER_PORT", listen_port, int),
            debug=_env("SERVER_DEBUG", server_raw.get("debug", True), bool),
            workers=_env("SERVER_WORKERS", server_raw.get("workers", 1), int),
            secret_key=_env("SERVER_SECRET_KEY", server_raw.get("secret_key", "change-me")),
        ),
        database=DatabaseConfig(
            url=db_url,
            echo=_env("DATABASE_ECHO", db_raw.get("echo", False), bool),
            pool_size=_env("DATABASE_POOL_SIZE", db_raw.get("pool_size", 10), int),
            pool_recycle=_env("DATABASE_POOL_RECYCLE", db_raw.get("pool_recycle", 3600), int),
            max_overflow=_env("DATABASE_MAX_OVERFLOW", db_raw.get("max_overflow", 20), int),
        ),
        redis=RedisConfig(
            enabled=_env("REDIS_ENABLED", redis_raw.get("enabled", False), bool),
            host=_env("REDIS_HOST", redis_raw.get("host", "127.0.0.1")),
            port=_env("REDIS_PORT", redis_raw.get("port", 6379), int),
            password=_env("REDIS_PASSWORD", redis_raw.get("password", "")),
            db=_env("REDIS_DB", redis_raw.get("db", 0), int),
            max_connections=_env("REDIS_MAX_CONNECTIONS", redis_raw.get("max_connections", 20), int),
            key_prefix=redis_prefix,
        ),
        security=SecurityConfig(
            encryption_key=_env("SECURITY_ENCRYPTION_KEY", sec_raw.get("encryption_key", "")),
            admin_username=_env("ADMIN_USERNAME", sec_raw.get("admin_username", "admin")),
            admin_password=_env("ADMIN_PASSWORD", sec_raw.get("admin_password", "admin123")),
            jwt_secret=_env("JWT_SECRET", sec_raw.get("jwt_secret", "change-me")),
            jwt_expire_hours=_env("JWT_EXPIRE_HOURS", sec_raw.get("jwt_expire_hours", 24), int),
            ip_whitelist=sec_raw.get("ip_whitelist", []),
            max_request_body=sec_raw.get("max_request_body", 1048576),
            max_array_length=sec_raw.get("max_array_length", 100),
            plugin_editor=sec_raw.get("plugin_editor", "super_admin"),
            trusted_proxies=sec_raw.get("trusted_proxies"),
            project_creator_role=sec_raw.get("project_creator_role", "developer"),
            datasource_creator_role=sec_raw.get("datasource_creator_role", "developer"),
        ),
        query=QueryConfig(
            default_max_rows=query_raw.get("default_max_rows", 10000),
            default_timeout=query_raw.get("default_timeout", 30),
            slow_query_threshold=query_raw.get("slow_query_threshold", 500),
            slow_query_alert_threshold=query_raw.get("slow_query_alert_threshold", 1000),
            mysql_driver=_env("MYSQL_DRIVER", query_raw.get("mysql_driver", "asyncmy")),
            defaults_visible_in_template=bool(query_raw.get("defaults_visible_in_template", False)),
            ddl_keywords=query_raw.get("ddl_keywords", [
                "DROP", "CREATE", "ALTER", "TRUNCATE",
                "INSERT", "UPDATE", "DELETE", "GRANT", "REVOKE"
            ]),
        ),
        cache=CacheConfig(
            default_ttl=cache_raw.get("default_ttl", 300),
            null_ttl=cache_raw.get("null_ttl", 60),
            ttl_jitter=cache_raw.get("ttl_jitter", 30),
            prewarm_interval=cache_raw.get("prewarm_interval", 60),
            prewarm_max_params=cache_raw.get("prewarm_max_params", 200),
            prewarm_threshold_ratio=cache_raw.get("prewarm_threshold_ratio", 0.2),
            prewarm_strategy=cache_raw.get("prewarm_strategy", "always"),
            local_ttl=float(cache_raw.get("local_ttl", 5)),
            local_max_mb=float(cache_raw.get("local_max_mb", 256)),
        ),
        log=LogConfig(
            log_dir=_env("LOG_DIR", log_raw.get("log_dir", "./logs")),
            retention_days=log_raw.get("retention_days", 3),
            level=_env("LOG_LEVEL", log_raw.get("level", "DEBUG")),
            file_level=_env("LOG_FILE_LEVEL", log_raw.get("file_level", "")),
            access_log=_env("LOG_ACCESS_LOG", log_raw.get("access_log", False), bool),
            gateway_info=_env("LOG_GATEWAY_INFO", log_raw.get("gateway_info", True), bool),
            queue_size=log_raw.get("queue_size", 10000),
        ),
        monitor=MonitorConfig(
            failure_rate_threshold=monitor_raw.get("failure_rate_threshold", 10),
            avg_latency_threshold=monitor_raw.get("avg_latency_threshold", 2000),
            single_latency_threshold=monitor_raw.get("single_latency_threshold", 5000),
            collect_interval=monitor_raw.get("collect_interval", 60),
            call_log_retention_days=int(monitor_raw.get("call_log_retention_days", 0) or 0),
        ),
        approval=ApprovalConfig(
            online_mode=(raw.get("approval", {}) or {}).get("online_mode", "any"),
        ),
        catalog=CatalogConfig(
            max_rows_per_source=int(cat_raw.get("max_rows_per_source", 200000)),
            dynamic_filter_max_keys=int(cat_raw.get("dynamic_filter_max_keys", 10000)),
            max_concurrency=max(1, int(cat_raw.get("max_concurrency", 8))),
            memory_limit=str(cat_raw.get("memory_limit", "1GB")),
            threads=max(1, int(cat_raw.get("threads", 4))),
            string_compare=str(cat_raw.get("string_compare", "nocase")).lower(),
            schema_cache_ttl=int(cat_raw.get("schema_cache_ttl", 300)),
        ),
        gateway=GatewayConfig(
            prefix=_env("GATEWAY_PREFIX", raw.get("gateway", {}).get("prefix", "/v1/data")),
            config_cache_ttl=_env("GATEWAY_CONFIG_CACHE_TTL", raw.get("gateway", {}).get("config_cache_ttl", 5), float),
            gzip_min_bytes=_env("GATEWAY_GZIP_MIN_BYTES", raw.get("gateway", {}).get("gzip_min_bytes", 8192), int),
        ),
    )

    return config


# 全局单例配置
settings = load_config()
