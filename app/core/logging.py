"""
全局日志配置模块（基于 loguru）

特性：
- 按日切割日志文件
- 可配置日志目录（config.yaml 或环境变量 LOG_DIR）
- 默认保留 3 天日志
- 支持控制台 + 文件双输出
- 自动记录异常堆栈
"""

import sys
from contextvars import ContextVar
from pathlib import Path
from loguru import logger

from app.core.config import BASE_DIR

# ---------------------------------------------------------------------------
# 全局 trace_id 上下文（v1.9+）
# ---------------------------------------------------------------------------
# 该 ContextVar 在请求中间件中被赋值，并通过 loguru patcher 注入到每一条日志
# record["extra"]["trace_id"] 中，从而让 {extra[trace_id]} 占位符总是有值，
# 即便是 lifespan/启动期日志（默认值 "-"）也不会缺字段。
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def get_trace_id() -> str:
    """获取当前请求上下文中的 trace_id（无则返回 "-"）"""
    try:
        return trace_id_var.get()
    except LookupError:
        return "-"


def set_trace_id(value: str) -> None:
    """设置当前请求上下文中的 trace_id（由中间件调用）"""
    trace_id_var.set(value or "-")


# 当前是否有任何输出会接收 DEBUG 日志（setup_logging 时确定）。
# 热路径上的 debug 日志常带 json.dumps / 大字典 repr，f-string 在调用前就会求值，
# 即使最终不输出也要付出拼接成本，所以先用 is_debug() 判断再拼。
_DEBUG_ENABLED = True


def is_debug() -> bool:
    return _DEBUG_ENABLED


def _trace_id_patcher(record):
    """loguru patcher：把当前上下文中的 trace_id 注入到日志 record.extra"""
    # 不覆盖已经显式 bind 的 trace_id
    if "trace_id" not in record["extra"]:
        record["extra"]["trace_id"] = get_trace_id()


# 移除 loguru 默认 handler，并安装 patcher（必须在 add 之前）
logger.remove()
logger.configure(patcher=_trace_id_patcher)


def setup_logging(log_dir: str = "./logs", level: str = "DEBUG", retention_days: int = 3,
                  file_level: str = ""):
    """
    初始化全局日志配置

    Args:
        log_dir: 日志文件目录，支持相对路径和绝对路径
        level: 日志级别（DEBUG / INFO / WARNING / ERROR）
        retention_days: 日志保留天数，默认 3 天
        file_level: 全量日志文件 / 网关日志文件的级别，留空则与 level 相同。
                    v2.19 之前这两个文件固定为 DEBUG，每个网关请求要写二十来条
                    debug 日志，压测中占到主线程约 40% 的 CPU。需要排查问题时可以
                    在 config.yaml 里临时设 log.file_level: DEBUG。
    """
    global _DEBUG_ENABLED
    file_level = (file_level or level or "INFO").upper()
    _DEBUG_ENABLED = "DEBUG" in (str(level).upper(), file_level)
    log_path = Path(log_dir)
    # 如果是相对路径，基于项目根目录解析，确保无论从哪里启动都写到项目根目录下
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    log_path.mkdir(parents=True, exist_ok=True)

    # 1. 控制台输出（彩色，简洁格式）
    logger.add(
        sys.stdout,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<magenta>trace={extra[trace_id]}</magenta> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # 2. 全量日志文件（按日切割，保留 N 天）
    logger.add(
        str(log_path / "app_{time:YYYY-MM-DD}.log"),
        level=file_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "trace={extra[trace_id]} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        rotation="00:00",       # 每天 0 点切割
        retention=f"{retention_days} days",
        compression="gz",       # 旧日志压缩
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
        enqueue=True,           # 异步写入，不阻塞主线程
    )

    # 3. 错误日志文件（仅 ERROR 及以上，按日切割）
    logger.add(
        str(log_path / "error_{time:YYYY-MM-DD}.log"),
        level="ERROR",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "trace={extra[trace_id]} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        rotation="00:00",
        retention=f"{retention_days} days",
        compression="gz",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )

    # 4. 网关调用日志文件（专门记录外部 API 调用）
    logger.add(
        str(log_path / "gateway_{time:YYYY-MM-DD}.log"),
        level=file_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "trace={extra[trace_id]} | "
            "{message}"
        ),
        rotation="00:00",
        retention=f"{retention_days} days",
        compression="gz",
        encoding="utf-8",
        filter=lambda record: record["extra"].get("logger_name") == "gateway",
        enqueue=True,
    )

    logger.info(f"日志系统初始化完成 | 目录: {log_path.resolve()} | 级别: {level} | 保留: {retention_days} 天")


# 创建带标签的子 logger，方便过滤
def get_logger(name: str = "app"):
    """获取带模块标签的 logger"""
    return logger.bind(logger_name=name)
