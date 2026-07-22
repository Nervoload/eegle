"""EEGle: reproducible neurophysiological model systems.

The alpha top-level namespace is intentionally small while clean foundation
packages are established. Import typed contracts from their owning package.
"""

from eegle._domain import ExecutionMode


__all__ = ["ExecutionMode", "__version__"]

__version__ = "0.1.0"
