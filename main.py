"""
OneData Portal 启动入口

使用方式:
  正式环境:  python main.py
  预发布环境: python main.py --env pre

  --env pre  时所有表名加 _pre 后缀，与正式表物理隔离；首次启动会把正式数据
  同步一份到空的 _pre 表，供测试使用，不影响正式内容。
"""
import os
import sys

# 尽早解析 --env 并写入环境变量：uvicorn reload/多进程会 spawn 子进程，
# 子进程不一定继承 argv，但会继承环境变量，确保子进程也识别到 pre。
def _parse_env_arg():
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--env" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--env="):
            return a.split("=", 1)[1].strip().lower()
    return os.environ.get("APP_ENV", "").strip().lower()

_env = _parse_env_arg()
if _env:
    os.environ["APP_ENV"] = _env

import uvicorn
from app.core.config import settings


def main():
    uvicorn.run(
        "app.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=settings.app.debug,
        workers=1 if settings.app.debug else settings.app.workers,
    )


if __name__ == "__main__":
    main()
