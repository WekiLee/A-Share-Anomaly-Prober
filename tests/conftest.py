"""pytest 配置：将 src/ 加入 sys.path，使未安装时也能 import ashare_prober。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config) -> None:  # noqa: ANN001
    """注册自定义 marker，避免 UnknownMarkWarning。"""
    config.addinivalue_line("markers", "network: 网络相关冒烟测试（默认跳过）")
