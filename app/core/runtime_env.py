# -*- coding: utf-8 -*-
"""
运行环境 (v2.8)

支持「预发布(pre)」与「正式(prod)」两套环境，同库不同表名区分：
  - 正式环境：表名无后缀（如 src_dop_users）
  - pre 环境：表名加 _pre 后缀（如 src_dop_users_pre）

判定方式（优先级从高到低）：
  - 命令行：python main.py --env pre
  - 环境变量：APP_ENV=pre（便于 uvicorn 子进程/容器场景）
  - 端口 (v2.18+)：config.yaml 的 environments.<env>.port 与启动端口
    （命令行 --port 或环境变量 SERVER_PORT）匹配时，按端口判定环境。
    例如 uvicorn app.main:app --port 3002 会被识别为 pre，
    避免「端口开的是预发、表却连到正式」这种误操作。

设计目的：正式数据与测试数据物理隔离，pre 环境用正式数据的副本做验证，
不会破坏已上线的正式内容；测试通过后再切正式环境运行。

注意：本模块必须在 models.py 之前被读取（config.py 会 import 它），
因为 __tablename__ 在类定义时就固定，后缀要在建模前确定。
"""
import os
import sys
from pathlib import Path

_PRE_ENV_NAME = "pre"
_PROD_ENV_NAME = "prod"

ENV_LABELS = {_PROD_ENV_NAME: "生产环境", _PRE_ENV_NAME: "预发环境"}


def _load_environments() -> dict:
    """读取 config.yaml 的 environments 段：{env: {"port": int, "url": str}}。

    这里不能 import app.core.config（config 依赖本模块），所以直接读 YAML。
    读不到/格式不对时返回空字典，退回到不按端口判定的老逻辑。
    """
    try:
        import yaml
        cfg_file = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        with open(cfg_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        envs = raw.get("environments") or {}
        return {str(k).lower(): (v or {}) for k, v in envs.items() if isinstance(v, dict)}
    except Exception:
        return {}


ENVIRONMENTS = _load_environments()


def _arg_value(name: str):
    """取命令行 --name value / --name=value 的值。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def startup_port():
    """启动端口：命令行 --port 优先，其次环境变量 SERVER_PORT。取不到返回 None。"""
    raw = _arg_value("--port") or os.environ.get("SERVER_PORT", "")
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _env_by_port(port) -> str:
    if port is None:
        return ""
    for name, conf in ENVIRONMENTS.items():
        try:
            if int(conf.get("port")) == port:
                return name
        except (TypeError, ValueError):
            continue
    return ""


def _detect_env() -> str:
    """检测当前运行环境：--env > APP_ENV > 端口映射 > 默认 prod。"""
    # 1) 命令行参数 --env pre / --env=pre
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--env" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--env="):
            return a.split("=", 1)[1].strip().lower()
    # 2) 环境变量兜底（uvicorn reload 子进程会继承环境变量，但不一定继承 argv）
    env = os.environ.get("APP_ENV", "").strip().lower()
    if env:
        return env
    # 3) 按端口判定
    env = _env_by_port(startup_port())
    if env:
        return env
    return _PROD_ENV_NAME


# 当前环境（模块加载时确定一次）
CURRENT_ENV = _detect_env()
IS_PRE = CURRENT_ENV == _PRE_ENV_NAME
IS_PROD = not IS_PRE
ENV_LABEL = ENV_LABELS.get(CURRENT_ENV, CURRENT_ENV)


def env_port(env: str):
    """environments 段里配置的某环境端口；未配置返回 None。"""
    try:
        return int((ENVIRONMENTS.get(env) or {}).get("port"))
    except (TypeError, ValueError):
        return None


def env_url(env: str) -> str:
    """environments 段里配置的某环境访问地址（用于页面互相跳转）；未配置返回空串。"""
    return str((ENVIRONMENTS.get(env) or {}).get("url") or "").rstrip("/")

# 表名后缀：pre 环境为 "_pre"，正式为空
TABLE_SUFFIX = "_pre" if IS_PRE else ""

# 传递给 uvicorn reload 子进程（reload 会重新 spawn 进程，argv 变化，靠环境变量兜底）
if IS_PRE:
    os.environ["APP_ENV"] = _PRE_ENV_NAME


def t(name: str) -> str:
    """给表名加当前环境后缀。__tablename__ 与 ForeignKey 都用它，保证一致。"""
    return f"{name}{TABLE_SUFFIX}"


def base_table_name(name: str) -> str:
    """去掉 _pre 后缀，取正式表名（同步/备份时用来定位源表）。"""
    if TABLE_SUFFIX and name.endswith(TABLE_SUFFIX):
        return name[: -len(TABLE_SUFFIX)]
    return name
