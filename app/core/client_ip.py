# -*- coding: utf-8 -*-
"""
客户端真实 IP (v2.21+)
=====================

原实现直接取 X-Forwarded-For 的第一个值。这个请求头客户端可以随便写，
服务直接暴露时，任何人都能伪造成白名单里的 IP 绕过 IP 白名单。

现在只有「直接连过来的对端」是可信代理（security.trusted_proxies，默认本机 + 内网网段）时
才采信 X-Forwarded-For，并从右往左跳过可信代理，取第一个不可信的地址作为客户端 IP
（最左侧的值可被客户端伪造，右侧的是各级代理追加的）。
"""
import ipaddress
from functools import lru_cache

from app.core.config import settings

_DEFAULT_TRUSTED = ["127.0.0.1/32", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


@lru_cache(maxsize=1)
def _trusted_networks():
    raw = getattr(settings.security, "trusted_proxies", None)
    items = _DEFAULT_TRUSTED if raw is None else raw
    nets = []
    for item in items:
        try:
            nets.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            continue
    return tuple(nets)


def is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks())


def resolve_client_ip(peer: str, forwarded_for: str) -> str:
    """peer: TCP 对端地址；forwarded_for: X-Forwarded-For 请求头。"""
    if not forwarded_for or not peer or not is_trusted_proxy(peer):
        return peer or "unknown"
    chain = [x.strip() for x in forwarded_for.split(",") if x.strip()]
    for ip in reversed(chain):
        if not is_trusted_proxy(ip):
            return ip
    return chain[0] if chain else peer
