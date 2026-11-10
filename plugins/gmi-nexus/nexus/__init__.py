"""GMI NEXUS — the adaptive intelligence layer for Hermes Agent.

The package is deliberately import-safe outside Hermes: every Hermes-specific
import (secret scope, plugin storage, the plugin context) is resolved lazily
inside a function so the engine can be unit-tested standalone.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
