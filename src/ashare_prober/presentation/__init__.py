"""展示层（⑤ 展示层 + 查看）。

对外暴露：
- ``Render``：``EventDossier`` + ``ResearchReport`` + ``ApprovalRequest`` → 自包含单文件 HTML。
- ``start_viewer``：本地静态文件服务（包裹 ``http.server``），指向上一步生成的 ``outputs/``。
"""

from __future__ import annotations

from ashare_prober.presentation.render import Render
from ashare_prober.presentation.viewer import start_viewer

__all__ = ["Render", "start_viewer"]
