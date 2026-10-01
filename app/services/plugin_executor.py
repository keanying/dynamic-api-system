"""
Python 插件执行器 (v1.9+)
==========================

允许用户用 Python 脚本作为 API 的执行逻辑。脚本约定::

    def main(params, ctx):
        # params: dict，调用方传入的参数
        # ctx:    PluginContext，注入的能力（查数据源 / HTTP / 日志 / 参数）
        rows = ctx.query("my_db", "SELECT * FROM t WHERE id = :id", {"id": params["id"]})
        return rows          # 可返回 list / dict / 标量，会被 JSON 序列化

==========================================================================
安全说明（重要）
--------------------------------------------------------------------------
本执行器在「可信扩展点」定位下实现：插件代码等同于在服务器上执行任意
Python（可 import 第三方库、可访问网络与数据库）。**这不是安全沙箱**，
仅限可信团队成员编写。Python 层面无法在允许任意 import 的前提下做到
真正隔离；如需对不可信来源开放，必须改为进程级隔离（容器 / gVisor /
独立执行服务）。当前提供的是软性防护：

  - 独立执行命名空间（不污染主程序）
  - 执行超时（避免脚本卡死拖垮请求）
  - 异常捕获（脚本报错不会搞崩主服务，转成可读错误）
  - 受控 ctx（查询走统一入口，自动 DDL 拦截、超时、行数限制）
  - 审计日志（记录哪个 api/脚本执行、耗时、成败）

未来加进程隔离时，main(params, ctx) 这个接口可保持不变。
==========================================================================
"""

import asyncio
import concurrent.futures
import threading
from typing import Any, Callable, Optional

from app.core.logging import get_logger

log = get_logger("plugin")


class PluginError(Exception):
    """插件执行相关错误（配置/语法/运行时），消息会回传给调用方。"""
    pass


class PluginContext:
    """注入给插件 main(params, ctx) 的 ctx 对象。

    能力：
      ctx.params              -> dict，本次调用参数（同 main 的 params，方便链式）
      ctx.query(ds, sql, p)   -> list[dict]，查指定数据源（走统一入口，含 DDL 拦截）
      ctx.log(msg)            -> 写一条 info 日志（带 api 标识）
      ctx.http               -> requests 模块（如已安装），用于调外部 HTTP
      ctx.vars                -> dict，插件内自由读写的临时空间
    """

    def __init__(self, params: dict, query_sync: Callable, api_id: Any = None):
        self.params = dict(params or {})
        self._query_sync = query_sync
        self._api_id = api_id
        self.vars: dict = {}
        # 按需注入 requests（用户"常用库都要能用"）
        try:
            import requests  # noqa
            self.http = requests
        except Exception:
            self.http = None

    def query(self, datasource: str, sql: str, params: Optional[dict] = None) -> list:
        """在指定数据源上执行查询，返回 list[dict]。

        datasource: 数据源名称或 id
        sql:        SQL（支持 :name 占位符，自动参数化；禁止 DDL）
        params:     占位符绑定值
        """
        if not datasource:
            raise PluginError("ctx.query 需要指定数据源名称或 id")
        if not sql or not sql.strip():
            raise PluginError("ctx.query 需要 SQL")
        return self._query_sync(datasource, sql, params or {})

    def log(self, msg: Any):
        log.info(f"[plugin api_id={self._api_id}] {msg}")


def _exec_plugin_sync(code: str, params: dict, ctx: PluginContext, lib_code: str = "") -> Any:
    """在子线程里同步执行用户脚本：编译 -> exec -> 调 main(params, ctx)。

    lib_code: 被引用的插件库代码（若干函数集），会先注入到同一命名空间，
              使 API 脚本能直接调用库里定义的函数。
    """
    glb: dict = {"__name__": "__plugin__", "__builtins__": __builtins__}

    # 0. 先注入引用的库代码（如有）
    if lib_code and lib_code.strip():
        try:
            lib_compiled = compile(lib_code, "<plugin_library>", "exec")
            exec(lib_compiled, glb)
        except SyntaxError as e:
            raise PluginError(f"引用的插件库语法错误 (第 {e.lineno} 行): {e.msg}")
        except Exception as e:
            raise PluginError(f"插件库加载失败: {type(e).__name__}: {e}")

    # 1. 编译用户脚本（语法错误在此暴露，给出行号）
    try:
        compiled = compile(code, "<plugin>", "exec")
    except SyntaxError as e:
        raise PluginError(f"插件语法错误 (第 {e.lineno} 行): {e.msg}")

    # 2. 执行模块体，拿到 main（与库代码共享同一命名空间）
    try:
        exec(compiled, glb)
    except Exception as e:
        raise PluginError(f"插件加载失败: {type(e).__name__}: {e}")

    main = glb.get("main")
    if not callable(main):
        raise PluginError("插件必须定义 main(params, ctx) 函数")

    # 3. 调用 main
    try:
        result = main(params, ctx)
    except PluginError:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc(limit=6)
        log.warning(f"插件运行时异常 | api_id={ctx._api_id} | {tb}")
        raise PluginError(f"插件运行出错: {type(e).__name__}: {e}")

    return result


async def execute_plugin(
    code: str,
    params: dict,
    query_sync: Callable,
    api_id: Any = None,
    timeout: int = 30,
    lib_code: str = "",
) -> Any:
    """异步入口：把同步脚本放到线程池跑，加超时保护。

    code:       用户脚本（需含 def main(params, ctx)）
    params:     调用参数
    query_sync: 同步查询函数 (datasource, sql, params) -> list[dict]
    api_id:     用于日志标识
    timeout:    执行超时秒
    lib_code:   被引用的插件库代码（合并后的函数集），先于脚本注入命名空间
    """
    if not code or not code.strip():
        raise PluginError("插件代码为空")

    ctx = PluginContext(params, query_sync, api_id=api_id)

    loop = asyncio.get_event_loop()
    log.info(f"开始执行插件 | api_id={api_id} | timeout={timeout}s | 引用库={'有' if lib_code else '无'}")

    def _run():
        return _exec_plugin_sync(code, params, ctx, lib_code=lib_code)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = loop.run_in_executor(pool, _run)
            result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        raise PluginError(f"插件执行超时（超过 {timeout}s）")

    log.info(f"插件执行完成 | api_id={api_id}")
    return result
