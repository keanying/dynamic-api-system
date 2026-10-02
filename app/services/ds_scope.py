# -*- coding: utf-8 -*-
"""
数据源访问范围 (v2.21+)
======================

1. 可用项目：DataSource.project_scope 为逗号分隔的项目编码，空 = 所有项目可用（兼容旧数据）。
   用编码而非 id：预发/生产两套环境项目 id 不同、编码一致，「发布到生产」时范围配置可直接沿用。
   单 SQL、流水线步骤引用的数据源、插件 ctx.query 引用的数据源都会校验。
2. 系统库保护：业务数据源与平台系统库常在同一个 MySQL 实例上，原来流水线可以用 database 字段
   切到平台库，SQL 里也可以直接写「平台库.src_dop_users」，借业务数据源账号读平台的用户/凭证数据。
   现在拦截这两种写法（以及 mysql / sys / performance_schema 系统库）。
   根本措施仍是给业务数据源配置最小权限的只读账号。
"""
import re
from typing import Optional, Set

from sqlalchemy import select

from app.core.config import settings

_SYSTEM_SCHEMAS = {"mysql", "sys", "performance_schema"}
_STR_OR_COMMENT_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|--[^\n]*|#(?!\{)[^\n]*|/\*.*?\*/", re.DOTALL)


def parse_scope(raw: Optional[str]) -> Set[str]:
    return {x.strip() for x in re.split(r"[,，\s]+", raw or "") if x.strip()}


def normalize_scope(raw: Optional[str]) -> str:
    return ",".join(sorted(parse_scope(raw)))


def is_allowed(datasource, project_code: Optional[str]) -> bool:
    scope = parse_scope(getattr(datasource, "project_scope", "") or "")
    return not scope or (project_code or "") in scope


def ensure_allowed(datasource, project_code: Optional[str]) -> None:
    if datasource is not None and not is_allowed(datasource, project_code):
        raise Exception(f"数据源「{datasource.name}」未对项目「{project_code}」开放，请联系管理员在数据源配置中添加该项目")


# ---- 项目编码缓存（随 gateway_cache.invalidate 一起清空）----
_code_cache = {}


def clear_cache() -> None:
    _code_cache.clear()


async def project_code(db, project_id: int) -> str:
    if project_id in _code_cache:
        return _code_cache[project_id]
    from app.models.models import Project
    code = (await db.execute(select(Project.code).where(Project.id == project_id))).scalar() or ""
    _code_cache[project_id] = code
    return code


# ---- 系统库保护 ----
def platform_db_name() -> str:
    try:
        from sqlalchemy.engine import make_url
        url = make_url(settings.database.url)
        if url.get_backend_name() == "sqlite":
            return ""
        return (url.database or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def protected_schemas() -> Set[str]:
    names = set(_SYSTEM_SCHEMAS)
    if platform_db_name():
        names.add(platform_db_name())
    return names


def check_database_override(db_name: str) -> None:
    if (db_name or "").strip().strip("`").lower() in protected_schemas():
        raise Exception(f"不允许切换到系统库「{db_name}」")


def schema_violation(sql: str, datasource) -> Optional[str]:
    """SQL（字符串/注释以外）里以「库名.表名」引用受保护的库时返回原因。
    数据源本身就登记为平台库时视为管理员有意为之，不拦截平台库名。"""
    names = protected_schemas()
    own = (getattr(datasource, "database_name", "") or "").lower()
    names.discard(own)
    if not names:
        return None
    # 只屏蔽字符串和注释，保留 `反引号标识符`（通用的 _code_only 会把反引号内容一并抹掉）
    code = _STR_OR_COMMENT_RE.sub(" ", sql or "").lower()
    for n in names:
        if re.search(rf"(?<![\w`.])`?{re.escape(n)}`?\s*\.\s*`?\w", code):
            return f"不允许访问系统库「{n}」"
    return None
