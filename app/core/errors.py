"""
统一错误码定义 & 响应工具函数

错误码规则：
- 0: 成功
- 1xxx: 认证/权限相关
- 2xxx: 用户管理相关
- 3xxx: 项目管理相关
- 4xxx: API 配置相关
- 5xxx: 网关/调用相关
- 6xxx: 数据源相关
- 7xxx: 监控/日志相关
- 9xxx: 系统级错误
"""

from enum import IntEnum
from fastapi.responses import JSONResponse
from typing import Any, Optional


class ErrCode(IntEnum):
    """统一错误码枚举"""

    # ===== 成功 =====
    SUCCESS = 0

    # ===== 1xxx: 认证/权限 =====
    AUTH_TOKEN_MISSING = 1001       # 缺少认证 Token
    AUTH_TOKEN_INVALID = 1002       # Token 无效或已过期
    AUTH_TOKEN_EXPIRED = 1003       # Token 已过期
    AUTH_LOGIN_FAILED = 1004        # 用户名或密码错误
    AUTH_USER_DISABLED = 1005       # 用户已被禁用
    AUTH_PERMISSION_DENIED = 1006   # 权限不足
    AUTH_IP_BLOCKED = 1007          # IP 不在白名单中

    # ===== 2xxx: 用户管理 =====
    USER_NOT_FOUND = 2001           # 用户不存在
    USER_ACCOUNT_EXISTS = 2002      # 账户已存在
    USER_CREATE_FAILED = 2003       # 创建用户失败
    USER_UPDATE_FAILED = 2004       # 更新用户失败
    USER_DELETE_FAILED = 2005       # 删除用户失败
    USER_CANNOT_DELETE_SELF = 2006  # 不能删除自己
    USER_CANNOT_DISABLE_SELF = 2007 # 不能禁用自己
    USER_PARAM_INVALID = 2008       # 用户参数无效

    # ===== 3xxx: 项目管理 =====
    PROJECT_NOT_FOUND = 3001        # 项目不存在
    PROJECT_CODE_EXISTS = 3002      # 项目编码已存在
    PROJECT_CREATE_FAILED = 3003    # 创建项目失败
    PROJECT_UPDATE_FAILED = 3004    # 更新项目失败
    PROJECT_DELETE_FAILED = 3005    # 删除项目失败
    PROJECT_DISABLED = 3006         # 项目已禁用
    PROJECT_EXPORT_FAILED = 3007    # 项目导出失败

    # ===== 4xxx: API 配置 =====
    API_NOT_FOUND = 4001            # API 不存在
    API_PATH_CONFLICT = 4002        # API 路径冲突
    API_CREATE_FAILED = 4003        # 创建 API 失败
    API_UPDATE_FAILED = 4004        # 更新 API 失败
    API_DELETE_FAILED = 4005        # 删除 API 失败
    API_DISABLED = 4006             # API 已禁用
    API_KEY_GENERATE_FAILED = 4007  # API Key 生成失败
    API_PARAM_INVALID = 4008        # API 参数无效

    # ===== 5xxx: 网关/调用 =====
    GW_PROJECT_NOT_FOUND = 5001     # 网关：项目不存在
    GW_PROJECT_DISABLED = 5002      # 网关：项目已禁用
    GW_API_NOT_FOUND = 5003         # 网关：未找到匹配的 API
    GW_API_KEY_INVALID = 5004       # 网关：API Key 无效
    GW_RATE_LIMITED = 5005          # 网关：请求过于频繁
    GW_DDL_BLOCKED = 5006           # 网关：禁止执行 DDL 操作
    GW_DATASOURCE_MISSING = 5007    # 网关：未配置数据源
    GW_DATASOURCE_NOT_FOUND = 5008  # 网关：数据源不存在
    GW_PARAM_MISSING = 5009         # 网关：缺少必填参数
    GW_PARAM_INVALID = 5010         # 网关：参数校验失败
    GW_SQL_EXECUTE_FAILED = 5011    # 网关：SQL 执行失败
    GW_TIMEOUT = 5012               # 网关：执行超时
    GW_DATASOURCE_TYPE_UNSUPPORTED = 5013  # 网关：不支持的数据源类型
    GW_EXECUTE_FAILED = 5014        # 网关：执行失败

    # ===== 6xxx: 数据源 =====
    DS_NOT_FOUND = 6001             # 数据源不存在
    DS_NAME_EXISTS = 6002           # 数据源名称已存在
    DS_CREATE_FAILED = 6003         # 创建数据源失败
    DS_UPDATE_FAILED = 6004         # 更新数据源失败
    DS_DELETE_FAILED = 6005         # 删除数据源失败
    DS_IN_USE = 6006                # 数据源正在被使用
    DS_CONNECT_FAILED = 6007        # 数据源连接失败
    DS_TYPE_UNSUPPORTED = 6008      # 不支持的数据源类型

    # ===== 7xxx: 监控/日志 =====
    LOG_NOT_FOUND = 7001            # 日志不存在
    LOG_CLEANUP_FAILED = 7002       # 日志清理失败

    # ===== 9xxx: 系统级 =====
    SYSTEM_ERROR = 9001             # 系统内部错误
    SYSTEM_PARAM_INVALID = 9002     # 请求参数无效
    SYSTEM_NOT_FOUND = 9003         # 资源不存在
    SYSTEM_NETWORK_ERROR = 9004     # 网络错误


# ===== 错误码对应的默认消息 =====
_CODE_MESSAGES = {
    ErrCode.SUCCESS: "操作成功",

    ErrCode.AUTH_TOKEN_MISSING: "缺少认证 Token",
    ErrCode.AUTH_TOKEN_INVALID: "Token 无效或已过期",
    ErrCode.AUTH_TOKEN_EXPIRED: "Token 已过期",
    ErrCode.AUTH_LOGIN_FAILED: "用户名或密码错误",
    ErrCode.AUTH_USER_DISABLED: "用户已被禁用",
    ErrCode.AUTH_PERMISSION_DENIED: "权限不足",
    ErrCode.AUTH_IP_BLOCKED: "IP 不在白名单中",

    ErrCode.USER_NOT_FOUND: "用户不存在",
    ErrCode.USER_ACCOUNT_EXISTS: "账户已存在",
    ErrCode.USER_CREATE_FAILED: "创建用户失败",
    ErrCode.USER_UPDATE_FAILED: "更新用户失败",
    ErrCode.USER_DELETE_FAILED: "删除用户失败",
    ErrCode.USER_CANNOT_DELETE_SELF: "不能删除自己",
    ErrCode.USER_CANNOT_DISABLE_SELF: "不能禁用自己",
    ErrCode.USER_PARAM_INVALID: "用户参数无效",

    ErrCode.PROJECT_NOT_FOUND: "项目不存在",
    ErrCode.PROJECT_CODE_EXISTS: "项目编码已存在",
    ErrCode.PROJECT_CREATE_FAILED: "创建项目失败",
    ErrCode.PROJECT_UPDATE_FAILED: "更新项目失败",
    ErrCode.PROJECT_DELETE_FAILED: "删除项目失败",
    ErrCode.PROJECT_DISABLED: "项目已禁用",
    ErrCode.PROJECT_EXPORT_FAILED: "项目导出失败",

    ErrCode.API_NOT_FOUND: "API 不存在",
    ErrCode.API_PATH_CONFLICT: "API 路径冲突",
    ErrCode.API_CREATE_FAILED: "创建 API 失败",
    ErrCode.API_UPDATE_FAILED: "更新 API 失败",
    ErrCode.API_DELETE_FAILED: "删除 API 失败",
    ErrCode.API_DISABLED: "API 已禁用",
    ErrCode.API_KEY_GENERATE_FAILED: "API Key 生成失败",
    ErrCode.API_PARAM_INVALID: "API 参数无效",

    ErrCode.GW_PROJECT_NOT_FOUND: "项目不存在",
    ErrCode.GW_PROJECT_DISABLED: "项目不存在或已禁用",
    ErrCode.GW_API_NOT_FOUND: "未找到匹配的 API",
    ErrCode.GW_API_KEY_INVALID: "API Key 无效",
    ErrCode.GW_RATE_LIMITED: "请求过于频繁，请稍后重试",
    ErrCode.GW_DDL_BLOCKED: "禁止执行 DDL 操作",
    ErrCode.GW_DATASOURCE_MISSING: "未配置数据源",
    ErrCode.GW_DATASOURCE_NOT_FOUND: "数据源不存在",
    ErrCode.GW_PARAM_MISSING: "缺少必填参数",
    ErrCode.GW_PARAM_INVALID: "参数校验失败",
    ErrCode.GW_SQL_EXECUTE_FAILED: "SQL 执行失败",
    ErrCode.GW_TIMEOUT: "执行超时",
    ErrCode.GW_DATASOURCE_TYPE_UNSUPPORTED: "不支持的数据源类型",
    ErrCode.GW_EXECUTE_FAILED: "执行失败",

    ErrCode.DS_NOT_FOUND: "数据源不存在",
    ErrCode.DS_NAME_EXISTS: "数据源名称已存在",
    ErrCode.DS_CREATE_FAILED: "创建数据源失败",
    ErrCode.DS_UPDATE_FAILED: "更新数据源失败",
    ErrCode.DS_DELETE_FAILED: "删除数据源失败",
    ErrCode.DS_IN_USE: "数据源正在被使用，无法删除",
    ErrCode.DS_CONNECT_FAILED: "数据源连接失败",
    ErrCode.DS_TYPE_UNSUPPORTED: "不支持的数据源类型",

    ErrCode.LOG_NOT_FOUND: "日志不存在",
    ErrCode.LOG_CLEANUP_FAILED: "日志清理失败",

    ErrCode.SYSTEM_ERROR: "系统内部错误",
    ErrCode.SYSTEM_PARAM_INVALID: "请求参数无效",
    ErrCode.SYSTEM_NOT_FOUND: "资源不存在",
    ErrCode.SYSTEM_NETWORK_ERROR: "网络错误",
}


def get_err_msg(code: ErrCode) -> str:
    """获取错误码对应的默认消息"""
    return _CODE_MESSAGES.get(code, "未知错误")


# ===== 统一响应构建函数 =====

def R_ok(data: Any = None, msg: str = "操作成功") -> dict:
    """成功响应"""
    return {
        "status": True,
        "code": int(ErrCode.SUCCESS),
        "data": data,
        "msg": msg,
    }


def R_fail(code: ErrCode, msg: Optional[str] = None, data: Any = None) -> dict:
    """失败响应"""
    return {
        "status": False,
        "code": int(code),
        "data": data,
        "msg": msg or get_err_msg(code),
    }


def R_error(code: ErrCode, msg: Optional[str] = None, http_status: int = 200) -> JSONResponse:
    """
    失败响应（返回 JSONResponse，可自定义 HTTP 状态码）
    用于需要设置非 200 HTTP 状态码的场景
    """
    return JSONResponse(
        status_code=http_status,
        content={
            "status": False,
            "code": int(code),
            "data": None,
            "msg": msg or get_err_msg(code),
        },
    )
