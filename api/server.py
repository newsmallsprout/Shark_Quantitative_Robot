"""
已弃用：第二套 FastAPI 入口。

规范 ASGI 应用与路由定义见 ``main`` 模块中的 ``app``；进程入口为 ``python main.py``。
详见仓库 ``adr/0001-single-http-entrypoint.md``。

若你曾在宿主代码中调用 ``create_api_server``，请改为在同一进程内使用 ``main`` 的启动流程，
或 ``uvicorn main:app``（需自行处理策略/行情循环与单体 ``main()`` 的等价性）。
"""

from __future__ import annotations

_MSG = (
    "api.server 不再是可启动的 HTTP 入口。"
    "请运行: python main.py  （ASGI: main:app）。"
    "说明见 adr/0001-single-http-entrypoint.md"
)


def create_api_server(*_args, **_kwargs) -> None:  # noqa: ARG001
    """历史 API；已移除独立 FastAPI 实例，调用即失败以免静默跑错栈。"""
    raise RuntimeError(_MSG)


# 刻意不提供第二份 ``app``，防止与 ``main:app`` 混淆。
