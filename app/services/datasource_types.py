"""数据源类型定义与辅助函数。"""

MYSQL_PROTOCOL_TYPES = {"mysql", "starrocks", "selectdb", "doris"}
REDIS_TYPES = {"redis"}
POSTGRESQL_TYPES = {"postgresql"}

SUPPORTED_DATASOURCE_TYPES = MYSQL_PROTOCOL_TYPES | REDIS_TYPES | POSTGRESQL_TYPES

DATASOURCE_DISPLAY_NAMES = {
    "mysql": "MySQL",
    "starrocks": "StarRocks",
    "selectdb": "SelectDB",
    "doris": "Apache Doris",
    "redis": "Redis",
    "postgresql": "PostgreSQL",
}

DATASOURCE_DEFAULT_PORTS = {
    "mysql": 3306,
    "starrocks": 9030,
    "selectdb": 9030,
    "doris": 9030,
    "redis": 6379,
    "postgresql": 5432,
}


def normalize_datasource_type(ds_type: str | None) -> str:
    """标准化数据源类型字符串。"""
    return (ds_type or "mysql").strip().lower()


def is_mysql_protocol(ds_type: str | None) -> bool:
    """判断是否为兼容 MySQL 协议的数据源。"""
    return normalize_datasource_type(ds_type) in MYSQL_PROTOCOL_TYPES


def is_supported_datasource_type(ds_type: str | None) -> bool:
    """判断是否为平台声明支持的数据源类型。"""
    return normalize_datasource_type(ds_type) in SUPPORTED_DATASOURCE_TYPES


def optional_database_name(database_name: str | None) -> str | None:
    """数据库名为空时返回 None，允许 SQL 使用 db.table 全限定名执行。

    MySQL 协议兼容数据源（如 StarRocks、SelectDB、Doris）支持在查询中使用
    `database.table` 形式指定库表。此时数据源配置中的默认数据库可以留空，
    连接层不应把空字符串作为默认库传入驱动，避免部分服务端把空默认库解析成
    异常的集群/库名并报 Unknown database。
    """
    value = (database_name or "").strip()
    return value or None
