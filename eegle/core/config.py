"""Compatibility import surface for configuration helpers."""

from eegle.config import DEFAULT_CONFIG, load_config, merged_config, resolve_path, task_config, write_config


__all__ = [
    "DEFAULT_CONFIG",
    "load_config",
    "merged_config",
    "resolve_path",
    "task_config",
    "write_config",
]
