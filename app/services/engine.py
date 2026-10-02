"""
动态调用引擎
- 动态路由匹配
- API Key 认证
- 参数自动解析与校验
- SQL 参数安全替换（参数化查询）
- DDL 拦截
- 结果集自动包装
- Redis 缓存
- 超时控制
- 慢查询记录
- 限流
"""

import re
import json
import orjson as _orjson
import time
import hashlib
import asyncio
import datetime
from typing import Optional, Dict, Any, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text as sa_text

from app.core.config import settings
from app.core.security import decrypt_value
from app.core.logging import get_logger, is_debug as _is_debug
from app.models.models import ApiConfig, ApiParameter, DataSource, CallLog, Project
from app.services.sql_template import render_template, extract_placeholders, SqlTplError

log = get_logger("engine")


# ============================================================
# 数据源类型 → 实际驱动协议
# StarRocks / SelectDB 都兼容 MySQL 协议，复用 aiomysql 即可
# ============================================================
MYSQL_COMPATIBLE_TYPES = {"mysql", "starrocks", "selectdb", "doris"}

# IN 展开的最大数组长度（防止超大数组把 SQL 撑爆 / DoS）
MAX_IN_EXPANSION = 10000


class RateLimiter:
    """简单的内存限流器"""

    def __init__(self):
        self._counters: Dict[int, list] = {}  # api_id -> [timestamp, ...]

    def check(self, api_id: int, qps: int) -> bool:
        now = time.time()
        if api_id not in self._counters:
            self._counters[api_id] = []

        # 清理 1 秒前的记录
        self._counters[api_id] = [t for t in self._counters[api_id] if now - t < 1.0]

        if len(self._counters[api_id]) >= qps:
            log.warning(f"限流触发 | api_id={api_id} | qps_limit={qps} | current={len(self._counters[api_id])}")
            return False

        self._counters[api_id].append(now)
        return True


# 全局限流器实例
rate_limiter = RateLimiter()

# 内存缓存 (当 Redis 未启用时使用)
_memory_cache: Dict[str, Tuple[Any, float]] = {}
# 内存缓存最大条目数，超出时先清过期项再按最早过期淘汰（防止无限增长吃满内存）
_MEMORY_CACHE_MAX_ENTRIES = 5000

# 共享 Redis 客户端（redis.asyncio 客户端自带连接池）
# v1.5 修正: 原实现每次缓存读/写/清都新建一条 TCP 连接再关闭，
# 高 QPS 下会产生大量 TIME_WAIT 并显著增加缓存操作延迟。
_redis_client = None


def _get_redis():
    """懒加载共享 Redis 客户端。"""
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis
        # v2.19: 用阻塞式连接池。默认连接池在连接数达到 max_connections 时
        # 直接抛 "Too many connections"，高并发下缓存读写大量失败、请求全部回落查库；
        # 阻塞式连接池会排队等空闲连接（最多 5 秒）。
        pool = aioredis.BlockingConnectionPool(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            max_connections=getattr(settings.redis, "max_connections", 20) or 20,
            timeout=5,
        )
        _redis_client = aioredis.Redis(connection_pool=pool)
    return _redis_client


def _evict_memory_cache_if_needed():
    """内存缓存超限时淘汰：先清过期，再按最早过期时间淘汰至限额内。"""
    if len(_memory_cache) < _MEMORY_CACHE_MAX_ENTRIES:
        return
    now = time.time()
    expired = [k for k, (_, exp) in _memory_cache.items() if exp <= now]
    for k in expired:
        _memory_cache.pop(k, None)
    if len(_memory_cache) >= _MEMORY_CACHE_MAX_ENTRIES:
        # 按过期时间升序淘汰最早的 10%
        victims = sorted(_memory_cache.items(), key=lambda kv: kv[1][1])
        for k, _ in victims[: max(1, _MEMORY_CACHE_MAX_ENTRIES // 10)]:
            _memory_cache.pop(k, None)
        log.info(f"内存缓存超限淘汰 | 当前条目={len(_memory_cache)}")


_LEADING_COMMENT_RE = re.compile(r"^\s*(--[^\n]*\n|#[^\n]*\n|/\*.*?\*/)", re.DOTALL)


def _strip_leading_comments(sql: str) -> str:
    """剥掉 SQL 开头的注释（防止 /*x*/DROP ... 这类注释前缀绕过 DDL 检查）。"""
    prev = None
    while prev != sql:
        prev = sql
        sql = _LEADING_COMMENT_RE.sub("", sql, count=1)
    return sql.strip()


def _check_ddl(sql: str) -> bool:
    """检查 SQL 是否包含 DDL 关键字"""
    sql_upper = _strip_leading_comments(sql or "").upper()
    for keyword in settings.query.ddl_keywords:
        if re.match(rf'^\s*{keyword}\b', sql_upper):
            log.warning(f"DDL 拦截 | keyword={keyword} | sql={sql[:100]}...")
            return True
    return False


# ---- 只读防护 (v2.20) ----
# 原来只在「模板原文的开头」查 DDL 关键字，存在多种绕过：用 $if$ 包一层、
# 「SELECT 1; DELETE ...」多语句（驱动默认允许多语句，实测会真的执行）、
# REPLACE / LOAD / INTO OUTFILE 等不在名单里的写法。
# 现在对「渲染后真正要执行的 SQL」检查，单 SQL / 管线 / 插件三条路径统一在 _execute_mysql 里拦。
_EXTRA_FORBIDDEN_START = ("REPLACE", "LOAD", "RENAME", "LOCK", "UNLOCK", "HANDLER")
_OUTFILE_RE = re.compile(r"\bINTO\s+(OUTFILE|DUMPFILE)\b", re.IGNORECASE)
_DML_WORD_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_LOCKING_READ_RE = re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE)


def _code_only(sql: str) -> str:
    """把字符串字面量和注释替换成空格，只留 SQL 代码部分做检查。"""
    from app.services.sql_template import split_literals
    parts = split_literals(sql or "")
    return "".join(p if i % 2 == 0 else " " for i, p in enumerate(parts))


def _readonly_violation(sql: str):
    """检查即将执行的 SQL 是否只读；违规返回原因，否则返回 None。"""
    code = _code_only(sql).strip()
    body = code.rstrip("; \t\r\n")
    if ";" in body:
        return "不允许一次执行多条 SQL 语句"
    if _check_ddl(body):
        return "禁止执行 DDL 操作"
    head = _strip_leading_comments(body).upper()
    for kw in _EXTRA_FORBIDDEN_START:
        if re.match(rf"^\s*\(?\s*{kw}\b", head):
            return f"禁止执行 {kw} 语句"
    if _OUTFILE_RE.search(body):
        return "禁止 SELECT ... INTO OUTFILE / DUMPFILE"
    if re.match(r"^\s*WITH\b", head) and _DML_WORD_RE.search(_LOCKING_READ_RE.sub(" ", body)):
        return "禁止在 WITH 语句中执行写操作"
    return None


def _build_cache_key(api_id: int, params: dict, version=None) -> str:
    """构建缓存 key。

    v2.20: 带上 API 版本号。修改 SQL 等配置会使 version 自增，旧缓存随之失效；
    原来修改后重新上线，调用方在缓存过期前（可能长达数小时）拿到的仍是旧 SQL 的结果。
    key 前缀仍是 api:{id}:，「清除缓存」按前缀删除不受影响。
    """
    param_str = json.dumps(params, sort_keys=True, default=str)
    if version is not None:
        param_str = f"v{version}|{param_str}"
    param_hash = hashlib.md5(param_str.encode()).hexdigest()
    return f"{settings.redis.key_prefix}api:{api_id}:{param_hash}"


async def _get_cache(key: str):
    """读缓存，返回 CachedResult 或 None。

    v2.20: 先查进程内热点缓存(L1)，再查 Redis；Redis 的 GET 与 PTTL 合并为一次往返，
    放进 L1 的寿命不超过 Redis 里的剩余寿命。
    """
    from app.services import result_cache as rc
    hit = rc.l1_get(key)
    if hit is not None:
        return hit
    if settings.redis.enabled:
        try:
            r = _get_redis()
            pipe = r.pipeline(transaction=False)
            pipe.get(key)
            pipe.pttl(key)
            val, pttl = await pipe.execute()
            if not val:
                return None
            entry = rc.CachedResult.decode(val)
            if entry is None:
                # 升级前写入的旧格式缓存（JSON 文本），兼容读取
                entry = rc.CachedResult.from_legacy(val)
            if pttl and pttl > 0:
                rc.l1_put(key, entry, pttl / 1000.0)
            return entry
        except Exception as e:
            log.error(f"Redis 缓存读取失败 | key={key} | error={str(e)}")
    else:
        # 内存缓存
        if key in _memory_cache:
            data, expire_at = _memory_cache[key]
            if time.time() < expire_at:
                return data
            del _memory_cache[key]
    return None


async def _set_cache(key: str, entry, ttl: int):
    """写缓存。entry 为 CachedResult（序列化/压缩已在构造时完成，这里不再重复序列化）。"""
    import random
    from app.services import result_cache as rc
    jitter = random.randint(0, settings.cache.ttl_jitter)
    actual_ttl = ttl + jitter

    if settings.redis.enabled:
        try:
            r = _get_redis()
            await r.setex(key, actual_ttl, entry.encode())
            rc.l1_put(key, entry, actual_ttl)
            log.debug(f"Redis 缓存已设置 | key={key} | ttl={actual_ttl}s | bytes={entry.size()}")
        except Exception as e:
            log.error(f"Redis 缓存写入失败 | key={key} | error={str(e)}")
    else:
        _evict_memory_cache_if_needed()
        _memory_cache[key] = (entry, time.time() + actual_ttl)
        log.debug(f"内存缓存已设置 | key={key} | ttl={actual_ttl}s")


async def _clear_api_cache(api_id: int):
    """清除指定 API 的缓存"""
    prefix = f"{settings.redis.key_prefix}api:{api_id}:"
    from app.services import result_cache as _rc
    _rc.l1_clear_prefix(prefix)
    if settings.redis.enabled:
        try:
            r = _get_redis()
            count = 0
            async for key in r.scan_iter(f"{prefix}*"):
                await r.delete(key)
                count += 1
            log.info(f"API 缓存已清除 | api_id={api_id} | 清除数={count}")
        except Exception as e:
            log.error(f"清除 API 缓存失败 | api_id={api_id} | error={str(e)}")
    else:
        keys_to_delete = [k for k in _memory_cache if k.startswith(prefix)]
        for k in keys_to_delete:
            del _memory_cache[k]
        log.info(f"内存缓存已清除 | api_id={api_id} | 清除数={len(keys_to_delete)}")


# ========== 缓存自动预热 (v2.13+) ==========
# 思路：开启「自动预热」的 API，每次网关请求都把参数组合记到 Redis 的一个
# Hash 里（key = 参数MD5，value = 参数原文 JSON + 最后请求时间）。后台调度器
# 定期扫这些参数，重新执行查询并回填缓存，让用户始终命中缓存、不必等冷查询。
#
# 为什么必须存参数原文：缓存 key 里只有 MD5，无法反推回参数，预热时没法重新
# 发起查询，所以要单独存一份原文。
#
# 按自然日隔离：参数记录的 key 带日期（...:{api_id}:{YYYYMMDD}），并设置成
# 当天 24 点过期。也就是说「今天来过的请求，只在今天被预热」，跨过 0 点自动
# 作废，第二天有新请求再重新开始累积。这样既贴合"参数有时效性"的实际情况，
# 也避免陈年参数一直占着预热任务。

def _prewarm_params_key(api_id: int) -> str:
    """存放某 API 历史请求参数组合的 Redis Hash key。

    v2.14 起不再按自然日分片：因为预热时时间字段会被「参数覆盖」刷新成当前时间
    （见 ApiConfig.prewarm_param_overrides），历史参数不会过期，没必要每天清空。

    参数无限增长由 cache.prewarm_max_params 上限兜底：超过上限时按最后请求时间
    淘汰最久没人用的那些，活跃参数每次被请求都会刷新时间戳，不会被误删。
    """
    return f"{settings.redis.key_prefix}prewarm:params:{api_id}"


async def _record_prewarm_params(api_id: int, params: dict, max_entries: int):
    """记录一次请求的参数组合，供后台预热使用。

    只在 Redis 模式下生效（内存模式下多 worker 无法共享，预热没意义）。
    失败不影响主流程 —— 记录参数是附加功能，不能拖垮正常请求。
    """
    if not settings.redis.enabled:
        return
    try:
        r = _get_redis()
        hkey = _prewarm_params_key(api_id)
        field = hashlib.md5(
            json.dumps(params, sort_keys=True, default=str).encode()
        ).hexdigest()
        payload = json.dumps(
            {"params": params, "last_seen": int(time.time())},
            default=str,
        )
        # hset + hlen 合并为一次往返（v2.19）
        pipe = r.pipeline(transaction=False)
        pipe.hset(hkey, field, payload)
        pipe.hlen(hkey)
        _, total = await pipe.execute()

        # 上限保护：参数组合可能无限增长（每个不同参数都是一条），
        # 超过上限时按 last_seen 淘汰最旧的，避免 Redis 膨胀、预热任务过载。
        if total > max_entries:
            all_items = await r.hgetall(hkey)
            parsed = []
            for f, v in all_items.items():
                try:
                    fs = f.decode() if isinstance(f, bytes) else f
                    d = json.loads(v)
                    parsed.append((fs, d.get("last_seen", 0)))
                except Exception:
                    continue
            parsed.sort(key=lambda x: x[1])  # 最旧的在前
            drop = [f for f, _ in parsed[: total - max_entries]]
            if drop:
                await r.hdel(hkey, *drop)
                log.debug(f"预热参数超上限，已淘汰最旧 {len(drop)} 条 | api_id={api_id}")
    except Exception as e:
        log.warning(f"记录预热参数失败（不影响本次请求） | api_id={api_id} | error={str(e)}")


async def _clear_prewarm_params(api_id: int):
    """清除某 API 记录的历史请求参数（随「清除缓存」一起调用）。

    同时删掉 v2.13 及更早版本留下的按日期分片的 key（形如 ...:{api_id}:20260819），
    避免升级后旧分片一直残留在 Redis 里。
    """
    if not settings.redis.enabled:
        return
    try:
        r = _get_redis()
        count = 0
        # 当前版本的 key（无日期后缀）
        if await r.delete(_prewarm_params_key(api_id)):
            count += 1
        # 兼容清理旧版按日期分片的 key
        pattern = f"{settings.redis.key_prefix}prewarm:params:{api_id}:*"
        async for key in r.scan_iter(pattern):
            await r.delete(key)
            count += 1
        log.info(f"预热参数记录已清除 | api_id={api_id} | 清除key数={count}")
    except Exception as e:
        log.error(f"清除预热参数失败 | api_id={api_id} | error={str(e)}")


async def _load_prewarm_params(api_id: int, only_today: bool = False) -> list:
    """读出某 API 记录的所有历史参数组合，供预热调度器使用。

    only_today=True 时只返回「今天被请求过」的参数（按 last_seen 判断）。
    用于「跨日停止预热」开关：0 点之后，昨天那批参数不再预热，
    直到当天有真实请求把它们的 last_seen 刷新到今天，才重新纳入预热。
    """
    if not settings.redis.enabled:
        return []
    try:
        import datetime
        today_start_ts = int(
            datetime.datetime.combine(
                datetime.date.today(), datetime.time(0, 0, 0)
            ).timestamp()
        )
        r = _get_redis()
        all_items = await r.hgetall(_prewarm_params_key(api_id))
        out = []
        for _, v in all_items.items():
            try:
                d = json.loads(v)
                if only_today and int(d.get("last_seen", 0)) < today_start_ts:
                    continue   # 今天还没被请求过，跳过
                out.append(d.get("params") or {})
            except Exception:
                continue
        return out
    except Exception as e:
        log.error(f"读取预热参数失败 | api_id={api_id} | error={str(e)}")
        return []


def _parse_sql_params(sql_template: str, params: dict, api_params: list) -> Tuple[str, dict]:
    """
    把 SQL 模板加工成最终可执行的 SQL：
      Step 1. 模板渲染（$if / $for / #{}）
      Step 2. 收集占位符（含 |modifier 后缀），补默认值、校验必填
      Step 3. 应用 modifier:
              - :name|like        -> 值前后加 %，转义内部 % _ 反斜杠
              - :name|like_left   -> 前缀 %（"...匹配"）
              - :name|like_right  -> 后缀 %（"匹配..."）
              - :name|raw         -> 不转换（等同于 :name）
      Step 4. 集合值自动展开为 IN (:name__0, :name__1, ...)

    LIKE 转义说明: 用户输入 50%off 不会被当成 LIKE 通配符，会被自动转义。
    """
    from app.services.sql_template import (
        extract_placeholders_with_modifiers, sub_outside_literals,
    )

    # Step 1: 渲染
    #   v1.5: 传入 tpl_binds 收集器启用 :{expr} 安全绑定插值
    #   （循环变量的嵌套字段等任意表达式的值 → 自动生成 :__tpl_bN 占位符并参数化绑定）
    tpl_binds: dict = {}
    try:
        rendered = render_template(sql_template, params, collect_binds=tpl_binds)
    except SqlTplError as e:
        raise ValueError(f"SQL 模板渲染失败: {e}")
    if tpl_binds:
        # 生成的绑定值并入参数（键为 __tpl_bN，不会与用户参数冲突）
        params = {**params, **tpl_binds}

    # Step 2: 收集占位符（含 modifier）
    placeholder_infos = extract_placeholders_with_modifiers(rendered)
    placeholder_names = []
    seen_names = set()
    for name, _, _ in placeholder_infos:
        if name not in seen_names:
            seen_names.add(name)
            placeholder_names.append(name)

    # 给每个基础名取值（默认值 / 必填校验）
    bound_base: dict = {}
    for name in placeholder_names:
        if name in params and params[name] is not None:
            bound_base[name] = params[name]
            continue
        ap = next((p for p in api_params if p.name == name), None)
        if ap is None:
            bound_base[name] = None
            continue
        if ap.default_value not in (None, ""):
            bound_base[name] = ap.default_value
        elif ap.required:
            raise ValueError(f"缺少必填参数: {name}")
        else:
            bound_base[name] = None

    # Step 3: modifier 处理 —— 把所有 :name 和 :name|mod 一次性替换
    final_bound: dict = {}
    # 已为某 (name, mod) 生成过的目标 key（避免重复声明）
    modifier_keys: dict = {}

    def _ensure_modifier_key(name: str, mod: str | None) -> str:
        """对 (name, mod) 计算最终绑定 key，写入 final_bound。"""
        key = (name, mod)
        if key in modifier_keys:
            return modifier_keys[key]
        base_value = bound_base.get(name)
        if mod is None or mod == "raw":
            # 原值保留（IN 展开稍后处理）
            target_key = name
            # 标量值才直接写入 final_bound，list 值留到 Step 4 展开
            if not isinstance(base_value, (list, tuple, set)):
                final_bound[target_key] = base_value
        elif mod in ("like", "like_left", "like_right"):
            target_key = f"{name}__{mod}"
            if base_value is None:
                final_bound[target_key] = None
            else:
                sv = _escape_like(str(base_value))
                if mod == "like":
                    final_bound[target_key] = f"%{sv}%"
                elif mod == "like_left":
                    final_bound[target_key] = f"%{sv}"
                else:  # like_right
                    final_bound[target_key] = f"{sv}%"
        else:
            raise ValueError(f"未知的占位符 modifier: |{mod}（仅支持 like / like_left / like_right / raw）")
        modifier_keys[key] = target_key
        return target_key

    # 用 callback 一次性替换 :name 或 :name|mod
    # v1.5: 仅替换字符串/注释之外的占位符（'%H:%i' 这类字面量里的冒号不再被误伤），
    #        并支持 \: 转义（\:xxx 不作为占位符）
    final_sql = sub_outside_literals(
        r"(?<!\\):([a-zA-Z_][a-zA-Z0-9_]*)(?:\|([a-zA-Z_][a-zA-Z0-9_]*))?",
        lambda m: ":" + _ensure_modifier_key(m.group(1), m.group(2)),
        rendered,
    )

    # Step 4: 集合类型展开 → IN
    # 此时 final_sql 中的 :name 已经是无 modifier 的最终名（modifier 走 :name__mod）
    # 还需要把那些 base 值是 list 的 :name 展开成 IN (...)
    for name, value in bound_base.items():
        if not isinstance(value, (list, tuple, set)):
            continue
        # 看看是否 SQL 里还在用纯 :name（即 base modifier=None 的形式）
        if name not in final_bound and (name, None) not in modifier_keys:
            # 没人用，跳过
            continue
        # 移除占位（list 不能直接绑定到单 placeholder）
        final_bound.pop(name, None)

        value = list(value)
        if len(value) > MAX_IN_EXPANSION:
            raise ValueError(
                f"参数 {name} 的数组长度 {len(value)} 超过 IN 展开上限 {MAX_IN_EXPANSION}，"
                "请缩小查询范围或改用临时表方案"
            )
        # 用户模板里 IN (:name) 和 IN :name 两种写法都支持：
        # 先替换「已带括号」的形式（吸收原括号，避免生成 IN ((a,b)) 被 MySQL
        # 当成行构造器报错），再替换裸占位符形式（补上括号）。
        wrapped_pat = rf"\(\s*(?<!\\):{re.escape(name)}\b(?!__)\s*\)"
        bare_pat = rf"(?<!\\):{re.escape(name)}\b(?!__)"
        if not value:
            # 空集合：替换成 (NULL) 避免 SQL 语法错误
            final_sql = sub_outside_literals(wrapped_pat, "(NULL)", final_sql)
            final_sql = sub_outside_literals(bare_pat, "(NULL)", final_sql)
            continue
        expanded_names = [f"{name}__{i}" for i in range(len(value))]
        in_clause = "(" + ", ".join(f":{n}" for n in expanded_names) + ")"
        final_sql = sub_outside_literals(wrapped_pat, in_clause, final_sql)
        final_sql = sub_outside_literals(bare_pat, in_clause, final_sql)
        for n, v in zip(expanded_names, value):
            final_bound[n] = v

    if _is_debug():
        log.debug(f"SQL 参数绑定完成 | placeholders={placeholder_names} | bound_keys={list(final_bound.keys())}")
    return final_sql, final_bound


def _escape_like(s: str) -> str:
    r"""转义 LIKE 模式里的 % _ \，避免用户输入被误解析为通配符。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_PCT2_SENTINEL = "\x00__PCT2__\x00"


def _escape_percent(s: str) -> str:
    """把 % 转义成 %% 供 pymysql 格式化，但保留已有的 %% 不重复转义。

    背景: aiomysql/pymysql 在传入绑定参数时会对 SQL 做 % 格式化，
    SQL 里的字面 %（如 DATE_FORMAT 的 '%H'、LIKE '%x%'）会触发
    "unsupported format character" 错误。历史上能跑通的 SQL 只可能
    没有 % 或已手写 %%，这两种情况本函数均保持原行为不变。
    """
    if "%" not in s:
        return s
    s = s.replace("%%", _PCT2_SENTINEL)
    s = s.replace("%", "%%")
    return s.replace(_PCT2_SENTINEL, "%%")


_PYFORMAT_PLACEHOLDER_RE = re.compile(r"(?<!\\):(\w+)")


def _to_pyformat(sql: str) -> str:
    r"""把 :name 占位符转换为 pymysql 的 %(name)s 风格 (v1.5 字面量感知版)。

    规则:
      - 仅转换字符串/注释/反引号标识符 *之外* 的 :name
        —— '%H:%i'、'{"a":{"b":1}}' 等字面量内的冒号原样保留，
           不再需要用中文全角冒号来规避
      - \: 转义还原为字面 :（模板里写 \:xxx 表示不想当占位符的冒号）
      - 字面 % 自动转义为 %%（已有 %% 保留），避免 pymysql 格式化报错
    """
    from app.services.sql_template import split_literals

    parts = split_literals(sql)
    for i, p in enumerate(parts):
        if i % 2:
            # 字符串/注释字面量: 只做 % 转义，不动冒号
            parts[i] = _escape_percent(p)
        else:
            # 代码段: 先转义 %，再转换占位符（新生成的 %(name)s 不能被转义）
            p = _escape_percent(p)
            p = _PYFORMAT_PLACEHOLDER_RE.sub(r"%(\1)s", p)
            p = p.replace("\\:", ":")   # \: 转义还原
            parts[i] = p
    return "".join(parts)


_INT_RE = re.compile(r"-?\d+")


def _coerce_param_types(params: dict, api_params) -> None:
    """按 ApiParameter 声明类型对字符串值做宽松转换（原地修改）。

    - Query / Header 参数天然是字符串，GET 请求声明了 number/boolean/array
      的参数此前不会被转换（v2.0.1 网关中的转换误写死在 '/weibo/comment' 上）；
      现在统一在引擎层按 API 自己的参数定义转换。
    - 宽松策略：转换失败保留原值，不报错（数据库侧通常也能做隐式转换），
      避免对存量调用产生行为冲击。
    """
    for ap in api_params:
        v = params.get(ap.name)
        if not isinstance(v, str):
            continue
        t = (ap.param_type or "string").lower()
        sv = v.strip()
        try:
            if t in ("number", "int", "integer"):
                params[ap.name] = int(sv) if _INT_RE.fullmatch(sv) else float(sv)
            elif t in ("boolean", "bool"):
                low = sv.lower()
                if low in ("true", "1", "yes", "on"):
                    params[ap.name] = True
                elif low in ("false", "0", "no", "off", ""):
                    params[ap.name] = False
            elif t in ("array", "object", "json"):
                parsed = json.loads(sv)
                params[ap.name] = parsed
        except Exception:
            pass  # 保留原值


def _validate_nested_params(params: dict, api_params) -> None:
    """按 item_schema 递归校验嵌套参数结构 (v2.1)。

    item_schema JSON 结构::
        {"item_type": "object",            # array 时元素类型: string/number/boolean/object
         "children": [                     # object / array<object> 的字段定义（可递归）
            {"name": "...", "param_type": "string", "required": true,
             "item_schema": {...}}         # 子字段还可以继续嵌套
         ]}

    只校验「必填字段存在且非空」与「array/object 的容器类型」，
    值类型宽松（数据库侧可隐式转换）。schema 解析失败静默跳过（宽容存量）。
    """
    def _walk(value, schema: dict, path: str):
        if not isinstance(schema, dict):
            return
        item_type = (schema.get("item_type") or "object").lower()
        children = schema.get("children") or []
        if isinstance(value, list):
            for i, item in enumerate(value):
                if item_type == "object":
                    if not isinstance(item, dict):
                        raise ValueError(f"参数 {path}[{i}] 应为对象，当前: {type(item).__name__}")
                    _check_children(item, children, f"{path}[{i}]")
        elif isinstance(value, dict):
            _check_children(value, children, path)

    def _check_children(obj: dict, children: list, path: str):
        for child in children:
            if not isinstance(child, dict):
                continue
            cname = child.get("name")
            if not cname:
                continue
            cval = obj.get(cname)
            if child.get("required") and (cval is None or cval == "" or cval == []):
                raise ValueError(f"缺少必填参数: {path}.{cname}")
            ctype = (child.get("param_type") or "string").lower()
            if cval is not None:
                if ctype == "array" and not isinstance(cval, list):
                    raise ValueError(f"参数 {path}.{cname} 应为数组，当前: {type(cval).__name__}")
                if ctype == "object" and not isinstance(cval, dict):
                    raise ValueError(f"参数 {path}.{cname} 应为对象，当前: {type(cval).__name__}")
            sub = child.get("item_schema")
            if sub and cval is not None:
                _walk(cval, sub, f"{path}.{cname}")

    for ap in api_params:
        raw = getattr(ap, "item_schema", "") or ""
        if not raw.strip():
            continue
        value = params.get(ap.name)
        if value is None:
            continue
        try:
            schema = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            log.debug(f"item_schema 解析失败，跳过嵌套校验 | param={ap.name}")
            continue
        _walk(value, schema, ap.name)


# ============================================================
# 单 SQL 模式 / 管线模式 分流
# ============================================================

def apply_param_defaults(params: dict, api_params) -> dict:
    """未传（或传 null）的参数补上配置的默认值，并按声明类型转换。返回新 dict。"""
    effective = dict(params or {})
    filled = {}
    for ap in api_params:
        if effective.get(ap.name) is None and ap.default_value not in (None, ""):
            filled[ap.name] = ap.default_value
    if filled:
        effective.update(filled)
        _coerce_param_types(effective, [ap for ap in api_params if ap.name in filled])
    return effective


async def _execute_single_sql_mode(api_config, params, api_params, datasource):
    """原有逻辑：渲染 sql_template + 执行 + 返回结果。"""
    from app.services.trace_context import get_trace
    tctx = get_trace()

    # 空 SQL 模板保护：SQL 类型 API 若没写模板，给明确报错而不是 None 崩溃
    if not (api_config.sql_template or "").strip():
        raise Exception("该 API 未配置 SQL 模板")

    # DDL 拦截
    if _check_ddl(api_config.sql_template):
        raise Exception("禁止执行 DDL 操作")

    # 参数默认值对模板条件可见（v2.21，query.defaults_visible_in_template，默认关闭保持原行为）：
    # 原来单 SQL 模式下默认值只用于填充占位符，$if$/$for$ 里看不到；流水线/插件模式则看得到。
    # 开启后两种模式一致。开启前可用 scripts/check_param_defaults.py 列出结果会变化的 API。
    if getattr(settings.query, "defaults_visible_in_template", False):
        params = apply_param_defaults(params, api_params)

    if tctx:
        tctx.mark_render_start()
    sql, bound_params = _parse_sql_params(api_config.sql_template, params, api_params)
    if tctx:
        tctx.mark_render_end()
        tctx.set_rendered_sql(sql)
    if _is_debug():
        log.debug(f"SQL 模板 | api_id={api_config.id} | sql={sql[:200]} | bound={bound_params}")

    password = decrypt_value(datasource.password_encrypted) if datasource.password_encrypted else ""

    if datasource.type in MYSQL_COMPATIBLE_TYPES:
        if tctx:
            try:
                tctx.set_executed_sql(_to_pyformat(sql), bound_params)
            except Exception:
                pass
        _q0 = time.time()
        data = await _execute_mysql(
            datasource, password, sql, bound_params,
            timeout=api_config.timeout, max_rows=api_config.max_rows,
        )
        if tctx:
            tctx.add_query_time((time.time() - _q0) * 1000)
    elif datasource.type == "redis":
        _q0 = time.time()
        data = await _execute_redis_command(datasource, password, sql, bound_params)
        if tctx:
            tctx.add_query_time((time.time() - _q0) * 1000)
    else:
        raise Exception(f"不支持的数据源类型: {datasource.type}")
    return data


async def _execute_plugin_mode(api_config, params, api_params, db, primary_datasource):
    """走 Python 插件模式 (v1.9+)：执行 plugin_code 里的 main(params, ctx)。

    给插件注入一个同步的 ctx.query(datasource, sql, params)，内部把异步 DB 查询
    桥接成同步（插件在独立线程跑，用 run_coroutine_threadsafe 回到主事件循环）。
    """
    from app.services.plugin_executor import execute_plugin, PluginError

    # 默认参数兜底
    effective_params = dict(params or {})
    for ap in api_params:
        if ap.name not in effective_params and ap.default_value:
            effective_params[ap.name] = ap.default_value

    main_loop = asyncio.get_event_loop()

    async def _resolve_ds(name_or_id):
        from app.models.models import DataSource
        from app.services import ds_scope
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and str(name_or_id).isdigit()):
            r = await db.execute(select(DataSource).where(DataSource.id == int(name_or_id)))
        else:
            r = await db.execute(select(DataSource).where(DataSource.name == name_or_id))
        ds = r.scalar_one_or_none()
        ds_scope.ensure_allowed(ds, await ds_scope.project_code(db, api_config.project_id))
        return ds

    async def _run_query_async(datasource, sql, qparams):
        if _check_ddl(sql):
            raise Exception("禁止执行 DDL 操作")
        ds = await _resolve_ds(datasource)
        if ds is None:
            raise Exception(f"数据源不存在: {datasource!r}")
        # 复用引擎的占位符解析（:name -> 参数化），但不强制 api_params 定义
        final_sql, bound = _parse_sql_params(sql, qparams or {}, [])
        password = decrypt_value(ds.password_encrypted) if ds.password_encrypted else ""
        if ds.type in MYSQL_COMPATIBLE_TYPES:
            return await _execute_mysql(
                ds, password, final_sql, bound,
                timeout=api_config.timeout, max_rows=api_config.max_rows,
            )
        if ds.type == "redis":
            return await _execute_redis_command(ds, password, final_sql, bound)
        raise Exception(f"不支持的数据源类型: {ds.type}")

    def query_sync(datasource, sql, qparams):
        """供插件线程调用的同步查询：把协程丢回主事件循环并阻塞等结果。"""
        fut = asyncio.run_coroutine_threadsafe(
            _run_query_async(datasource, sql, qparams), main_loop
        )
        return fut.result(timeout=api_config.timeout)

    # 解析插件引用的库：脚本里以 `# require: name1, name2` 声明（可多行）
    lib_code = ""
    try:
        import re as _re
        from app.models.models import PluginLibrary
        refs = []
        for line in (api_config.plugin_code or "").splitlines():
            m = _re.match(r"\s*#\s*require\s*:\s*(.+)", line, _re.IGNORECASE)
            if m:
                refs += [x.strip() for x in m.group(1).split(",") if x.strip()]
        if refs:
            seen = set()
            uniq = [x for x in refs if not (x in seen or seen.add(x))]
            r = await db.execute(
                select(PluginLibrary).where(
                    PluginLibrary.name.in_(uniq),
                    PluginLibrary.is_enabled == True,
                )
            )
            found = {lib.name: lib.code for lib in r.scalars().all()}
            missing = [n for n in uniq if n not in found]
            if missing:
                raise Exception(f"引用的插件库不存在或已禁用: {', '.join(missing)}")
            # 按引用顺序拼接
            lib_code = "\n\n".join(f"# ===== plugin library: {n} =====\n{found[n]}" for n in uniq)
    except Exception as e:
        raise Exception(f"加载插件库失败: {e}")

    try:
        data = await execute_plugin(
            api_config.plugin_code,
            effective_params,
            query_sync=query_sync,
            api_id=api_config.id,
            timeout=api_config.timeout,
            lib_code=lib_code,
        )
    except PluginError as e:
        raise Exception(str(e))
    return data


async def _execute_pipeline_mode(api_config, params, api_params, db, primary_datasource):
    """走多步管线。

    primary_datasource: API 主数据源（向后兼容），管线步骤若没显式指定 datasource
                      会用这个作 fallback。
    """
    from app.services.pipeline_executor import execute_pipeline, parse_pipeline, PipelineError

    try:
        steps = parse_pipeline(api_config.pipeline_steps)
    except PipelineError as e:
        raise Exception(f"管线配置错误: {e}")

    log.debug(f"开始执行管线 | api_id={api_config.id} | steps={len(steps)}")

    # 默认参数兜底
    effective_params = dict(params or {})
    for ap in api_params:
        if ap.name not in effective_params and ap.default_value:
            effective_params[ap.name] = ap.default_value

    async def datasource_resolver(name_or_id):
        # 支持按 id（int 或数字字符串）或 name 查找
        from app.models.models import DataSource
        from app.services import ds_scope
        if isinstance(name_or_id, int) or (isinstance(name_or_id, str) and name_or_id.isdigit()):
            r = await db.execute(select(DataSource).where(DataSource.id == int(name_or_id)))
        else:
            r = await db.execute(select(DataSource).where(DataSource.name == name_or_id))
        ds = r.scalar_one_or_none()
        ds_scope.ensure_allowed(ds, await ds_scope.project_code(db, api_config.project_id))
        return ds

    async def sql_runner(ds, sql, bound, timeout, max_rows):
        if _check_ddl(sql):
            raise Exception("禁止执行 DDL 操作")
        password = decrypt_value(ds.password_encrypted) if ds.password_encrypted else ""
        if ds.type in MYSQL_COMPATIBLE_TYPES:
            return await _execute_mysql(ds, password, sql, bound, timeout=timeout, max_rows=max_rows)
        if ds.type == "redis":
            return await _execute_redis_command(ds, password, sql, bound)
        raise Exception(f"不支持的数据源类型: {ds.type}")

    return_step = None
    # 允许 API 配置在 pipeline_steps JSON 顶部放一个 _return 字段；
    # 或者用 step.is_return=true 标记某一步
    for step in steps:
        if step.get("is_return"):
            return_step = step["name"]

    data, step_results = await execute_pipeline(
        steps, effective_params,
        datasource_resolver=datasource_resolver,
        sql_runner=sql_runner,
        timeout=api_config.timeout,
        max_rows=api_config.max_rows,
        return_step=return_step,
    )
    if _is_debug():
        log.debug(f"管线执行完成 | api_id={api_config.id} | steps_summary={step_results}")
    return data


async def execute_api(
    api_config: ApiConfig,
    params: dict,
    client_ip: str,
    db: AsyncSession,
    call_source: str = "gateway",
    trace_id: str = "",
    api_params=None,
    datasource=None,
    raw_result: bool = False,
) -> dict:
    """
    执行动态 API 调用
    返回标准响应格式 {"status": True/False, "data": ..., "msg": ...}

    api_params / datasource: 调用方已查好时直接传入（网关的接口配置缓存），
    省掉每次请求各一次系统库查询；不传则照旧在这里查。
    raw_result: 为 True 时，开了缓存的 API 的 data 返回 CachedResult（已序列化好的 JSON
    字节），由网关直接拼进响应，不再解析/重新序列化（v2.20）。
    """
    from app.services import result_cache as rc
    start_time = time.time()

    # 预热由后台调度器触发，日志加统一前缀与正常调用区分开，
    # 便于排查时一眼看出「这条慢查询是后台预热跑的，不是用户请求」
    _is_prewarm = (call_source == "prewarm")
    _tag = "[预热] " if _is_prewarm else ""

    # v2.19: 每请求的过程日志降为 DEBUG，INFO 只保留网关层一条汇总（网关响应）
    if _is_debug():
        log.debug(f"{_tag}开始执行 API | api_id={api_config.id} | name={api_config.name} | url_path={api_config.url_path} | method={api_config.method} | client_ip={client_ip} | trace_id={trace_id}")
        log.debug(f"{_tag}请求参数 | api_id={api_config.id} | params={json.dumps(params, default=str, ensure_ascii=False)[:1000]}")

    # v2.4: 关键节点写入 trace 上下文（由网关层负责建档与落库）
    from app.services.trace_context import get_trace
    tctx = get_trace()

    cache_key = None
    flight = None   # 本请求作为「领头人」去查库时的 single-flight 句柄
    try:
        # 0. HTML(静态页面) 类型：直接返回渲染后的页面，不走 SQL/数据源/DDL 检查。
        #    静态页面 API 本就没有 sql_template，若继续往下会因 sql_template 为空/None 报错。
        if (getattr(api_config, "api_type", "sql") or "sql").lower() == "html":
            from app.api.gateway import _build_html_page
            html_page = _build_html_page(api_config, params)
            if tctx:
                tctx.set_row_count(0)
            return {"status": True, "data": html_page, "msg": "html", "_is_html": True}

        # 1. 限流检查
        if api_config.rate_limit_enabled:
            log.debug(f"限流检查 | api_id={api_config.id} | qps_limit={api_config.rate_limit_qps}")
            if not rate_limiter.check(api_config.id, api_config.rate_limit_qps):
                raise Exception("请求过于频繁，请稍后重试")

        # 2. DDL 拦截（仅对有 SQL 模板的类型；html/plugin 可能没有 sql_template）
        _sql_tpl = api_config.sql_template or ""
        log.debug(f"DDL 检查 | api_id={api_config.id} | sql_preview={_sql_tpl[:100]}...")
        if _check_ddl(_sql_tpl):
            raise Exception("禁止执行 DDL 操作")

        # 3. 缓存检查
        #    cache_key 只在这里按「原始入参」算一次，读缓存和写缓存共用。
        #    之前写缓存时重新计算，而那时 params 已被 _coerce_param_types 原地转换过
        #    （GET 查询参数 "7" -> 7），读写 key 对不上，GET 请求的缓存永远不命中。
        # 数据同步（写入类）API 不走缓存
        _is_sync = (getattr(api_config, "api_type", "sql") or "sql").lower() == "sync"
        if api_config.cache_enabled and not _is_sync:
            cache_key = _build_cache_key(api_config.id, params, getattr(api_config, "version", None))
            # 开了自动预热的 API：记下这次的参数组合，供后台调度器定期回填缓存。
            # 放在缓存命中判断之前，保证命中时也会刷新 last_seen（表示这个参数还活跃）。
            # 注意：即使配了「预热参数覆盖」也要记录 —— 覆盖是以历史参数为底，
            # 只替换其中的时间等字段，业务字段（如 supplierId）仍取自历史请求。
            # 但预热自身触发的执行不再记录，否则「覆盖后的参数」会被当成新的历史参数
            # 反复写回，导致参数集合自我膨胀。
            if getattr(api_config, "cache_prewarm", False) and call_source != "prewarm":
                await _record_prewarm_params(
                    api_config.id, params, settings.cache.prewarm_max_params
                )
            log.debug(f"缓存检查 | api_id={api_config.id} | cache_key={cache_key}")
            # 预热的语义是「不读缓存、强制查最新结果、再写回同一个 key」。
            # 若预热也走缓存命中，就只会在缓存彻底过期后才真正查库，
            # 那时用户已经先撞上一次冷查询了，预热就失去意义。
            if call_source == "prewarm":
                log.debug(f"预热执行：跳过缓存读取，强制查询最新结果 | api_id={api_config.id}")
                cached = None
            else:
                cached = await _get_cache(cache_key)
            if cached is not None:
                elapsed = (time.time() - start_time) * 1000
                log.debug(f"缓存命中，直接返回 | api_id={api_config.id} | elapsed={elapsed:.2f}ms")
                if tctx:
                    tctx.set_cache_hit(True)
                    tctx.set_row_count(cached.rows)
                return {"status": True, "data": cached if raw_result else cached.to_python(), "msg": "from cache"}

            # 3.5 并发未命中合并 (v2.20)：同一 key 已有请求在查库，就等它的结果，不再重复查
            if call_source != "prewarm":
                waiting = rc.flight_get(cache_key)
                if waiting is not None:
                    entry = await asyncio.shield(waiting)
                    if tctx:
                        tctx.set_row_count(entry.rows)
                    return {"status": True, "data": entry if raw_result else entry.to_python(), "msg": ""}
                flight = rc.flight_start(cache_key)

        # 4. 获取数据源
        #    插件模式可以不依赖主数据源（纯计算 / 只调外部 HTTP / 自行 ctx.query 指定源），
        #    因此插件模式下数据源缺失不报错；其余模式保持强制。
        is_plugin = getattr(api_config, "api_type", "sql") == "plugin" and \
            (getattr(api_config, "plugin_code", "") or "").strip()

        if datasource is None and api_config.datasource_id:
            ds_result = await db.execute(
                select(DataSource).where(DataSource.id == api_config.datasource_id)
            )
            datasource = ds_result.scalar_one_or_none()

        if not is_plugin:
            if not api_config.datasource_id:
                raise Exception("未配置数据源")
            if not datasource:
                raise Exception("数据源不存在")

        # 数据源可用项目范围 (v2.21)
        from app.services import ds_scope
        proj_code = await ds_scope.project_code(db, api_config.project_id)
        ds_scope.ensure_allowed(datasource, proj_code)

        if datasource:
            if _is_debug():
                log.debug(f"数据源 | api_id={api_config.id} | ds_id={datasource.id} | ds_name={datasource.name} | type={datasource.type} | host={datasource.host}:{datasource.port}")

        # 4.5 数据同步模式 (v2.17+)：入参是固定的 tableName/pkId/data 三件套，
        #     不走 ApiParameter 那套参数定义与校验，直接交给同步执行器。
        #     调用方身份(必须超管)已在网关层校验，这里只管执行。
        if (getattr(api_config, "api_type", "sql") or "sql").lower() == "sync":
            from app.services.data_sync import execute_sync, SyncError
            if not datasource:
                raise Exception("数据同步 API 必须配置数据源")
            if datasource.type not in MYSQL_COMPATIBLE_TYPES:
                raise Exception(f"数据同步暂只支持 MySQL 系数据源，当前为 {datasource.type}")
            password = decrypt_value(datasource.password_encrypted)
            try:
                sync_result = await execute_sync(
                    api_config, params, datasource, password,
                    api_config.timeout or settings.query.default_timeout,
                )
            except SyncError as e:
                # 校验类错误：直接把原因返回给调用方，便于对方修正请求
                elapsed = (time.time() - start_time) * 1000
                log.warning(f"{_tag}数据同步校验失败 | api_id={api_config.id} | error={str(e)}")
                return {"status": False, "data": None, "msg": str(e)}
            elapsed = (time.time() - start_time) * 1000
            log.info(
                f"{_tag}数据同步完成 | api_id={api_config.id} | name={api_config.name} | "
                f"elapsed={elapsed:.2f}ms | {sync_result}"
            )
            # 同步类不写缓存（写入型操作缓存没有意义）
            return {"status": True, "data": sync_result, "msg": "同步完成"}

        # 5. 获取参数定义
        if api_params is None:
            params_result = await db.execute(
                select(ApiParameter).where(ApiParameter.api_id == api_config.id)
            )
            api_params = params_result.scalars().all()
        if _is_debug():
            log.debug(f"参数定义 | api_id={api_config.id} | 参数数={len(api_params)} | 参数名={[p.name for p in api_params]}")

        # 5.5 按声明类型做宽松转换（主要针对 Query/Header 里以字符串到达的参数）
        _coerce_param_types(params, api_params)

        # 6. 参数校验
        for ap in api_params:
            if ap.required and ap.name not in params:
                if not ap.default_value:
                    raise ValueError(f"缺少必填参数: {ap.name}")

        # 6.5 嵌套结构校验（仅对配置了 item_schema 的 array/object 参数生效，
        #     存量未配置的参数完全不受影响）
        _validate_nested_params(params, api_params)

        # 7. 分流：插件模式 > 管线模式 > 单 SQL 模式
        pipeline_raw = getattr(api_config, "pipeline_steps", None)
        if is_plugin:
            data = await _execute_plugin_mode(
                api_config, params, api_params, db, datasource,
            )
        elif pipeline_raw and pipeline_raw.strip():
            data = await _execute_pipeline_mode(
                api_config, params, api_params, db, datasource,
            )
        else:
            data = await _execute_single_sql_mode(
                api_config, params, api_params, datasource,
            )

        # 9. 写入缓存（沿用第 3 步按原始入参算出的 key）
        #    v2.20: 结果在这里序列化（并按需预压缩）一次，缓存和响应共用
        entry = None
        if cache_key:
            entry = rc.CachedResult.from_data(data)
            ttl = api_config.cache_ttl or settings.cache.default_ttl
            if data is None or (isinstance(data, list) and len(data) == 0):
                await _set_cache(cache_key, entry, settings.cache.null_ttl)
            else:
                await _set_cache(cache_key, entry, ttl)
            if flight is not None:
                rc.flight_finish(cache_key, flight, entry)
                flight = None

        elapsed = (time.time() - start_time) * 1000
        if tctx:
            tctx.set_row_count(len(data) if isinstance(data, list) else 0)

        if elapsed > settings.query.slow_query_threshold:
            log.warning(f"{_tag}慢查询 | api_id={api_config.id} | name={api_config.name} | elapsed={elapsed:.2f}ms | threshold={settings.query.slow_query_threshold}ms")

        log.debug(f"{_tag}API 执行成功 | api_id={api_config.id} | name={api_config.name} | elapsed={elapsed:.2f}ms | 返回行数={len(data) if isinstance(data, list) else 'N/A'}")
        return {"status": True, "data": entry if (raw_result and entry is not None) else data, "msg": ""}

    except Exception as e:
        elapsed = (time.time() - start_time) * 1000
        if flight is not None:
            # 等着这次结果的并发请求一起收到同样的错误
            rc.flight_finish(cache_key, flight, error=e)
            flight = None
        if tctx:
            import traceback
            tctx.set_error_stack(traceback.format_exc())
        log.error(f"{_tag}API 执行失败 | api_id={api_config.id} | name={api_config.name} | elapsed={elapsed:.2f}ms | error={str(e)}")
        return {"status": False, "data": None, "msg": str(e)}
    finally:
        # 被取消等非 Exception 的退出：也要通知等待者，不能让它们一直挂着
        if flight is not None:
            rc.flight_finish(cache_key, flight, error=RuntimeError("查询已取消，请重试"))


# ========== 业务数据源连接池 (v2.15+) ==========
# 原实现每次查询都 aiomysql.connect() 新建连接、查完 close()，零复用。
# 单请求时建连开销(TCP握手+认证，走公网/内网 RDS 通常 20~100ms)被查询耗时掩盖，
# 感觉不出来；但并发上来后就是每个请求都重新建连，叠加 --workers N 后对 RDS 的
# 瞬时连接压力是 N 倍，很容易撞上 max_connections / 建连速率限制而卡顿。
#
# 这里改为按数据源缓存连接池：连接建一次反复用，池大小取数据源配置的 pool_size。
_mysql_pools: dict = {}
_pool_lock = asyncio.Lock()


def _resolve_mysql_driver() -> str:
    """业务数据源 MySQL 驱动 (v2.20)：默认 asyncmy（C 扩展，解析结果集比纯 Python 的
    aiomysql 快 4~6 倍，返回值类型、参数转义、报错信息经逐项比对一致）；
    未安装或配置 query.mysql_driver: aiomysql 时使用 aiomysql。"""
    want = (getattr(settings.query, "mysql_driver", "asyncmy") or "asyncmy").lower()
    if want == "asyncmy":
        try:
            import asyncmy  # noqa: F401
            return "asyncmy"
        except ImportError:
            log.warning("未安装 asyncmy，业务数据源改用 aiomysql")
    return "aiomysql"


MYSQL_DRIVER = _resolve_mysql_driver()


def _pool_alive(pool) -> bool:
    return pool is not None and not getattr(pool, "_closed", False)


def _pool_key(datasource: DataSource) -> str:
    """同一个 host:port/db + 账号 视为同一个池。
    带上 updated_at，数据源配置改过之后会自然换用新池，不会一直用旧连接。"""
    return (
        f"{datasource.id}:{datasource.host}:{datasource.port}:"
        f"{datasource.database_name}:{datasource.username}:"
        f"{getattr(datasource, 'updated_at', '')}"
    )


async def _get_mysql_pool(datasource: DataSource, password: str):
    """取得(或创建)某数据源的连接池。"""
    key = _pool_key(datasource)
    pool = _mysql_pools.get(key)
    if _pool_alive(pool):
        return pool

    async with _pool_lock:
        # 双重检查：可能在等锁期间已被别的协程创建
        pool = _mysql_pools.get(key)
        if _pool_alive(pool):
            return pool

        size = int(getattr(datasource, "pool_size", 10) or 10)
        if MYSQL_DRIVER == "asyncmy":
            import asyncmy as _driver
        else:
            import aiomysql as _driver
        # minsize 保持较小，避免空闲时占着一堆连接；maxsize 才是并发上限
        db_kw = {"database" if MYSQL_DRIVER == "asyncmy" else "db": datasource.database_name}
        pool = await _driver.create_pool(
            host=datasource.host,
            port=datasource.port,
            user=datasource.username,
            password=password,
            **db_kw,
            charset="utf8mb4",
            minsize=1,
            maxsize=max(2, size),
            # 回收空闲连接，避免被 MySQL 的 wait_timeout 掐断后拿到坏连接
            pool_recycle=3600,
            autocommit=True,
        )
        _mysql_pools[key] = pool
        # 同一数据源修改配置后 key 会变（含 updated_at），旧池不会再被使用，关闭它（v2.20：原来一直泄漏）
        stale_prefix = f"{datasource.id}:"
        for old_key in [k for k in _mysql_pools if k != key and k.startswith(stale_prefix)]:
            old = _mysql_pools.pop(old_key)
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        log.info(
            f"业务数据源连接池已创建 | ds={datasource.name} | "
            f"{datasource.host}:{datasource.port}/{datasource.database_name} | maxsize={max(2, size)} | driver={MYSQL_DRIVER}"
        )
        return pool


async def close_all_mysql_pools():
    """应用关闭时释放所有业务连接池。"""
    async with _pool_lock:
        for key, pool in list(_mysql_pools.items()):
            try:
                pool.close()
                await pool.wait_closed()
            except Exception as e:  # noqa: BLE001
                log.warning(f"关闭连接池失败 | key={key} | error={str(e)}")
        _mysql_pools.clear()
    log.info("业务数据源连接池已全部关闭")


async def _execute_mysql(
    datasource: DataSource,
    password: str,
    sql: str,
    params: dict,
    timeout: int = 30,
    max_rows: int = 10000,
) -> list:
    """执行 MySQL 查询（走连接池，连接复用不再每次新建）

    v2.19:
      - 改用流式游标(SSDictCursor)：只从网络读 max_rows 行。原来的缓冲游标会先把
        整个结果集全部读进内存、逐行解码，再截取前 max_rows 行 —— SELECT 一张
        几十万行的表只为返回 1 万行，绝大部分时间和内存都浪费在丢弃的行上。
        结果超过 max_rows 时直接丢弃这条连接（服务端随之停止发送），不再读完剩余行。
      - 执行出错/超时的连接不再还回池里：超时被取消时连接正处在协议中途，
        还回去会让下一个请求拿到「半截结果」的坏连接。
    """
    import aiomysql

    if _is_debug():
        log.debug(f"MySQL 查询 | host={datasource.host}:{datasource.port} | db={datasource.database_name} | timeout={timeout}s")

    # 只读防护：检查渲染后真正要执行的 SQL（v2.20），在建池/借连接之前做
    violation = _readonly_violation(sql)
    if not violation:
        from app.services.ds_scope import schema_violation
        violation = schema_violation(sql, datasource)
    if violation:
        log.warning(f"拦截非只读 SQL | ds={datasource.name} | 原因={violation} | sql={sql[:200]}")
        raise Exception(violation)

    pool = await _get_mysql_pool(datasource, password)

    # 从池里借一条连接。池满时 acquire 会等待，这里同样受 timeout 约束，
    # 避免并发打满后无限期挂住。
    try:
        conn = await asyncio.wait_for(pool.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        # 原来这里抛出的 TimeoutError 消息为空，日志和接口返回里只有一个空 error
        raise Exception(
            f"等待数据源连接超时（{timeout}s）：数据源「{datasource.name}」连接池已满"
            f"（上限 {pool.maxsize}），请调大该数据源的连接池或降低并发"
        )

    # 将 :param 风格转为 %(param)s 风格 (MySQL 参数化)
    # v1.5: 字面量感知转换，见 _to_pyformat 说明
    mysql_sql = _to_pyformat(sql)
    if _is_debug():
        log.debug(f"MySQL SQL | sql={mysql_sql} | params={params}")

    async def _run():
        if MYSQL_DRIVER == "asyncmy":
            from asyncmy.cursors import SSCursor
            # asyncmy 的 SSDictCursor.fetchmany 返回的是元组（库的缺陷），
            # 这里用 SSCursor 取元组，再按 aiomysql DictCursor 的规则命名列
            cur = conn.cursor(SSCursor)
            await cur.execute(mysql_sql, params)
            names = _dict_column_names(getattr(cur._result, "fields", None) or [])  # noqa: SLF001
        else:
            cur = await conn.cursor(aiomysql.SSCursor)
            await cur.execute(mysql_sql, params)
            names = _dict_column_names(getattr(cur._result, "fields", None) or [])  # noqa: SLF001
        rows = await cur.fetchmany(max_rows)
        # 再探一行判断是否还有剩余；有剩余说明被 max_rows 截断
        truncated = (await cur.fetchone()) is not None if len(rows) >= max_rows else False
        if not truncated:
            await cur.close()
        return names, rows, truncated

    discard = True
    try:
        try:
            names, rows, truncated = await asyncio.wait_for(_run(), timeout=timeout)
        except asyncio.TimeoutError:
            raise Exception(f"查询超时（超过 {timeout}s）")
        # 截断时连接上还有没读完的行，直接丢弃这条连接，比把剩余行读完快得多
        discard = truncated
    finally:
        if discard:
            _discard_conn(conn)
        pool.release(conn)
        if discard and MYSQL_DRIVER == "aiomysql":
            # aiomysql 归还已关闭的连接时不会唤醒排队等连接的协程，这里补一次
            # （asyncmy 的 release 在任何情况下都会唤醒）
            asyncio.ensure_future(pool._wakeup())  # noqa: SLF001

    # 组装成字典并把特殊类型转为可序列化格式（一次完成，不再先建一遍 dict 再复制）
    result = [{k: _clean_value(v) for k, v in zip(names, row)} for row in rows]

    if _is_debug():
        log.debug(f"MySQL 查询结果 | rows={len(result)} | max_rows={max_rows} | truncated={truncated}")
    return result

def _discard_conn(conn) -> None:
    """关闭一条处在协议中途的连接，使连接池不再复用它。"""
    if MYSQL_DRIVER == "asyncmy":
        # asyncmy 的 close() 只关 socket、不把 connected 置为 False，连接池会把这条已关闭的
        # 连接当成可用连接收回，下一个请求拿到就报 2006 MySQL server has gone away（压测中实际出现）。
        # _close_on_cancel 是它为「读取中途被取消」准备的方法：关闭并标记为已断开。
        closer = getattr(conn, "_close_on_cancel", None)
        if closer is not None:
            closer()
            return
    conn.close()


def _dict_column_names(fields) -> list:
    """与 aiomysql/pymysql DictCursor 一致的列名：重名列从第二个起用「表名.列名」。"""
    names, seen = [], set()
    for f in fields:
        name = f.name
        if name in seen:
            name = f"{f.table_name}.{name}"
        seen.add(f.name)
        names.append(name)
    return names


# 原样返回的类型（format_as_json 对它们本来就不做处理），跳过函数调用
_PASSTHROUGH_TYPES = (int, float, bool, type(None), __import__("decimal").Decimal)
_DATE_TYPES = (datetime.datetime, datetime.date)


def _clean_value(v):
    """单元格转可序列化值（规则同 v1.5：JSON 文本解析成结构、日期转 isoformat、bytes 解码）。
    最常见的几种类型内联处理，结果与 format_as_json + 类型转换完全一致。"""
    t = type(v)
    if t in _PASSTHROUGH_TYPES:
        return v
    if t is str:
        stripped = v.lstrip(" \t\n\r")
        if not stripped or stripped[0] not in _JSON_FIRST_CHARS:
            return v
        return _loads_json_text(v)
    if t in _DATE_TYPES:
        return v.isoformat()
    v = format_as_json(v)
    if isinstance(v, _DATE_TYPES):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode('utf-8', errors='replace')
    return v


# orjson 与标准库 json.loads 在几类边界写法上结果不同：NaN/Infinity（orjson 报错）、
# 超过 64 位的整数（orjson 会变成浮点数丢精度）、超大/超小指数（溢出处理不同）。
# 文本里出现这些写法的特征时直接用标准库，保证解析结果与原来逐值一致。
_STDLIB_JSON_HINT = re.compile(r"[NI]|\d{19}|[eE][+-]?\d{3}")


# orjson 解析失败、而标准库可能成功的只剩「反斜杠转义（如单独的代理对 \\ud800）」和
# 字符串本身含代理字符这两类；其余情况 orjson 失败即可判定不是 JSON
_STDLIB_RETRY_HINT = re.compile("[\\\\\ud800-\udfff]")

# 以字母开头的合法 JSON 只可能是这几个字面量（标准库 json.loads 的结果）
_JSON_LITERALS = {"true": True, "false": False, "null": None, "NaN": float("nan"), "Infinity": float("inf")}


def _loads_json_text(text):
    """解析 JSON 文本；不是合法 JSON 时原样返回（语义同原 format_as_json）。"""
    body = text.strip(" \t\n\r")
    if body[:1] in ("t", "f", "n", "N", "I"):
        return _JSON_LITERALS.get(body, text)
    if _STDLIB_JSON_HINT.search(text) is None:
        try:
            return _orjson.loads(text)
        except Exception:
            if _STDLIB_RETRY_HINT.search(text) is None:
                return text
    try:
        return json.loads(text)
    except Exception:
        return text


_JSON_FIRST_CHARS = frozenset('{["-0123456789tfnNI')


def format_as_json(data):
    """如果值是 JSON 文本则解析成结构，其余原样返回。

    v1.5 修正: 只对 str/bytes 尝试解析（datetime/Decimal/数字等直接跳过），
    解析失败降为 debug 日志 —— 原实现对每个非 dict 单元格都 json.loads 并打
    warning，一次上万行的查询会打出上万条无意义告警，且拖慢序列化。
    解析成功的返回值与原实现完全一致（含 "123"->123 等历史行为）。
    """
    if isinstance(data, dict):
        return data  # 已经是字典，直接返回
    if isinstance(data, str):
        # 快速路径 (v2.19)：合法 JSON 文本去掉前导空白后，首字符只可能是下面这些
        # （对象/数组/字符串/数字/true/false/null/NaN/Infinity）。
        # 绝大多数普通文本（中文、字母开头）不可能解析成功，直接跳过，
        # 省掉一次必然失败的 json.loads（抛异常的代价远高于一次判断）。
        stripped = data.lstrip(" \t\n\r")
        if not stripped or stripped[0] not in _JSON_FIRST_CHARS:
            return data
        return _loads_json_text(data)
    if isinstance(data, (bytes, bytearray)):
        try:
            return json.loads(data)
        except Exception:
            return data
    return data

# 开放数据 API 不应能执行的 Redis 管理/破坏性命令（v2.20）
_REDIS_FORBIDDEN = frozenset({
    "FLUSHALL", "FLUSHDB", "CONFIG", "SHUTDOWN", "DEBUG", "SLAVEOF", "REPLICAOF", "MIGRATE",
    "MODULE", "ACL", "CLIENT", "CLUSTER", "FAILOVER", "SAVE", "BGSAVE", "BGREWRITEAOF",
    "SWAPDB", "SCRIPT", "EVAL", "EVALSHA", "FUNCTION", "FCALL", "MONITOR", "SYNC", "PSYNC",
})


async def _execute_redis_command(
    datasource: DataSource,
    password: str,
    command_template: str,
    params: dict,
) -> Any:
    """执行 Redis 命令"""
    import redis.asyncio as aioredis

    log.debug(f"Redis 连接 | host={datasource.host}:{datasource.port} | db={datasource.database_name or 0}")

    r = aioredis.Redis(
        host=datasource.host,
        port=datasource.port,
        password=password or None,
        db=int(datasource.database_name or 0),
    )

    try:
        # 简单替换参数。按名称长度倒序替换（v2.20）：原来按字典顺序，
        # 先替换 :id 会把 :id2 改成「值+2」
        command = command_template
        for k in sorted(params, key=len, reverse=True):
            v = params[k]
            command = command.replace(f":{k}", str(v) if v is not None else "")

        parts = command.strip().split()
        if not parts:
            raise Exception("空的 Redis 命令")

        cmd = parts[0].upper()
        args = parts[1:]
        if cmd in _REDIS_FORBIDDEN:
            raise Exception(f"禁止通过 API 执行 Redis 管理命令: {cmd}")

        log.debug(f"Redis 命令 | cmd={cmd} | args={args}")

        result = await r.execute_command(cmd, *args)

        if isinstance(result, bytes):
            decoded = result.decode('utf-8', errors='replace')
            log.debug(f"Redis 结果 | type=bytes | value={decoded[:200]}")
            return decoded
        elif isinstance(result, list):
            decoded = [
                item.decode('utf-8', errors='replace') if isinstance(item, bytes) else item
                for item in result
            ]
            log.debug(f"Redis 结果 | type=list | count={len(decoded)}")
            return decoded
        log.debug(f"Redis 结果 | type={type(result).__name__} | value={str(result)[:200]}")
        return result
    finally:
        await r.close()
