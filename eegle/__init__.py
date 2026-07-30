"""EEGle: reproducible neurophysiological model systems.

The alpha top-level namespace is intentionally small while clean foundation
packages are established. Import typed contracts from their owning package.
"""

from importlib.metadata import PackageNotFoundError, version

from eegle._domain import ExecutionMode


__all__ = ["ExecutionMode", "__version__"]

try:
    __version__ = version("eegle")
except PackageNotFoundError:  # Source tree imported without installation metadata.
    __version__ = "0+unknown"
