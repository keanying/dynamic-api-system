#!/usr/bin/env python3
"""
检查开启 query.defaults_visible_in_template 后，哪些 API 的结果可能变化。

用法（项目根目录）:  python scripts/check_param_defaults.py            # 正式环境
                     python scripts/check_param_defaults.py --env pre  # 预发环境

判定：单 SQL 模式的 API，某个参数配置了默认值，且该参数出现在 $if$/$elseif$/$for$ 条件里。
这类 API 在调用方不传该参数时，开启前条件看到的是「未传」，开启后看到的是默认值。
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.models import ApiConfig, ApiParameter, Project  # noqa: E402
from app.services.sql_template import collect_expressions  # noqa: E402


async def main():
    async with async_session() as db:
        apis = (await db.execute(select(ApiConfig))).scalars().all()
        projects = {p.id: p.code for p in (await db.execute(select(Project))).scalars().all()}
        params = (await db.execute(select(ApiParameter))).scalars().all()
    await engine.dispose()
    by_api = {}
    for p in params:
        if p.default_value not in (None, ""):
            by_api.setdefault(p.api_id, []).append(p)

    hits = []
    for api in apis:
        if (api.api_type or "sql") != "sql" or (api.pipeline_steps or "").strip() or not api.sql_template:
            continue
        defaults = by_api.get(api.id, [])
        if not defaults:
            continue
        try:
            exprs = " ".join(e for _, e in collect_expressions(api.sql_template))
        except Exception:  # noqa: BLE001
            continue
        names = [p.name for p in defaults if re.search(rf"\b{re.escape(p.name)}\b", exprs)]
        if names:
            hits.append((projects.get(api.project_id, "?"), api, names))

    if not hits:
        print("没有受影响的 API，可以放心开启 query.defaults_visible_in_template")
        return
    print(f"以下 {len(hits)} 个 API 在调用方不传对应参数时，开启后结果可能变化，请逐个确认：\n")
    for code, api, names in hits:
        print(f"  [{code}] {api.method} {api.url_path}  {api.name}  (id={api.id}, 状态={api.status})  参数: {', '.join(names)}")


if __name__ == "__main__":
    asyncio.run(main())
