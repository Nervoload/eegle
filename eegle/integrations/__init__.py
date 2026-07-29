"""Optional task, transport, framework, format, and vendor integrations."""

from eegle.integrations.mne import MneRawExport, dense_batch_to_mne_raw


__all__ = ["MneRawExport", "dense_batch_to_mne_raw"]
