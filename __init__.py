"""Hermes native plugin entrypoint.

The plugin core lives in ``src/github_app_auth/``. This shim adds ``src/`` to
``sys.path`` so the absolute import resolves when Hermes loads the plugin
directory by file path (without a parent package context).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_app_auth import register

__all__ = ["register"]
