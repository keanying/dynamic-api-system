# -*- coding: utf-8 -*-
"""
跨环境发布：预发(pre) → 生产(prod) (v2.18+)
==========================================

预发进程负责「冻结快照」，生产进程负责「核对差异 + 写入生产」。
两个进程各自只用自己环境的 ORM 模型（pre 进程的模型映射到 _pre 表），
唯一共享的是 src_dop_release_requests 这张交接表。

快照只包含「API 的业务配置」，不包含 id / 生命周期状态 / 锁定 / 责任人 / 版本号（各环境自己的管理数据）。
API 自己的 api_key 随发布一起带到生产 (v2.24)；生产没有该项目时，审核通过会按预发的项目自动创建
（含项目 Key、成员、环境变量），生产已有项目但没设项目 Key 时补上预发的项目 Key。
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
    ("api_key", "独立 API Key", False),
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


def mask_key(k: str) -> str:
    k = str(k)
    return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "****"


def _inline(a: str, b: str):
    """一对改动行的行内差异：返回 (左片段, 右片段)，片段为 [文本, 是否改动]。"""
    if len(a) + len(b) > 4000:   # 超长行（压缩过的 JS 等）逐字符比对太慢，整行高亮
        return [[a, True]], [[b, True]]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < 0.5:      # 大半行都变了，整行高亮比零碎的字符高亮好读
        return [[a, True]], [[b, True]]
    ops = sm.get_opcodes()
    left, right = [], []
    for k, (op, i1, i2, j1, j2) in enumerate(ops):
        # 夹在两段改动之间、很短的相同片段并入改动，避免高亮支离破碎
        hl = op != "equal" or (0 < k < len(ops) - 1 and (i2 - i1) < 3)
        for out, txt in ((left, a[i1:i2]), (right, b[j1:j2])):
            if not txt:
                continue
            if out and out[-1][1] == hl:
                out[-1][0] += txt
            else:
                out.append([txt, hl])
    return left or [["", False]], right or [["", False]]


def split_rows(a_lines: List[str], b_lines: List[str], context: int = 3) -> List[Dict[str, Any]]:
    """左右并排的逐行差异。

    每行 {"t": eq/del/add/chg, "ln": 左行号, "rn": 右行号, "l": 片段, "r": 片段}；
    连续相同的大段只保留前后 context 行，中间折叠为 {"t": "skip", "n": 行数}。
    """
    rows: List[Dict[str, Any]] = []
    # 行数很多时打开 autojunk（difflib 的启发式加速），否则对比大文件会很慢
    sm = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=len(a_lines) + len(b_lines) > 4000)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            eq = [{"t": "eq", "ln": i1 + k + 1, "rn": j1 + k + 1,
                   "l": [[a_lines[i1 + k], False]], "r": [[b_lines[j1 + k], False]]} for k in range(i2 - i1)]
            first, last = not rows, i2 == len(a_lines) and j2 == len(b_lines)
            head = 0 if first else context
            tail = 0 if last else context
            if len(eq) > head + tail + 1:
                rows.extend(eq[:head])
                rows.append({"t": "skip", "n": len(eq) - head - tail})
                rows.extend(eq[len(eq) - tail:] if tail else [])
            else:
                rows.extend(eq)
            continue
        n = max(i2 - i1, j2 - j1)
        for k in range(n):
            li, rj = i1 + k, j1 + k
            has_l, has_r = li < i2, rj < j2
            if has_l and has_r:
                l, r = _inline(a_lines[li], b_lines[rj])
                rows.append({"t": "chg", "ln": li + 1, "rn": rj + 1, "l": l, "r": r})
            elif has_l:
                rows.append({"t": "del", "ln": li + 1, "rn": None, "l": [[a_lines[li], True]], "r": None})
            else:
                rows.append({"t": "add", "ln": None, "rn": rj + 1, "l": None, "r": [[b_lines[rj], True]]})
    return rows


def diff_snapshots(before: Optional[Dict[str, Any]], after: Dict[str, Any],
                   before_label: str = "生产当前", after_label: str = "待发布") -> Dict[str, Any]:
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
                    fromfile=before_label, tofile=after_label, lineterm="", n=3,
                ))
                # 左右并排 (v2.24)：左 before（生产），右 after（预发 / 待发布）
                item["split"] = split_rows(_text_lines(b_val), _text_lines(a_val))
        else:
            item["before"] = b_val if before is not None else None
            item["after"] = a_val
            if f == "api_key":   # 密钥只显示首尾
                item["before"] = mask_key(item["before"]) if item["before"] else item["before"]
                item["after"] = mask_key(a_val) if a_val else a_val
        if changed:
            changed_count += 1
        fields.append(item)
    return {"is_new": before is None, "changed_count": changed_count, "fields": fields}


# ---------------------------------------------------------------------------
# 读取另一环境的配置 (v2.23)：预发和生产同库不同表名（_pre 后缀），
# 直接按表名读对方的表，预发也能实时对比生产，不需要生产进程参与。
# ---------------------------------------------------------------------------
_BOOL_API_FIELDS = {c.name for c in ApiConfig.__table__.columns if c.type.python_type is bool} \
    if hasattr(ApiConfig.__table__.c.is_enabled.type, "python_type") else set()
_BOOL_PARAM_FIELDS = {"required"}


def _as_bool(v: Any) -> Any:
    """MySQL 读回来的布尔是 1/0，和 ORM 的 True/False 统一，避免被误判为有差异。"""
    if v in (None, ""):
        return v
    if isinstance(v, (bytes, bytearray)):
        v = int.from_bytes(v, "little")
    return bool(v)


async def read_env_snapshot(db, suffix: str, project_code: str, method: str, url_path: str):
    """读取指定环境（suffix="" 为生产，"_pre" 为预发）里某个 API 的快照。

    返回 (项目是否存在, 快照或 None, {"id", "version", "status"} 或 None)。
    表不存在（对方环境从未启动过）时抛 ReleaseError。
    """
    from sqlalchemy import text
    tp, ta, td, tparam = (f"src_dop_projects{suffix}", f"src_dop_api_configs{suffix}",
                          f"src_dop_datasources{suffix}", f"src_dop_api_parameters{suffix}")
    try:
        prow = (await db.execute(text(f"SELECT id FROM {tp} WHERE code = :c"), {"c": project_code})).first()
    except Exception as e:  # noqa: BLE001
        raise ReleaseError(f"读取{'生产' if not suffix else '预发'}环境失败：{e}")
    if not prow:
        return False, None, None
    arow = (await db.execute(
        text(f"SELECT * FROM {ta} WHERE project_id = :p AND method = :m AND url_path = :u"),
        {"p": prow[0], "m": method, "u": url_path},
    )).mappings().first()
    if not arow:
        return True, None, None
    ds_name = ""
    if arow.get("datasource_id"):
        ds_name = (await db.execute(text(f"SELECT name FROM {td} WHERE id = :i"), {"i": arow["datasource_id"]})).scalar() or ""
    prows = (await db.execute(
        text(f"SELECT * FROM {tparam} WHERE api_id = :a ORDER BY sort_order, id"), {"a": arow["id"]},
    )).mappings().all()
    return True, _row_snapshot(arow, ds_name, prows), _row_meta(arow)


def _row_snapshot(arow, ds_name: str, prows) -> Dict[str, Any]:
    """原生 SQL 读出的 API 行 + 参数行 → 快照（与 build_snapshot 的结果可直接比较）。"""
    snap: Dict[str, Any] = {"_v": SNAPSHOT_VERSION}
    for f in _COPY_FIELDS:
        v = arow.get(f)
        snap[f] = _norm(_as_bool(v) if f in _BOOL_API_FIELDS else v)
    snap["datasource_name"] = ds_name or ""
    snap["parameters"] = [
        {k: _norm(_as_bool(p.get(k)) if k in _BOOL_PARAM_FIELDS else p.get(k)) for k in PARAM_FIELDS} for p in prows
    ]
    return snap


def _row_meta(arow) -> Dict[str, Any]:
    return {"id": arow["id"], "name": arow.get("name"), "version": arow.get("version"), "status": arow.get("status"),
            "is_locked": bool(_as_bool(arow.get("is_locked")))}


async def read_env_project_snapshots(db, suffix: str, project_code: str):
    """一次读出某环境里一个项目的全部 API 快照：{(method, url_path): (快照, meta)}。项目不存在返回 None。"""
    from sqlalchemy import text
    tp, ta, td, tparam = (f"src_dop_projects{suffix}", f"src_dop_api_configs{suffix}",
                          f"src_dop_datasources{suffix}", f"src_dop_api_parameters{suffix}")
    try:
        pid = (await db.execute(text(f"SELECT id FROM {tp} WHERE code = :c"), {"c": project_code})).scalar()
    except Exception as e:  # noqa: BLE001
        raise ReleaseError(f"读取{'生产' if not suffix else '预发'}环境失败：{e}")
    if not pid:
        return None
    arows = (await db.execute(text(f"SELECT * FROM {ta} WHERE project_id = :p"), {"p": pid})).mappings().all()
    if not arows:
        return {}
    ids = [r["id"] for r in arows]
    params: Dict[int, list] = {}
    for p in (await db.execute(text(
        f"SELECT * FROM {tparam} WHERE api_id IN ({', '.join(str(int(i)) for i in ids)}) ORDER BY sort_order, id"
    ))).mappings().all():
        params.setdefault(p["api_id"], []).append(p)
    ds_ids = {r["datasource_id"] for r in arows if r.get("datasource_id")}
    ds_names = {}
    if ds_ids:
        ds_names = {i: n for i, n in (await db.execute(text(
            f"SELECT id, name FROM {td} WHERE id IN ({', '.join(str(int(i)) for i in ds_ids)})"
        ))).all()}
    return {(r["method"], r["url_path"]): (_row_snapshot(r, ds_names.get(r.get("datasource_id"), ""), params.get(r["id"], [])),
                                          _row_meta(r)) for r in arows}


async def prod_presence(db, project_code: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """预发用：某项目在生产里已有的 API {(method, url_path): {"version", "status"}}。"""
    from sqlalchemy import text
    pid = (await db.execute(text("SELECT id FROM src_dop_projects WHERE code = :c"), {"c": project_code})).scalar()
    if not pid:
        return {}
    rows = (await db.execute(text(
        "SELECT method, url_path, version, status FROM src_dop_api_configs WHERE project_id = :p"
    ), {"p": pid})).all()
    return {(r[0], r[1]): {"version": r[2], "status": r[3]} for r in rows}


async def delete_everywhere(db, project_code: str, method: str, url_path: str) -> Tuple[Optional[int], Optional[int]]:
    """删除审核通过（生产进程执行）：删除生产和预发中的该 API。返回 (生产 api_id, 预发 api_id)。"""
    from sqlalchemy import text
    _, prod_api = await find_target(db, project_code, method, url_path)
    prod_id = None
    if prod_api:
        prod_id = prod_api.id
        try:
            from app.services.engine import _clear_api_cache, _clear_prewarm_params
            await _clear_api_cache(prod_id)
            await _clear_prewarm_params(prod_id)
        except Exception as e:  # noqa: BLE001
            log.warning(f"删除前清缓存失败（不影响删除）| api_id={prod_id} | error={e}")
        await db.execute(delete(ApiParameter).where(ApiParameter.api_id == prod_id))
        await db.delete(prod_api)
        await db.flush()

    await delete_base(db, project_code, method, url_path)

    pre_id = None
    try:
        pid = (await db.execute(text("SELECT id FROM src_dop_projects_pre WHERE code = :c"), {"c": project_code})).scalar()
        if pid:
            pre_id = (await db.execute(text(
                "SELECT id FROM src_dop_api_configs_pre WHERE project_id = :p AND method = :m AND url_path = :u"
            ), {"p": pid, "m": method, "u": url_path})).scalar()
        if pre_id:
            for table in ("src_dop_api_parameters_pre", "src_dop_api_approvals_pre", "src_dop_api_owner_approvals_pre"):
                try:
                    await db.execute(text(f"DELETE FROM {table} WHERE api_id = :a"), {"a": pre_id})
                except Exception:  # noqa: BLE001  老版本可能没有某张表
                    pass
            try:
                await db.execute(text("UPDATE src_dop_call_logs_pre SET api_id = NULL WHERE api_id = :a"), {"a": pre_id})
            except Exception:  # noqa: BLE001
                pass
            await db.execute(text("DELETE FROM src_dop_api_configs_pre WHERE id = :a"), {"a": pre_id})
    except Exception as e:  # noqa: BLE001
        raise ReleaseError(f"删除预发中的该 API 失败：{e}")
    return prod_id, pre_id


async def _pre_project_key(db, project_code: str) -> str:
    from sqlalchemy import text
    try:
        return (await db.execute(text("SELECT api_key FROM src_dop_projects_pre WHERE code = :c"),
                                 {"c": project_code})).scalar() or ""
    except Exception:  # noqa: BLE001
        return ""


async def create_project_from_pre(db, project_code: str, submitter_username: str, approver: User) -> Project:
    """生产没有该项目时按预发的项目创建 (v2.24)：编码、名称、描述、项目 Key、成员、环境变量一并带过来。"""
    from sqlalchemy import text
    from app.models.models import ProjectMember, ProjectVariable
    try:
        prow = (await db.execute(text(
            "SELECT id, name, description, api_key, is_active FROM src_dop_projects_pre WHERE code = :c"
        ), {"c": project_code})).mappings().first()
    except Exception as e:  # noqa: BLE001
        raise ReleaseError(f"读取预发项目失败：{e}")
    if not prow:
        raise ReleaseError(f"生产和预发都不存在项目「{project_code}」")
    project = Project(code=project_code, name=prow["name"], description=prow["description"] or "",
                      api_key=prow["api_key"] or "", is_active=bool(_as_bool(prow["is_active"])) if prow["is_active"] is not None else True)
    db.add(project)
    await db.flush()

    # 成员：用户表两边共用，id 一致，直接按预发的成员和角色加入
    member_ids = set()
    for uid, role in (await db.execute(text(
        "SELECT m.user_id, m.project_role FROM src_dop_project_members_pre m "
        "JOIN src_dop_users u ON u.id = m.user_id WHERE m.project_id = :p"
    ), {"p": prow["id"]})).all():
        db.add(ProjectMember(project_id=project.id, user_id=uid, project_role=role or "developer"))
        member_ids.add(uid)
    if not member_ids:
        submitter = (await db.execute(select(User).where(User.username == submitter_username))).scalar_one_or_none()
        db.add(ProjectMember(project_id=project.id, user_id=(submitter or approver).id, project_role="manager"))

    try:
        for v in (await db.execute(text(
            "SELECT name, var_type, offset_days, date_format, const_value, description "
            "FROM src_dop_project_variables_pre WHERE project_id = :p"
        ), {"p": prow["id"]})).mappings().all():
            db.add(ProjectVariable(project_id=project.id, **dict(v)))
    except Exception as e:  # noqa: BLE001  老版本没有环境变量表
        log.warning(f"复制项目环境变量失败（不影响发布）| project={project_code} | error={e}")
    await db.flush()
    log.info(f"发布时自动创建生产项目 | project={project_code} | 成员 {len(member_ids)} 个")
    return project


async def apply_release(db, snapshot: Dict[str, Any], project_code: str, method: str, url_path: str,
                        submitter_username: str, approver: User) -> Tuple[ApiConfig, Optional[Dict[str, Any]]]:
    """把快照写入当前（生产）环境并直接上线。

    返回 (生产 API, 发布前的生产快照或 None)。调用方负责 commit。
    """
    from app.core.permissions import is_super_admin
    from app.services.api_lifecycle import STATUS_ONLINE

    project, api = await find_target(db, project_code, method, url_path)
    if not project:
        project = await create_project_from_pre(db, project_code, submitter_username, approver)
    elif not (project.api_key or "").strip():
        pre_key = await _pre_project_key(db, project_code)
        if pre_key:
            project.api_key = pre_key
            log.info(f"发布时补上生产项目 Key | project={project_code}")

    if (snapshot.get("api_type") or "sql") in ("sync", "update") and not is_super_admin(approver):
        raise ReleaseError("远端同步 / 更新类 API 只能由超级管理员发布到生产")
    from app.api.api_configs import plugin_edit_denied
    if ((snapshot.get("api_type") or "sql") == "plugin" or (snapshot.get("plugin_code") or "").strip()) \
            and plugin_edit_denied(approver):
        raise ReleaseError("插件类 API 只能由超级管理员发布到生产")

    ds_id = None
    ds_name = snapshot.get("datasource_name") or ""
    if ds_name:
        ds_id = (await db.execute(select(DataSource.id).where(DataSource.name == ds_name))).scalars().first()
        if not ds_id:
            raise ReleaseError(f"生产环境不存在数据源「{ds_name}」，请先在生产创建同名数据源")

    # 多源 SQL (v2.22)：SQL 里按名称引用数据源（数据源名.库名.表名），生产需有同名数据源
    if (snapshot.get("api_type") or "sql") == "federated":
        try:
            from app.services.federated import referenced_catalogs
            names = referenced_catalogs(snapshot.get("sql_template") or "")
        except ImportError:
            names = []
        if names:
            found = set((await db.execute(select(DataSource.name).where(DataSource.name.in_(names)))).scalars().all())
            missing = [n for n in names if n not in found]
            if missing:
                raise ReleaseError(f"生产环境不存在数据源「{'」「'.join(missing)}」（多源 SQL 中引用），请先在生产创建同名数据源")

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
    # v2.24：发布到生产后为「上线 + 锁定」，生产里只有项目管理员 / 超管能解锁后修改
    api.is_locked = True
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


async def pre_delete_blocked(db, api) -> Optional[str]:
    """预发：该 API 已发布到生产时返回提示语（不能在预发单独删除），否则 None。"""
    from app.core.runtime_env import IS_PRE
    if not IS_PRE:
        return None
    project = (await db.execute(select(Project).where(Project.id == api.project_id))).scalar_one_or_none()
    if not project:
        return None
    try:
        _, _, meta = await read_env_snapshot(db, "", project.code, api.method, api.url_path)
    except ReleaseError:
        return None
    if not meta:
        return None
    return "该 API 已发布到生产，预发不能单独删除。请提交删除审核，生产管理员同意后会同时删除生产和预发中的该 API"


# ---------------------------------------------------------------------------
# 同步基线与「拉取生产」(v2.24)：类似 git，用两边最近一次一致时的配置（基线）判断差异来自哪边
# ---------------------------------------------------------------------------
SYNC_STATES = {
    "same": "一致",
    "pre_ahead": "预发有新改动",        # 生产 = 基线，预发改过 → 待发布到生产
    "prod_ahead": "生产有变更",         # 预发 = 基线，生产改过 → 可拉取到预发
    "both": "两边都有改动",
    "diverged": "有差异",               # 没有基线，分不清是哪边改的
    "prod_only": "仅生产有",
    "pre_only": "仅预发有",
}


def _field_val(snap: Dict[str, Any], f: str) -> Any:
    return _params_text(snap.get(f, [])) if f == "parameters" else _norm(snap.get(f))


def same_snapshot(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    """两份快照是否一致。只做逐字段比较，不生成差异明细（大 HTML / JS 生成差异很慢）。"""
    if a is None or b is None:
        return a is None and b is None
    return all(_field_val(a, f) == _field_val(b, f) for f, _, _ in API_FIELDS + [("parameters", "", True)])


def sync_state(prod: Optional[Dict[str, Any]], pre: Optional[Dict[str, Any]], base: Optional[Dict[str, Any]]) -> str:
    if prod is None and pre is None:
        return "same"
    if prod is None:
        return "pre_only"
    if pre is None:
        return "prod_only"
    if same_snapshot(prod, pre):
        return "same"
    if base is None:
        return "diverged"
    prod_changed, pre_changed = not same_snapshot(prod, base), not same_snapshot(pre, base)
    if prod_changed and pre_changed:
        return "both"
    if prod_changed:
        return "prod_ahead"
    if pre_changed:
        return "pre_ahead"
    return "same"


async def load_bases(db, project_code: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """项目下所有 API 的基线 {(method, url_path): {"snapshot", "source", "username", "updated_at"}}。

    没有基线记录的（本功能上线前发布的），用最近一次审核通过的发布单快照代替。
    """
    from app.models.models import EnvSyncBase, ReleaseRequest
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    rows = (await db.execute(
        select(ReleaseRequest).where(ReleaseRequest.project_code == project_code, ReleaseRequest.status == "approved",
                                     ReleaseRequest.action == "publish").order_by(ReleaseRequest.id)
    )).scalars().all()
    for r in rows:
        try:
            out[(r.method, r.url_path)] = {"snapshot": json.loads(r.snapshot or "{}"), "source": "release",
                                           "username": r.reviewer_username, "updated_at": r.reviewed_at}
        except ValueError:
            pass
    for b in (await db.execute(select(EnvSyncBase).where(EnvSyncBase.project_code == project_code))).scalars().all():
        try:
            out[(b.method, b.url_path)] = {"snapshot": json.loads(b.snapshot or "{}"), "source": b.source,
                                           "username": b.username, "updated_at": b.updated_at}
        except ValueError:
            pass
    return out


async def save_base(db, project_code: str, method: str, url_path: str, snapshot: Dict[str, Any],
                    prod_version: Optional[int], source: str, username: str) -> None:
    from app.models.models import EnvSyncBase
    from app.core.timezone import now as _now
    row = (await db.execute(select(EnvSyncBase).where(
        EnvSyncBase.project_code == project_code, EnvSyncBase.method == method, EnvSyncBase.url_path == url_path,
    ))).scalar_one_or_none()
    if row is None:
        row = EnvSyncBase(project_code=project_code, method=method, url_path=url_path)
        db.add(row)
    row.snapshot = json.dumps(snapshot, ensure_ascii=False, default=str)
    row.prod_version = prod_version
    row.source = source
    row.username = username
    row.updated_at = _now()
    await db.flush()


async def delete_base(db, project_code: str, method: str, url_path: str) -> None:
    from app.models.models import EnvSyncBase
    await db.execute(delete(EnvSyncBase).where(
        EnvSyncBase.project_code == project_code, EnvSyncBase.method == method, EnvSyncBase.url_path == url_path,
    ))


async def apply_to_pre(db, snapshot: Dict[str, Any], project: Project, api: Optional[ApiConfig],
                       prod_status: Optional[str], user: User, ignore_lock: bool = False) -> ApiConfig:
    """预发进程：把生产的配置写进预发（拉取生产）。api 为 None 时在预发新建。调用方负责 commit。

    ignore_lock：超管「从生产同步全部」时，锁定的 API 也覆盖（锁定状态保持不变）。
    """
    from app.core.permissions import is_super_admin
    from app.api.api_configs import plugin_edit_denied
    from app.services.api_lifecycle import STATUS_DRAFT, STATUS_ONLINE, STATUS_PENDING, STATUS_APPROVED

    if api is not None:
        if getattr(api, "is_locked", False) and not ignore_lock:
            raise ReleaseError("预发中的该 API 已锁定，请先解锁")
        if api.status in (STATUS_PENDING, STATUS_APPROVED):
            raise ReleaseError("预发中的该 API 正在上线审批中，请先撤回")
    api_type = snapshot.get("api_type") or "sql"
    if api_type in ("sync", "update") and not is_super_admin(user):
        raise ReleaseError("远端同步 / 更新类 API 只能由超级管理员拉取")
    if (api_type == "plugin" or (snapshot.get("plugin_code") or "").strip()) and plugin_edit_denied(user):
        raise ReleaseError("插件类 API 只能由超级管理员拉取")

    ds_id = None
    ds_name = snapshot.get("datasource_name") or ""
    if ds_name:
        ds_id = (await db.execute(select(DataSource.id).where(DataSource.name == ds_name))).scalars().first()
        if not ds_id:
            raise ReleaseError(f"预发环境不存在数据源「{ds_name}」，请先在预发创建同名数据源")

    if api is None:
        api = ApiConfig(project_id=project.id, created_by=user.id, owner_id=user.id, version=0,
                        status=STATUS_ONLINE if prod_status == STATUS_ONLINE else STATUS_DRAFT)
        db.add(api)
    for f in _COPY_FIELDS:
        if f in snapshot:
            setattr(api, f, snapshot[f])
    api.datasource_id = ds_id
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
    try:
        from app.services.engine import _clear_api_cache, _clear_prewarm_params
        await _clear_api_cache(api.id)
        await _clear_prewarm_params(api.id)
    except Exception as e:  # noqa: BLE001
        log.warning(f"拉取后清缓存失败（不影响拉取）| api_id={api.id} | error={e}")
    return api
