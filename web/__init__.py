"""web 包 —— Model Morph 的插件 Web API。

本模块把调度器的各种读写能力以 Dashboard Plugin Page 后端 API 的形式暴露出来，
供 ``pages/model-morph`` 前端通过 bridge 调用。
"""

from .api import register_all

__all__ = ["register_all"]
