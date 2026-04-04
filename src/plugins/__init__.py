"""Plugin registry for report-type extensions.

Each report type (e.g. ``financial_company``, ``general``) is implemented as a
lightweight plugin under ``src/plugins/<name>/plugin.py``.  Call
:func:`load_plugin` with the ``target_type`` string from the user config to
obtain the corresponding :class:`~src.plugins.base_plugin.ReportPlugin`.
"""
from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.plugins.base_plugin import ReportPlugin

from src.utils.logger import get_logger

logger = get_logger()

# Maps target_type → ReportPlugin subclass (populated lazily).
_PLUGIN_REGISTRY: dict[str, type[ReportPlugin]] = {}


def register_plugin(target_type: str):
    """Class decorator that registers a plugin under *target_type*."""

    def wrapper(cls: type[ReportPlugin]) -> type[ReportPlugin]:
        _PLUGIN_REGISTRY[target_type] = cls
        return cls

    return wrapper


def load_plugin(target_type: str) -> ReportPlugin:
    """Return an instance of the plugin for *target_type*.

    The first call for a given type triggers a lazy import of
    ``src.plugins.<target_type>.plugin`` to populate the registry.

    Legacy aliases (e.g. ``"company"`` → ``"financial_company"``) are
    resolved automatically by :class:`~src.config.config.ConfigSchema`,
    but we also handle them here for safety.
    """
    from src.config.config import _TARGET_TYPE_ALIASES
    target_type = _TARGET_TYPE_ALIASES.get(target_type, target_type)

    if target_type not in _PLUGIN_REGISTRY:
        mod_name = f"src.plugins.{target_type}.plugin"
        try:
            importlib.import_module(mod_name)
        except ModuleNotFoundError:
            raise ValueError(
                f"No plugin found for target_type={target_type!r}. "
                f"Expected module {mod_name} to exist."
            )

    cls = _PLUGIN_REGISTRY.get(target_type)
    if cls is None:
        raise ValueError(
            f"Plugin module for {target_type!r} was imported but did not "
            f"register itself via @register_plugin."
        )

    logger.info("Loaded plugin: %s (%s)", target_type, cls.__name__)
    return cls()
