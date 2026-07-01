"""Input-contract helpers for channel-specific EEG model inputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_input_contract(
    contract: dict[str, Any] | None,
    *,
    fallback_channel_names: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Merge legacy flat model keys with the new nested input contract."""
    source = dict(contract or {})
    nested = dict(source.get("input_contract") or {})
    fallback = [str(value) for value in fallback_channel_names]
    required = _string_list(
        nested.get(
            "required_channels",
            source.get("required_channels", source.get("channel_names", fallback)),
        )
    )
    optional = _string_list(nested.get("optional_channels", source.get("optional_channels", [])))
    channel_order = _string_list(nested.get("channel_order", source.get("channel_order", required or fallback)))
    if not channel_order and (required or optional):
        channel_order = [*required, *optional]
    groups = dict(nested.get("channel_groups", source.get("channel_groups", {})) or {})
    normalized = {
        **source,
        **nested,
        "input_layout": str(nested.get("input_layout", source.get("input_layout", "channels_x_samples"))),
        "input_units": str(nested.get("input_units", source.get("input_units", "microvolts"))),
        "channel_names": channel_order or fallback,
        "channel_order": channel_order or fallback,
        "required_channels": required or channel_order or fallback,
        "optional_channels": optional,
        "channel_groups": {str(key): _string_list(value) for key, value in groups.items()},
        "missing_channel_policy": str(
            nested.get("missing_channel_policy", source.get("missing_channel_policy", "error"))
        ),
        "resampling": nested.get("resampling", source.get("resampling", "none")),
        "tensor_layout": str(nested.get("tensor_layout", source.get("tensor_layout", "batch_1_channels_samples"))),
    }
    return normalized


def select_contract_channels(channel_names: list[str], contract: dict[str, Any]) -> tuple[list[int], list[str]]:
    """Resolve channel order and required-channel policy against a live stream."""
    normalized = normalize_input_contract(contract, fallback_channel_names=channel_names)
    available = list(channel_names)
    required = [name for name in normalized.get("required_channels", []) if name]
    desired = [name for name in normalized.get("channel_order", []) if name]
    for name in normalized.get("optional_channels", []):
        if name not in desired:
            desired.append(name)
    missing_required = [name for name in required if name not in available]
    policy = str(normalized.get("missing_channel_policy", "error")).lower()
    if missing_required and policy != "drop":
        raise ValueError("model-required channels missing from epoch: " + ", ".join(missing_required))
    selected = [name for name in desired if name in available]
    if not selected:
        if policy == "drop":
            raise ValueError("no model input channels are available after applying missing_channel_policy=drop")
        raise ValueError("model channel contract resolved to an empty channel list")
    return [available.index(name) for name in selected], selected


def resampling_mode(contract: dict[str, Any]) -> str:
    raw = contract.get("resampling", "none")
    if isinstance(raw, dict):
        raw = raw.get("mode", "none")
    return str(raw or "none").strip().lower()


def validate_supported_resampling(contract: dict[str, Any]) -> str:
    mode = resampling_mode(contract)
    if mode not in {"none", "disabled", "strict_match"}:
        raise ValueError(
            f"resampling mode '{mode}' is not supported in realtime inference; "
            "train or export the bundle at the stream sample rate, or resample offline before training"
        )
    return mode


def contract_hash(contract: dict[str, Any]) -> str:
    encoded = json.dumps(contract, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]
