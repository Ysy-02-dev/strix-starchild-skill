"""Strix Lab prediction market — Starchild agent skill.

The public functions are re-exported here as well as in ``exports.py`` so that
``strix.strix_buy(...)`` works whether the loader binds the package or the
exports module. ``SKILL.md`` documents the package form.
"""

from .exports import *  # noqa: F401,F403
from .exports import __all__ as _EXPORTED

__version__ = "0.1.0"

__all__ = list(_EXPORTED)
