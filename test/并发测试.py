#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============ 改这里 ============
BASE_URL = "https://data.12301dev.com"          # 你的服务地址
API_KEY = "pfk_AFLBf47dqAbSsl6RDKUtOR7bKgQ4THyu"   # xpftkey
TIMEOUT = 120                           # 单请求超时(秒)
# ================================

# 你实际的 5 个请求
REQUESTS = [
    ("每日报表趋势", "/v1/data/digitalBrain/dailyReportTrend", {"supplierId":[1757026],"startDate":"2026-02-20","endDate":"2026-08-20"}),
    ("长周期对比(环比)", "/v1/data/digitalBrain/longRangeCompareReportSummary", {"supplierId":[1757026],"startTime":"2026-02-20 00:00:00","endTime":"2026-08-20 17:16:06","compareStartTime":"2025-02-20 00:00:00","compareEndTime":"2025-08-20 17:16:06"}),
    ("长周期对比(同比)", "/v1/data/digitalBrain/longRangeCompareReportSummary", {"supplierId":[1757026],"startTime":"2026-02-20 00:00:00","endTime":"2026-08-20 17:16:06","compareStartTime":"2025-08-22 00:00:00","compareEndTime":"2026-02-19 17:16:06"}),
    ("产品报表统计", "/v1/data/digitalBrain/productReportSummary", {"supplierId":[1757026],"startTime":"2026-02-20 00:00:00","endTime":"2026-08-20 17:16:06"}),
    ("销售渠道报表统计", "/v1/data/digitalBrain/salesChannelReportSummary", {"supplierId":[1757026],"startTime":"2026-02-20 00:00:00","endTime":"2026-08-20 17:16:06"}),
]

# 忽略自签名证书（内网环境常见），线上正式证书不受影响
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

T0 = None
_print_lock = threading.Lock()


def call(idx, label, path, payload):
    """发一个请求，返回 (序号, 名称, 发起时刻, 返回时刻, 耗时, 状态, 备注)"""
    url = BASE_URL.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("xpftkey", API_KEY)

    start = time.time()
    status, note = "?", ""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as r:
            raw = r.read()
            status = r.status
            try:
                d = json.loads(raw)
                note = "from cache" if d.get("msg") == "from cache" else \
                       ("ok" if d.get("status") else f"业务失败:{d.get('msg','')[:30]}")
            except Exception:
                note = f"非JSON({len(raw)}字节)"
    except Exception as e:
        status = "ERR"
        note = str(e)[:60]
    end = time.time()

    # 请求一返回就立刻打印 —— 这样能直观看到"谁先回来"
    with _print_lock:
        print(f"  ✓ [{end - T0:6.2f}s] {label} 返回 (耗时 {end - start:.2f}s, {status}, {note})")

    return (idx, label, start - T0, end - T0, end - start, status, note)


def main():
    global T0
    print("=" * 78)
    print(f"并发验证：同时发起 {len(REQUESTS)} 个请求")
    print(f"目标: {BASE_URL}")
    print("=" * 78)
    print()
    print("实时返回顺序（按实际返回先后打印）：")

    T0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=len(REQUESTS)) as pool:
        futs = [
            pool.submit(call, i, label, path, payload)
            for i, (label, path, payload) in enumerate(REQUESTS, 1)
        ]
        for f in as_completed(futs):
            results.append(f.result())

    total = time.time() - T0
    results.sort(key=lambda x: x[3])   # 按返回时刻排序

    print()
    print("=" * 78)
    print("汇总（按返回先后排序）")
    print("=" * 78)
    print(f"{'名称':<22}{'发起于':>9}{'返回于':>10}{'耗时':>9}   {'状态':<6}{'备注'}")
    print("-" * 78)
    for _, label, st, en, cost, status, note in results:
        print(f"{label:<22}{st:>8.2f}s{en:>9.2f}s{cost:>8.2f}s   {str(status):<6}{note}")

    print("-" * 78)
    starts = [r[2] for r in results]
    ends = [r[3] for r in results]
    costs = [r[4] for r in results]
    print(f"发起时刻跨度: {max(starts) - min(starts):.2f}s   (接近 0 = 确实同时发出)")
    print(f"返回时刻跨度: {max(ends) - min(ends):.2f}s")
    print(f"最快 {min(costs):.2f}s / 最慢 {max(costs):.2f}s / 总墙钟 {total:.2f}s")
    print()

    # ---- 判定 ----
    print("=" * 78)
    print("结论")
    print("=" * 78)
    if max(starts) - min(starts) > 1.0:
        print("⚠ 请求没有真正同时发出（发起跨度 > 1s），本机线程/网络有瓶颈，")
        print("  结果仅供参考。")

    spread = max(ends) - min(ends)
    cost_spread = max(costs) - min(costs)

    if spread < 0.5 and cost_spread > 1.0:
        print("✗ 各请求耗时差异明显，但几乎同一时刻返回")
        print("  → 后端存在串行/阻塞，先完成的在等最后一个")
    elif spread >= 0.5:
        print("✓ 各请求独立返回，先查完的先返回（后端并发正常）")
        print(f"  最快的比最慢的早回 {spread:.2f} 秒")
        print()
        print("  如果页面上仍表现为「一起刷新」，那就是前端的问题：")
        print("  多半用了 Promise.all([...]) 等全部完成才统一渲染。")
        print("  改成各自 .then() 分别渲染即可做到谁快谁先显示。")
    else:
        print("? 各请求耗时本来就接近，无法据此判断，")
        print("  建议挑几个耗时差异大的接口再测一次。")

    # 串行对照（可选）
    print()
    print("提示：如果想确认并发确实生效，可对比串行耗时 —— ")
    print(f"  串行预计约 {sum(costs):.1f}s，本次并发总墙钟 {total:.1f}s")
    if total < sum(costs) * 0.7:
        print("  → 并发生效（总时间远小于各请求耗时之和）")
    else:
        print("  → 总时间接近耗时之和，并发可能未生效，需进一步排查")


if __name__ == "__main__":
    main()