# -*- coding: utf-8 -*-
"""
数据同步执行器 (v2.17+)
======================

用途：把外部推送过来的数据写入指定的数据源表，是本系统第一个「写入类」API。

请求格式：
    {
      "tableName": "order_table",
      "pkId": ["order_id"],
      "data": [{"order_id": 121212, "order_name": "测试订单"}]
    }

写入逻辑：逐条按 pkId 先查存在性，存在则 UPDATE，不存在则 INSERT。
（目标表 pkId 字段上没有唯一索引，所以不能用 INSERT ... ON DUPLICATE KEY UPDATE）

安全边界（写入类 API 必须比查询严格得多）：
  1. 只有超级管理员能创建/管理这类 API，调用也必须带超管 token
  2. 表名、字段名必须在该 API 的白名单(sync_tables)里登记过
     —— 表名和字段名在 SQL 里无法用占位符参数化，只能靠白名单精确比对，
        这是防注入的唯一可靠手段
  3. 值一律走参数化绑定，不拼进 SQL 文本
  4. 遇错即停：已写入的保留（不回滚），返回失败位置和原因，便于定位后续补数
"""
import json
import re
from typing import Any, Dict, List, Tuple

from app.core.logging import get_logger

log = get_logger("data_sync")

# 合法标识符：只允许字母、数字、下划线。
# 白名单里登记的表名/字段名也要过这一关，避免有人在白名单里写入奇怪的东西。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SyncError(Exception):
    """数据同步失败，消息直接返回给调用方。"""
    pass


def parse_whitelist(raw: str) -> Dict[str, List[str]]:
    """解析白名单配置。

    形如 {"order_table": ["order_id", "order_name"]}
    返回 {表名: [字段名, ...]}；配置为空或非法时抛 SyncError。
    """
    if not raw or not raw.strip():
        raise SyncError("该同步 API 未配置允许写入的表，请先在编辑页登记表名和字段")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise SyncError(f"同步表白名单不是合法 JSON：{e}")
    if not isinstance(data, dict) or not data:
        raise SyncError('同步表白名单须是对象，形如 {"order_table": ["order_id", "order_name"]}')

    out: Dict[str, List[str]] = {}
    for table, cols in data.items():
        if not _IDENT_RE.match(str(table)):
            raise SyncError(f"白名单表名不合法：{table}（只允许字母、数字、下划线）")
        if not isinstance(cols, list) or not cols:
            raise SyncError(f"表 {table} 未配置允许的字段")
        safe_cols = []
        for c in cols:
            if not _IDENT_RE.match(str(c)):
                raise SyncError(f"白名单字段名不合法：{table}.{c}")
            safe_cols.append(str(c))
        out[str(table)] = safe_cols
    return out


def validate_request(params: dict, whitelist: Dict[str, List[str]]) -> Tuple[str, List[str], List[dict]]:
    """校验请求参数，返回 (表名, 主键字段列表, 数据行列表)。"""
    table = (params.get("tableName") or "").strip()
    if not table:
        raise SyncError("缺少参数 tableName")
    if table not in whitelist:
        raise SyncError(
            f"表 {table} 不在允许写入的白名单内（当前允许：{', '.join(whitelist.keys())}）"
        )
    allowed = set(whitelist[table])

    pk = params.get("pkId")
    if isinstance(pk, str):
        pk = [pk]
    if not isinstance(pk, list) or not pk:
        raise SyncError('缺少参数 pkId，形如 "pkId": ["order_id"]')
    pk = [str(x) for x in pk]
    for k in pk:
        if k not in allowed:
            raise SyncError(f"主键字段 {k} 不在表 {table} 的允许字段内")

    rows = params.get("data")
    if not isinstance(rows, list) or not rows:
        raise SyncError("缺少参数 data，或 data 不是非空数组")

    # 逐行校验字段合法性 —— 有一行含非法字段就整体拒绝，
    # 避免写了一半才发现问题（写入类操作要尽量在动手前就拦住）
    for idx, row in enumerate(rows):
        if not isinstance(row, dict) or not row:
            raise SyncError(f"data[{idx}] 不是有效对象")
        for col in row.keys():
            if col not in allowed:
                raise SyncError(
                    f"data[{idx}] 含未登记字段 {col}（表 {table} 允许：{', '.join(sorted(allowed))}）"
                )
        for k in pk:
            if k not in row:
                raise SyncError(f"data[{idx}] 缺少主键字段 {k}")
            if row[k] is None:
                raise SyncError(f"data[{idx}] 主键字段 {k} 不能为 null")

    return table, pk, rows


def _q(name: str) -> str:
    """给标识符加反引号。name 已经过 _IDENT_RE 校验，这里只做包裹。"""
    return f"`{name}`"


def build_statements(table: str, pk: List[str], row: dict) -> Tuple[str, dict, str, dict, str, dict]:
    """为一行数据构造 (查存在性SQL, 绑定值, UPDATE SQL, 绑定值, INSERT SQL, 绑定值)。

    值全部走参数化绑定；表名/字段名来自白名单校验后的安全标识符。
    """
    where_parts, where_binds = [], {}
    for i, k in enumerate(pk):
        ph = f"pk_{i}"
        where_parts.append(f"{_q(k)} = %({ph})s")
        where_binds[ph] = row[k]
    where_sql = " AND ".join(where_parts)

    select_sql = f"SELECT 1 FROM {_q(table)} WHERE {where_sql} LIMIT 1"

    # UPDATE：只更新非主键字段；如果这行只有主键字段，就没什么可更新的
    set_cols = [c for c in row.keys() if c not in pk]
    if set_cols:
        set_parts, set_binds = [], {}
        for i, c in enumerate(set_cols):
            ph = f"set_{i}"
            set_parts.append(f"{_q(c)} = %({ph})s")
            set_binds[ph] = row[c]
        update_sql = f"UPDATE {_q(table)} SET {', '.join(set_parts)} WHERE {where_sql}"
        update_binds = {**set_binds, **where_binds}
    else:
        update_sql, update_binds = "", {}

    cols = list(row.keys())
    ins_parts, ins_binds = [], {}
    for i, c in enumerate(cols):
        ph = f"ins_{i}"
        ins_parts.append(f"%({ph})s")
        ins_binds[ph] = row[c]
    insert_sql = (
        f"INSERT INTO {_q(table)} ({', '.join(_q(c) for c in cols)}) "
        f"VALUES ({', '.join(ins_parts)})"
    )

    return select_sql, where_binds, update_sql, update_binds, insert_sql, ins_binds


async def execute_sync(api_config, params: dict, datasource, password: str, timeout: int) -> dict:
    """执行数据同步，返回统计结果。

    遇错即停：已写入的保留，返回失败的行号和原因。
    """
    import aiomysql
    from app.services.engine import _get_mysql_pool

    whitelist = parse_whitelist(getattr(api_config, "sync_tables", "") or "")
    table, pk, rows = validate_request(params, whitelist)

    log.info(
        f"数据同步开始 | api_id={api_config.id} | table={table} | pk={pk} | 行数={len(rows)}"
    )

    pool = await _get_mysql_pool(datasource, password)
    conn = await pool.acquire()

    inserted = updated = 0
    failed_index = None
    failed_reason = ""

    try:
        # autocommit 模式下每条语句独立提交 —— 符合「遇错即停、已写的保留」的要求
        async with conn.cursor() as cur:
            for idx, row in enumerate(rows):
                try:
                    sel, sel_b, upd, upd_b, ins, ins_b = build_statements(table, pk, row)
                    await cur.execute(sel, sel_b)
                    exists = await cur.fetchone()

                    if exists:
                        if upd:
                            await cur.execute(upd, upd_b)
                            updated += 1
                        else:
                            # 整行只有主键，没有可更新的列，跳过但不算失败
                            log.debug(f"第 {idx} 行只含主键字段，无需更新")
                    else:
                        await cur.execute(ins, ins_b)
                        inserted += 1
                except Exception as e:  # noqa: BLE001
                    failed_index = idx
                    failed_reason = str(e)
                    log.error(
                        f"数据同步失败并中止 | api_id={api_config.id} | table={table} | "
                        f"行号={idx} | error={failed_reason}"
                    )
                    break
    finally:
        pool.release(conn)

    result = {
        "tableName": table,
        "total": len(rows),
        "inserted": inserted,
        "updated": updated,
        "processed": inserted + updated,
    }
    if failed_index is not None:
        result["failedIndex"] = failed_index
        result["failedReason"] = failed_reason
        result["failedRow"] = rows[failed_index]
        log.warning(
            f"数据同步部分完成 | api_id={api_config.id} | 成功={inserted + updated}/{len(rows)} | "
            f"失败于第 {failed_index} 行"
        )
    else:
        log.info(
            f"数据同步完成 | api_id={api_config.id} | table={table} | "
            f"新增={inserted} | 更新={updated}"
        )
    return result
