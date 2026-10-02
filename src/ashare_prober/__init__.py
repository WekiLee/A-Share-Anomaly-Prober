"""A-Share Anomaly Prober —— 面向 A 股的异动「前因后果」探测与多视角研判工具。

本包实现五层架构中的「① 数据层」：主/备数据源、TTL 缓存、本地落库与统一门面。
其余层次（异动引擎 / 归因 / 研判 / 展示）由其他模块负责。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
