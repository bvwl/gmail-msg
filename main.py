"""项目主入口。

直接执行 ``python main.py`` 即可启动服务；启动参数统一在本文件中维护。
"""

from __future__ import annotations

import uvicorn

# 启动配置：修改这里即可改变本地开发服务行为。
APP_MODULE = "app.main:app"
HOST = "127.0.0.1"
PORT = 8000
RELOAD = True
LOG_LEVEL = "info"


def main() -> None:
    """启动 FastAPI 服务。"""

    uvicorn.run(
        APP_MODULE,
        host=HOST,
        port=PORT,
        reload=RELOAD,
        log_level=LOG_LEVEL,
    )


if __name__ == "__main__":
    main()
