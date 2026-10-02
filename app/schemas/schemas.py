"""
Pydantic 数据校验模型（请求/响应）
"""

from pydantic import BaseModel, Field, model_validator, field_validator
from typing import Optional, List, Any
from datetime import datetime


# ========== NULL 兜底基类 ==========
# 历史数据里很多列是 NULL，而响应模型字段声明为 str/int/bool/float（非 Optional）。
# Pydantic 拿 None 校验这些类型会直接 500。这里在校验前把 None 统一转成类型默认值：
#   str->""  int->0  float->0.0  bool->False
# 只对「声明了非 Optional 标量类型」的字段生效；Optional 字段、datetime、嵌套模型不动。
class NullSafeModel(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _coerce_none(cls, data):
        # data 可能是 ORM 对象（from_attributes）或 dict
        if data is None:
            return data
        defaults = {str: "", int: 0, float: 0.0, bool: False}
        try:
            fields = cls.model_fields
        except Exception:
            return data

        def _default_for(ann):
            # 仅当字段类型正好是 str/int/bool/float 时兜底（Optional[...] 的 annotation 不是这些裸类型，跳过）
            return defaults.get(ann, None)

        if isinstance(data, dict):
            for fname, finfo in fields.items():
                if fname in data and data[fname] is None:
                    dv = _default_for(finfo.annotation)
                    if dv is not None:
                        data[fname] = dv
            return data
        else:
            # ORM 对象：包一层，读到 None 的标量字段就替换默认值
            patched = {}
            for fname, finfo in fields.items():
                alias = finfo.alias or fname
                val = getattr(data, fname, None)
                if val is None:
                    dv = _default_for(finfo.annotation)
                    if dv is not None:
                        patched[alias] = dv
                    else:
                        patched[alias] = None
                else:
                    patched[alias] = val
            return patched


# ========== 通用响应 ==========
class ApiResponse(BaseModel):
    status: bool = True
    data: Any = None
    msg: str = ""


# ========== 用户 ==========
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    nickname: str = ""


# ========== 用户管理 ==========
class UserCreate(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=64)  # 用户名（显示名称）
    username: str = Field(..., min_length=2, max_length=64)  # 账户（登录用）
    password: str = Field(..., min_length=4, max_length=128)
    is_active: bool = True
    global_role: str = "user"   # super_admin / developer / user


class UserUpdate(BaseModel):
    nickname: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    global_role: Optional[str] = None   # super_admin / user（仅超管可改）


class UserOut(NullSafeModel):
    id: int
    nickname: str = ""
    username: str
    is_active: bool
    global_role: str = "user"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ========== 项目 ==========
class ProjectCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, pattern=r'^[a-zA-Z][a-zA-Z0-9_]*$')
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    api_key: str = ""


class ProjectUpdate(BaseModel):
    code: Optional[str] = Field(None, min_length=1, max_length=64, pattern=r'^[a-zA-Z][a-zA-Z0-9_]*$')
    name: Optional[str] = None
    description: Optional[str] = None
    api_key: Optional[str] = None


class ProjectOut(NullSafeModel):
    id: int
    code: str = ""
    name: str
    description: str
    api_key: str
    is_active: bool
    api_count: int = 0
    total_calls: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ========== 数据源 ==========
class DataSourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    type: str = "mysql"
    host: str
    port: int
    username: str = ""
    password: str = ""
    database_name: str = ""
    pool_size: int = 10
    extra_config: str = "{}"
    project_scope: str = ""   # 可用项目编码，逗号分隔，空 = 全部项目


class DataSourceUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    database_name: Optional[str] = None
    pool_size: Optional[int] = None
    extra_config: Optional[str] = None
    project_scope: Optional[str] = None


class DataSourceOut(NullSafeModel):
    id: int
    name: str
    type: str
    host: str
    port: int
    username: str
    database_name: str
    pool_size: int
    extra_config: str
    status: str
    last_test_at: Optional[datetime] = None
    api_count: int = 0
    created_by: Optional[int] = None
    created_by_name: str = ""
    project_scope: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ========== API 参数 ==========
class ApiParameterSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    param_type: Optional[str] = "string"
    required: Optional[bool] = False
    default_value: Optional[str] = ""
    description: Optional[str] = ""
    sort_order: Optional[int] = 0
    # v2.1: 嵌套结构定义 JSON 文本（array/object 类型使用，见 models.ApiParameter）
    #   普通参数没有嵌套结构，前端会传 null；这里容忍 None 并兜底为空串，避免 422。
    item_schema: Optional[str] = ""

    @field_validator("param_type", "default_value", "description", "item_schema", mode="before")
    @classmethod
    def _none_to_str_default(cls, v, info):
        if v is None:
            return "string" if info.field_name == "param_type" else ""
        return v

    @field_validator("required", mode="before")
    @classmethod
    def _none_to_false(cls, v):
        return False if v is None else v

    @field_validator("sort_order", mode="before")
    @classmethod
    def _none_to_zero(cls, v):
        return 0 if v is None else v


# ========== API 配置 ==========
def _normalize_url_path(v):
    """规整 url_path：去首尾空白、去尾部多余斜杠、确保以 / 开头。
    避免因存了首尾空格/尾斜杠导致网关精确匹配失败（5003 未找到匹配的 API）。"""
    if v is None:
        return v
    s = str(v).strip()
    if not s:
        return s
    # 去掉尾部多余斜杠（但保留根 "/"）
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    if not s.startswith("/"):
        s = "/" + s
    return s


class ApiConfigCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    url_path: str = Field(..., min_length=1, max_length=256)
    method: str = "GET"
    datasource_id: Optional[int] = None
    sql_template: str = ""
    pipeline_steps: str = ""   # JSON 数组字符串；非空时走多步管线，覆盖 sql_template
    # API 类型：sql（数据API，默认）/ html（静态页面API）
    api_type: str = "sql"
    html_content: str = ""
    css_content: str = ""
    js_content: str = ""
    plugin_code: str = ""
    is_enabled: bool = True
    api_key: str = ""
    require_api_key: bool = True
    cache_enabled: bool = False
    cache_ttl: int = 300
    cache_prewarm: bool = False   # v2.13: 缓存自动预热
    prewarm_param_overrides: str = ""   # v2.14: 预热参数覆盖 {"字段":"${变量}"}，支持多组数组
    prewarm_stop_daily: bool = False    # v2.16: 跨日停止预热
    sync_tables: str = ""               # v2.17: 数据同步白名单
    timeout: int = 30
    rate_limit_enabled: bool = False
    rate_limit_qps: int = 100
    max_rows: int = 10000
    parameters: List[ApiParameterSchema] = []

    @field_validator("url_path")
    @classmethod
    def _v_url_path(cls, v):
        return _normalize_url_path(v)

    @field_validator("method")
    @classmethod
    def _v_method(cls, v):
        return (v or "GET").strip().upper()

    @model_validator(mode="before")
    @classmethod
    def _coerce_none_fields(cls, data):
        # 前端对不适用的字段常传 null（如 html API 传 sql_template=null），
        # 这些字段声明为非 Optional str，None 会 422。这里统一把 None 兜底为空串。
        if isinstance(data, dict):
            str_fields = ("description", "sql_template", "pipeline_steps", "api_type",
                          "html_content", "css_content", "js_content", "plugin_code", "api_key")
            for f in str_fields:
                if f in data and data[f] is None:
                    data[f] = ""
            if data.get("api_type") in (None, ""):
                data["api_type"] = "sql"
        return data


class ApiConfigUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    url_path: Optional[str] = None
    method: Optional[str] = None

    @field_validator("url_path")
    @classmethod
    def _v_url_path_upd(cls, v):
        return _normalize_url_path(v) if v is not None else v

    @field_validator("method")
    @classmethod
    def _v_method_upd(cls, v):
        return (v.strip().upper() if v else v)
    datasource_id: Optional[int] = None
    sql_template: Optional[str] = None
    pipeline_steps: Optional[str] = None
    api_type: Optional[str] = None
    html_content: Optional[str] = None
    css_content: Optional[str] = None
    js_content: Optional[str] = None
    plugin_code: Optional[str] = None
    is_enabled: Optional[bool] = None
    api_key: Optional[str] = None
    require_api_key: Optional[bool] = None
    cache_enabled: Optional[bool] = None
    cache_ttl: Optional[int] = None
    cache_prewarm: Optional[bool] = None
    prewarm_param_overrides: Optional[str] = None
    prewarm_stop_daily: Optional[bool] = None
    sync_tables: Optional[str] = None
    timeout: Optional[int] = None
    rate_limit_enabled: Optional[bool] = None
    rate_limit_qps: Optional[int] = None
    max_rows: Optional[int] = None
    parameters: Optional[List[ApiParameterSchema]] = None


class ApiParameterOut(NullSafeModel):
    id: int
    name: str
    param_type: str
    required: bool
    default_value: str
    description: str
    sort_order: int
    item_schema: Optional[str] = None

    model_config = {"from_attributes": True}


class ApiConfigOut(NullSafeModel):
    id: int
    project_id: int
    datasource_id: Optional[int] = None
    owner_id: Optional[int] = None
    owner_name: Optional[str] = ""
    name: str
    description: str
    url_path: str
    method: str
    sql_template: str
    pipeline_steps: str = ""
    api_type: str = "sql"
    html_content: str = ""
    css_content: str = ""
    js_content: str = ""
    plugin_code: str = ""
    status: str = "draft"
    is_locked: bool = False
    approval_submitter_id: Optional[int] = None   # 待上线时，该上线申请的提交者（前端判断显示"上线"按钮）
    is_enabled: bool
    api_key: str
    require_api_key: bool = True
    cache_enabled: bool
    cache_ttl: int
    cache_prewarm: bool = False
    prewarm_param_overrides: str = ""
    prewarm_stop_daily: bool = False
    sync_tables: str = ""
    timeout: int
    rate_limit_enabled: bool
    rate_limit_qps: int
    max_rows: int
    version: int
    parameters: List[ApiParameterOut] = []
    datasource_name: str = ""
    total_calls: int = 0
    avg_time_ms: float = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ========== 调用日志 ==========
class CallLogOut(NullSafeModel):
    id: int
    api_id: Optional[int] = None
    project_id: Optional[int] = None
    api_name: str
    url_path: str
    method: str
    request_params: Optional[str] = None
    response_status: Optional[str] = None
    response_time_ms: Optional[float] = None
    status_code: Optional[int] = None
    error_message: Optional[str] = None
    client_ip: Optional[str] = None
    response_data: Optional[str] = ""
    is_slow_query: Optional[bool] = None
    call_source: str = "gateway"
    # v2.4 关键节点
    rendered_sql: Optional[str] = None
    executed_sql: Optional[str] = None
    render_time_ms: Optional[float] = None
    query_time_ms: Optional[float] = None
    row_count: Optional[int] = None
    cache_hit: Optional[bool] = None
    error_stack: Optional[str] = None
    trace_id: Optional[str] = ""
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ========== 仪表盘统计 ==========
class DashboardStats(BaseModel):
    total_apis: int = 0
    total_calls: int = 0
    avg_response_time: float = 0
    failure_rate: float = 0
    today_calls: int = 0


class ApiRankItem(BaseModel):
    api_id: int
    api_name: str
    url_path: str
    value: float  # 耗时或调用次数
    call_count: int = 0


# ========== 动态调用测试 ==========
class TestApiRequest(BaseModel):
    params: dict = {}
