"""Torch epoch model adapter imports."""

from eegle.realtime.models import TorchEEGNetAdapter, TorchEpochAdapter, TorchShallowConvNetAdapter

TorchScriptAdapter = TorchEpochAdapter


__all__ = ["TorchEEGNetAdapter", "TorchEpochAdapter", "TorchScriptAdapter", "TorchShallowConvNetAdapter"]
