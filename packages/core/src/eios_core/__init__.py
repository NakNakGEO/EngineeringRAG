"""Engineering Intelligence OS core: settings, logging, correlation IDs, health primitives.

This package must stay free of web frameworks, database drivers and vendor SDKs so that every
other package (and future domain code) can depend on it safely.
"""

from importlib import metadata

try:
    __version__ = metadata.version("eios-core")
except metadata.PackageNotFoundError:  # pragma: no cover - only when run from a bare checkout
    __version__ = "0.0.0"

__all__ = ["__version__"]
