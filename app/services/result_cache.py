# -*- coding: utf-8 -*-
"""
接口结果缓存与响应拼装 (v2.20+)
==============================

针对「缓存数据体大、命中缓存也慢、高并发扛不住」做的几件事：

1. 只序列化一次
   结果第一次查出来时用 orjson 序列化成 JSON 字节（CachedResult.raw），写缓存和
   返回响应都用这份字节。命中缓存时不再「json.loads 成上万个 Python 对象，再
   json.dumps 回去」—— 2MB 的结果原来每次命中要 ~90ms CPU，现在直接拼接，<1ms。

2. 预压缩 + gzip 拼接
   较大的结果在写缓存时顺便压成一段 raw deflate（SYNC_FLUSH 结尾、字节对齐、不带结束标志），
   和 CRC32 一起存进缓存。客户端请求头带 Accept-Encoding: gzip 时，把
   「信封前半段(独立压缩) + 缓存里的压缩块 + 信封后半段(独立压缩)」直接首尾相接，
   CRC32 用 crc32_combine 合并 —— 得到合法的 gzip 响应，无需每次重新压缩。
   JSON 一般能压到 1/10 左右，大响应的网络传输时间随之大幅下降；Redis 里存的也是压缩后的数据。

3. 进程内热点缓存（L1）
   热门 key 在本进程内存里再留几秒（cache.local_ttl），高并发下大部分请求连 Redis 都不用访问。
   L1 过期时间不会超过该 key 在 Redis 里的剩余寿命。

4. 并发未命中合并（single-flight）
   同一个 key 缓存失效的瞬间，本进程内只让一个请求去查库，其余请求等它的结果，
   避免几百个并发同时打同一条 SQL、把数据库连接数打满。
"""
import asyncio
import json
import struct
import time
import zlib
from collections import OrderedDict
from typing import Any, Dict, Optional

import orjson

from app.core.config import settings
from app.core.fast_json import dumps_compat, json_default

# 缓存值格式：MAGIC + 头(flags, rows, crc32, raw_len) + 数据
_MAGIC = b"ODC2"
_HEADER = struct.Struct("<BIII")
_FLAG_DEFLATED = 1

# 序列化后超过这个大小才预压缩（小结果压缩收益小）
COMPRESS_MIN = 8 * 1024

_ORJSON_OPTS = orjson.OPT_NON_STR_KEYS | orjson.OPT_PASSTHROUGH_DATETIME


def dumps_bytes(obj: Any) -> bytes:
    """紧凑 JSON 字节。类型转换规则与 FastAPI 一致（Decimal -> 数字、日期 -> isoformat 等）。
    orjson 处理不了的极端情况（超过 64 位的整数等）退回标准库。"""
    try:
        return orjson.dumps(obj, default=json_default, option=_ORJSON_OPTS)
    except (TypeError, orjson.JSONEncodeError):
        return dumps_compat(obj).encode("utf-8")


def _deflate_block(data: bytes) -> bytes:
    """独立压缩的一段 raw deflate，SYNC_FLUSH 结尾（字节对齐、非最终块），可与其它段直接拼接。"""
    c = zlib.compressobj(1, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush(zlib.Z_SYNC_FLUSH)


# ---------- crc32_combine（zlib 的 C 函数未在 Python 中暴露，按其算法移植） ----------
def _gf2_times(mat, vec):
    s = 0
    i = 0
    while vec:
        if vec & 1:
            s ^= mat[i]
        vec >>= 1
        i += 1
    return s


def _gf2_square(mat):
    return [_gf2_times(mat, mat[n]) for n in range(32)]


def _compose(mat, op):
    """先做 op 再做 mat 的复合变换。"""
    return [_gf2_times(mat, op[n]) for n in range(32)]


def crc32_shift_op(len2: int) -> list:
    """「在 CRC 后面追加 len2 个字节」对应的线性变换（32×32 GF(2) 矩阵）。
    crc32(A + B) = apply(op, crc32(A)) ^ crc32(B)。按 zlib crc32_combine 的算法移植，
    计算一次约 2ms，按条目缓存后每次合并只需 32 次位运算。"""
    op = [1 << n for n in range(32)]   # 单位矩阵
    if len2 <= 0:
        return op
    odd = [0xEDB88320] + [1 << n for n in range(31)]
    even = _gf2_square(odd)
    odd = _gf2_square(even)
    while True:
        even = _gf2_square(odd)
        if len2 & 1:
            op = _compose(even, op)
        len2 >>= 1
        if not len2:
            break
        odd = _gf2_square(even)
        if len2 & 1:
            op = _compose(odd, op)
        len2 >>= 1
        if not len2:
            break
    return op


def crc32_combine(crc1: int, crc2: int, len2: int, op: Optional[list] = None) -> int:
    """已知 crc32(A)、crc32(B) 和 len(B)，求 crc32(A + B)。"""
    return _gf2_times(op or crc32_shift_op(len2), crc1) ^ crc2


class CachedResult:
    """一份序列化好的接口结果（data 字段的 JSON）。"""

    __slots__ = ("_raw", "deflated", "crc", "length", "rows", "_shift_op")

    def __init__(self, raw: Optional[bytes], deflated: Optional[bytes], crc: int, length: int, rows: int):
        self._raw = raw
        self.deflated = deflated
        self.crc = crc
        self.length = length
        self.rows = rows
        self._shift_op = None

    def shift_op(self) -> list:
        if self._shift_op is None:
            self._shift_op = crc32_shift_op(self.length)
        return self._shift_op

    @classmethod
    def from_data(cls, data: Any, compress: bool = True) -> "CachedResult":
        raw = dumps_bytes(data)
        rows = len(data) if isinstance(data, list) else 0
        deflated = _deflate_block(raw) if compress and len(raw) >= COMPRESS_MIN else None
        return cls(raw, deflated, zlib.crc32(raw), len(raw), rows)

    @property
    def raw(self) -> bytes:
        if self._raw is None:
            self._raw = zlib.decompressobj(-15).decompress(self.deflated)
        return self._raw

    def preview(self, limit: int) -> str:
        """前 limit 个字符（日志用），压缩存储时只解压需要的部分。"""
        if self._raw is None and self.deflated is not None:
            head = zlib.decompressobj(-15).decompress(self.deflated, limit * 4)
        else:
            head = self.raw[: limit * 4]
        return head.decode("utf-8", errors="ignore")[:limit]

    def to_python(self) -> Any:
        return orjson.loads(self.raw)

    def ensure_deflated(self) -> bytes:
        if self.deflated is None:
            self.deflated = _deflate_block(self.raw)
        return self.deflated

    def size(self) -> int:
        return len(self.deflated) if self.deflated is not None else self.length

    # ---- 缓存存储格式 ----
    def encode(self) -> bytes:
        if self.deflated is not None:
            return _MAGIC + _HEADER.pack(_FLAG_DEFLATED, self.rows, self.crc, self.length) + self.deflated
        return _MAGIC + _HEADER.pack(0, self.rows, self.crc, self.length) + self.raw

    @classmethod
    def decode(cls, blob: bytes) -> Optional["CachedResult"]:
        """解析缓存值；不是本格式（升级前写入的旧缓存）返回 None。"""
        if not blob.startswith(_MAGIC):
            return None
        flags, rows, crc, length = _HEADER.unpack_from(blob, len(_MAGIC))
        body = blob[len(_MAGIC) + _HEADER.size:]
        if flags & _FLAG_DEFLATED:
            return cls(None, body, crc, length, rows)
        return cls(body, None, crc, length, rows)

    @classmethod
    def from_legacy(cls, blob: bytes) -> "CachedResult":
        """v2.19 及之前写入的缓存（标准 JSON 文本），读出来转成新结构。"""
        return cls.from_data(json.loads(blob), compress=False)


# ============================================================
# 进程内热点缓存（L1）
# ============================================================
_l1: "OrderedDict[str, tuple]" = OrderedDict()   # key -> (过期时间, CachedResult)
_l1_bytes = 0


def _l1_ttl() -> float:
    return float(getattr(settings.cache, "local_ttl", 0) or 0)


def _l1_max_bytes() -> int:
    return int(float(getattr(settings.cache, "local_max_mb", 0) or 0) * 1024 * 1024)


def l1_get(key: str) -> Optional[CachedResult]:
    hit = _l1.get(key)
    if hit is None:
        return None
    if hit[0] < time.monotonic():
        l1_pop(key)
        return None
    _l1.move_to_end(key)
    return hit[1]


def l1_put(key: str, entry: CachedResult, ttl: float) -> None:
    global _l1_bytes
    ttl = min(ttl, _l1_ttl())
    limit = _l1_max_bytes()
    if ttl <= 0 or limit <= 0 or entry.size() > limit // 4:
        return
    l1_pop(key)
    _l1[key] = (time.monotonic() + ttl, entry)
    _l1_bytes += entry.size()
    while _l1_bytes > limit and _l1:
        _, (_, old) = _l1.popitem(last=False)   # 淘汰最久没用的
        _l1_bytes -= old.size()


def l1_pop(key: str) -> None:
    global _l1_bytes
    hit = _l1.pop(key, None)
    if hit is not None:
        _l1_bytes -= hit[1].size()


def l1_clear_prefix(prefix: str) -> int:
    keys = [k for k in _l1 if k.startswith(prefix)]
    for k in keys:
        l1_pop(k)
    return len(keys)


# ============================================================
# 并发未命中合并（single-flight）
# ============================================================
_flights: Dict[str, asyncio.Future] = {}


def flight_get(key: str) -> Optional[asyncio.Future]:
    return _flights.get(key)


def flight_start(key: str) -> asyncio.Future:
    fut = asyncio.get_running_loop().create_future()
    _flights[key] = fut
    return fut


def flight_finish(key: str, fut: asyncio.Future, result=None, error: Optional[BaseException] = None) -> None:
    _flights.pop(key, None)
    if fut.done():
        return
    if error is not None:
        fut.set_exception(error)
        fut.exception()   # 标记已读取，没有等待者时也不报 "exception was never retrieved"
    else:
        fut.set_result(result)


# ============================================================
# 网关响应拼装
# ============================================================
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"


def _envelope_parts(result: dict):
    """把 result 拆成 data 之前 / 之后两段 JSON 字节（键顺序与原来一致）。"""
    before, after, seen_data = [], [], False
    for k, v in result.items():
        if k == "data":
            seen_data = True
            continue
        piece = dumps_bytes(k) + b":" + dumps_bytes(v)
        (after if seen_data else before).append(piece)
    prefix = b"{" + b",".join(before) + (b"," if before else b"") + b'"data":'
    suffix = (b"," + b",".join(after) if after else b"") + b"}"
    return prefix, suffix


def build_response_body(result: dict, entry: CachedResult, gzip_ok: bool):
    """返回 (body, 是否 gzip)。gzip_ok 时用预压缩块拼出 gzip 流。"""
    prefix, suffix = _envelope_parts(result)
    if not gzip_ok:
        return prefix + entry.raw + suffix, False
    deflated = entry.ensure_deflated()
    head = _deflate_block(prefix)
    c = zlib.compressobj(1, zlib.DEFLATED, -15)
    tail = c.compress(suffix) + c.flush(zlib.Z_FINISH)
    crc = crc32_combine(zlib.crc32(prefix), entry.crc, entry.length, entry.shift_op())
    crc = zlib.crc32(suffix, crc)
    total = len(prefix) + entry.length + len(suffix)
    body = b"".join((
        _GZIP_HEADER, head, deflated, tail,
        struct.pack("<II", crc & 0xFFFFFFFF, total & 0xFFFFFFFF),
    ))
    return body, True
