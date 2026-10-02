# -*- coding: utf-8 -*-
"""
网关接口配置缓存 (v2.19+)
========================

网关每个请求都要查 项目 → API → 参数定义 → 数据源 四次系统库，命中结果缓存的
请求也一样。这些配置几乎不变，这里在进程内按 (项目编码, 方法, 路径) 缓存一小段时间。

一致性：
  - 本进程内任何 /api/** 写请求完成后整体清空（见 env_guard.AdminWriteMiddleware），
    单 worker 部署下改完配置立即生效；
  - 多 worker 部署时，其它 worker 最多延迟 gateway.config_cache_ttl 秒生效；
  - config_cache_ttl 设为 0 关闭缓存，行为与之前完全一致。

缓存的是已脱离 session 的 ORM 实例（expire_on_commit=False，属性已加载），网关只读不写。
只缓存「找到了」的结果；项目不存在 / API 不存在等错误每次都实时查库。
"""
import asyncio
import time
from typing import Dict, Optional, Tuple

from sqlalchemy import select, func, or_

from app.core.config import settings
from app.models.models import ApiConfig, ApiParameter, DataSource, Project

# key -> (过期时间, project, api_config, api_params, datasource)
_entries: Dict[Tuple[str, str, str], tuple] = {}
_MAX_ENTRIES = 5000


def ttl() -> float:
    return float(getattr(settings.gateway, "config_cache_ttl", 0) or 0)


def invalidate() -> None:
    _entries.clear()
    from app.services import ds_scope
    ds_scope.clear_cache()


def get(project_code: str, method: str, url_path: str):
    if ttl() <= 0:
        return None
    hit = _entries.get((project_code, method, url_path))
    if hit is None:
        return None
    if hit[0] < time.monotonic():
        _entries.pop((project_code, method, url_path), None)
        return None
    return hit[1:]


def put(project_code: str, method: str, url_path: str, project, api_config, api_params, datasource) -> None:
    if ttl() <= 0:
        return
    if len(_entries) >= _MAX_ENTRIES:
        _entries.clear()
    _entries[(project_code, method, url_path)] = (
        time.monotonic() + ttl(), project, api_config, api_params, datasource,
    )


async def load_api(db, project_id: int, method: str, url_path: str) -> Optional[ApiConfig]:
    """按网关规则匹配 API（逻辑同 v2.0.2：容忍首尾空格/尾部斜杠，未主动下线即可调用）。"""
    def _q(path):
        return select(ApiConfig).where(
            ApiConfig.project_id == project_id,
            func.trim(ApiConfig.url_path) == path,
            ApiConfig.method == method,
            ApiConfig.is_enabled == True,  # noqa: E712
            or_(ApiConfig.status != "offline", ApiConfig.status.is_(None)),
        )

    api_config = (await db.execute(_q(url_path))).scalars().first()
    clean_path = url_path.rstrip("/") or "/"
    if api_config is None and clean_path != url_path:
        api_config = (await db.execute(_q(clean_path))).scalars().first()
    return api_config


async def load_project(db, project_code: str) -> Optional[Project]:
    return (await db.execute(select(Project).where(Project.code == project_code))).scalar_one_or_none()


async def load_extras(db, api_config: ApiConfig):
    """API 的参数定义和主数据源。"""
    api_params = (await db.execute(
        select(ApiParameter).where(ApiParameter.api_id == api_config.id)
    )).scalars().all()
    datasource = None
    if api_config.datasource_id:
        datasource = (await db.execute(
            select(DataSource).where(DataSource.id == api_config.datasource_id)
        )).scalar_one_or_none()
    return api_params, datasource


# 同一 key 正在加载时，其它并发请求等它的结果（v2.20）：
# 缓存过期的瞬间几百个请求同时到达，不会各自去查一遍系统库、把连接池打满
_loading: Dict[Tuple[str, str, str], "asyncio.Future"] = {}


async def resolve(db, project_code: str, method: str, url_path: str):
    """返回 (project, api_config, api_params, datasource)；项目/API 不存在时对应项为 None。"""
    hit = get(project_code, method, url_path)
    if hit is not None:
        return hit
    key = (project_code, method, url_path)
    waiting = _loading.get(key)
    if waiting is not None:
        return await asyncio.shield(waiting)

    fut = asyncio.get_running_loop().create_future()
    _loading[key] = fut
    try:
        api_config = api_params = datasource = None
        project = await load_project(db, project_code)
        if project is not None and project.is_active:
            api_config = await load_api(db, project.id, method, url_path)
            if api_config is not None:
                api_params, datasource = await load_extras(db, api_config)
                put(project_code, method, url_path, project, api_config, api_params, datasource)
        res = (project, api_config, api_params, datasource)
        fut.set_result(res)
        return res
    except BaseException as e:
        # 被取消时不把 CancelledError 传给等待者（会被当成它们自己被取消），换成普通错误
        fut.set_exception(e if isinstance(e, Exception) else RuntimeError("加载接口配置被中断，请重试"))
        fut.exception()   # 没有等待者时也不报 "exception was never retrieved"
        raise
    finally:
        _loading.pop(key, None)
