"""
数据模型定义
包含：用户、项目、API配置、API参数、数据源、调用日志
"""

import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Float, ForeignKey, Index
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import relationship

# 大文本列类型：MySQL 用 LONGTEXT（最大 4GB），其它库（SQLite 等）退回普通 Text。
# 静态页面 HTML/CSS/JS 可能很大（内联地图库、坐标数据等），普通 TEXT(64KB) 会被截断。
_BigText = Text().with_variant(LONGTEXT, "mysql")
from app.core.database import Base
from app.core.timezone import now as _cst_now
from app.core.runtime_env import t as _t


class User(Base):
    __tablename__ = _t("src_dop_users")

    id = Column(Integer, primary_key=True, autoincrement=True)
    nickname = Column(String(64), nullable=False, default="")  # 用户名（显示名称）
    username = Column(String(64), unique=True, nullable=False, index=True)  # 账户（登录用）
    password_hash = Column(String(256), nullable=False)
    is_active = Column(Boolean, default=True)
    # 全局角色 (v2.0+): super_admin（超级管理员，全平台最高权限）/ user（普通用户，默认）
    # 普通用户注册后无任何项目权限，需被项目管理员加入项目并赋予项目角色后才能操作。
    global_role = Column(String(16), nullable=False, default="user", index=True)
    created_at = Column(DateTime, default=_cst_now)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)


class ProjectMember(Base):
    """用户-项目关联表 (v2.0+)：谁在哪个项目、担任什么项目角色。

    project_role:
        manager   项目管理员（可管成员、审批上线、发起删项目等）
        developer 研发（可创建/编辑草稿、提交上线、参与审核）
        viewer    只读（仅查看，可选）
    """
    __tablename__ = _t("src_dop_project_members")

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey(_t("src_dop_projects") + ".id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey(_t("src_dop_users") + ".id", ondelete="CASCADE"), nullable=False, index=True)
    project_role = Column(String(16), nullable=False, default="developer")
    created_at = Column(DateTime, default=_cst_now)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)

    __table_args__ = (
        Index(_t("uq_project_user"), "project_id", "user_id", unique=True),
    )


class Project(Base):
    __tablename__ = _t("src_dop_projects")

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(64), unique=True, nullable=False, index=True)  # 英文编码，用于网关路由
    name = Column(String(128), nullable=False, index=True)
    description = Column(Text, default="")
    api_key = Column(String(256), default="")  # 项目级 API Key
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_cst_now)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)

    # 关联
    apis = relationship("ApiConfig", back_populates="project", cascade="all, delete-orphan")


class DataSource(Base):
    __tablename__ = _t("src_dop_datasources")

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    type = Column(String(32), nullable=False, default="mysql")  # mysql / redis / postgresql
    host = Column(String(256), nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String(128), default="")
    password_encrypted = Column(String(512), default="")  # 加密存储
    database_name = Column(String(128), default="")
    pool_size = Column(Integer, default=10)
    extra_config = Column(Text, default="{}")  # JSON 格式额外配置
    status = Column(String(32), default="unknown")  # unknown / connected / error
    last_test_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, nullable=True, index=True)   # 创建人 user_id (v2.7+)
    created_at = Column(DateTime, default=_cst_now)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)

    # 关联
    apis = relationship("ApiConfig", back_populates="datasource")


class ApiConfig(Base):
    __tablename__ = _t("src_dop_api_configs")

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey(_t("src_dop_projects") + ".id", ondelete="CASCADE"), nullable=False, index=True)
    datasource_id = Column(Integer, ForeignKey(_t("src_dop_datasources") + ".id", ondelete="SET NULL"), nullable=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    url_path = Column(String(256), nullable=False)  # 如 /api/users
    method = Column(String(16), nullable=False, default="GET")  # GET/POST/PUT/DELETE
    sql_template = Column(Text, default="")  # SQL 模板，参数占位符 :param
    is_enabled = Column(Boolean, default=True)
    api_key = Column(String(256), default="")  # 独立 API Key，空则使用项目级
    # API Key 校验开关（v1.8+）：
    #   True  -> 校验 API Key（若 api.api_key 为空则回退到 project.api_key）
    #   False -> 完全跳过 API Key 校验，公开访问（HTML 静态页面 API 默认建议关闭）
    require_api_key = Column(Boolean, default=True, nullable=False)
    cache_enabled = Column(Boolean, default=False)
    cache_ttl = Column(Integer, default=300)  # 缓存 TTL 秒
    cache_prewarm = Column(Boolean, default=False)  # 自动预热 (v2.13+)：缓存到期前后台自动按历史参数重新查询回填
    # 预热参数覆盖 (v2.14+)：JSON 映射 {"字段名": "${变量名}"}。
    # 预热时以「历史请求参数」为底，只把这里列出的字段用变量求值覆盖掉，其余字段保持原值。
    # 典型用途：supplierId 等业务字段保留历史请求的各种取值（多个景区各自预热），
    # 只把 startTime/endTime 这类时间字段刷新成当前时间，避免预热到过期数据。
    prewarm_param_overrides = Column(Text, default="")
    # 预热跨日停止开关 (v2.16+)：开启后，每天 0 点起暂停该 API 的自动预热，
    # 直到当天有新的真实请求进来（重新记录参数）才恢复预热。
    # 用途：避免那些「今天没人再用」的参数被无意义地预热一整天，白耗数据库资源。
    prewarm_stop_daily = Column(Boolean, default=False)
    # 数据同步白名单 (v2.17+)：api_type='sync' 时生效。
    # JSON 文本，形如：
    #   {"order_table": ["order_id", "order_name"],
    #    "ticket_table": ["ticket_id", "price"]}
    # 只有登记在册的表名/字段才允许写入 —— 这是写入类 API 的核心安全边界，
    # 表名和字段名都不能来自用户输入直接拼 SQL（无法参数化，只能靠白名单比对）。
    sync_tables = Column(Text, default="")
    timeout = Column(Integer, default=30)  # 超时秒数
    rate_limit_enabled = Column(Boolean, default=False)
    rate_limit_qps = Column(Integer, default=100)
    max_rows = Column(Integer, default=10000)
    version = Column(Integer, default=1)
    # 多步骤管线 (v1.6+): JSON 数组，配置后会覆盖 sql_template 走 pipeline_executor
    # 单步快捷模式仍保留 sql_template 字段以向后兼容
    pipeline_steps = Column(Text, default="")  # JSON 字符串，空则走单 SQL 模式
    # API 类型 (v1.8+):
    #   sql    -> 数据 API，走 sql_template / pipeline_steps（默认，向后兼容）
    #   html   -> 静态页面 API，调用时直接渲染 html/css/js 为页面返回
    #   plugin -> Python 插件 API (v1.9+)，执行 plugin_code 里的 main(params, ctx)
    api_type = Column(String(16), nullable=False, default="sql", index=True)
    html_content = Column(_BigText, default="")  # HTML 正文（LONGTEXT，支持大页面）
    css_content = Column(_BigText, default="")   # 内联 CSS（LONGTEXT）
    js_content = Column(_BigText, default="")    # 内联 JS（LONGTEXT）
    # Python 插件 (v1.9+): 用户编写的脚本，需含 def main(params, ctx) 并 return 结果。
    # 安全说明：插件代码等同于在服务器上执行任意 Python，仅限可信成员编写。
    plugin_code = Column(Text, default="")
    # API 生命周期状态 (v2.0+):
    #   draft    草稿（新建默认；可自由编辑）
    #   pending  待上线（已提交上线审批，审批中；不可编辑）
    #   online   已上线（审批通过；只读，不可编辑、不可删除；仅此状态可被网关调用）
    #   offline  下线（已上线后下线；语义上回到草稿，可再次编辑/提交）
    # 说明：is_enabled 仍保留作为"启用/停用"开关，与生命周期状态正交。
    status = Column(String(16), nullable=False, default="draft", index=True)
    is_locked = Column(Boolean, default=False)   # 锁定开关 (v2.9+)：锁定后不可编辑/提交上线，需解锁
    created_by = Column(Integer, nullable=True, index=True)   # 创建人 (v2.9+)
    owner_id = Column(Integer, nullable=True, index=True)     # 责任人 (v2.10+)：默认=创建人；删除/上线/下线需责任人或管理员，非责任人走申请审批
    created_at = Column(DateTime, default=_cst_now)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)

    # 唯一约束：同一项目下 url_path + method 唯一
    __table_args__ = (
        Index(_t("ix_api_project_path_method"), "project_id", "url_path", "method", unique=True),
    )

    # 关联
    project = relationship("Project", back_populates="apis")
    datasource = relationship("DataSource", back_populates="apis")
    parameters = relationship("ApiParameter", back_populates="api_config", cascade="all, delete-orphan")
    logs = relationship("CallLog", back_populates="api_config")  # 不级联删除，保留历史日志


class ProjectVariable(Base):
    """项目环境变量 (v2.14+)：供缓存预热的参数模板引用。

    典型用途：预热时参数里的时间不能写死（写死会导致明天还在预热昨天的数据），
    改成引用变量 ${TODAY}、${NOW} 等，预热时实时求值。

    var_type 决定怎么求值：
      date        日期，按 offset_days 偏移。offset=0 今天，-1 昨天，1 明天
      datetime    日期时间（精确到秒），按 offset_days 偏移
      week_start  本周一（offset_days 按周偏移，-1 = 上周一）
      week_end    本周日
      month_start 本月一号（offset_days 按月偏移，-1 = 上月一号）
      month_end   本月最后一天
      now         当前时刻（忽略 offset）
      day_start   当天 00:00:00，按 offset_days 偏移
      day_end     当天 23:59:59，按 offset_days 偏移
      const       固定字符串，直接用 const_value

    date_format 可自定义输出格式，留空则按类型用默认格式。
    """
    __tablename__ = _t("src_dop_project_variables")

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey(_t("src_dop_projects") + ".id", ondelete="CASCADE"),
                        nullable=False, index=True)
    name = Column(String(64), nullable=False, index=True)   # 变量名，模板里写 ${name}
    var_type = Column(String(32), nullable=False, default="date")
    offset_days = Column(Integer, default=0)                # 偏移量（天/周/月，取决于类型）
    date_format = Column(String(64), default="")            # 自定义格式，留空用类型默认
    const_value = Column(String(256), default="")           # var_type=const 时的固定值
    description = Column(String(256), default="")
    created_at = Column(DateTime, default=_cst_now)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)

    __table_args__ = (
        Index(_t("ix_proj_var_unique"), "project_id", "name", unique=True),
    )


class ApiParameter(Base):
    __tablename__ = _t("src_dop_api_parameters")

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_id = Column(Integer, ForeignKey(_t("src_dop_api_configs") + ".id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    param_type = Column(String(32), nullable=False, default="string")  # string/number/boolean/array/object
    required = Column(Boolean, default=False)
    default_value = Column(String(256), default="")
    description = Column(String(512), default="")
    sort_order = Column(Integer, default=0)
    # v2.1: 嵌套结构定义（JSON 文本）。仅 array/object 类型使用：
    #   {"item_type":"object","children":[{"name":"channelName","param_type":"string",
    #     "required":true,"example":"抖音"},{"name":"prdId","param_type":"array",
    #     "item_schema":{"item_type":"string"},"required":true,"example":["476855"]}]}
    # children 可递归嵌套 item_schema。空串/NULL = 无嵌套定义（存量数据）。
    item_schema = Column(Text, default="")

    # 关联
    api_config = relationship("ApiConfig", back_populates="parameters")


class CallLog(Base):
    __tablename__ = _t("src_dop_call_logs")

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_id = Column(Integer, ForeignKey(_t("src_dop_api_configs") + ".id", ondelete="SET NULL"), nullable=True, index=True)  # nullable: 网关拦截时可能无匹配 API
    project_id = Column(Integer, nullable=True, index=True)  # nullable: 项目不存在时记录
    api_name = Column(String(128), default="")
    url_path = Column(String(256), default="")
    method = Column(String(16), default="")
    request_params = Column(Text, default="{}")  # JSON
    response_status = Column(String(32), default="success")  # success / error
    response_time_ms = Column(Float, default=0)  # 耗时毫秒
    status_code = Column(Integer, default=200)
    error_message = Column(Text, default="")
    client_ip = Column(String(64), default="")
    response_data = Column(Text, default="")  # 响应数据 JSON（截取前5000字符）
    is_slow_query = Column(Boolean, default=False)
    call_source = Column(String(32), default="gateway")  # gateway / test / unknown
    # v2.4: 关键节点回填字段。日志由网关层建档，业务逻辑通过 trace 上下文回填这些节点。
    rendered_sql = Column(Text, default="")      # 渲染后的 SQL（模板展开、条件求值后）
    executed_sql = Column(Text, default="")      # 实际提交数据库的 SQL（占位符转 %(name)s 后，含绑定值注释）
    render_time_ms = Column(Float, default=0)    # 模板渲染+参数解析耗时
    query_time_ms = Column(Float, default=0)     # 数据库查询耗时
    row_count = Column(Integer, default=0)       # 返回行数
    cache_hit = Column(Boolean, default=False)   # 是否命中缓存
    error_stack = Column(Text, default="")       # 失败时的异常堆栈
    # 全链路 trace_id (v1.9+)：与服务端 X-Trace-Id 响应头、应用日志中的 trace= 字段保持一致
    # 便于通过页面调用日志直接定位到对应的应用日志（grep trace=...）
    trace_id = Column(String(64), default="", index=True)
    created_at = Column(DateTime, default=_cst_now, index=True)

    # 关联
    api_config = relationship("ApiConfig", back_populates="logs")


class ApiApproval(Base):
    """API 上线审批单 (v2.2+)。

    审批规则：项目管理员必须通过 + 指定的一名研发也通过，两票齐全才上线。
    任一方驳回 -> 整单驳回，API 回到草稿。

    overall_status: pending / approved / rejected / withdrawn
    manager_decision / reviewer_decision: pending / approved / rejected
    """
    __tablename__ = _t("src_dop_api_approvals")

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_id = Column(Integer, ForeignKey(_t("src_dop_api_configs") + ".id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey(_t("src_dop_projects") + ".id", ondelete="CASCADE"), nullable=False, index=True)

    submitter_id = Column(Integer, nullable=False, index=True)   # 提交人
    reviewer_id = Column(Integer, nullable=True, index=True)     # 指定会审研发（管理员上线单可为空）

    overall_status = Column(String(16), nullable=False, default="pending", index=True)

    manager_decision = Column(String(16), nullable=False, default="pending")
    manager_id = Column(Integer, nullable=True)                 # 实际做出管理员决定的人
    manager_comment = Column(String(512), default="")
    manager_at = Column(DateTime, nullable=True)

    reviewer_decision = Column(String(16), nullable=False, default="pending")
    reviewer_comment = Column(String(512), default="")
    reviewer_at = Column(DateTime, nullable=True)

    remark = Column(String(512), default="")                    # 提交说明
    created_at = Column(DateTime, default=_cst_now, index=True)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)


class ApiOwnerApproval(Base):
    """责任人操作审批单 (v2.10+)。

    用于「删除 / 上线 / 下线」三类操作的申请-审批：
      - 责任人本人或管理员：直接执行，不产生本单据。
      - 非责任人：发起申请生成本单据(pending)，由责任人或项目管理员审批。
        通过(approved)后执行该操作；驳回(rejected)则不执行。

    action: delete / online / offline
    status: pending / approved / rejected / cancelled
    """
    __tablename__ = _t("src_dop_api_owner_approvals")

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_id = Column(Integer, ForeignKey(_t("src_dop_api_configs") + ".id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey(_t("src_dop_projects") + ".id", ondelete="CASCADE"), nullable=False, index=True)

    action = Column(String(16), nullable=False, index=True)      # delete / online / offline
    status = Column(String(16), nullable=False, default="pending", index=True)

    requester_id = Column(Integer, nullable=False, index=True)   # 申请人(非责任人)
    owner_id_snapshot = Column(Integer, nullable=True)           # 申请时该 API 的责任人(快照)
    reason = Column(String(512), default="")                     # 申请理由

    decider_id = Column(Integer, nullable=True)                  # 审批人(责任人或管理员)
    decide_comment = Column(String(512), default="")
    decided_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=_cst_now, index=True)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)


class AuditLog(Base):
    """操作审计日志 (v2.2+)：记录谁在什么时候对什么做了什么敏感操作。"""
    __tablename__ = _t("src_dop_audit_logs")

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)
    username = Column(String(64), default="")
    action = Column(String(64), nullable=False, index=True)     # 如 api.submit / api.approve / project.delete
    target_type = Column(String(32), default="")                # api / project / member ...
    target_id = Column(Integer, nullable=True)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=_cst_now, index=True)


class PluginLibrary(Base):
    """插件库 (v2.3+)：可复用的 Python 代码片段（工具函数集），供 API 插件引用。

    一个库条目就是一段 Python 代码，里面可以定义若干函数/常量。
    API 的 plugin_code 通过"引用"机制把这些库代码注入到执行命名空间，
    即可直接调用库里定义的函数（类似 import 一个内部模块的效果）。
    """
    __tablename__ = _t("src_dop_plugin_libraries")

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False, index=True)  # 库标识名（英文，引用时用）
    title = Column(String(128), default="")        # 显示名称
    description = Column(String(512), default="")
    code = Column(Text, nullable=False, default="") # Python 代码（函数集）
    is_enabled = Column(Boolean, default=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_cst_now)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)


class ProjectDeletionRequest(Base):
    """删除项目申请 (v2.4+)。

    规则：项目管理员发起删除申请 -> 需「超级管理员」或「另一名项目管理员」审批通过才真正删除。
    （发起人不能审批自己的申请。）

    status: pending / approved（已删除）/ rejected / canceled
    """
    __tablename__ = _t("src_dop_project_deletions")

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, nullable=False, index=True)   # 不加外键，因为审批通过后项目会被删
    project_name = Column(String(128), default="")
    requester_id = Column(Integer, nullable=False, index=True)
    reason = Column(String(512), default="")
    status = Column(String(16), nullable=False, default="pending", index=True)
    approver_id = Column(Integer, nullable=True)
    approver_comment = Column(String(512), default="")
    created_at = Column(DateTime, default=_cst_now, index=True)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)


class DataSourceDeletionRequest(Base):
    """删除数据源审批单 (v2.7+)。

    删除数据源需审批；记录中间状态(pending)与结果状态(approved/rejected/canceled)。
    审批人：超级管理员或管理员（非发起人）。
    若数据源仍被 API 引用，则不允许发起删除。
    """
    __tablename__ = _t("src_dop_datasource_deletions")

    id = Column(Integer, primary_key=True, autoincrement=True)
    datasource_id = Column(Integer, nullable=False, index=True)
    datasource_name = Column(String(128), default="")
    requester_id = Column(Integer, nullable=False, index=True)
    reason = Column(String(512), default="")
    status = Column(String(16), nullable=False, default="pending", index=True)  # pending/approved/rejected/canceled
    approver_id = Column(Integer, nullable=True)
    approver_comment = Column(String(512), default="")
    created_at = Column(DateTime, default=_cst_now, index=True)
    updated_at = Column(DateTime, default=_cst_now, onupdate=_cst_now)
