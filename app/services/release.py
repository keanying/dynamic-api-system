# -*- coding: utf-8 -*-
"""
跨环境发布：预发(pre) → 生产(prod) (v2.18+)
==========================================

预发进程负责「冻结快照」，生产进程负责「核对差异 + 写入生产」。
两个进程各自只用自己环境的 ORM 模型（pre 进程的模型映射到 _pre 表），
唯一共享的是 src_dop_release_requests 这张交接表。

快照只包含「API 的业务配置」，不包含：
  - id / 生命周期状态 / 责任人 / 版本号：属于各环境自己的管理数据
  - api_key：密钥按环境分别管理，发布时保留生产原值（新 API 为空，即使用项目级 Key）
数据源按名称映射：预发和生产可以连不同的库，只要名称一致即可。
"""
import difflib
import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, delete

from app.core.logging import get_logger
from app.models.models import ApiConfig, ApiParameter, DataSource, Project, User

log = get_logger("release")

SNAPSHOT_VERSION = 1

# (字段, 显示名, 是否多行文本)
API_FIELDS: List[Tuple[str, str, bool]] = [
    ("name", "名称", False),
    ("description", "描述", True),
    ("method", "请求方法", False),
    ("url_path", "路径", False),
    ("api_type", "API 类型", False),
    ("datasource_name", "数据源", False),
    ("sql_template", "SQL 模板", True),
    ("pipeline_steps", "多步骤管线", True),
    ("html_content", "HTML", True),
    ("css_content", "CSS", True),
    ("js_content", "JS", True),
    ("plugin_code", "插件代码", True),
    ("sync_tables", "同步表白名单", True),
    ("is_enabled", "启用", False),
    ("require_api_key", "需要 API Key", False),
    ("cache_enabled", "缓存", False),
    ("cache_ttl", "缓存 TTL(秒)", False),
    ("cache_prewarm", "自动预热", False),
    ("prewarm_param_overrides", "预热参数覆盖", True),
    ("prewarm_stop_daily", "跨日停止预热", False),
    ("timeout", "超时(秒)", False),
    ("rate_limit_enabled", "限流", False),
    ("rate_limit_qps", "限流 QPS", False),
    ("max_rows", "最大行数", False),
]

PARAM_FIELDS = ("name", "param_type", "required", "default_value", "description", "sort_order", "item_schema")

# 直接拷到 ApiConfig 上的字段（datasource_name 需要映射，单独处理）
_COPY_FIELDS = [f for f, _, _ in API_FIELDS if f != "datasource_name"]


class ReleaseError(Exception):
    """发布失败，消息直接返回给前端。"""
    pass


def _norm(v: Any) -> Any:
    """把 None 统一成空串，避免 None 与 "" 被误判为有差异。"""
    return "" if v is None else v


async def build_snapshot(db, api: ApiConfig) -> Dict[str, Any]:
    """把当前环境的一个 API 冻结成与环境无关的快照。"""
    ds_name = ""
    if api.datasource_id:
        ds_name = (await db.execute(
            select(DataSource.name).where(DataSource.id == api.datasource_id)
        )).scalar() or ""

    params = (await db.execute(
        select(ApiParameter).where(ApiParameter.api_id == api.id).order_by(ApiParameter.sort_order, ApiParameter.id)
    )).scalars().all()

    snap: Dict[str, Any] = {"_v": SNAPSHOT_VERSION}
    for f in _COPY_FIELDS:
        snap[f] = _norm(getattr(api, f, None))
    snap["datasource_name"] = ds_name
    snap["parameters"] = [{k: _norm(getattr(p, k, None)) for k in PARAM_FIELDS} for p in params]
    return snap


async def find_target(db, project_code: str, method: str, url_path: str) -> Tuple[Optional[Project], Optional[ApiConfig]]:
    """按自然键在当前环境里找目标项目和 API。"""
    project = (await db.execute(select(Project).where(Project.code == project_code))).scalar_one_or_none()
    if not project:
        return None, None
    api = (await db.execute(
        select(ApiConfig).where(
            ApiConfig.project_id == project.id,
            ApiConfig.method == method,
            ApiConfig.url_path == url_path,
        )
    )).scalar_one_or_none()
    return project, api


def _text_lines(v: Any) -> List[str]:
    return str(_norm(v)).splitlines()


def _params_text(params: List[dict]) -> str:
    """参数列表转成逐行可读文本，便于做行级 diff。"""
    return "\n".join(json.dumps(p, ensure_ascii=False, sort_keys=True) for p in (params or []))


def diff_snapshots(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> Dict[str, Any]:
    """对比生产当前配置(before，None=生产没有该 API)与待发布快照(after)。

    返回 {is_new, changed_count, fields: [...]}；多行文本字段附带 unified diff 行。
    """
    fields = []
    changed_count = 0
    base = before or {}
    for f, label, multiline in API_FIELDS + [("parameters", "参数", True)]:
        if f == "parameters":
            b_val, a_val = _params_text(base.get(f, [])), _params_text(after.get(f, []))
        else:
            b_val, a_val = _norm(base.get(f)), _norm(after.get(f))
        changed = (before is None and a_val not in ("", None, [])) or (before is not None and b_val != a_val)
        item = {"field": f, "label": label, "changed": changed, "multiline": multiline}
        if multiline:
            if changed:
                item["diff"] = list(difflib.unified_diff(
                    _text_lines(b_val), _text_lines(a_val),
                    fromfile="生产当前", tofile="待发布", lineterm="", n=3,
                ))
        else:
            item["before"] = b_val if before is not None else None
            item["after"] = a_val
        if changed:
            changed_count += 1
        fields.append(item)
    return {"is_new": before is None, "changed_count": changed_count, "fields": fields}


async def apply_release(db, snapshot: Dict[str, Any], project_code: str, method: str, url_path: str,
                        submitter_username: str, approver: User) -> Tuple[ApiConfig, Optional[Dict[str, Any]]]:
    """把快照写入当前（生产）环境并直接上线。

    返回 (生产 API, 发布前的生产快照或 None)。调用方负责 commit。
    """
    from app.core.permissions import is_super_admin
    from app.services.api_lifecycle import STATUS_ONLINE

    project, api = await find_target(db, project_code, method, url_path)
    if not project:
        raise ReleaseError(f"生产环境不存在项目「{project_code}」，请先在生产创建同编码的项目")

    if (snapshot.get("api_type") or "sql") == "sync" and not is_super_admin(approver):
        raise ReleaseError("数据同步类 API 只能由超级管理员发布到生产")

    ds_id = None
    ds_name = snapshot.get("datasource_name") or ""
    if ds_name:
        ds_id = (await db.execute(select(DataSource.id).where(DataSource.name == ds_name))).scalars().first()
        if not ds_id:
            raise ReleaseError(f"生产环境不存在数据源「{ds_name}」，请先在生产创建同名数据源")

    before = await build_snapshot(db, api) if api else None

    if api is None:
        submitter = (await db.execute(select(User).where(User.username == submitter_username))).scalar_one_or_none()
        owner_id = submitter.id if submitter else approver.id
        api = ApiConfig(project_id=project.id, created_by=owner_id, owner_id=owner_id, version=0)
        db.add(api)

    for f in _COPY_FIELDS:
        if f in snapshot:
            setattr(api, f, snapshot[f])
    api.datasource_id = ds_id
    api.status = STATUS_ONLINE
    api.is_locked = False
    api.version = (api.version or 0) + 1
    await db.flush()

    await db.execute(delete(ApiParameter).where(ApiParameter.api_id == api.id))
    for idx, p in enumerate(snapshot.get("parameters") or []):
        db.add(ApiParameter(
            api_id=api.id,
            name=p.get("name", ""),
            param_type=p.get("param_type") or "string",
            required=bool(p.get("required")),
            default_value=p.get("default_value") or "",
            description=p.get("description") or "",
            sort_order=p.get("sort_order") if p.get("sort_order") not in (None, "") else idx,
            item_schema=p.get("item_schema") or "",
        ))
    await db.flush()

    # 生产内容已变，旧缓存和预热参数作废
    try:
        from app.services.engine import _clear_api_cache, _clear_prewarm_params
        await _clear_api_cache(api.id)
        await _clear_prewarm_params(api.id)
    except Exception as e:  # noqa: BLE001
        log.warning(f"发布后清缓存失败（不影响发布）| api_id={api.id} | error={e}")

    return api, before
