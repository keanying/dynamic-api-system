# -*- coding: utf-8 -*-
"""
运行环境 (v2.8)

支持「预发布(pre)」与「正式(prod)」两套环境，同库不同表名区分：
  - 正式环境：表名无后缀（如 src_dop_users）
  - pre 环境：表名加 _pre 后缀（如 src_dop_users_pre）

判定方式（命令行参数优先）：
  - 命令行：python main.py --env pre   或   uvicorn ... 时 APP_ENV=pre
  - 环境变量：APP_ENV=pre（作为兜底，便于 uvicorn 子进程/容器场景）

设计目的：正式数据与测试数据物理隔离，pre 环境用正式数据的副本做验证，
不会破坏已上线的正式内容；测试通过后再切正式环境运行。

注意：本模块必须在 models.py 之前被读取（config.py 会 import 它），
因为 __tablename__ 在类定义时就固定，后缀要在建模前确定。
"""
import os
import sys

_PRE_ENV_NAME = "pre"


def _detect_env() -> str:
    """检测当前运行环境。命令行 --env 优先，其次环境变量 APP_ENV。"""
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
    return "prod"


# 当前环境（模块加载时确定一次）
CURRENT_ENV = _detect_env()
IS_PRE = CURRENT_ENV == _PRE_ENV_NAME

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
