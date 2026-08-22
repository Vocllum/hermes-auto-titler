"""Hermes plugin root entry.

Hermes directory plugins require an ``__init__.py`` at the plugin root and
import the plugin directory as a module (hermes_cli/plugins.py). This thin
shim exposes the ``hermes_auto_titler`` package next to it to the loader.
Kept as a real file (not a symlink) so Windows checkouts without symlink
support still load.
"""
import sys
from pathlib import Path

_here = Path(__file__).parent
for _c in (_here, _here.resolve()):
    if str(_c) not in sys.path:
        sys.path.insert(0, str(_c))

from hermes_auto_titler import register  # noqa: E402

__all__ = ["register"]
