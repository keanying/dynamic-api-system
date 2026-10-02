# -*- coding: utf-8 -*-
"""
缓存自动预热调度器 (v2.13+)
============================

作用
----
对「开启了缓存 + 开启了自动预热」的 API，后台定期检查其缓存剩余寿命，
在快过期时用记录下来的历史请求参数重新执行查询、把结果回填缓存。
这样用户请求过来时始终命中缓存，不必等那次冷查询。

工作流程
--------
1. 网关每次请求，若该 API 开了预热，就把参数组合记进 Redis
   （见 engine._record_prewarm_params）
2. 本调度器每隔 prewarm_interval 秒跑一轮：
   - 查出所有 cache_enabled=1 且 cache_prewarm=1 的 API
   - 读出它记录的历史参数组合
   - 对每个组合看缓存剩余 TTL，低于阈值就重新执行查询并写缓存

前置条件
--------
- redis.enabled 必须为 true。内存缓存模式下多进程无法共享，预热没有意义，
  此时调度器会直接跳过（并在启动时打一条日志说明）。

多 worker 说明
--------------
用 Redis 分布式锁保证同一时刻只有一个 worker 在跑预热，
避免开多 worker 后同一批查询被重复执行 N 遍打爆数据库。
"""
import asyncio
import time

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("cache_prewarm")

_task = None
_stopping = False

# 分布式锁：保证多 worker 下同一轮只有一个进程执行
_LOCK_TTL = 300  # 锁自动过期时间(秒)，防止进程崩溃后锁永久残留


def _lock_key() -> str:
    return f"{settings.redis.key_prefix}prewarm:lock"


async def _acquire_lock(r) -> bool:
    """尝试获取分布式锁（SET NX EX）。拿不到说明别的 worker 正在跑，本轮跳过。"""
    try:
        # nx=True 只在 key 不存在时设置；ex 到期自动释放
        ok = await r.set(_lock_key(), str(int(time.time())), nx=True, ex=_LOCK_TTL)
        return bool(ok)
    except Exception as e:
        log.warning(f"获取预热锁失败 | error={str(e)}")
        return False


async def _release_lock(r):
    try:
        await r.delete(_lock_key())
    except Exception:
        pass


async def _prewarm_one_api(api_config, params_list, db_session_factory):
    """对单个 API 的所有历史参数组合做预热。"""
    from app.services.engine import (
        _build_cache_key, _get_redis, execute_api,
    )
    from app.models.models import ApiParameter
    from sqlalchemy import select

    ttl = api_config.cache_ttl or settings.cache.default_ttl
    threshold = max(1, int(ttl * settings.cache.prewarm_threshold_ratio))
    # 预热策略：
    #   always（默认）—— 每轮扫描把记录的参数组合全部刷一遍，不管缓存还剩多久。
    #       这样缓存内容始终是「最近一轮扫描时的最新数据」，也不受各 API 自己的
    #       TTL 影响；扫描间隔(cache.prewarm_interval)就等于数据新鲜度上限。
    #   near_expiry —— 只在缓存快过期时才刷，省数据库资源，但两次刷新之间
    #       缓存内容会一直是旧的。
    strategy = getattr(settings.cache, "prewarm_strategy", "always")
    r = _get_redis()

    warmed = 0
    skipped = 0
    for params in params_list:
        if _stopping:
            break
        try:
            cache_key = _build_cache_key(api_config.id, params, getattr(api_config, "version", None))
            if strategy == "near_expiry":
                remain = await r.ttl(cache_key)
                # remain: -2=key不存在(已过期), -1=永不过期, >0=剩余秒数
                if remain > threshold:
                    skipped += 1
                    continue

            # 每个参数组合用独立 session，避免一个失败影响其它
            async with db_session_factory() as db:
                # 注意：execute_api 的第三个参数是 client_ip（不是参数定义列表），
                # 这里标记来源为 prewarm，一来日志可区分，二来避免预热执行时
                # 又把参数重复记录进历史（否则参数集合会自我膨胀）
                await execute_api(api_config, params, "prewarm", db,
                                  call_source="prewarm")
            warmed += 1
        except Exception as e:
            # 单个参数组合失败不影响其它组合，只记日志
            log.warning(
                f"预热失败 | api_id={api_config.id} | params={params} | error={str(e)}"
            )
    return warmed


async def _run_once(db_session_factory):
    """跑一轮预热。"""
    from app.services.engine import _get_redis, _load_prewarm_params
    from app.models.models import ApiConfig
    from sqlalchemy import select

    r = _get_redis()
    if not await _acquire_lock(r):
        log.debug("预热锁被其它 worker 持有，本轮跳过")
        return

    try:
        async with db_session_factory() as db:
            result = await db.execute(
                select(ApiConfig).where(
                    ApiConfig.cache_enabled == True,      # noqa: E712
                    ApiConfig.cache_prewarm == True,      # noqa: E712
                    ApiConfig.is_enabled == True,         # noqa: E712
                )
            )
            apis = result.scalars().all()

        if not apis:
            log.debug("没有开启自动预热的 API，本轮结束")
            return

        total_warmed = 0
        for api_config in apis:
            if _stopping:
                break
            params_list, source = await _resolve_prewarm_params(api_config, db_session_factory)
            if not params_list:
                continue
            n = await _prewarm_one_api(api_config, params_list, db_session_factory)
            total_warmed += n
            if n:
                log.info(
                    f"[预热] 完成 | api_id={api_config.id} | name={api_config.name} | "
                    f"来源={source} | 参数组合={len(params_list)} | 实际刷新={n}"
                )
        if total_warmed:
            log.info(f"本轮预热结束 | API数={len(apis)} | 共刷新={total_warmed}")
    finally:
        await _release_lock(r)


async def _resolve_prewarm_params(api_config, db_session_factory):
    """决定这个 API 用什么参数来预热。

    始终以「历史请求参数」为底 —— 这样每个不同的业务参数组合（比如不同景区的
    supplierId）都能各自预热到自己的缓存。

    「预热参数覆盖」(prewarm_param_overrides) 支持两种写法：
      单组：{"startTime": "${T_7}", "endTime": "${NOW}"}
      多组：[{"startTime": "${T_1}", ...}, {"startTime": "${T_7}", ...}, ...]

    多组用于同一个 API 需要按多个时间段各缓存一份的场景（近1日/近7日/近30日）：
    其它参数（supplierId 等）取自历史请求保持不变，只有时间字段按组切换，
    因此最终预热组合数 = 历史参数组合数 × 覆盖组数。

    返回 (参数列表, 来源说明)
    """
    from app.services.engine import _load_prewarm_params

    # 跨日停止开关：开启时只预热「今天被请求过」的参数，
    # 0 点后昨天那批自动停掉，直到当天有新请求才恢复
    only_today = bool(getattr(api_config, "prewarm_stop_daily", False))
    params_list = await _load_prewarm_params(api_config.id, only_today=only_today)
    if not params_list:
        if only_today:
            log.debug(f"跨日停止生效：今天暂无请求，跳过预热 | api_id={api_config.id}")
        return [], "历史参数"

    overrides_raw = (getattr(api_config, "prewarm_param_overrides", "") or "").strip()
    if not overrides_raw:
        return params_list, "历史参数"

    try:
        import json
        from app.services.project_vars import build_var_map, substitute
        from app.models.models import ProjectVariable
        from sqlalchemy import select

        overrides = json.loads(overrides_raw)
        # 统一成「组列表」处理：单个对象视为只有一组
        if isinstance(overrides, dict):
            groups = [overrides]
        elif isinstance(overrides, list):
            groups = [g for g in overrides if isinstance(g, dict)]
            if not groups:
                raise ValueError("参数覆盖数组里没有有效的对象元素")
        else:
            raise ValueError(
                '参数覆盖须是 JSON 对象或对象数组，'
                '形如 {"startTime":"${T_7}"} 或 [{"startTime":"${T_1}"},{"startTime":"${T_7}"}]'
            )

        async with db_session_factory() as db:
            vr = await db.execute(
                select(ProjectVariable).where(
                    ProjectVariable.project_id == api_config.project_id
                )
            )
            variables = vr.scalars().all()

        # 所有组共用同一份变量求值快照，保证同一轮预热里各组时间基准一致
        var_map = build_var_map(variables)
        resolved_groups = [substitute(g, var_map) for g in groups]

        merged = []
        for p in params_list:
            for rg in resolved_groups:
                np = dict(p)
                np.update(rg)   # 只覆盖该组列出的字段，其余保持历史请求原值
                merged.append(np)

        fields = sorted({k for rg in resolved_groups for k in rg})
        return merged, f"历史参数×{len(resolved_groups)}组覆盖{fields}"
    except Exception as e:
        # 覆盖规则配错了不能让预热整个停摆，退回用原始历史参数
        log.warning(
            f"预热参数覆盖解析失败，改用原始历史参数 | api_id={api_config.id} | error={str(e)}"
        )
        return params_list, "历史参数(覆盖规则无效)"


async def _loop(db_session_factory):
    interval = max(10, settings.cache.prewarm_interval)
    log.info(f"缓存预热调度器已启动 | 扫描间隔={interval}s")
    while not _stopping:
        try:
            await _run_once(db_session_factory)
        except Exception as e:
            log.error(f"预热轮次异常（不影响服务） | error={str(e)}")
        # 用小步 sleep，便于停服时快速退出
        slept = 0
        while slept < interval and not _stopping:
            await asyncio.sleep(1)
            slept += 1
    log.info("缓存预热调度器已停止")


def start(db_session_factory):
    """在应用启动时调用。未开 Redis 则不启动。"""
    global _task, _stopping
    if not settings.redis.enabled:
        log.info("缓存预热未启动：需要 redis.enabled=true（内存缓存模式下预热无意义）")
        return
    _stopping = False
    _task = asyncio.create_task(_loop(db_session_factory))


async def stop():
    """在应用关闭时调用，优雅停止。"""
    global _stopping, _task
    _stopping = True
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except asyncio.TimeoutError:
            _task.cancel()
        except Exception:
            pass
        _task = None
