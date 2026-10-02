"""
动态调用网关路由
处理所有 /gw/{project_code}/... 的请求，匹配到对应 API 配置并执行
所有外部调用（含成功和失败）都记录到 call_logs 表
"""

import json
import time
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func

from app.core.database import get_db
from app.core.config import settings
from app.core.errors import ErrCode, get_err_msg
from app.core.logging import get_logger
from app.models.models import ApiConfig, Project, CallLog, ApiParameter
from app.services.engine import execute_api
from app.services import call_log_writer, gateway_cache, result_cache
from app.core.logging import is_debug

log = get_logger("gateway")

# 每请求一条的网关 INFO 日志开关（log.gateway_info）
_GATEWAY_INFO = settings.log.gateway_info

# 使用配置的网关前缀
GATEWAY_PREFIX = settings.gateway.prefix.rstrip("/")
router = APIRouter(prefix=GATEWAY_PREFIX, tags=["动态调用网关"])


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        log.debug(f"从 X-Forwarded-For 获取客户端 IP: {ip}")
        return ip
    ip = request.client.host if request.client else "unknown"
    return ip


def _check_ip_whitelist(client_ip: str) -> bool:
    """检查 IP 白名单"""
    if not settings.security.ip_whitelist:
        return True
    allowed = client_ip in settings.security.ip_whitelist
    if not allowed:
        log.warning(f"IP 白名单拦截 | client_ip={client_ip} | whitelist={settings.security.ip_whitelist}")
    return allowed


async def _verify_api_key(
    request: Request,
    api_config: ApiConfig,
    project: Project,
) -> bool:
    """验证 API Key（Header 或 Query）

    优先尊重 api_config.require_api_key 开关：
      - False -> 直接放行，不校验
      - True  -> 取 api_config.api_key 或 project.api_key 作为期望值进行比对；
                 如果两者都没配置，记一条告警并放行（保持向后兼容，避免误锁库）。
    """
    # 显式关闭则直接放行
    require_flag = getattr(api_config, "require_api_key", True)
    if require_flag is False:
        log.debug(f"API Key 校验已关闭（require_api_key=False） | api_id={api_config.id}")
        return True

    api_key = (
        request.headers.get("xPftKey")
        or request.query_params.get("api_key")
        or ""
    )

    expected_key = api_config.api_key or project.api_key

    if not expected_key:
        log.debug(f"API Key 未配置，跳过认证 | api_id={api_config.id}")
        return True

    import secrets as _secrets
    valid = _secrets.compare_digest(str(api_key), str(expected_key))
    if not valid:
        log.warning(f"API Key 验证失败 | api_id={api_config.id} | 提供的Key={api_key[:10]}... | 期望Key={expected_key[:10]}...")
    else:
        log.debug(f"API Key 验证通过 | api_id={api_config.id}")
    return valid


async def _record_error_log(
    db: AsyncSession,
    api_id,  # Optional[int]: None 表示未匹配到 API
    project_id,  # Optional[int]: None 表示未匹配到项目
    api_name: str,
    url_path: str,
    method: str,
    request_params: str,
    error_message: str,
    client_ip: str,
    response_data: str,
    elapsed_ms: float,
    status_code: int = 400,
    trace_id: str = "",
):
    """记录失败调用日志到 call_logs 表（用于网关拦截的请求）。

    v2.19: 交给后台批量写入（call_log_writer），不再占用请求事务。db 参数保留仅为兼容。
    """
    try:
        call_log_writer.submit(dict(
            api_id=api_id,
            project_id=project_id,
            api_name=api_name,
            url_path=url_path,
            method=method,
            request_params=request_params[:5000] if request_params else "{}",
            response_status="error",
            response_time_ms=elapsed_ms,
            status_code=status_code,
            error_message=error_message[:2000] if error_message else "",
            client_ip=client_ip,
            response_data=response_data[:5000] if response_data else "",
            is_slow_query=False,
            call_source="gateway",
            trace_id=trace_id or "",
        ))
        log.debug(f"网关拦截日志已登记 | url_path={url_path} | trace_id={trace_id} | error={error_message[:100]}")
    except Exception as e:
        log.error(f"记录网关拦截日志失败 | error={str(e)}")


def _build_html_page(api_config: ApiConfig, params: dict) -> str:
    """根据 ApiConfig 上的 html/css/js 字段拼装一个完整的静态 HTML 页面

    - html_content：放在 <body> 内（允许直接是片段；如果用户已写完整 <html>，则原样返回）
    - css_content： 注入到 <head><style> 中
    - js_content：  注入到 <body> 末尾的 <script> 中
    - 通过 window.__API_PARAMS__ 把调用时的 query/body 参数透传给前端 JS
    """
    html_body = (api_config.html_content or "").strip()
    css_body = (api_config.css_content or "").strip()
    js_body = (api_config.js_content or "").strip()

    params_js = (
        f"<script>window.__API_PARAMS__ = "
        f"{json.dumps(params, default=str, ensure_ascii=False)};</script>"
    )

    # 若用户已经写了完整的 HTML 文档，原样返回（仅注入参数变量），不再二次包裹
    head_check = html_body[:200].lower().lstrip()
    if head_check.startswith("<!doctype") or head_check.startswith("<html"):
        lower_body = html_body.lower()
        if "</head>" in lower_body:
            idx = lower_body.rfind("</head>")
            return html_body[:idx] + params_js + html_body[idx:]
        return params_js + html_body

    import html as _html
    title = _html.escape(api_config.name or "OneData Page")
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "  <meta charset=\"UTF-8\" />\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        f"  <title>{title}</title>\n"
        f"  <style>{css_body}</style>\n"
        f"  {params_js}\n"
        "</head>\n"
        "<body>\n"
        f"{html_body}\n"
        f"<script>{js_body}</script>\n"
        "</body>\n"
        "</html>"
    )


async def _parse_request_params(request: Request, method: str, db: AsyncSession = None) -> dict:
    """解析请求参数（Query + Body + Header）

    v2.0.2 修正: 移除此前误写死在 '/weibo/comment' 上的类型转换逻辑
    （它对每个 GET 请求都额外查一次库，且用错误 API 的参数定义去转换所有请求）。
    Query/Header 字符串参数的类型转换统一移到引擎层 _coerce_param_types，
    按实际命中的 API 自己的参数定义执行 —— 对所有声明了 number/boolean/array
    类型的 GET API 都生效，而不只 /weibo/comment。
    """
    params = {}

    # Query 参数（原始字符串，类型转换在引擎层按 API 参数定义进行）
    for key, value in request.query_params.items():
        if key != "api_key":
            params[key] = value

    # Body 参数
    if method in ("POST", "PUT"):
        try:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                body = await request.json()
                if isinstance(body, dict):
                    params.update(body)
                if is_debug():
                    log.debug(f"解析 JSON Body | body={json.dumps(body, default=str, ensure_ascii=False)[:500]}")
            elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                form = await request.form()
                params.update(dict(form))
                log.debug(f"解析 Form Body | form={dict(form)}")
        except Exception as e:
            log.warning(f"解析请求体失败 | error={str(e)}")

    # Header 参数 (X-Param- 前缀)
    for key, value in request.headers.items():
        if key.lower().startswith("x-param-"):
            param_name = key[8:]
            params[param_name] = value

    return params


@router.api_route("/{project_code}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def gateway_handler(
    project_code: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """动态网关入口 - 所有请求（含失败）都记录日志"""
    start_time = time.time()
    client_ip = _get_client_ip(request)
    method = request.method.upper()
    url_path = f"/{path}"
    full_url = f"{GATEWAY_PREFIX}/{project_code}{url_path}"
    # 从中间件注入的 trace_id（请求级唯一）
    trace_id = getattr(request.state, "trace_id", "") or ""

    # 尽早解析请求参数，以便记录到日志中
    params = await _parse_request_params(request, method, db=db)
    request_params_json = json.dumps(params, default=str, ensure_ascii=False)

    log.debug(f"网关请求 | {method} {full_url} | client_ip={client_ip} | params={request_params_json[:500]}")

    # 1. IP 白名单检查
    if not _check_ip_whitelist(client_ip):
        elapsed = (time.time() - start_time) * 1000
        error_msg = get_err_msg(ErrCode.AUTH_IP_BLOCKED)
        result = {"status": False, "code": ErrCode.AUTH_IP_BLOCKED.value, "msg": error_msg, "data": None, "trace_id": trace_id}
        log.warning(f"网关拒绝: IP 不在白名单 | {method} {full_url} | client_ip={client_ip}")
        await _record_error_log(
            db, api_id=None, project_id=None, api_name=f"[IP拦截:{url_path}:{project_code}]",
            url_path=full_url, method=method,
            request_params=request_params_json, error_message=error_msg,
            client_ip=client_ip, response_data=json.dumps(result, ensure_ascii=False),
            elapsed_ms=elapsed, status_code=403, trace_id=trace_id,
        )
        return result

    # 2~3. 查找项目与 API（v2.19: 命中配置缓存时不查库，见 gateway_cache）
    project, api_config, api_params, datasource = await gateway_cache.resolve(db, project_code, method, url_path)
    if not project or not project.is_active:
        elapsed = (time.time() - start_time) * 1000
        error_msg = get_err_msg(ErrCode.GW_PROJECT_DISABLED)
        result = {"status": False, "code": ErrCode.GW_PROJECT_DISABLED.value, "msg": error_msg, "data": None, "trace_id": trace_id}
        log.warning(f"网关拒绝: 项目不存在或已禁用 | project_code={project_code}")
        await _record_error_log(
            db, api_id=None, project_id=None, api_name=f"[{url_path}:项目不存在:{project_code}]",
            url_path=full_url, method=method,
            request_params=request_params_json, error_message=error_msg,
            client_ip=client_ip, response_data=json.dumps(result, ensure_ascii=False),
            elapsed_ms=elapsed, status_code=404, trace_id=trace_id,
        )
        return result

    log.debug(f"项目匹配成功 | project_code={project_code} | project_id={project.id} | project_name={project.name}")

    # 3. 匹配 API
    #    兼容性说明 (v2.0.2 修正): 只要 is_enabled=True 且 status 不是明确的 'offline'
    #    即可对外调用；url_path 容忍首尾空格/尾部斜杠。具体规则见 gateway_cache.load_api。
    if not api_config:
        elapsed = (time.time() - start_time) * 1000
        error_msg = f"未找到匹配的 API: {method} /{project_code}{url_path}"
        result = {"status": False, "code": ErrCode.GW_API_NOT_FOUND.value, "msg": error_msg, "data": None, "trace_id": trace_id}
        log.warning(f"网关拒绝: 未找到匹配 API | {method} {url_path} | project_id={project.id}")
        await _record_error_log(
            db, api_id=None, project_id=project.id, api_name=f"[API未找到:{url_path}:{method}]",
            url_path=full_url, method=method,
            request_params=request_params_json, error_message=error_msg,
            client_ip=client_ip, response_data=json.dumps(result, ensure_ascii=False),
            elapsed_ms=elapsed, status_code=404, trace_id=trace_id,
        )
        return result

    log.debug(f"API 匹配成功 | api_id={api_config.id} | api_name={api_config.name} | url_path={api_config.url_path}")

    # 数据同步 API (v2.17+) 的调用鉴权走下面统一的 API Key 流程，与普通 API 一致。
    # 说明：创建/修改这类 API 仍然只有超级管理员可以操作（见 api_configs.py），
    # 但调用方往往是拿不到超管 token 的外部系统，所以调用侧用 xpftkey。
    # ⚠️ 这意味着持有该 API Key 的人就能往目标表写数据，
    #    key 要按写权限的标准保管：不要对外散发、不要写进前端代码、定期轮换。

    # 4. API Key 认证
    if not await _verify_api_key(request, api_config, project):
        elapsed = (time.time() - start_time) * 1000
        error_msg = get_err_msg(ErrCode.GW_API_KEY_INVALID)
        result = {"status": False, "code": ErrCode.GW_API_KEY_INVALID.value, "msg": error_msg, "data": None, "trace_id": trace_id}
        log.warning(f"网关拒绝: API Key 无效 | api_id={api_config.id} | {method} {full_url}")
        await _record_error_log(
            db, api_id=api_config.id, project_id=project.id, api_name=f'[{api_config.name}:{method}]',
            url_path=full_url, method=method,
            request_params=request_params_json, error_message=error_msg,
            client_ip=client_ip, response_data=json.dumps(result, ensure_ascii=False),
            elapsed_ms=elapsed, status_code=401, trace_id=trace_id,
        )
        # HTML 类型 API 鉴权失败：返回 HTML 错误页，避免被浏览器当作 JSON 解析失败
        if (api_config.api_type or "sql").lower() == "html":
            return HTMLResponse(
                content=(
                    "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"/>"
                    "<title>401 Unauthorized</title></head>"
                    f"<body style=\"font-family:system-ui;padding:40px;\">"
                    f"<h1>401 Unauthorized</h1><p>{error_msg}</p>"
                    f"<p style=\"color:#888;font-size:12px;\">trace_id: {trace_id}</p></body></html>"
                ),
                status_code=401,
            )
        return result

    log.debug(f"网关执行 | api_id={api_config.id} | api_name={api_config.name} | {method} {full_url} | params={request_params_json[:500]}")

    # 5a. HTML 类型 API：直接渲染静态页面返回，不走 SQL 引擎
    if (api_config.api_type or "sql").lower() == "html":
        try:
            html_page = _build_html_page(api_config, params)
            elapsed = (time.time() - start_time) * 1000
            log.info(
                f"网关响应(HTML) | api_id={api_config.id} | api_name={api_config.name} "
                f"| {method} {full_url} | elapsed={elapsed:.1f}ms | bytes={len(html_page)}"
            )
            # 写一条成功日志，便于在监控里看到调用
            try:
                call_log_writer.submit(dict(
                    api_id=api_config.id,
                    project_id=project.id,
                    api_name=api_config.name,
                    url_path=full_url,
                    method=method,
                    request_params=request_params_json[:5000],
                    response_status="success",
                    response_time_ms=elapsed,
                    status_code=200,
                    error_message="",
                    client_ip=client_ip,
                    response_data=f"[HTML page, {len(html_page)} bytes]",
                    is_slow_query=elapsed >= settings.query.slow_query_threshold,
                    call_source="gateway",
                    trace_id=trace_id,
                ))
            except Exception as log_err:
                log.warning(f"记录 HTML API 调用日志失败 | error={str(log_err)}")
            return HTMLResponse(content=html_page, status_code=200)
        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            error_msg = f"HTML 页面渲染失败: {str(e)}"
            log.exception(error_msg)
            await _record_error_log(
                db, api_id=api_config.id, project_id=project.id, api_name=api_config.name,
                url_path=full_url, method=method,
                request_params=request_params_json, error_message=error_msg,
                client_ip=client_ip, response_data="",
                elapsed_ms=elapsed, status_code=500, trace_id=trace_id,
            )
            return HTMLResponse(
                content=(
                    f"<h1>500 Internal Error</h1><pre>{error_msg}</pre>"
                    f"<p style=\"color:#888;font-size:12px;\">trace_id: {trace_id}</p>"
                ),
                status_code=500,
            )

    # 5. 执行 API
    #    v2.4: 日志由网关层统一建档 + 回填。建立 trace 上下文，
    #    execute_api 内部通过 get_trace() 往上下文写关键节点，执行后这里落库。
    from app.services.trace_context import TraceContext, set_trace, reset_trace
    tctx = TraceContext(trace_id=trace_id)
    _tok = set_trace(tctx)
    try:
        result = await execute_api(api_config, params, client_ip, db, trace_id=trace_id,
                                   api_params=api_params, datasource=datasource, raw_result=True)
    finally:
        reset_trace(_tok)

    # 6. 记录响应日志（每请求只打这一条 INFO）
    elapsed = (time.time() - start_time) * 1000
    ok = bool(result.get("status"))
    data = result.get("data")
    # v2.20: 结果统一成「序列化好的 JSON 字节」（开了缓存的 API 由引擎直接给出，
    # 命中缓存时无需解析；未开缓存的在这里序列化一次），日志预览也从这份字节里截取
    entry = data if isinstance(data, result_cache.CachedResult) else result_cache.CachedResult.from_data(data, compress=False)
    response_data = entry.preview(5000) if ok else ""
    if _GATEWAY_INFO:
        log.info(
            f"网关响应 | api_id={api_config.id} | api_name={api_config.name} | {method} {full_url} | "
            f"状态={'成功' if ok else '失败'} | elapsed={elapsed:.1f}ms | rows={tctx.row_count} | "
            f"cache={'hit' if tctx.cache_hit else 'miss'} | client_ip={client_ip} | "
            f"params={request_params_json[:500]} | msg={result.get('msg', '')} | "
            f"data_preview={(response_data[:500] if data else 'null')}"
        )

    # 6.5 建档 + 回填：登记一条完整的调用日志（含关键节点），由后台批量落库
    try:
        fields = dict(
            api_id=api_config.id,
            project_id=api_config.project_id,
            api_name=api_config.name,
            url_path=full_url,
            method=method,
            request_params=request_params_json[:5000],
            response_status="success" if ok else "error",
            response_time_ms=elapsed,
            status_code=200 if ok else 500,
            error_message="" if ok else str(result.get("msg", ""))[:2000],
            client_ip=client_ip,
            response_data=response_data,
            is_slow_query=elapsed >= settings.query.slow_query_threshold,
            call_source="gateway",
            trace_id=trace_id,
        )
        fields.update(tctx.as_log_fields())   # 渲染SQL/执行SQL/分阶段耗时/行数/缓存命中/错误堆栈
        call_log_writer.submit(fields)
    except Exception as log_err:
        log.warning(f"记录调用日志失败 | trace_id={trace_id} | error={str(log_err)}")

    # 7. 在返回中加入 code 字段和 trace_id
    if result.get("status"):
        result["code"] = 0
    else:
        result["code"] = ErrCode.GW_SQL_EXECUTE_FAILED.value
    result["trace_id"] = trace_id

    # 拼装响应：data 部分直接用序列化好的字节；客户端支持 gzip 且数据较大时，
    # 用预压缩块拼出 gzip 流（大结果传输量通常降到 1/10 左右）
    gzip_min = int(getattr(settings.gateway, "gzip_min_bytes", 0) or 0)
    gzip_ok = (
        gzip_min > 0 and entry.length >= gzip_min
        and "gzip" in request.headers.get("accept-encoding", "").lower()
    )
    body, gz = result_cache.build_response_body(result, entry, gzip_ok)
    headers = {"Vary": "Accept-Encoding"}
    if gz:
        headers["Content-Encoding"] = "gzip"
    return Response(content=body, media_type="application/json", headers=headers)
