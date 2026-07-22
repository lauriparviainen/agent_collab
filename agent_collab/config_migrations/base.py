"""Shared core for the config-migration package.

Holds the pieces every other module in the package depends on: the current
schema constant, the migration exceptions, and the shared logger. This module
imports nothing else from the package, so it is the safe bottom of the import
graph — step modules, the scope filters, the write-back path, and the package
``__init__`` all import from here without risk of a cycle.
"""

from __future__ import annotations

import logging

CURRENT_CONFIG_SCHEMA = 10

_logger = logging.getLogger("agent_collab.config")


class ConfigError(ValueError):
    """Raised when agent-collab configuration is invalid.

    Defined here so ``ConfigMigrationError`` can subclass it without a
    circular import; ``agent_collab.config`` re-exports it as the public name.
    """


class ConfigMigrationError(ConfigError):
    """Raised when config data cannot be migrated to the current schema.

    Subclassing ``ConfigError`` keeps every ``except ConfigError`` fail-safe
    (daemon retention, sanitized session-start errors) working for migration
    failures too.
    """
