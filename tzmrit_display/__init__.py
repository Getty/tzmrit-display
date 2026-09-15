"""Driver and system monitor for HONGTAI USB LCD panels (33c3:7791/7792)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("tzmrit-display")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
