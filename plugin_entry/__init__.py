"""Hermes 插件安装入口（薄层）。

Hermes 目录插件要求在插件根目录存在 ``__init__.py``，并把插件目录当作
``hermes_plugins.<slug>`` 模块导入（hermes_cli/plugins.py:2043 起）。
本文件被 symlink 为 ``~/.hermes/plugins/hermes-auto-titler/__init__.py``，
负责把项目子包 ``hermes_auto_titler`` 暴露给加载器。

加载器传入的 ``__file__`` 是插件根（symlink 路径），子包就在同一目录下；
保险起见两种路径都加入 sys.path。
"""
import sys
from pathlib import Path

_here = Path(__file__).parent
for _c in (_here, _here.resolve()):
    if str(_c) not in sys.path:
        sys.path.insert(0, str(_c))

from hermes_auto_titler import register  # noqa: E402

__all__ = ["register"]
